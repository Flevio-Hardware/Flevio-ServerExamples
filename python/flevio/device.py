"""
The other half: encoding, as the device does it.

A server never needs this. It is here for three reasons, all of them
practical:

* **You can test without hardware.** :mod:`flevio.simulator` drives a whole
  truck through this module, so ``python -m flevio.simulator`` gives your
  server something to decode today rather than when the units arrive.
* **You can prove your decoder.** Encode a record, decode it, compare. The
  test suite does exactly that, including against a corpus produced by the
  firmware's own C encoder.
* **It is the clearest specification of the delta coding.** Thirty lines of
  :func:`encode_records` say more than a page of prose about what the device
  leaves out and why.

The delta rule, in one sentence: the first record of a packet is absolute,
and every record after it stores its time as a difference, its position as a
difference from the last record *that had one*, and only the IO elements
whose value changed - plus the ids that disappeared.
"""

from __future__ import annotations

import struct
from typing import Iterable, List, Optional

from .protocol import (
    DATA_F_IDENT,
    F_ALT,
    F_DELTA,
    F_HEADING,
    F_POS,
    F_PRIO_SHIFT,
    F_SATS,
    F_SPEED,
    IO_STRING_MIN, IO_EXT, EXT_BLOB,
    K_DATA,
    K_HELLO,
    K_PING,
    K_REPLY,
    PAYLOAD_MAX,
    Record,
    build_frame,
    put_str8,
    put_varint,
)

__all__ = [
    "put_svarint",
    "encode_record",
    "encode_records",
    "data_frame",
    "split_records",
    "hello_frame",
    "ping_frame",
    "reply_frame",
    "MAX_RECORDS",
]

#: A frame carries at most this many records, matching ``PROTO_MAX_IO``'s
#: sibling in the firmware. The count is a single byte on the wire, but the
#: device stops well before that to keep a frame inside the Cat-M1 MTU.
MAX_RECORDS = 32


def put_svarint(v: int) -> bytes:
    """Zigzag, then varint: small negative numbers cost one byte, not ten."""
    return put_varint((v << 1) ^ (-1 if v < 0 else 0))


def _pos(v: Optional[float]) -> Optional[int]:
    """Degrees to what a record stores: 1e-5 of a degree, about 1.1 m."""
    return None if v is None else round(v * 1e5)


