"""
Commands for devices that are not connected right now.

A device on MQTT connects with a clean session and takes its commands at
QoS 0, so the broker keeps nothing for it while it is away: a command
published to <prefix>/<imei>/cmd while the truck is in a tunnel is gone.
That is deliberate - the modem's acknowledgements are not reliable enough
for QoS 1 to mean "exactly once", and a RESET run twice is not a small
thing. The queue belongs on the server, which is the one party that knows
what it asked for and can tell whether it was answered.

This is that queue, in SQLite so that the process that serves it and the
tool that adds to it can be different programs:

    q = CommandQueue("commands.db")
    q.add("865341041242696", "GETSTATUS")          # any time, online or not

and, in the process that talks to the broker:

    q.set_online(imei, True)                       # on "online" in /status
    for cmd in q.due(now):                         # every second or so
        publish(cmd.imei, protocol.command(cmd.wire_id, cmd.text))
        q.mark_sent(cmd.id, now)
    q.on_reply(imei, cmd_id, text, now)            # on a REPLY frame in /reply

Commands go out as F2 CMD frames rather than plain text, so the answer
carries the command's id and is matched exactly, not by guessing which of
several questions it belongs to. One command per device is in flight at a
time, in the order they were added; the next goes when the previous one is
answered, or has timed out.

A command that was sent but not answered may or may not have run - the
answer can be what got lost. Re-sending is right for questions (GETSTATUS)
and settings (SETPARAMS writes the same value twice), wrong for actions:
RESET, DEFAULTS, SETOUT and SIM are therefore tried once unless the caller
says otherwise, and reported as "unanswered" rather than repeated.
"""

import sqlite3
import time
from dataclasses import dataclass
from typing import List, Optional

