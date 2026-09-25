#!/usr/bin/env python3
"""
Flevio server over MQTT: shows everything the devices publish, tracks who
is online, and delivers commands whether a device is connected or not.
Nothing is stored - this is the starting point for connecting devices to
your own system.

For devices that report to your broker (Protocol MQTT) instead of to a
socket of yours. The broker carries the frames; this is the server behind it.

    pip install paho-mqtt
    export FLEVIO_MQTT_PASSWORD=...            # the broker user's password
    python3 examples/mqtt_server.py --host broker.example.com --user me
    python3 examples/mqtt_server.py ... --ca ca.crt   # a private authority
    python3 examples/mqtt_server.py ... --hex         # the raw bytes as well

It prints the lines to type into a device to point it at the broker, then
every message as it arrives. At the prompt:

    list                         devices seen, online or not
    odo <imei>                   odometer, engine hours, ignition, position now
    <imei> <command>             any command: GETSTATUS, POLLQ, LIVETRACK 10 ...
    queue                        the commands and their answers
    cancel <id>                  drop a command that has not gone out
    param <id | name | word>     what a configuration parameter is
    help | quit

The broker does not keep a command for a device that is offline, so this
does: a command waits until the device's status says "online", goes out one
at a time, and is done when its answer comes back (flevio/cmdqueue.py). The
queue is in memory here; give CommandQueue a file name to keep it across
restarts.

Two things your own server must get right:

* Duplicates. Delivery is "at least once": a device that did not see the
  broker's receipt sends the frame again. Store records unique on
  (imei, ts, event) and the second copy is harmless.
* Records written before the device knew the date carry seconds since boot
  (record.dated is False). Use the time they arrived; do not file them in 1970.
"""

import argparse
import os
import queue as threadq
import ssl
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from flevio import catalog, params, protocol as p
from flevio.cmdqueue import CommandQueue


# --- showing ---------------------------------------------------------------

def now() -> str:
    return time.strftime("%H:%M:%S")


def show_record(imei: str, r) -> None:
    """Two or three lines per record: what and where, then every element."""
    when = (time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(r.ts)) if r.dated
            else "%d s after power-up (no clock yet)" % r.ts)
    if r.has_fix:
        where = "%.5f,%.5f" % (r.lat, r.lon)
        where += "".join((
            " alt %d m" % r.alt_m if r.alt_m is not None else "",
            " hdg %d" % r.heading_deg if r.heading_deg is not None else "",
            " %d sats" % r.sats if r.sats is not None else "",
            " hdop %.1f" % r.hdop if r.hdop is not None else ""))
    else:
        where = "no position"
    print("%s %s %-14s %-5s %s | %s | %s km/h" % (
        now(), imei, catalog.event_name(r.event), catalog.PRIORITY.get(r.priority, r.priority),
        when, where, r.speed_kph if r.speed_kph is not None else "-"))
    if r.io:
        print("      " + ", ".join(catalog.describe(k, v) for k, v in sorted(r.io.items())))
    if r.ext:
        print("      " + ", ".join(catalog.describe_ext(k, v) for k, v in sorted(r.ext.items())))


def show_odo(answer: str) -> None:
    """GETODO answers key=value pairs - what a backend would parse."""
    v = dict(kv.split("=", 1) for kv in answer.split() if "=" in kv)
    print("  odometer %s km (%s), engine hours %s (%s), ignition %s, "
          "position %s,%s (%s s old)" % (
              v.get("odo"), v.get("odo_src"), v.get("hours"), v.get("hours_src"),
              v.get("ign"), v.get("lat"), v.get("lon"), v.get("fix_age")))
    print("  raw: " + answer)


def show_param(words: str) -> None:
    """What a parameter is: ``param 145``, ``param move_send_period_s``,
    ``param period``."""
    p_ = params.get(words)
    found = [p_] if p_ else params.find(words)
    if not found:
        print("  no parameter matches %r" % words)
    for q in found[:12]:
        rng = ""
        if q.choices:
            rng = ", ".join("%d=%s" % (v, n) for v, n, _ in q.choices)
        elif q.min is not None:
            rng = ("%s..%s %s" % (q.min, q.max, q.unit)).strip()
        print("  %3d %-24s %s - %s, default %s, %s%s" % (
            q.id, q.key, q.title, rng or q.type, q.show(q.default), q.access,
            " (secret)" if q.secret else ""))
        if len(found) == 1 and q.help:
            print("      " + q.help)
    if len(found) > 12:
        print("  ... %d more - be more specific" % (len(found) - 12))


