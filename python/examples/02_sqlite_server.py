#!/usr/bin/env python3
"""
A server you could actually leave running: SQLite, plus a command console.

    python3 examples/02_sqlite_server.py fleet.db

It stores every record, commits before acknowledging, and gives you a prompt
on stdin for talking to a connected device:

    > list
    865341041238314   47 records   last seen 3 s ago
    > 865341041238314 GETSTATUS
    OK ign=1 moving=1 gsm=4 sats=11 vbat=4050 vext=13800 queue=0 fw=2.0.0
    > 865341041238314 EVENTS SERVER OFF IDLING,SPEEDING
    OK events updated

The console is the thing worth copying. An engineer wanting to ask a truck a
question, two thousand kilometres away, for a few hundred bytes of airtime,
is most of what a fleet backend is for.

Have a look at what landed:

    sqlite3 fleet.db "SELECT ts, event, lat, lon, json_extract(io,'$.engine_rpm')
                      FROM records ORDER BY id DESC LIMIT 10;"
"""

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from flevio import catalog
from flevio.server import Server
from flevio.sinks import SqliteSink

log = logging.getLogger("example")


class Storing(SqliteSink):
    """The stock SQLite sink, with a line of logging on top."""

    async def on_records(self, session, records) -> int:
        n = await super().on_records(session, records)
        interesting = [r for r in records if catalog.is_essential(r.event)]
        for r in interesting:
            log.info("%s %s", session.imei, catalog.event_name(r.event))
        log.info("%s stored %d record(s)", session.imei, n)
        return n


async def console(server: Server) -> None:
    """Read commands from stdin without blocking the event loop."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)

    print(__doc__.split("The console")[0].strip(), "\n")
    while True:
        line = (await reader.readline()).decode().strip()
        if not line:
            continue
        if line in ("quit", "exit"):
            raise SystemExit(0)
        if line == "list":
            if not server.sessions:
                print("  nothing connected")
            for imei, s in server.sessions.items():
                print("  %-16s %4d records  %s" % (imei, s.records_in, s.peer))
            continue

        imei, _, text = line.partition(" ")
        session = server.sessions.get(imei)
        if session is None:
            print("  %s is not connected" % imei)
            continue
        if not text:
            print("  usage: <imei> <command>")
            continue
        try:
            print("  " + await session.send_command(text, timeout=60))
        except asyncio.TimeoutError:
            # Not an error: the truck is in a tunnel. Nothing on the device
            # retries a command, so queue it again yourself if it matters.
            print("  no answer in 60 s - the device is out of coverage")
        except ConnectionError as e:
            print("  %s" % e)


async def main(path: str, port: int) -> None:
    async with Storing(path) as sink:
        server = Server(sink, tcp_port=port, udp_port=port)
        await asyncio.gather(server.serve(), console(server))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    db = sys.argv[1] if len(sys.argv) > 1 else "fleet.db"
    tcp = int(sys.argv[2]) if len(sys.argv) > 2 else 5600
    try:
        asyncio.run(main(db, tcp))
    except (KeyboardInterrupt, SystemExit):
        pass