# Commands that do something when run twice. Tried once by default.
ONE_SHOT_VERBS = {"RESET", "DEFAULTS", "SETOUT", "SIM"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS commands (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    imei         TEXT    NOT NULL,
    text         TEXT    NOT NULL,
    state        TEXT    NOT NULL DEFAULT 'pending',
                 -- pending | sent | done | unanswered | expired | cancelled
    created_at   REAL    NOT NULL,
    sent_at      REAL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    reply        TEXT,
    replied_at   REAL
);
CREATE INDEX IF NOT EXISTS commands_imei_state ON commands (imei, state);
CREATE TABLE IF NOT EXISTS command_devices (
    imei      TEXT PRIMARY KEY,
    online    INTEGER NOT NULL DEFAULT 0,
    changed_at REAL
);
"""


@dataclass
class Command:
    id: int
    imei: str
    text: str
    state: str
    created_at: float
    sent_at: Optional[float]
    attempts: int
    max_attempts: int
    reply: Optional[str]
    replied_at: Optional[float]

    @property
    def wire_id(self) -> int:
        """The id in the CMD frame (16 bits) - the REPLY carries it back."""
        return self.id & 0xFFFF


def default_attempts(text: str) -> int:
    words = {w.upper() for w in text.replace(";", " ").split()}
    return 1 if words & ONE_SHOT_VERBS else 3


class CommandQueue:
    def __init__(self, path: str = ":memory:", reply_timeout_s: float = 90,
                 ttl_s: float = 7 * 86400):
        self.db = sqlite3.connect(path, isolation_level=None, timeout=10)
        self.db.executescript(SCHEMA)
        self.reply_timeout_s = reply_timeout_s
        self.ttl_s = ttl_s

    # --- adding and looking --------------------------------------------

    def add(self, imei: str, text: str, max_attempts: Optional[int] = None,
            now: Optional[float] = None) -> int:
        text = text.strip()
        if not text:
            raise ValueError("empty command")
        n = max_attempts if max_attempts is not None else default_attempts(text)
        cur = self.db.execute(
            "INSERT INTO commands (imei, text, created_at, max_attempts) VALUES (?, ?, ?, ?)",
            (imei, text, now if now is not None else time.time(), max(1, n)))
        return cur.lastrowid

    def cancel(self, cmd_id: int) -> bool:
        cur = self.db.execute(
            "UPDATE commands SET state='cancelled' WHERE id=? AND state='pending'", (cmd_id,))
        return cur.rowcount == 1

    def get(self, cmd_id: int) -> Optional[Command]:
        row = self.db.execute("SELECT * FROM commands WHERE id=?", (cmd_id,)).fetchone()
        return Command(*row) if row else None

    def list(self, imei: Optional[str] = None, limit: int = 50) -> List[Command]:
        if imei:
            rows = self.db.execute(
                "SELECT * FROM commands WHERE imei=? ORDER BY id DESC LIMIT ?", (imei, limit))
        else:
            rows = self.db.execute("SELECT * FROM commands ORDER BY id DESC LIMIT ?", (limit,))
        return [Command(*r) for r in rows.fetchall()]

    # --- presence ------------------------------------------------------

    def set_online(self, imei: str, online: bool, now: Optional[float] = None) -> None:
        self.db.execute(
            "INSERT INTO command_devices (imei, online, changed_at) VALUES (?, ?, ?) "
            "ON CONFLICT(imei) DO UPDATE SET online=excluded.online, changed_at=excluded.changed_at",
            (imei, 1 if online else 0, now if now is not None else time.time()))

    def is_online(self, imei: str) -> bool:
        row = self.db.execute("SELECT online FROM command_devices WHERE imei=?", (imei,)).fetchone()
        return bool(row and row[0])

    # --- the cycle -----------------------------------------------------

    def due(self, now: Optional[float] = None) -> List[Command]:
        """What to send now: at most one per online device, oldest first.

        Also does the housekeeping that decides it: a sent command with no
        answer past the timeout goes back to pending (if it may be retried)
        or ends as unanswered; a pending command past its time to live ends
        as expired.
        """
        now = now if now is not None else time.time()
        self.db.execute(
            "UPDATE commands SET state='expired' WHERE state='pending' AND created_at < ?",
            (now - self.ttl_s,))
        stale = now - self.reply_timeout_s
        self.db.execute(
            "UPDATE commands SET state = CASE WHEN attempts < max_attempts "
            "THEN 'pending' ELSE 'unanswered' END "
            "WHERE state='sent' AND sent_at < ?", (stale,))

        rows = self.db.execute(
            "SELECT c.* FROM commands c JOIN command_devices d ON d.imei = c.imei "
            "WHERE d.online = 1 AND c.state = 'pending' "
            "AND NOT EXISTS (SELECT 1 FROM commands s WHERE s.imei = c.imei AND s.state = 'sent') "
            "AND c.id = (SELECT MIN(id) FROM commands p WHERE p.imei = c.imei AND p.state = 'pending') "
            "ORDER BY c.id").fetchall()
        return [Command(*r) for r in rows]

    def mark_sent(self, cmd_id: int, now: Optional[float] = None) -> None:
        self.db.execute(
            "UPDATE commands SET state='sent', sent_at=?, attempts=attempts+1 WHERE id=?",
            (now if now is not None else time.time(), cmd_id))

    def device_gone(self, imei: str, now: Optional[float] = None) -> None:
        """The device went offline. What is in flight stays in flight: it
        may have run, and its answer may still come when the device is
        back. The timeout decides."""
        self.set_online(imei, False, now)

    def on_reply(self, imei: str, wire_id: int, text: str,
                 now: Optional[float] = None) -> Optional[Command]:
        """A REPLY frame arrived. Returns the command it answers, if any.

        Matched on the device and the id; a reply to a command already
        counted as unanswered still completes it - late is not wrong.
        """
        now = now if now is not None else time.time()
        rows = self.db.execute(
            "SELECT * FROM commands WHERE imei=? AND state IN ('sent','unanswered','pending') "
            "AND attempts > 0 ORDER BY id DESC", (imei,)).fetchall()
        for r in rows:
            c = Command(*r)
            if c.wire_id == wire_id:
                self.db.execute(
                    "UPDATE commands SET state='done', reply=?, replied_at=? WHERE id=?",
                    (text, now, c.id))
                return self.get(c.id)
        return None
