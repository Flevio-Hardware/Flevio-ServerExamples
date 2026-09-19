#!/usr/bin/env python3
"""
The smallest useful server: print every record that arrives.

    python3 examples/01_print_records.py

Then, in another terminal:

    python3 -m flevio.simulator

Nothing is stored, so this acknowledges everything - which is honest here and
would not be in production. Example 2 is the version that keeps the data.
"""

import asyncio
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from flevio import catalog
from flevio.server import Handler, Server


class Printer(Handler):
    async def on_hello(self, session, hello) -> bool:
        print("\n=== %s connected - serial %s, firmware %s, config rev %d, ack %s\n"
              % (hello.imei, hello.serial, hello.fw, hello.cfg_revision,
                 "on" if hello.want_ack else "off"))
        return True

    async def on_records(self, session, records) -> int:
        for r in records:
            when = (time.strftime("%H:%M:%S", time.gmtime(r.ts)) if r.dated
                    else "+%ds" % r.ts)          # no date yet: seconds since boot
            where = "%9.5f %10.5f" % (r.lat, r.lon) if r.has_fix else "     no fix      "
            print("%s  %-14s %s  %3s km/h  %s" % (
                when,
                catalog.event_name(r.event),
                where,
                r.speed_kph if r.speed_kph is not None else "-",
                " ".join(catalog.describe(k, v) for k, v in sorted(r.io.items())
                         # The identity strings repeat on every record; skip
                         # them here so the line stays readable.
                         if k not in (250, 251)),
            ))
        return len(records)

    async def on_disconnect(self, session) -> None:
        if session.imei:
            print("=== %s gone after %d records" % (session.imei, session.records_in))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5600
    try:
        asyncio.run(Server(Printer(), tcp_port=port, udp_port=port).serve())
    except KeyboardInterrupt:
        pass
