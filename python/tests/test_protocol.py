"""
The decoder against the firmware's own encoder.

``corpus.json`` is not written by hand. It is the output of the C test
binary in the firmware tree (``test/proto/test_f2.c``), which encodes twelve
records - deltas, removed elements, negative values, a changed VIN, a clock
step backwards, records with no fix and records with no date - and prints
both the logical records and the bytes it produced. If this file passes, a
Python server reads a real device exactly as the device meant it.

Run with ``python -m pytest`` or plainly::

    python3 tests/test_protocol.py
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from flevio import device, protocol as p  # noqa: E402
from flevio.protocol import DecodeError, FrameParser, Record  # noqa: E402

CORPUS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "corpus.json")


def load():
    with open(CORPUS) as fh:
        return json.load(fh)


# --------------------------------------------------------------------------
# The pieces
# --------------------------------------------------------------------------


def test_crc_check_value():
    """The standard check value for CRC-16/IBM. If this is wrong, nothing
    else can be right - every frame would be rejected at both ends."""
    assert p.crc16(b"123456789") == 0xBB3D


def test_varint_roundtrip():
    for v in (0, 1, 127, 128, 300, 2 ** 31, 2 ** 63 - 1):
        got, off = p.get_varint(p.put_varint(v), 0)
        assert got == v and off == len(p.put_varint(v))


def test_svarint_roundtrip():
    for v in (0, -1, 1, -127, 127, -740059730, 2 ** 40, -(2 ** 40)):
        got, _ = p.get_svarint(device.put_svarint(v), 0)
        assert got == v


def test_truncated_varint_is_an_error():
    try:
        p.get_varint(b"\x80\x80", 0)
    except DecodeError:
        return
    raise AssertionError("a varint with no end should not decode")


# --------------------------------------------------------------------------
# Frames
# --------------------------------------------------------------------------


def test_parser_handles_any_split():
    """TCP gives you whatever it gives you. One byte at a time must work."""
    frames = device.hello_frame("865341041238314", "FT1", "2.0.0", 41)
    frames += device.ping_frame() + device.reply_frame(7, "OK")
    parser = FrameParser()
    out = []
    for i in range(len(frames)):
        parser.feed(frames[i:i + 1])
        out += parser.frames()
    assert [f.kind for f in out] == [p.K_HELLO, p.K_PING, p.K_REPLY]


def test_parser_skips_bytes_that_cannot_start_a_frame():
    """Noise without a 0xF2, and a 0xF2 with an impossible length, are both
    thrown away without waiting for anything."""
    good = device.ping_frame()
    parser = FrameParser()
    parser.feed(b"noise\xf2\xff\xffmore noise" + good)
    assert [f.kind for f in parser.frames()] == [p.K_PING]


def test_parser_resynchronises_after_a_false_header():
    """Garbage whose second and third bytes read as a plausible length is the
    hard case: the parser cannot know it is garbage until it has enough bytes
    to test the CRC, so it waits, fails the check, drops one byte and finds
    the real frame. Nothing is lost - it is only late."""
    good = device.ping_frame()
    parser = FrameParser()
    parser.feed(b"\xf2\x00\x72" + b"x" * 8)      # claims a 114-byte payload
    assert parser.frames() == [], "cannot judge it yet, and must not guess"
    parser.feed(b"y" * 120 + good)
    assert [f.kind for f in parser.frames()] == [p.K_PING]


def test_a_corrupt_frame_is_dropped_not_delivered():
    raw = bytearray(device.data_frame(1, [Record(ts=1789500000, event=1, priority=0)]))
    raw[-1] ^= 0xFF                       # break the CRC
    parser = FrameParser()
    parser.feed(bytes(raw) + device.ping_frame())
    kinds = [f.kind for f in parser.frames()]
    assert p.K_DATA not in kinds


# --------------------------------------------------------------------------
# The corpus: the C encoder against this decoder
# --------------------------------------------------------------------------


def _expected(e: dict) -> dict:
    """The firmware prints raw integers; turn them into what we decode to."""
    out = {
        "ts": e["ts"],
        "event": e["event"],
        "priority": e["priority"],
        "io": {int(k): v for k, v in e["io"].items()},
        "ext": {int(k): (bytes.fromhex(v) if isinstance(v, str) else v)
                for k, v in e.get("ext", {}).items()},
    }
    out["lat"] = e["lat_1e5"] / 1e5 if "lat_1e5" in e else None
    out["lon"] = e["lon_1e5"] / 1e5 if "lon_1e5" in e else None
    out["alt_m"] = e.get("alt_m")
    out["heading_deg"] = e.get("heading_deg")
    out["speed_kph"] = e.get("speed_kph")
    out["sats"] = e.get("sats")
    out["hdop"] = e["hdop_x10"] / 10.0 if "hdop_x10" in e else None
    return out


def _compare(exp: dict, got: Record, where: str):
    assert got.ts == exp["ts"], "%s: ts %d != %d" % (where, got.ts, exp["ts"])
    assert got.event == exp["event"], where
    assert got.priority == exp["priority"], where
    if exp["lat"] is None:
        assert got.lat is None, "%s: unexpected position" % where
    else:
        # A 1e-5 degree is about a metre; the protocol carries the integer,
        # so this has to be exact, not close.
        assert round(got.lat * 1e5) == round(exp["lat"] * 1e5), where
        assert round(got.lon * 1e5) == round(exp["lon"] * 1e5), where
    for field in ("alt_m", "heading_deg", "speed_kph", "sats", "hdop"):
        assert getattr(got, field) == exp[field], "%s: %s" % (where, field)
    assert got.io == exp["io"], "%s: io %r != %r" % (where, got.io, exp["io"])
    assert got.ext == exp["ext"], "%s: ext %r != %r" % (where, got.ext, exp["ext"])


def test_corpus_data_frames():
    corpus = load()
    records = corpus["records"]
    seen = 0
    for f in corpus["frames"]:
        if f["kind"] != "data":
            continue
        batch = p.decode_data(_payload(f))
        assert batch.seq == f["seq"]
        assert batch.imei == f["imei"]
        assert len(batch.records) == f["count"]
        for i, rec in enumerate(batch.records):
            _compare(_expected(records[f["first"] + i]), rec,
                     "seq %d record %d" % (f["seq"], i))
            seen += 1
    assert seen == sum(f["count"] for f in corpus["frames"] if f["kind"] == "data")
    assert seen >= 17, "the corpus should exercise every frame"


def _payload(f: dict) -> bytes:
    """Take the frame out of the hex the firmware printed, envelope and all."""
    parser = FrameParser()
    parser.feed(bytes.fromhex(f["hex"]))
    frames = parser.frames()
    assert len(frames) == 1, "%s should be exactly one frame" % f["kind"]
    return frames[0].payload


def test_corpus_hello_ping_reply():
    corpus = load()
    for f in corpus["frames"]:
        if f["kind"] == "hello":
            h = p.decode_hello(_payload(f))
            assert h.imei == f["imei"]
            assert h.serial == f["serial"]
            assert h.fw == f["fw"]
            assert h.cfg_revision == f["cfg_revision"]
            assert h.want_ack == f["want_ack"]
            assert h.version == 2
            assert (h.model, h.hw, h.caps) == (f["model"], f["hw"], f["caps"])
        elif f["kind"] == "reply":
            cmd_id, text = p.decode_reply(_payload(f))
            assert cmd_id == f["cmd_id"] and text == f["text"]
        elif f["kind"] == "ping":
            parser = FrameParser()
            parser.feed(bytes.fromhex(f["hex"]))
            assert parser.frames()[0].kind == p.K_PING


def test_delta_coding_actually_saves_what_it_claims():
    """The whole point of the codec. 469 bytes for what cost 789 stored."""
    corpus = load()
    first = [f for f in corpus["frames"] if f["kind"] == "data"][0]
    assert first["count"] == 12
    wire = len(first["hex"]) // 2
    stored = sum(corpus["stored_bytes"])
    assert wire < stored * 0.7, "%d on the wire against %d stored" % (wire, stored)
    # Under 45 bytes for the bare drive; the corpus also carries a Bluetooth
    # thermometer and the receiver's accuracy on every point, which is what
    # a real unit with sensors sends, and that costs a few bytes a record.
    assert wire / first["count"] < 50, "a record should average well under 50 bytes"


# --------------------------------------------------------------------------
# Our own encoder, so the simulator is trustworthy
# --------------------------------------------------------------------------


def test_python_encoder_matches_python_decoder():
    recs = [
        Record(ts=1789500000, event=1, priority=1, lat=40.712753, lon=-74.005973,
               alt_m=-12, heading_deg=350, speed_kph=0, sats=11, hdop=0.9,
               io={1: 1, 66: 13820, 250: "1FUJGLDR8CSBP1234"}),
        Record(ts=1789500060, event=3, priority=0, lat=40.713000, lon=-74.006500,
               alt_m=-10, heading_deg=352, speed_kph=64, sats=12, hdop=0.8,
               io={1: 1, 66: 13810, 250: "1FUJGLDR8CSBP1234", 204: 1450}),
        # No fix, and an element has gone away.
        Record(ts=1789500120, event=29, priority=0,
               io={1: 1, 66: 13810, 250: "1FUJGLDR8CSBP1234"}),
        # A fix again after the gap: the position must be absolute here.
        Record(ts=1789500180, event=30, priority=0, lat=40.714, lon=-74.007,
               sats=9, hdop=1.4, io={1: 1, 66: 13805, 250: "1FUJGLDR8CSBP1234"}),
    ]
    frame = device.data_frame(42, recs)
    parser = FrameParser()
    parser.feed(frame)
    batch = p.decode_data(parser.frames()[0].payload)
    assert batch.seq == 42 and len(batch.records) == len(recs)
    for want, got in zip(recs, batch.records):
        assert got.ts == want.ts and got.event == want.event
        assert got.io == want.io
        if want.lat is None:
            assert got.lat is None
        else:
            assert round(got.lat * 1e5) == round(want.lat * 1e5)
            assert round(got.lon * 1e5) == round(want.lon * 1e5)


def test_clock_going_backwards_survives():
    """The device gets its first GNSS fix mid-packet and the clock jumps
    back. A delta timestamp is signed precisely so this decodes."""
    recs = [
        Record(ts=1789500600, event=3, priority=0),
        Record(ts=1789500000, event=3, priority=0),
    ]
    parser = FrameParser()
    parser.feed(device.data_frame(1, recs))
    batch = p.decode_data(parser.frames()[0].payload)
    assert [r.ts for r in batch.records] == [1789500600, 1789500000]


def test_trailing_bytes_are_refused():
    """Better to re-send a batch than to acknowledge half of one."""
    parser = FrameParser()
    parser.feed(device.data_frame(1, [Record(ts=1789500000, event=1, priority=0)]))
    payload = parser.frames()[0].payload + b"\x00"
    try:
        p.decode_data(payload)
    except DecodeError:
        return
    raise AssertionError("a batch with trailing bytes should not decode")


def test_undated_records_are_flagged():
    r = Record(ts=94, event=0, priority=1, io={248: 0, 246: 1})
    assert not r.dated
    assert Record(ts=1789500000, event=0, priority=0).dated



# --------------------------------------------------------------------------
# Extended elements
# --------------------------------------------------------------------------


def test_extended_elements_round_trip_and_delta():
    """Wired inputs and a Bluetooth thermometer, coded and decoded by the
    Python pair; then the same thing an old decoder would see."""
    from flevio import catalog
    mac = bytes.fromhex("c47c8d6a1234")
    a = Record(ts=1789500000, event=3, priority=0, io={1: 1},
               ext={1351: 10, 1021: 12480, 0x8000 | 2000: mac, 2004: -1250})
    b = Record(ts=1789500060, event=3, priority=0, io={1: 1},
               ext={1351: 10, 0x8000 | 2000: mac, 2004: -1225})   # ain1 gone, temp up
    c = Record(ts=1789500120, event=3, priority=0, io={1: 1})     # everything gone
    frame = device.data_frame(7, [a, b, c])
    parser = FrameParser()
    parser.feed(frame)
    batch = p.decode_data(parser.frames()[0].payload)
    assert [r.ext for r in batch.records] == [a.ext, b.ext, {}]
    assert batch.records[1].delta and batch.records[2].delta
    assert catalog.ext_name(1021) == "ain1"
    assert catalog.ext_name(0x8000 | 2000) == "ble0_mac"
    assert catalog.ext_name(2004) == "ble0_temp"
    assert catalog.decode_ext(a.ext)["ble0_mac"] == mac.hex()


def test_a_decoder_that_predates_extended_elements_still_keeps_its_place():
    """Element 254 is in the string range, so a decoder that has never heard
    of it reads the length and skips - and lands on the next record."""
    from flevio.protocol import get_varint, get_svarint, get_str8, F_POS, F_ALT, \
        F_HEADING, F_SPEED, F_SATS, F_DELTA

    def legacy_records(payload: bytes, count: int):
        off = 4                                       # seq, flags, count
        out = []
        for _ in range(count):
            _, off = get_varint(payload, off)
            event, flags = payload[off], payload[off + 1]
            off += 2
            if flags & F_POS:
                _, off = get_svarint(payload, off)
                _, off = get_svarint(payload, off)
            if flags & F_ALT:
                _, off = get_svarint(payload, off)
            if flags & F_HEADING:
                off += 1
            if flags & F_SPEED:
                _, off = get_varint(payload, off)
            if flags & F_SATS:
                off += 2
            n, off = get_varint(payload, off)
            io = {}
            for _ in range(n):
                io_id = payload[off]
                off += 1
                if io_id >= 250:
                    _, off = get_str8(payload, off)   # skips 254 without a thought
                else:
                    io[io_id], off = get_svarint(payload, off)
            if flags & F_DELTA:
                m, off = get_varint(payload, off)
                off += m
            out.append((event, io))
        assert off == len(payload), "the legacy walk did not end on the last byte"
        return out

    mac = bytes.fromhex("c47c8d6a1234")
    recs = [
        Record(ts=1789500000, event=3, priority=0, lat=40.7, lon=-74.0, speed_kph=80,
               io={1: 1, 66: 13800}, ext={1351: 10, 0x8000 | 2000: mac, 2004: -1250}),
        Record(ts=1789500060, event=3, priority=0, lat=40.71, lon=-74.01, speed_kph=82,
               io={1: 1, 66: 13820}, ext={1351: 15, 2004: -1200}),   # mac gone: tombstone
        Record(ts=1789500120, event=6, priority=1, io={1: 0}),
    ]
    frame = device.data_frame(9, recs)
    parser = FrameParser()
    parser.feed(frame)
    payload = parser.frames()[0].payload
    seen = legacy_records(payload, 3)
    assert [e for e, _ in seen] == [3, 3, 6]
    assert seen[0][1] == {1: 1, 66: 13800}

if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok   %s" % name)
            except Exception as e:  # noqa: BLE001
                failed += 1
                print("FAIL %s: %s" % (name, e))
    print("\n%s" % ("all tests passed" if not failed else "%d failed" % failed))
    sys.exit(1 if failed else 0)