def show_answer(command: str, answer: str) -> None:
    """GETODO and GETPARAMS answers are key=value lists: named here, the
    way a backend would parse them. Anything else is shown as it came."""
    words = [w.upper() for w in command.split()[:2]]
    if "GETODO" in words:
        show_odo(answer)
    elif "GETPARAMS" in words and params.parse(answer):
        for pid, value in params.parse(answer).items():
            print("  " + params.describe(pid, value))
    else:
        print("  " + answer)


def show_queue(cmds) -> None:
    if not cmds:
        print("  (no commands)")
    for c in cmds:
        when = time.strftime("%H:%M:%S", time.localtime(c.created_at))
        extra = ""
        if c.state == "done":
            extra = " -> " + (c.reply or "")
        elif c.attempts:
            extra = " (sent %d of %d)" % (c.attempts, c.max_attempts)
        print("  %5d  %s  %s  %-10s %s%s" % (c.id, when, c.imei, c.state, c.text, extra))


def instructions(args) -> str:
    """What to type into a device so that it reports to this broker. The
    password is the broker user's: typed on the device, never printed here."""
    tls = "0" if args.plain else "1"
    setting = "30=%s;31=%d;32=2;36=%s;37=%s;38=<password>;39=%s;" % (
        args.host, args.port, tls, args.user or "<user>", args.topic)
    return "\n".join([
        "",
        "=" * 72,
        " Flevio server - MQTT%s, %s:%d, topics %s/<imei>/..." % (
            "" if args.plain else " over TLS", args.host, args.port, args.topic),
        "=" * 72,
        "",
        " Point a device at this broker:",
        "",
        "   USB console:  cfg setparams " + setting,
        "                 cfg save",
        "   SMS:          <sms_password> SETPARAMS " + setting,
        "   Configurator: Server 1 -> Protocol MQTT, Address %s, Port %d," % (args.host, args.port),
        "                 TLS %s, user, password, topic prefix %s" % (
            "off" if args.plain else "encrypted", args.topic),
        "",
        "   36=2 instead of 1 also checks the broker's certificate - when the",
        "   broker's authority is in the device's bundle.",
        "",
        " Commands: list | odo <imei> | <imei> <command> | queue | cancel <id> |",
        "           param <id> | help | quit",
        "=" * 72,
        "",
    ])


HELP = """  list                     devices seen, online or not
  odo <imei>               odometer, engine hours, ignition, speed, position, clock
  <imei> <command>         GETSTATUS, GETINFO, GETVEHICLE, POLLQ, LIVETRACK 10,
                           GETPARAMS 140,145, SETPARAMS 145=300; ...
                           (waits here if the device is offline)
  queue                    the commands and their answers
  cancel <id>              drop a command that has not gone out
  param <id|name|word>     what a configuration parameter is: param 145, param period
  quit"""


# --- serving ---------------------------------------------------------------