def encode_record(r: Record, prev: Optional[Record]) -> bytes:
    """One record, delta-coded against ``prev`` when there is one."""
    delta = prev is not None
    body = bytearray()
    flags = (r.priority & 0x03) << F_PRIO_SHIFT
    if delta:
        flags |= F_DELTA

    if r.lat is not None and r.lon is not None:
        flags |= F_POS
    if r.alt_m is not None:
        flags |= F_ALT
    if r.heading_deg is not None:
        flags |= F_HEADING
    if r.speed_kph is not None:
        flags |= F_SPEED
    if r.sats is not None:
        flags |= F_SATS

    head = bytearray()
    if delta:
        head += put_svarint(r.ts - prev.ts)
    else:
        head += put_varint(r.ts)
    head += bytes([r.event & 0xFF, flags])

    if flags & F_POS:
        lat, lon = _pos(r.lat), _pos(r.lon)
        # Only a predecessor that actually had a position can be a base for
        # one. After a tunnel the next fix is absolute again.
        if delta and prev.lat is not None:
            body += put_svarint(lat - _pos(prev.lat))
            body += put_svarint(lon - _pos(prev.lon))
        else:
            body += put_svarint(lat) + put_svarint(lon)
    if flags & F_ALT:
        body += put_svarint(r.alt_m)
    if flags & F_HEADING:
        body += bytes([min(179, r.heading_deg // 2)])
    if flags & F_SPEED:
        body += put_varint(r.speed_kph)
    if flags & F_SATS:
        body += bytes([r.sats & 0xFF, int(round((r.hdop or 0) * 10)) & 0xFF])

    # Elements: what changed, then what went away. Extended elements ride
    # inside id 254; one that went away is a tombstone in the changed list,
    # because the removal list is byte-wide.
    base = prev.io if delta else {}
    base_ext = prev.ext if delta else {}
    changed = [(k, v) for k, v in sorted(r.io.items()) if base.get(k) != v]
    ext_changed = [(k, v) for k, v in sorted(r.ext.items()) if base_ext.get(k) != v]
    ext_gone = [k for k in sorted(base_ext) if k not in r.ext]
    body += put_varint(len(changed) + len(ext_changed) + len(ext_gone))
    for io_id, value in changed:
        body += bytes([io_id])
        if io_id >= IO_STRING_MIN:
            body += put_str8(value if isinstance(value, str) else str(value))
        else:
            body += put_svarint(int(value))
    for ext_id, value in ext_changed:
        body += _ext_element(ext_id, value)
    for ext_id in ext_gone:
        body += _ext_element(ext_id, None)
    if delta:
        gone = [k for k in sorted(base) if k not in r.io]
        body += put_varint(len(gone)) + bytes(gone)

    return bytes(head + body)


def _ext_element(ext_id: int, value) -> bytes:
    """``254, len, varint ext_id, payload``; ``value=None`` is a tombstone."""
    inner = put_varint(ext_id)
    if value is not None:
        if ext_id & EXT_BLOB:
            inner += bytes(value)
        else:
            inner += put_svarint(int(value))
    if len(inner) > 255:
        raise ValueError("extended element %d does not fit" % ext_id)
    return bytes([IO_EXT, len(inner)]) + inner


def encode_records(records: Iterable[Record]) -> List[bytes]:
    """Encode a run of records: the first absolute, the rest as deltas."""
    out: List[bytes] = []
    prev: Optional[Record] = None
    for r in records:
        out.append(encode_record(r, prev))
        prev = r
    return out


def data_frame(seq: int, records: List[Record], imei: Optional[str] = None) -> bytes:
    """A DATA frame. Pass ``imei`` for UDP, leave it out for TCP.

    Raises :class:`ValueError` if the records do not fit; the device's own
    encoder instead sends as many as fit and keeps the rest, which is what
    :func:`split_records` below imitates.
    """
    if len(records) > MAX_RECORDS:
        raise ValueError("at most %d records per frame" % MAX_RECORDS)
    payload = bytearray(struct.pack(">H", seq & 0xFFFF))
    payload += bytes([DATA_F_IDENT if imei else 0])
    if imei:
        payload += put_str8(imei)
    payload += bytes([len(records)])
    for blob in encode_records(records):
        payload += blob
    if len(payload) > PAYLOAD_MAX:
        raise ValueError("%d records do not fit in one frame" % len(records))
    return build_frame(K_DATA, bytes(payload))


def split_records(records: List[Record], imei: Optional[str] = None) -> List[List[Record]]:
    """Chop a list into batches that each fit one frame, as the device does."""
    out: List[List[Record]] = []
    batch: List[Record] = []
    for r in records:
        batch.append(r)
        if len(batch) > MAX_RECORDS:
            out.append(batch[:-1])
            batch = [r]
            continue
        try:
            data_frame(0, batch, imei)
        except ValueError:
            out.append(batch[:-1])
            batch = [r]
    if batch:
        out.append(batch)
    return [b for b in out if b]


def hello_frame(
    imei: str,
    serial: str = "",
    fw: str = "",
    cfg_revision: int = 0,
    want_ack: bool = True,
    version: int = 2,
    model: str = "",
    hw: str = "",
    caps: str = "",
) -> bytes:
    payload = bytes([version, 0x01 if want_ack else 0x00])
    payload += put_str8(imei) + put_str8(serial) + put_str8(fw)
    payload += put_varint(cfg_revision)
    if version >= 2:
        payload += put_str8(model) + put_str8(hw) + put_str8(caps)
    return build_frame(K_HELLO, payload)


def ping_frame() -> bytes:
    return build_frame(K_PING)


def reply_frame(cmd_id: int, text: str) -> bytes:
    return build_frame(K_REPLY, struct.pack(">H", cmd_id & 0xFFFF) + text.encode())
