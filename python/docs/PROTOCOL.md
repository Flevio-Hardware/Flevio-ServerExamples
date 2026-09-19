# Flevio Compact Protocol - codec 0xF2

The wire format between an FE-OT100 and the customer's data server. It
replaces our earlier 0xF1 format entirely; nothing speaks 0xF1 any more.

Two implementations are the contract, and the tests hold them to each other:

* the C encoder the device runs, in the firmware;
* `flevio/protocol.py` in this repository - the reference decoder, plain
  Python, no dependencies. `tests/corpus.json` is a corpus produced by that C
  encoder, and `tests/test_protocol.py` decodes it field for field. If those
  pass, this decoder reads a real device exactly as the device meant it.

Everything is big-endian unless it is a varint.

## Why it looks like this

Cat-M1 airtime is the budget. A routine point on the old format was 100-130
bytes: eight-byte timestamp, fixed-width fields, every IO element repeated in
every record with its width group headers, the VIN and serial as 30 bytes of
string per record. The same point in 0xF2 is 25-40 bytes, three things doing
the work:

1. **Varints.** A speed is one byte, an odometer in metres five, a timestamp
   five once per packet and one or two bytes after that. Nothing is padded to
   a fixed width.
2. **Deltas inside a packet.** The first record of a packet is complete. Each
   record after it is coded against its predecessor: the time as a
   difference, the position as a difference, and only the IO elements whose
   value changed. Coolant, voltage, engine hours, the VIN - unchanged between
   two points a minute apart - cost nothing after the first record.
3. **Nothing per record that is per connection.** Identity travels in a
   `HELLO` once per TCP connection; on UDP it is in the frame header, once per
   datagram.

Deltas never cross a packet boundary. The server needs no state between
packets, a lost packet corrupts nothing after it, and a packet can be decoded
from a database dump on its own.

## Frames

```
 offset  size  field
 0       1     0xF2          codec id, also the sync byte
 1       1     kind
 2       2     length        of the payload, 0..1380
 4       n     payload
 4+n     2     crc16         over bytes 1 .. 4+n-1 (kind, length, payload)
```

CRC-16/IBM: polynomial 0xA001 (reflected 0x8005), init 0, no final xor. It
has a published check value, so an implementation can be verified on its own:
`crc16("123456789") == 0xBB3D`. A frame whose CRC fails is
skipped byte by byte until the next 0xF2 that starts a valid frame;
`flevio.protocol.FrameParser` shows how.

| kind | name  | direction | payload |
|------|-------|-----------|---------|
| 0x01 | HELLO | device → server | `u8 version=1`, `u8 flags` (bit0: the device expects ACKs), `str8 imei`, `str8 serial`, `str8 fw`, `varint cfg_revision` |
| 0x02 | DATA  | device → server | `u16 seq`, `u8 flags` (bit0: `str8 imei` follows), `u8 count`, records |
| 0x03 | PING  | device → server | empty; keepalive on an idle TCP socket |
| 0x04 | REPLY | device → server | `u16 cmd_id`, text (the rest of the payload) |
| 0x81 | ACK   | server → device | `u16 seq`, `u8 accepted` |
| 0x82 | CMD   | server → device | `u16 cmd_id`, text |
| 0x83 | TIME  | server → device | `i64 unix_ms` |

`str8` is a length byte followed by that many bytes, no terminator.

### The conversation over TCP

```
device                                  server
  ── HELLO(ack flag) ───────────────────►
  ◄──────────────────────── ACK(seq 0, 1) ──   (only when the device asked for ACKs)
  ── DATA(seq 4711, 12 records) ────────►
  ◄──────────────────── ACK(4711, 12) ─────
  ── DATA(seq 4712, 3 records) ─────────►
  ◄──────────────────── ACK(4712, 3) ──────
  ◄──────────── CMD(9, "GETSTATUS") ───────   any time
  ── REPLY(9, "ign=1 mov=1 spd=87 ...") ─►
  ── PING ──────────────────────────────►     every srv1_keepalive_s while idle
```

**ACK mode on** (`srv1_ack_mode = 1`, the default): the device waits up to
`srv1_ack_timeout_s` for the ACK carrying the DATA frame's `seq`. `accepted`
is how many of the frame's records the server stored, counted from the first;
the device marks exactly that many as delivered and sends the rest again in
its next frame. `accepted = 0` is a rejection. No ACK in time closes the
socket and reopens it after `tcp_embargo_s`. The HELLO must be answered with
`ACK(0, 1)`; `ACK(0, 0)` means "I do not know this IMEI" and the device backs
off.

