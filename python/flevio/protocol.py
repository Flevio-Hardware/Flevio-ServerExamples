"""
The Flevio Compact Protocol, codec 0xF2 - framing and records.

This module is the whole wire format and nothing else: it opens no sockets,
writes no files and has no dependencies beyond the standard library. If you
are writing your own server in another language, this file is the
specification you want in front of you; `flevio/server.py` is only one way
of using it.

Everything is big-endian except the varints, which are LEB128.

    +--------+--------+--------+--------+---------- ... ----------+-------+
    |  0xF2  |  kind  |     length      |         payload         | crc16 |
    +--------+--------+--------+--------+---------- ... ----------+-------+
         1        1            2                  length              2

CRC-16/IBM (poly 0xA001 reflected, init 0, no final xor) over kind, length
and payload. A widely implemented standard with a published check value, so a
port to another language can be verified against something other than this
file: `crc16(b"123456789") == 0xBB3D`.

A DATA frame carries up to 32 records. The first is complete; every record
after it is coded as the change from the one before - a different timestamp,
a position offset, and only the IO elements whose value actually moved. The
device sends about 28 bytes for a routine point that way instead of 120.

Deltas never cross a frame boundary, so a decoder needs no state between
frames and one lost packet cannot corrupt the next.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Union

__all__ = [
    "CODEC_ID", "VERSION",
    "K_HELLO", "K_DATA", "K_PING", "K_REPLY", "K_ACK", "K_CMD", "K_TIME",
    "DecodeError", "Frame", "FrameParser", "Record", "Hello", "DataBatch",
    "crc16", "build_frame", "ack", "command", "time_sync",
    "decode_hello", "decode_data", "decode_reply",
]

CODEC_ID = 0xF2
VERSION = 1

# Device -> server
K_HELLO = 0x01
K_DATA = 0x02
K_PING = 0x03
K_REPLY = 0x04
# Server -> device
K_ACK = 0x81
K_CMD = 0x82
K_TIME = 0x83

KIND_NAMES = {
    K_HELLO: "HELLO", K_DATA: "DATA", K_PING: "PING", K_REPLY: "REPLY",
    K_ACK: "ACK", K_CMD: "CMD", K_TIME: "TIME",
}

HELLO_F_ACK = 0x01      # the device expects its packets to be acknowledged
DATA_F_IDENT = 0x01     # the IMEI follows in the frame (UDP has no handshake)

# Record header flags
F_POS = 0x01
F_ALT = 0x02
F_HEADING = 0x04
F_SPEED = 0x08
F_SATS = 0x10
F_PRIO_SHIFT = 5
F_PRIO_MASK = 0x60
F_DELTA = 0x80

PRIORITY_NAMES = {0: "low", 1: "high", 2: "panic"}

# Element ids at or above this carry a string rather than a number.
IO_STRING_MIN = 250
# One of them carries a 16-bit element id inside it: the extended elements
# (wired inputs, sensors, Bluetooth sensors...). See ``Record.ext``.
IO_EXT = 254
# Set on an extended id: the value is bytes, not a number.
EXT_BLOB = 0x8000

# The device never sends a payload larger than this, and neither should you.
PAYLOAD_MAX = 1380
FRAME_MAX = PAYLOAD_MAX + 6


class DecodeError(ValueError):
    """Raised for anything that is not a well-formed frame or record.

    Every failure is this one exception on purpose: a server should treat a
    malformed frame as one event ("that device sent something I cannot
    read"), not as a dozen different bugs.
    """


# --------------------------------------------------------------------------
# Primitives
# --------------------------------------------------------------------------

def crc16(data: bytes) -> int:
    """CRC-16/IBM, the polynomial the device uses."""
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def get_varint(buf: bytes, off: int) -> Tuple[int, int]:
    """Unsigned LEB128. Returns (value, offset after it)."""
    out = 0
    for i in range(10):
        if off + i >= len(buf):
            raise DecodeError("varint runs past the end of the payload")
        b = buf[off + i]
        out |= (b & 0x7F) << (7 * i)
        if not b & 0x80:
            return out, off + i + 1
    raise DecodeError("varint longer than 10 bytes")


def get_svarint(buf: bytes, off: int) -> Tuple[int, int]:
    """Zigzag-coded signed varint: 0, -1, 1, -2 ... -> 0, 1, 2, 3 ..."""
    u, off = get_varint(buf, off)
    return (u >> 1) ^ -(u & 1), off


def put_varint(v: int) -> bytes:
    if v < 0:
        raise ValueError("varints are unsigned; zigzag a signed value first")
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        out.append(b | (0x80 if v else 0))
        if not v:
            return bytes(out)


def get_str8(buf: bytes, off: int) -> Tuple[str, int]:
    """A length byte and that many bytes. No terminator."""
    if off >= len(buf):
        raise DecodeError("string length runs past the end")
    n = buf[off]
    off += 1
    if off + n > len(buf):
        raise DecodeError("string body runs past the end")
    return buf[off:off + n].decode("utf-8", "replace"), off + n


def put_str8(s: str) -> bytes:
    raw = s.encode()[:255]
    return bytes([len(raw)]) + raw


# --------------------------------------------------------------------------
# Frames
# --------------------------------------------------------------------------

@dataclass
class Frame:
    kind: int
    payload: bytes

    @property
    def kind_name(self) -> str:
        return KIND_NAMES.get(self.kind, f"0x{self.kind:02X}")


def build_frame(kind: int, payload: bytes = b"") -> bytes:
    """Wrap a payload in the envelope, CRC included."""
    if len(payload) > PAYLOAD_MAX:
        raise ValueError(f"payload of {len(payload)} bytes exceeds {PAYLOAD_MAX}")
    body = bytes([kind]) + struct.pack(">H", len(payload)) + payload
    return bytes([CODEC_ID]) + body + struct.pack(">H", crc16(body))


class FrameParser:
    """Turns a TCP byte stream into frames.

    Feed it whatever `recv` gave you, however it was split, and take out the
    frames that are complete. On garbage it resynchronises on the next 0xF2
    that begins a frame with a valid CRC - byte by byte, exactly as the
    firmware's own parser does, so both ends recover from the same damage in
    the same way.

    Garbage whose second and third bytes happen to read as a plausible
    length makes the parser wait until it has that many bytes before it can
    test the CRC, fail it, and step forward one byte. That is not a flaw to
    fix by guessing: a short frame and the beginning of a long one look
    identical until the long one arrives, and a parser that guessed would
    throw away real data. Frames come out late in that case, never wrong.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> None:
        self._buf += data

    def __len__(self) -> int:
        return len(self._buf)

    def frames(self) -> List[Frame]:
        out: List[Frame] = []
        while True:
            i = self._buf.find(bytes([CODEC_ID]))
            if i < 0:
                self._buf.clear()
                return out
            if i:
                del self._buf[:i]
            if len(self._buf) < 4:
                return out
            plen = struct.unpack(">H", self._buf[2:4])[0]
            if plen > PAYLOAD_MAX:
                del self._buf[0]        # not a frame start after all
                continue
            total = 4 + plen + 2
            if len(self._buf) < total:
                return out              # the rest is still on the way
            want = struct.unpack(">H", self._buf[4 + plen:6 + plen])[0]
            if crc16(bytes(self._buf[1:4 + plen])) != want:
                del self._buf[0]
                continue
            out.append(Frame(self._buf[1], bytes(self._buf[4:4 + plen])))
            del self._buf[:total]


# --- what a server sends ---------------------------------------------------

def ack(seq: int, accepted: int) -> bytes:
    """Acknowledge a DATA frame, or a HELLO with seq 0.

    `accepted` is how many of the frame's records you have **stored**,
    counted from the first. The device marks exactly that many as delivered
    and sends the rest again, so a partial number is a valid answer and not
    an error - it is how back-pressure is expressed. Zero is a refusal; for
    a HELLO it means "I do not know this IMEI".
    """
    if not 0 <= accepted <= 255:
        raise ValueError("accepted must fit in a byte")
    return build_frame(K_ACK, struct.pack(">HB", seq & 0xFFFF, accepted))


def command(cmd_id: int, text: str) -> bytes:
    """Send a command to the device.

    The text is the same language an SMS or the driver application uses:

        GETSTATUS
        EVENTS SERVER OFF IDLING
        SETPARAMS 140=30;
        <config_password> RESET

    Without the configuration password in front it runs at the device's user
    level - status, event masks, periods and thresholds. With it, at the
    protected level, which is where the APN and the server address live. The
    device answers with a REPLY carrying the same `cmd_id`.
    """
    return build_frame(K_CMD, struct.pack(">H", cmd_id & 0xFFFF) + text.encode())


def time_sync(unix_ms: int) -> bytes:
    """Give a device your clock.

    A tracker has no battery-backed clock. Between power-up and its first
    GNSS fix it still writes records, and they carry uptime rather than a
    date (`time_src` = 0). This is a courtesy for that window; a device that
    has satellites or a network clock ignores it.
    """
    return build_frame(K_TIME, struct.pack(">q", unix_ms))


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------

@dataclass
class Record:
    """One record, as the device meant it.

    `io` maps element id to value: an integer for a measurement, a string
    for the VIN, the serial number and the firmware version. `flevio.catalog`
    turns those into names, units and human-readable values; nothing in this
    module interprets them, because the meaning of ids 100-199 comes from the
    published vehicle database rather than from firmware.
    """

    ts: int                                   # unix seconds, or uptime seconds
    event: int
    priority: int
    lat: Optional[float] = None
    lon: Optional[float] = None
    alt_m: Optional[int] = None
    heading_deg: Optional[int] = None
    speed_kph: Optional[int] = None
    sats: Optional[int] = None
    hdop: Optional[float] = None
    io: Dict[int, Union[int, str]] = field(default_factory=dict)
    # Extended elements: ``ext_id -> int``, or ``bytes`` when bit 15 of the
    # id is set (a MAC, a driver's key). ``flevio.catalog.ext_name`` names
    # them. Empty on a device that has nothing of the kind to say.
    ext: Dict[int, Union[int, bytes]] = field(default_factory=dict)
    delta: bool = False                       # how it arrived, for diagnostics

    @property
    def has_fix(self) -> bool:
        return self.lat is not None

    @property
    def dated(self) -> bool:
        """False for a record written before the device knew the date.

        Then `ts` is seconds since that boot and element 248 (`time_src`) is
        0. Date it from its arrival; do not file it in 1970.
        """
        return self.io.get(248, 1) != 0 and self.ts > 1_600_000_000


def decode_record(buf: bytes, off: int, prev: Optional[Record]) -> Tuple[Record, int]:
    """Decode one record. `prev` is the previous record of the same frame."""
    raw_ts, p = get_varint(buf, off)
    if p + 2 > len(buf):
        raise DecodeError("record header runs past the end")
    event, flags = buf[p], buf[p + 1]
    p += 2

    delta = bool(flags & F_DELTA)
    if delta and prev is None:
        raise DecodeError("delta record with nothing before it")
    ts = prev.ts + ((raw_ts >> 1) ^ -(raw_ts & 1)) if delta else raw_ts

    rec = Record(ts=ts, event=event,
                 priority=(flags & F_PRIO_MASK) >> F_PRIO_SHIFT, delta=delta)

    if flags & F_POS:
        dlat, p = get_svarint(buf, p)
        dlon, p = get_svarint(buf, p)
        # Hundred-thousandths of a degree, about 1.1 m. The receiver is
        # honest to three to five metres, so the digits below this were
        # never a measurement - they were varint bytes spent on noise.
        #
        # In a delta record the position is a difference - but only from a
        # predecessor that had one. A record written in a tunnel carries no
        # position at all, and the next one with a fix starts again.
        if delta and prev is not None and prev.lat is not None:
            lat_1e5 = round(prev.lat * 1e5) + dlat
            lon_1e5 = round(prev.lon * 1e5) + dlon
        else:
            lat_1e5, lon_1e5 = dlat, dlon
        rec.lat, rec.lon = lat_1e5 / 1e5, lon_1e5 / 1e5
    if flags & F_ALT:
        rec.alt_m, p = get_svarint(buf, p)
    if flags & F_HEADING:
        if p >= len(buf):
            raise DecodeError("heading runs past the end")
        rec.heading_deg = buf[p] * 2          # 2-degree resolution
        p += 1
    if flags & F_SPEED:
        rec.speed_kph, p = get_varint(buf, p)
    if flags & F_SATS:
        if p + 2 > len(buf):
            raise DecodeError("satellites run past the end")
        rec.sats, rec.hdop = buf[p], buf[p + 1] / 10.0
        p += 2

    # Elements. A delta record starts from its predecessor's set, applies
    # what changed, then removes what is gone.
    io: Dict[int, Union[int, str]] = dict(prev.io) if delta and prev else {}
    ext: Dict[int, Union[int, bytes]] = dict(prev.ext) if delta and prev else {}
    n, p = get_varint(buf, p)
    for _ in range(n):
        if p >= len(buf):
            raise DecodeError("element runs past the end")
        io_id = buf[p]
        p += 1
        if io_id == IO_EXT:
            # An extended element: u8 len, varint ext_id, then a zigzag
            # number or raw bytes to the end of len. An ext_id with nothing
            # after it is a tombstone - in a delta record, "this one is
            # gone". A decoder that does not know 254 skips it as a string,
            # which is why the extension costs nobody an upgrade.
            if p >= len(buf):
                raise DecodeError("extended element runs past the end")
            ln = buf[p]
            p += 1
            body = buf[p:p + ln]
            if len(body) != ln:
                raise DecodeError("extended element body runs past the end")
            p += ln
            ext_id, q = get_varint(body, 0)
            if ext_id == 0 or ext_id > 0xFFFF:
                raise DecodeError("extended element id out of range")
            if q == len(body):
                ext.pop(ext_id, None)
            elif ext_id & EXT_BLOB:
                ext[ext_id] = bytes(body[q:])
            else:
                ext[ext_id], _ = get_svarint(body, q)
        elif io_id >= IO_STRING_MIN:
            io[io_id], p = get_str8(buf, p)
        else:
            io[io_id], p = get_svarint(buf, p)
    if delta:
        m, p = get_varint(buf, p)
        for _ in range(m):
            if p >= len(buf):
                raise DecodeError("removal list runs past the end")
            io.pop(buf[p], None)
            p += 1
    rec.io = io
    rec.ext = ext
    return rec, p


# --- payloads --------------------------------------------------------------

@dataclass
class Hello:
    """The device introducing itself, once per TCP connection."""

    version: int
    want_ack: bool
    imei: str
    serial: str
    fw: str
    cfg_revision: int
    # Version 2 - what the unit is. Empty on a version 1 HELLO.
    model: str = ""          # "FE-OT100", "FE-ST-50"
    hw: str = ""             # the board revision, "rev0.3"
    caps: str = ""           # "gnss,lte-m,ble,can2,...": what it can produce


def decode_hello(payload: bytes) -> Hello:
    if len(payload) < 2:
        raise DecodeError("hello is too short")
    ver, flags = payload[0], payload[1]
    imei, p = get_str8(payload, 2)
    serial, p = get_str8(payload, p)
    fw, p = get_str8(payload, p)
    rev, p = get_varint(payload, p)
    model = hw = caps = ""
    if ver >= 2 and p < len(payload):
        model, p = get_str8(payload, p)
        hw, p = get_str8(payload, p)
        caps, p = get_str8(payload, p)
    return Hello(ver, bool(flags & HELLO_F_ACK), imei, serial, fw, rev, model, hw, caps)


@dataclass
class DataBatch:
    seq: int
    imei: Optional[str]       # present on UDP, where there is no handshake
    records: List[Record]


def decode_data(payload: bytes) -> DataBatch:
    if len(payload) < 4:
        raise DecodeError("data frame is too short")
    seq = struct.unpack(">H", payload[:2])[0]
    flags = payload[2]
    p = 3
    imei = None
    if flags & DATA_F_IDENT:
        imei, p = get_str8(payload, p)
    count = payload[p]
    p += 1

    records: List[Record] = []
    prev: Optional[Record] = None
    for _ in range(count):
        rec, p = decode_record(payload, p, prev)
        records.append(rec)
        prev = rec
    if p != len(payload):
        # Trailing bytes mean the count and the content disagree. Refusing
        # the whole frame is right: a partly understood batch acknowledged
        # as if it were whole is how records go missing silently.
        raise DecodeError(f"{len(payload) - p} byte(s) left after {count} record(s)")
    return DataBatch(seq, imei, records)


def decode_reply(payload: bytes) -> Tuple[int, str]:
    """A device's answer to a CMD: the same id, and the text."""
    if len(payload) < 2:
        raise DecodeError("reply is too short")
    return struct.unpack(">H", payload[:2])[0], payload[2:].decode("utf-8", "replace")