def read_stdin(lines: "threadq.Queue") -> None:
    """Typed lines, handed to the main loop."""
    for line in sys.stdin:
        lines.put(line.strip())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", required=True, help="the broker")
    ap.add_argument("--port", type=int, default=8883)
    ap.add_argument("--user", default="", help="password: FLEVIO_MQTT_PASSWORD")
    ap.add_argument("--ca", help="the broker's CA certificate (PEM), if not a public one")
    ap.add_argument("--plain", action="store_true", help="no TLS (port 1883)")
    ap.add_argument("--topic", default="flevio", help="the devices' topic prefix (param 39)")
    ap.add_argument("--hex", action="store_true", help="show every message's raw bytes too")
    ap.add_argument("--client-id", default="flevio-server",
                    help="a fixed id, so the broker keeps this server's session")
    ap.add_argument("--timeout", type=float, default=90, help="seconds to wait for an answer")
    args = ap.parse_args()

    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        sys.exit("this needs paho-mqtt: pip install paho-mqtt")

    print(instructions(args))
    cmdq = CommandQueue(reply_timeout_s=args.timeout)      # in memory
    seen = {}                                              # imei -> what we know
    prefix = args.topic.rstrip("/") + "/"

    def device(imei: str) -> dict:
        return seen.setdefault(imei, {"online": False, "hello": None, "last": 0.0})

    def on_connect(c, userdata, flags, reason, props):
        if reason.is_failure:
            sys.exit("broker refused: %s" % reason)
        # QoS 1 and a persistent session (fixed client id, clean session
        # off): what the devices send while this server restarts waits in
        # the broker instead of being lost.
        c.subscribe(prefix + "+/#", qos=1)
        print("%s connected to the broker, listening on %s+/#" % (now(), prefix))

    def on_message(c, userdata, m):
        if not m.topic.startswith(prefix):
            return
        parts = m.topic[len(prefix):].split("/")
        if len(parts) != 2:
            return
        imei, leaf = parts
        data = m.payload
        if leaf == "cmd":
            return                                  # our own commands, echoed back
        d = device(imei)
        d["last"] = time.time()
        if args.hex:
            print("%s %s/%s %d bytes%s: %s" % (now(), imei, leaf, len(data),
                                               " (retained)" if m.retain else "", data.hex()))

        if leaf == "status":
            online = data.strip() == b"online"
            d["online"] = online
            if online:
                cmdq.set_online(imei, True)
            else:
                cmdq.device_gone(imei)
            print("%s %s %s%s" % (now(), imei, "online" if online else "OFFLINE",
                                  " (as of the broker's last word)" if m.retain else ""))
            return
        if leaf == "reply" and data[:1] != bytes([p.CODEC_ID]):
            # A plain-text answer: a command someone sent as text.
            print("%s %s answered: %s" % (now(), imei, data.decode("utf-8", "replace")))
            return

        parser = p.FrameParser()
        parser.feed(data)
        for f in parser.frames():
            try:
                if f.kind == p.K_HELLO:
                    h = p.decode_hello(f.payload)
                    d["hello"] = h
                    print("\n%s %s is %s %s, firmware %s, serial %s, config rev %s%s" % (
                        now(), imei, h.model or "?", h.hw or "", h.fw, h.serial or "-",
                        h.cfg_revision, " (retained)" if m.retain else ""))
                elif f.kind == p.K_DATA:
                    b = p.decode_data(f.payload)
                    d["online"] = True
                    cmdq.set_online(imei, True)
                    print("%s %s data seq %d, %d record(s)" % (now(), imei, b.seq, len(b.records)))
                    for r in b.records:
                        show_record(imei, r)
                elif f.kind == p.K_REPLY:
                    wire_id, text = p.decode_reply(f.payload)
                    done = cmdq.on_reply(imei, wire_id, text)
                    if done:
                        print("%s %s answered #%d %s" % (now(), imei, done.id, done.text))
                        show_answer(done.text, text)
                    else:
                        print("%s %s answered #%d: %s" % (now(), imei, wire_id, text))
            except p.DecodeError as e:
                print("%s %s/%s unreadable: %s - %s" % (now(), imei, leaf, e, data.hex()))

    def typed(line: str) -> bool:
        """One line from the prompt. False to stop."""
        if not line:
            return True
        if line in ("quit", "exit"):
            return False
        if line == "help":
            print(HELP)
        elif line == "list":
            if not seen:
                print("  no device seen yet")
            for imei, d in sorted(seen.items()):
                h = d["hello"]
                print("  %-16s %-7s %-8s fw %-8s last heard %s" % (
                    imei, "online" if d["online"] else "offline",
                    (h.model if h else "") or "?", h.fw if h else "?",
                    time.strftime("%H:%M:%S", time.localtime(d["last"]))))
        elif line == "queue":
            show_queue(cmdq.list())
        elif line.startswith("cancel "):
            try:
                ok = cmdq.cancel(int(line.split()[1]))
                print("  cancelled" if ok else "  not waiting - nothing to cancel")
            except ValueError:
                print("  usage: cancel <id>")
        elif line.startswith("param "):
            show_param(line[6:].strip())
        else:
            word, _, rest = line.partition(" ")
            imei, text = (rest.strip(), "GETODO") if word == "odo" else (word, rest.strip())
            if not imei.isdigit() or not text:
                print("  usage: <imei> <command>   (help for more)")
                return True
            cid = cmdq.add(imei, text)
            print("  #%d queued - %s" % (
                cid, "going out now" if cmdq.is_online(imei)
                else "goes out when %s comes online" % imei))
        return True

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=args.client_id,
                         clean_session=False)
    if args.user:
        client.username_pw_set(args.user, os.environ.get("FLEVIO_MQTT_PASSWORD", ""))
    if not args.plain:
        client.tls_set(ca_certs=args.ca, cert_reqs=ssl.CERT_REQUIRED)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(args.host, args.port, keepalive=60)

    lines = threadq.Queue()
    threading.Thread(target=read_stdin, args=(lines,), daemon=True).start()

    # One thread: network, prompt and command queue take turns.
    try:
        while True:
            client.loop(timeout=0.5)
            while not lines.empty():
                if not typed(lines.get()):
                    return
            for cmd in cmdq.due():
                client.publish("%s%s/cmd" % (prefix, cmd.imei),
                               p.command(cmd.wire_id, cmd.text), qos=1)
                cmdq.mark_sent(cmd.id)
                print("%s %s <- #%d %s (attempt %d of %d)" % (
                    now(), cmd.imei, cmd.id, cmd.text, cmd.attempts + 1, cmd.max_attempts))
    except KeyboardInterrupt:
        pass
    finally:
        client.disconnect()


if __name__ == "__main__":
    main()