**ACK mode off** (`srv1_ack_mode = 0`): the server sends nothing back for
HELLO or DATA (it may still send CMD and TIME). A frame counts as delivered
when the modem reports it sent. Cheaper by one small frame per batch; a
record in a TCP connection that dies mid-flight is lost.

**UDP** (`srv1_protocol = 1`): no HELLO, no ACKs regardless of the mode; every
DATA frame carries the IMEI (flags bit0). One frame per datagram.

Commands from the server run through the same parser as SMS commands. The
text is `[config_password] VERB
args`; without the password it runs at the user level. The reply carries the
same `cmd_id` so the server can match them up; a command that reboots the
device (`RESET`) is replied to before the reboot.

`TIME` is a courtesy for a device that has neither a fix nor a network clock
yet; it is ignored once either exists. Send it after the HELLO if you send it
at all.

## Records

```
 varint   ts        absolute record: unix seconds
                    delta record:    zigzag difference from the previous record
 u8       event     reason code (see flevio.catalog.EVENTS)
 u8       flags
            bit 0   position follows       (lat, lon)
            bit 1   altitude follows
            bit 2   heading follows
            bit 3   speed follows
            bit 4   satellites + HDOP follow
            bits 5-6  priority: 0 low, 1 high, 2 panic
            bit 7   this is a delta record
 [pos]    zigzag lat_1e5, zigzag lon_1e5   (1e-5 deg, about 1.1 m)
                    absolute record: the values
                    delta record:    differences from the previous record's
                                     position, if it had one; else absolute
 [alt]    zigzag metres
 [hdg]    u8 degrees / 2               (2-degree resolution)
 [spd]    varint km/h
 [sats]   u8 satellites, u8 HDOP x 10  (255 = 25.5 or worse)
 varint   n
 n ×      element: u8 id, then
            id < 250:  zigzag varint value
            id >= 250: u8 length, bytes  (a string: VIN, serial, firmware)
 [delta records only]
 varint   m
 m ×      u8 id     elements present in the previous record and absent here
```

Decoding a delta record: start from a copy of the previous record's element
map, apply the `n` changed or new elements, remove the `m` listed ids. The
header fields other than position (altitude, heading, speed, satellites) are
always absolute when present and simply absent when the device had no fix.

A record with no fix has none of bits 0-4 set. A record written before the
device knew the date carries the uptime in seconds as `ts` and element 248
(`time_src`) = 0; the server should date it from arrival.

A record whose position was **reckoned** rather than measured - the device
was in a tunnel and built the position from the vehicle bus distance and an
estimated heading - carries element 244 (`pos_src`) = 1 and element 243
(`pos_err_m`), the radius the device believes that position is good to. It
has no altitude and no satellite count, because there are none. Do not draw
one as though it were a fix, and for an ELD treat the record as only as good
as the radius attached to it.

The unit of every element is fixed by its id; `flevio.catalog.IO` is the
full list, with the scale and offset that turn the wire integer back into a
physical quantity. Ids 100-199 are assigned by the vehicle database rather
than by firmware, so a device may send an id this table has not heard of -
keep it under its number rather than dropping it.

Position is carried in **hundred-thousandths of a degree**, about 1.1 m, not
in ten-millionths. A ten-millionth is 1.1 cm and the receiver is honest to
three to five metres, so those last two digits were never a measurement -
they were varint bytes spent on noise. Anything that used to round a
position to 1e-7 should now round to 1e-5; nothing else about the record
changed.

### Worked example

Produced by the real encoder in the firmware, not by hand. Two
periodic points while moving, 60 s apart, 1.2 km further on; speed 82 → 84,
odometer +1500 m, bus speed and rpm changed; ignition, voltages, coolant,
fuel, engine hours, VIN and serial unchanged. The full frame:

```
f2 02 007e                       codec, DATA, 126-byte payload
  1267 00 02                     seq 4711, no IMEI (TCP), 2 records
  e0b4a6d506 03 1f ...           record 1, absolute, 94 bytes (16 elements, 2 strings)
  78 03 9f f2c001 896a 17 b1 54 0b09 03 c9c0a00a ca88b08b9c03 ccfc16 00
                                 record 2, delta, 28 bytes
af57                             CRC
```

Record 2, byte by byte:

```
78             ts: zigzag(+60 s)
03             event ON_PERIODIC
9F             flags: pos alt hdg spd sats, priority low, DELTA
F2 C0 01       zigzag Δlat  = +12345
89 6A          zigzag Δlon  = -6789
17             zigzag altitude = -12 m
B1             heading 354° / 2
54             speed 84 km/h
0B 09          11 satellites, HDOP 0.9
03             three elements changed
C9 C0 A0 0A      201 bus_speed_mkph = 84000
CA 88 B0 8B 9C 03  202 odometer_m   = 432106500
CC FC 16         204 rpm            = 1470
00             nothing removed
```

Twenty-eight bytes for the second point against 94 for the keyframe and
about 120 in the old format. A batch of twelve one-minute points on the
highway is roughly 94 + 11 × 28 + 10 ≈ 410 bytes on the wire, including the
VIN and serial in every record (`periodic_identity = 1`) - the strings cost
only in the keyframe.

## Sizing

`FCP_PAYLOAD_MAX` is 1380 so a frame stays under the Cat-M1 path MTU. The
device puts up to 32 records in a frame and stops at the last whole one that
fits; a batch that does not fit is simply two frames.

`PROTO_REC_MAX` (400) bounds one absolute record: 32 elements at up to 11
bytes each plus two strings. The device drops elements past 32, so the
fixed set and the things a record is about (a fault code on `DTC_NEW`, the
shock on `SHOCK`) go in first and the database extras last.

## A minimal server loop

The short version, if you are writing your own rather than using
`flevio.server`:

```python
from flevio import protocol as p

parser = p.FrameParser()
imei = None
while True:
    parser.feed(sock.recv(2048))
    for f in parser.frames():
        if f.kind == p.K_HELLO:
            h = p.decode_hello(f.payload)
            imei = h.imei if known(h.imei) else None
            if h.want_ack:
                sock.send(p.ack(0, 1 if imei else 0))
        elif f.kind == p.K_DATA and imei:
            batch = p.decode_data(f.payload)
            stored = store(imei, batch.records)   # how many are COMMITTED
            sock.send(p.ack(batch.seq, stored))   # only in ACK mode
        elif f.kind == p.K_REPLY:
            cmd_id, text = p.decode_reply(f.payload)
            complete_command(imei, cmd_id, text)
        elif f.kind == p.K_PING:
            sock.send(p.ack(0, 1))
```

Decode the whole frame before acknowledging any of it; a frame that fails to
decode is a bug on one side or the other, and no acknowledgement plus a log
line is the honest answer - the device will send it again.

`store()` returning before the write has committed is the one way to lose
data with this protocol. Until the server says otherwise, the device is the
only copy.

## Extending it

The protocol is ours and it is meant to grow. A new parameter needs no new
frame, no new version and no change to a server that already works: it is a
new element id with a value, and the delta coding means it costs nothing on
the wire in the records where it has not changed.

**A server written today keeps working against a device shipped in three
years.** That is a property of the format, not a promise: an element is an id
and a value, and whether the value is a number or a string is decided by the
*range* the id falls in, not by a type tag the server has to understand. So a
decoder meets an id it has never heard of, decodes it correctly anyway, and
hands it to you as `io_233`. `flevio/catalog.py` does exactly this; anything
you port should too. Refusing a record because one of its elements is unknown
is the one reaction that turns a routine firmware update into an outage.

The room that is actually left, which is worth knowing before you plan a
product around it:

| space | width | used | free |
|---|---|---|---|
| numeric elements | ids 1–249 | 152 | **97** |
| string elements | ids 250–255 | 3 | **3** |
| events | ids 0–255 | 36 | **220** |

Values themselves are unbounded — a signed varint grows to fit, so a counter
that outgrows four bytes needs no format change at all.

Two honest caveats about that table:

* **Element ids are one byte**, in the record and in the removal list both.
  256 is a hard wall, not a soft one. 97 free numeric ids is comfortable for
  years of new J1939 and OBD-II parameters, but it is a finite budget and it
  should be spent deliberately rather than one id per idea.
* **The string range is nearly full.** Three ids left, and a string element is
  how anything text-shaped travels. If you need more than three, that is the
  change worth planning for rather than discovering.

When the one-byte id does run out, the hook is already in place: `HELLO`
carries `version`, the server sees it before any record arrives, and widening
the id to a varint is a version-2 change that an existing server can refuse
cleanly instead of misreading. That is the intended path, and it is the
reason the version byte is in the handshake rather than in each record.
