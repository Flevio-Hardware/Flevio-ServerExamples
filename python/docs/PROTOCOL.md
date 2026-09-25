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
| 0x01 | HELLO | device → server | `u8 version` (2), `u8 flags` (bit0: the device expects ACKs), `str8 imei`, `str8 serial`, `str8 fw`, `varint cfg_revision`; version 2 adds `str8 model`, `str8 hw`, `str8 caps` (see *HELLO version 2*) |
| 0x02 | DATA  | device → server | `u16 seq`, `u8 flags` (bit0: `str8 imei` follows), `u8 count`, records |
| 0x03 | PING  | device → server | empty; keepalive on an idle TCP socket |
| 0x04 | REPLY | device → server | `u16 cmd_id`, text (the rest of the payload) |
| 0x81 | ACK   | server → device | `u16 seq`, `u8 accepted` |
| 0x82 | CMD   | server → device | `u16 cmd_id`, text |
| 0x83 | TIME  | server → device | `i64 unix_ms` |

Kinds 0x05-0x0F (device → server) and 0x84-0x8F (server → device) are
reserved for later - a binary upload such as a photo or a bus capture, a
binary download. A frame of a kind you do not know is skipped by its length.

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
text is `[config_password] VERB args`; without the password it runs at the
user level. The reply carries the same `cmd_id` so the server can match them
up; a command that reboots the device (`RESET`) is replied to before the
reboot. *Commands and settings* below lists them.

Switching things is a command too: `SETOUT 1 ON` closes output 1 and `SETOUT
1 OFF SAFE` opens it once the vehicle has been still for five seconds - the
form an immobiliser should always be driven in. The reply says what happened
now; the `DOUT_CHANGED` record (event 52) says when the relay actually moved,
and every record carries the state of every output as extended element
`dout<n>` (and every input as `din<n>`, with `DIN_CHANGED`, event 53, when
one moves), so the server's picture of the relay never depends on having
seen the reply. `SETOUT` needs the password on this channel.

`TIME` is a courtesy for a device that has neither a fix nor a network clock
yet; it is ignored once either exists. Send it after the HELLO if you send it
at all.

### The conversation over MQTT

With `srv1_protocol = 2` (mqtt) the device does not open a socket of its own:
it connects to the customer's broker (`srv1_host`, `srv1_port`, usually 8883)
and carries the very same frames as MQTT messages under a topic prefix
(`srv1_topic`, param 39, default `flevio`):

```
    <prefix>/<imei>/hello    HELLO frame, retained: who the unit is
    <prefix>/<imei>/status   "online", retained; "offline" is the last will
    <prefix>/<imei>/data     DATA frames, QoS 1, one frame per message
    <prefix>/<imei>/cmd      subscribed: a command as plain text or a CMD frame
    <prefix>/<imei>/reply    the answer: text for text, a REPLY frame for a frame
```

* The client id is the IMEI; `srv1_user` / `srv1_password` (params 37, 38)
  are the broker's.
* There is no ACK frame. A DATA message is delivered when the broker
  acknowledges it (PUBACK, QoS 1); that is the receipt the device waits for.
  The broker - and whatever subscribes to it with a persistent session - is
  then responsible for not losing it.
* No PING either: the session is kept alive at `srv1_keepalive_s`.
* HELLO is published once per session, retained, so a subscriber that
  starts later still learns what the unit is.
* Each message holds exactly one frame and each DATA frame is
  self-contained (delta records refer only to the record before them in the
  same frame), so a subscriber decodes a message on its own.
* Delivery is at least once: a DATA frame whose receipt the device did not
  see comes again. Store records idempotently - unique on (imei, ts, event).
* Commands are taken at QoS 0 and the broker does not hold them for a
  device that is offline. A command for a device that may be away belongs in
  a queue on the server, sent when `status` says `online` and matched to its
  REPLY by `cmd_id` - `flevio/cmdqueue.py` and `examples/mqtt_server.py` do
  exactly that.
* TLS (`srv1_tls`, param 36): 0 off, 1 encrypted, 2 encrypted and the
  broker's certificate checked against the authorities built into the
  firmware - a handful of the public roots that sign most cloud brokers.
* The second server (`srv2_*`, params 43-47) can be a broker too.

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

Not every element in a record is a fresh reading. A signal is reported at
the value the server already has until the real one moves past the deadband
its vehicle-database entry gives it - which the delta coder then writes as
nothing at all. A server sees a step function at the resolution somebody
asked for, not a smoothed or interpolated value, and never a reading older
than 60 seconds.

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

### Extended elements

The byte-wide id is the right size for what a vehicle bus says and the wrong
size for everything else a tracker grows: wired inputs and outputs, 1-Wire
and RS-485 sensors, a dozen Bluetooth sensors with six readings each. Rather
than a second codec, one id carries a 16-bit space inside it:

```
 u8      254        the extended-element id
 u8      len        of everything that follows, as for any string element
 varint  ext_id     which element (table below)
 ...                zigzag varint value - or, when bit 15 of ext_id is set,
                    raw bytes to the end of len
```

Every decoder written before this existed already handles it: 254 is in the
string range, so it reads `len` and skips. A record may carry any number of
them; each `ext_id` is one element and is delta-coded like any other - sent
when it appears or changes, silent while it does not. In a delta record an
extended element the previous record had and this one has not is written as
a **tombstone**: id 254, `len` covering only the `ext_id`, nothing after it.
An old decoder skips that too. The byte-wide removal list at the end of a
delta record never names 254.

Bit 15 of `ext_id` (`0x8000`) says the value is bytes rather than a number;
a MAC address, a driver's key, a raw advertisement. Everything else is an
integer in the unit named, fractions scaled rather than rounded. Ids below
60000 are assigned in `proto.h`; 60000-65535 are for a customer's own use
and Flevio never assigns them.

| ext_id | name | value |
|--------|------|-------|
| 1001-1008 | `din1`..`din8` | digital input, 0/1 |
| 1011-1018 | `dout1`..`dout8` | output, the commanded state 0/1 |
| 1021-1028 | `ain1`..`ain8` | analogue input, mV |
| 1031-1034 | `pulse1`..`pulse4` | pulse counter, lifetime count |
| 1041-1042 | `freq1`..`freq2` | frequency input, Hz x10 |
| 1100 (bytes) | `driver_id` | the key or card as presented: 8-byte iButton, RFID uid, BLE card id |
| 1101 | `driver_id_kind` | 1 iButton, 2 RFID, 3 BLE card, 4 keypad |
| 1102 | `driver_auth` | 0 unknown, 1 authorised, 2 rejected |
| 1111-1118 | `temp1`..`temp8` | 1-Wire thermometer, degC x100, signed |
| 1121-1128 (bytes) | `temp_id1`..`temp_id8` | its 8-byte ROM id, on the first record it appears in |
| 1201-1204 | `fuel_level1`..`4` | RS-485 level sensor, in the unit its calibration gives, x10 |
| 1211-1214 | `fuel_temp1`..`4` | degC |
| 1221-1224 | `fuel_raw1`..`4` | the sensor's raw count, for calibration |
| 1231-1234 | `axle_load1`..`4` | kg |
| 1240 | `tacho_state` | tachograph driver state as the unit reports it |
| 1241 (bytes) | `tacho_card` | driver card number |
| 1301-1307 | `cell_mcc`, `cell_mnc`, `cell_tac`, `cell_id`, `cell_rsrp` (dBm), `cell_rsrq` (dB x10), `cell_rat` (1 LTE-M, 2 NB-IoT, 3 GSM) | where the device was when it had no fix |
| 1310 (bytes) | `cell_iccid` | |
| 1351 | `gnss_acc_m` | the receiver's own error estimate, metres, in 5 m steps |
| 1352 | `gnss_fix_age_s` | how old the position in the header is |
| 1353 | `gnss_jamming` | 0/1 |
| 1354 | `gnss_sats_view` | satellites in view (used are in the header) |
| 1355 | `gnss_ttff_s` | on the first fix of a boot |
| 1356 | `gnss_alt_acc_m` | |
| 1401-1405 | `dev_temp_c`, `dev_heap_free`, `dev_modem_resets`, `dev_uptime_s`, `dev_queue` | device health |
| 1410, 1411 (bytes) | `dev_hw_rev`, `dev_model` | on `POWER_UP` |
| 1451, 1452 | `geofence_id`, `geofence_state` (1 entered, 2 left) | |
| 1461-1465 | `trip_id`, `trip_distance_m`, `trip_duration_s`, `trip_idle_s`, `trip_max_kph` | on `TRIP_STOP` |
| 1501 (bytes), 1502 | `media_id`, `media_kind` (1 photo, 2 clip, 3 bus capture, 4 audio) | a reference to something uploaded another way |
| 2000 + 32·slot + n | `ble<slot>_...` | Bluetooth sensors, below |
| 4000-7999 | `vdb<n>` | vehicle-database signals beyond ids 100-199, assigned by the database |
| 60000-65535 | `private<n>` | yours |

**Bluetooth sensors** have thirty-two slots of thirty-two ids each; a slot is
one sensor the device was told to listen for (or found, when allowed). Its
MAC goes out on the first record it appears in and then only when it
changes, like any other element. Which fields a slot carries depends on the
sensor; an absent field means the sensor has no such reading, not zero.

