#!/usr/bin/env python3
"""
Decode captured bytes, with no server and no device.

    python3 examples/04_decode_hex.py f20201cf0011000ce0b4a6d506031f...
    tcpdump -A ... | python3 examples/04_decode_hex.py -
    python3 examples/04_decode_hex.py --corpus            # the shipped sample

This is the tool you want at three in the morning when a customer sends you a
packet capture and asks what their truck said. It takes hex on the command
line or on stdin - whitespace, colons and 0x prefixes are all fine - and
prints every frame in it.
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from flevio import catalog
from flevio import protocol as p


def unhex(text: str) -> bytes:
    cleaned = re.sub(r"0x|[^0-9a-fA-F]", "", text)
    if len(cleaned) % 2:
        cleaned = cleaned[:-1]
    return bytes.fromhex(cleaned)


def show(raw: bytes) -> None:
    parser = p.FrameParser()
    parser.feed(raw)
    frames = parser.frames()
    if not frames:
        print("no complete frame in %d byte(s)%s" % (
            len(raw), " - is the capture truncated?" if len(raw) > 6 else ""))
        return

    for f in frames:
        print("\n%s  (%d byte payload)" % (f.kind_name, len(f.payload)))
        if f.kind == p.K_HELLO:
            h = p.decode_hello(f.payload)
            print("  imei %s  serial %s  fw %s  cfg rev %d  wants ACKs: %s"
                  % (h.imei, h.serial, h.fw, h.cfg_revision, h.want_ack))
        elif f.kind == p.K_DATA:
            b = p.decode_data(f.payload)
            print("  seq %d%s, %d record(s)"
                  % (b.seq, "  imei %s" % b.imei if b.imei else "", len(b.records)))
            for i, r in enumerate(b.records):
                print("  [%d] %s%s %s  prio %s%s" % (
                    i,
                    r.ts, "" if r.dated else " (uptime, undated)",
                    catalog.event_name(r.event),
                    catalog.PRIORITY.get(r.priority, r.priority),
                    "  delta" if r.delta else "  keyframe",
                ))
                if r.has_fix:
                    print("      %.6f, %.6f  alt %s  hdg %s  %s km/h  %s sats  hdop %s"
                          % (r.lat, r.lon, r.alt_m, r.heading_deg,
                             r.speed_kph, r.sats, r.hdop))
                for k, v in sorted(r.io.items()):
                    print("      %s" % catalog.describe(k, v))
        elif f.kind == p.K_REPLY:
            cmd_id, text = p.decode_reply(f.payload)
            print("  command #%d answered: %s" % (cmd_id, text))
        elif f.kind == p.K_ACK:
            print("  seq %d, accepted %d"
                  % (int.from_bytes(f.payload[:2], "big"), f.payload[2]))
        elif f.kind == p.K_CMD:
            print("  command #%d: %s" % (int.from_bytes(f.payload[:2], "big"),
                                         f.payload[2:].decode("utf-8", "replace")))
    if len(parser):
        print("\n%d trailing byte(s) that do not form a frame" % len(parser))


def main(argv) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    if argv[0] == "--corpus":
        import json
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "tests", "corpus.json")
        for f in json.load(open(path))["frames"]:
            show(bytes.fromhex(f["hex"]))
        return 0
    text = sys.stdin.read() if argv[0] == "-" else " ".join(argv)
    show(unhex(text))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
