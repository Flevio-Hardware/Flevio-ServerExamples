#!/usr/bin/env python3
"""
Flevio server over TCP and UDP: shows everything the devices send, and
takes commands for them at a prompt. Nothing is stored - this is the
starting point for connecting devices to your own system.

    python3 examples/server.py                          # TCP and UDP on 5600
    python3 examples/server.py --port 7000 --public your.server.com

It prints the lines to type into a device to point it here, then every
message as it arrives. At the prompt:

    list                         who is connected
    odo <imei>                   odometer, engine hours, ignition, position now
    <imei> <command>             any command: GETSTATUS, POLLQ, LIVETRACK 10 ...
    param <id | name | word>     what a configuration parameter is
    help | quit

"odo" is GETODO - the call to make when a driver changes duty status.

Your own server starts from Log below: on_records() is where records go
into your database, queue or API. Acknowledge only what you have stored -
the number it returns is what the device deletes from its memory.

Try it without a truck:

    python3 -m flevio.simulator --port 5600
"""

import argparse
import asyncio
import logging
import os
import socket
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from flevio import catalog, params
from flevio.server import Handler, Server


# --- how to point a device here --------------------------------------------

def lan_address() -> str:
    """This machine's address on its network. Connecting a UDP socket sends
    nothing; it only makes the OS choose the outgoing interface."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))            # a documentation address
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def instructions(address: str, port: int, public_given: bool) -> str:
    tcp = "30=%s;31=%d;32=0;" % (address, port)
    lines = [
        "",
        "=" * 72,
        " Flevio server - TCP and UDP on port %d" % port,
        "=" * 72,
        "",
        " Point a device at %s:%d" % (address, port),
        "",
        "   USB console:   cfg setparams %s" % tcp,
        "                  cfg save",
        "   SMS:           <sms_password> SETPARAMS %s" % tcp,
        "   Another server it already talks to:",
        "                  <config_password> SETPARAMS %s" % tcp,
        "   Configurator:  Server 1 -> Address %s, Port %d, Protocol TCP" % (address, port),
        "",
        "   UDP instead of TCP: 32=1 (no acknowledgements - TCP is the normal choice)",
        "",
    ]
    if not public_given:
        lines += [
            " %s is this machine's address on its local network. A device on" % address,
            " the mobile network needs the PUBLIC address: run this on a server that",
            " has one, or forward TCP and UDP port %d on your router to this machine," % port,
            " then start again with --public <that address or a DNS name>.",
            "",
        ]
    lines += [" Commands: list | odo <imei> | <imei> <command> | param <id> | help | quit",
              "=" * 72, ""]
    return "\n".join(lines)


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
    p = params.get(words)
    found = [p] if p else params.find(words)
    if not found:
        print("  no parameter matches %r" % words)
    for p in found[:12]:
        rng = ""
        if p.choices:
            rng = ", ".join("%d=%s" % (v, n) for v, n, _ in p.choices)
        elif p.min is not None:
            rng = ("%s..%s %s" % (p.min, p.max, p.unit)).strip()
        print("  %3d %-24s %s - %s, default %s, %s%s" % (
            p.id, p.key, p.title, rng or p.type, p.show(p.default), p.access,
            " (secret)" if p.secret else ""))
        if len(found) == 1 and p.help:
            print("      " + p.help)
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


class Log(Handler):
    """Shows everything. Replace the prints with your own storage."""

    async def on_hello(self, session, hello) -> bool:
        print("\n%s %s connected from %s: %s %s, firmware %s, serial %s, config rev %s"
              % (now(), hello.imei, session.peer, hello.model or "?", hello.hw or "",
                 hello.fw, hello.serial or "-", hello.cfg_revision))
        return True

    async def on_records(self, session, records) -> int:
        for r in records:
            show_record(session.imei, r)
        return len(records)          # a real server: how many it has STORED

    async def on_reply(self, session, cmd_id, text) -> None:
        print("%s %s late answer to #%d: %s" % (now(), session.imei, cmd_id, text))

    async def on_bad_frame(self, session, error, frame) -> None:
        print("%s %s unreadable frame: %s" % (now(), session.imei or session.peer, error))

    async def on_disconnect(self, session) -> None:
        if session.imei:
            print("%s %s gone after %d records" % (now(), session.imei, session.records_in))


# --- the prompt ------------------------------------------------------------

HELP = """  list                     connected devices
  odo <imei>               odometer, engine hours, ignition, speed, position, clock
  <imei> <command>         GETSTATUS, GETINFO, GETVEHICLE, POLLQ, LIVETRACK 10,
                           GETPARAMS 140,145, SETPARAMS 145=300; ...
  param <id|name|word>     what a configuration parameter is: param 145, param period
  quit"""


async def prompt(server: Server) -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    while True:
        raw = await reader.readline()
        if not raw:
            await asyncio.Event().wait()             # no keyboard: just keep serving
        line = raw.decode().strip()
        if not line:
            continue
        if line in ("quit", "exit"):
            return
        if line == "help":
            print(HELP)
            continue
        if line.startswith("param "):
            show_param(line[6:].strip())
            continue
        if line == "list":
            if not server.sessions:
                print("  nothing connected yet")
            for imei, s in server.sessions.items():
                print("  %-16s %5d records  %s" % (imei, s.records_in, s.peer))
            continue

        word, _, rest = line.partition(" ")
        imei, text = (rest.strip(), "GETODO") if word == "odo" else (word, rest.strip())
        session = server.sessions.get(imei)
        if session is None:
            print("  %s is not connected (list shows who is)" % imei)
            continue
        if not text:
            print("  usage: <imei> <command>")
            continue
        try:
            answer = await session.send_command(text, timeout=60)
        except asyncio.TimeoutError:
            # Not an error: the truck is in a tunnel. The device does not
            # retry a command, so send it again when it matters.
            print("  no answer in 60 s - out of coverage? Send it again later.")
            continue
        except ConnectionError as e:
            print("  %s" % e)
            continue
        show_answer(text, answer)


async def main(args) -> None:
    address = args.public or lan_address()
    print(instructions(address, args.port, bool(args.public)))
    server = Server(Log(), tcp_port=args.port, udp_port=args.port)
    serving = asyncio.create_task(server.serve())
    console = asyncio.create_task(prompt(server))
    await asyncio.wait({serving, console}, return_when=asyncio.FIRST_COMPLETED)
    serving.cancel()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=5600, help="TCP and UDP port")
    ap.add_argument("--public", help="the address or DNS name devices should use")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)-7s %(message)s")
    try:
        asyncio.run(main(ap.parse_args()))
    except KeyboardInterrupt:
        pass
