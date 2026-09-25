"""
Somewhere to put the records: JSON lines and SQLite, both dependency-free.

A sink is a :class:`~flevio.server.Handler` that stores what arrives. Both of
these follow the rule that makes the protocol safe: they return the number of
records they have **committed**, never the number they were handed. Copy that
part above all else when you write your own.

    from flevio.server import Server
    from flevio.sinks import SqliteSink

    async with SqliteSink("fleet.db") as sink:
        await Server(sink).serve()

Neither is a production database. SQLite will carry a few hundred devices on
a small machine; past that, the shape to copy is the same with Postgres and a
connection pool, and the ``executemany`` in :meth:`SqliteSink.on_records` is
where a ``COPY`` would go.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from typing import List, Optional

from . import catalog
from .protocol import Record
from .server import Handler, Session

__all__ = ["JsonLinesSink", "SqliteSink", "record_to_dict"]


def record_to_dict(imei: Optional[str], r: Record, received_at: Optional[float] = None) -> dict:
    """A record as plain JSON-safe data, names and units resolved.

    ``dated`` is the field to look at before you file this anywhere by time.
    A device that has not had a fix since it powered up writes records with
    seconds since boot in ``ts``; storing those as 1970 is the classic way to
    ruin a trip report. Here they keep their raw ``ts`` and ``dated: false``,
    and ``received_at`` is what you should date them from.
    """
    d = {
        "imei": imei,
        "ts": r.ts,
        "dated": r.dated,
        "received_at": received_at if received_at is not None else time.time(),
        "event": catalog.event_name(r.event),
        "event_code": r.event,
        "priority": catalog.PRIORITY.get(r.priority, r.priority),
    }
    if r.has_fix:
        d["lat"] = r.lat
        d["lon"] = r.lon
    for key, value in (
        ("alt_m", r.alt_m),
        ("heading_deg", r.heading_deg),
        ("speed_kph", r.speed_kph),
        ("sats", r.sats),
        ("hdop", r.hdop),
    ):
        if value is not None:
            d[key] = value
    d["io"] = catalog.decode_io(r.io)
    if r.ext:
        d["ext"] = catalog.decode_ext(r.ext)
    return d


class JsonLinesSink(Handler):
    """One JSON object per line. The simplest thing that is still honest.

    Appends, flushes and ``fsync``s before acknowledging, so a power cut
    between the write and the answer costs a duplicate rather than a hole -
    the right way round. Duplicates are unavoidable in any store-and-forward
    protocol (the device re-sends what it was not told about), so de-duplicate
    on ``imei + ts + event`` wherever you read this.
    """

    def __init__(self, path: str = "records.jsonl", fsync: bool = True) -> None:
        self.path = path
        self.fsync = fsync
        self._fh = open(path, "a", encoding="utf-8")
        self._lock = asyncio.Lock()

    async def on_records(self, session: Session, records: List[Record]) -> int:
        now = time.time()
        async with self._lock:
            def write() -> None:
                for r in records:
                    self._fh.write(json.dumps(record_to_dict(session.imei, r, now)) + "\n")
                self._fh.flush()
                if self.fsync:
                    os.fsync(self._fh.fileno())

            await asyncio.get_running_loop().run_in_executor(None, write)
        return len(records)

    def close(self) -> None:
        self._fh.close()

    async def __aenter__(self) -> "JsonLinesSink":
        return self

    async def __aexit__(self, *exc) -> None:
        self.close()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    imei        TEXT PRIMARY KEY,
    serial      TEXT,
    fw          TEXT,
    cfg_rev     INTEGER,
    first_seen  REAL,
    last_seen   REAL
);

CREATE TABLE IF NOT EXISTS records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    imei        TEXT NOT NULL,
    ts          INTEGER NOT NULL,
    dated       INTEGER NOT NULL,
    received_at REAL NOT NULL,
    event       INTEGER NOT NULL,
    priority    INTEGER NOT NULL,
    lat         REAL,
    lon         REAL,
    alt_m       INTEGER,
    heading_deg INTEGER,
    speed_kph   INTEGER,
    sats        INTEGER,
    hdop        REAL,
    io          TEXT NOT NULL,
    -- The device re-sends anything it was not told we stored, so the same
    -- record legitimately arrives twice. This is what makes that harmless.
    UNIQUE (imei, ts, event)
);

CREATE INDEX IF NOT EXISTS records_imei_ts ON records (imei, ts DESC);
"""


class SqliteSink(Handler):
    """Records in a table, de-duplicated, committed before acknowledging.

    The database work happens in a thread so the event loop keeps serving
    other devices while SQLite is writing.
    """

    def __init__(self, path: str = "fleet.db") -> None:
        self.path = path
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self._lock = asyncio.Lock()

    async def on_hello(self, session: Session, hello) -> bool:
        now = time.time()
        async with self._lock:
            await asyncio.get_running_loop().run_in_executor(
                None, self._upsert_device, hello, now
            )
        return True

    def _upsert_device(self, hello, now: float) -> None:
        self._db.execute(
            """INSERT INTO devices (imei, serial, fw, cfg_rev, first_seen, last_seen)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(imei) DO UPDATE SET
                 serial=excluded.serial, fw=excluded.fw,
                 cfg_rev=excluded.cfg_rev, last_seen=excluded.last_seen""",
            (hello.imei, hello.serial, hello.fw, hello.cfg_revision, now, now),
        )
        self._db.commit()

    async def on_records(self, session: Session, records: List[Record]) -> int:
        if not records:
            return 0
        now = time.time()
        rows = [
            (
                session.imei, r.ts, int(r.dated), now, r.event, r.priority,
                r.lat, r.lon, r.alt_m, r.heading_deg, r.speed_kph, r.sats, r.hdop,
                json.dumps(catalog.decode_io(r.io)),
            )
            for r in records
        ]
        async with self._lock:
            try:
                await asyncio.get_running_loop().run_in_executor(None, self._insert, rows, session.imei, now)
            except sqlite3.Error:
                # Nothing was committed. Zero is the truthful answer, and the
                # device still has every one of them.
                return 0
        return len(records)

    def _insert(self, rows, imei: Optional[str], now: float) -> None:
        self._db.executemany(
            """INSERT OR IGNORE INTO records
               (imei, ts, dated, received_at, event, priority, lat, lon,
                alt_m, heading_deg, speed_kph, sats, hdop, io)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        if imei:
            self._db.execute("UPDATE devices SET last_seen=? WHERE imei=?", (now, imei))
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    async def __aenter__(self) -> "SqliteSink":
        return self

    async def __aexit__(self, *exc) -> None:
        self.close()