| +n | name | value |
|----|------|-------|
| 0 (bytes) | `mac` | 6 bytes |
| 1 | `rssi` | dBm, negative |
| 2, 3 | `batt_pct`, `batt_mv` | |
| 4 | `temp` | degC x100, signed |
| 5 | `humidity` | % x10 |
| 6 | `pressure` | hPa x10 |
| 7 | `lux` | |
| 8 | `magnet` | door: 0 closed, 1 open |
| 9, 10 | `moving`, `move_count` | |
| 11, 12 | `pitch`, `roll` | degrees, signed |
| 13, 14 | `flags`, `custom` | sensor-defined |
| 15 (bytes) | `adv` | the raw advertisement, for a sensor nobody has decoded yet |
| 16 (bytes) | `name` | |
| 17 | `kind` | what the device took it for: 1 beacon, 2 thermometer, 3 door, 4 fuel cap, 5 tyre, 6 custom |
| 18 | `age_s` | seconds since it was last heard |
| 19 | `tpms_kpa` | tyre pressure |
| 20 | `fuel_pct` | % x10 |

A device says which of these it can produce in its capability list (HELLO
version 2 and the `caps` line of `cfg info`): `din1`, `ain1`, `dout1`,
`ble-sensors`, `1wire`, `rs485`. A server that stores records from several
models therefore knows, per unit, which extended ids can ever arrive.

The decoder returns them as `Record.ext`, a dictionary from `ext_id` to
`int` or `bytes`; `catalog.ext_name()` names them, `catalog.describe_ext()`
prints one, `catalog.decode_ext()` turns the whole dictionary into
JSON-ready `{name: value}`.

### HELLO version 2

Version 2 appends three `str8` fields after `cfg_revision`: `model`
(`FE-OT100`, `FE-ST-50`), `hw` (the board revision the production bench
wrote into the unit, `rev0.3`) and `caps` (the capability list, comma
separated). A decoder written for version 1 reads the fields it always did
and ignores the rest; a decoder for version 2 reads them when `version >= 2`
and there are bytes left.

## Commands and settings

A command is text, the same on every channel - the server (CMD frame, or the
MQTT `cmd` topic), SMS and the device's console. The ones a server uses most:

| command | answer |
|---|---|
| `GETSTATUS` | ignition, movement, signal, satellites, voltages, queue, server link |
| `GETINFO` | model, firmware, IMEI, serial, SIM |
| `GETVEHICLE` | VIN, bus protocol, what the vehicle reports |
| `GETODO` | odometer and engine hours now - see below |
| `POLLQ` | a POLL record now, through the normal queue |
| `LIVETRACK <min>` / `LIVETRACK OFF` | a LIVE record every `demand_period_s` (param 151) for that long |
| `CHECKIN` | the device checks in with device management now (configuration, firmware) |
| `GETPARAMS <id>,<id>...` | `id=value;` for each |
| `SETPARAMS <id>=<value>;...` | `OK applied=n rejected=n denied=n cfg=<revision>` |
| `EVENTS [SERVER\|BLE ON\|OFF <name>,...]` | which events reach which channel |
| `SETOUT <n> ON\|OFF [SAFE]` / `GETOUT` | switch an output (password) / the outputs now |
| `RESET` | reboot, answered first (password) |

`GETODO` is the call for an ELD at a duty-status change: one line of
`key=value` pairs, split on spaces and `=`, `-` where a value is unknown:

```
odo=432105.0 odo_src=bus hours=7200.0 hours_src=bus ign=1 eng=1 spd=0
lat=40.71275 lon=-74.00597 fix_age=1 utc=1790367448
```

`odo` is km and `hours` engine hours, one decimal, each with where it came
from: `bus` (the vehicle reported it) or the device's own count (`gps` for
distance, `calc` for hours). `fix_age` is seconds since that position;
`utc` is the device's clock in unix seconds, `-` before it has one.

**Settings** are numbered parameters, read and written as `id=value;`
pairs. A value is always a number except for text: yes/no is `1`/`0`, a
choice is its number (`32=2` is MQTT). `flevio/params.json` lists every one -
key, title, type, unit, range, default, choices, and who may change it:
`user` parameters from any channel, `protected` ones only after the
configuration password, `locked` ones only through device management. It is
generated from the same registry as the device's own settings, so it matches
the firmware it ships with; `flevio.params` reads it:

```python
from flevio import params
params.setparams({"move_send_period_s": 300})   # 'SETPARAMS 145=300;' - checked first
params.parse("140=60;145=120;")                  # {140: '60', 145: '120'}
params.describe(32, "2")                         # '32 srv1_protocol = mqtt (...)'
```

A secret parameter (a password) is never sent back unless the command
carried the configuration password.

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
| events | ids 0–255 | 37 | **219** |

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
