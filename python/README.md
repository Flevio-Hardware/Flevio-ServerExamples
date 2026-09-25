# Flevio Python Server

A reference data server for the **Flevio Compact Protocol (codec 0xF2)** — the
wire format an [FE-OT100](https://firsteld.com) ELD tracker speaks to whatever
server you point it at.

It is complete, tested against the firmware's own encoder, and written to be
read: the decoder is one file with no dependencies, the server is another, and
between them they cover every frame, every event, every data element and
every configuration parameter the device has. Take the whole thing and build
your product on it, or take `flevio/protocol.py` alone and port it to your
language.

Python 3.8 or newer. **No dependencies** for TCP and UDP; `paho-mqtt` for MQTT.

## Two servers

Both show everything the devices send, as it arrives, and take commands at a
prompt. Neither stores anything: they are where your own system starts.

**TCP and UDP** — the device connects to you:

```bash
git clone https://github.com/Flevio-Hardware/Flevio-ServerExamples
cd Flevio-ServerExamples/python

python3 examples/server.py --public your.server.com     # TCP and UDP on :5600
python3 -m flevio.simulator                             # a truck, in another terminal
```

**MQTT** — the device publishes to your broker, and this listens there:

```bash
pip install paho-mqtt
export FLEVIO_MQTT_PASSWORD=...                         # the broker user's password
python3 examples/mqtt_server.py --host broker.example.com --user me
```

Each prints, first, the exact lines to point a device at it — for the USB
console, by SMS, and in the Configurator:

```
   USB console:   cfg setparams 30=your.server.com;31=5600;32=0;
                  cfg save
   SMS:           <sms_password> SETPARAMS 30=your.server.com;31=5600;32=0;
```

then every message, one record per line with every element named:

```
20:26:56 865341041238314 IGN_ON  high  2026-09-25 20:27:55 UTC | 40.71275,-74.00597 alt 43 m hdg 296 12 sats hdop 1.8 | 0 km/h
      ignition = on, ext_voltage = 13.687 V, odometer = 432105.0 km, engine_hours = 7200.0 h, engine_rpm = 700 rpm, ...
```

At the prompt:

| | |
|---|---|
| `list` | devices connected (MQTT: seen, online or not) |
| `odo <imei>` | odometer, engine hours, ignition, position — `GETODO`, for a duty-status change |
| `<imei> <command>` | any command: `GETSTATUS`, `GETINFO`, `GETVEHICLE`, `POLLQ`, `LIVETRACK 10`, `GETPARAMS 140,145`, `SETPARAMS 145=300;` … |
| `param <id \| name \| word>` | what a configuration parameter is: `param 145`, `param period` |
| `queue`, `cancel <id>` | MQTT: commands waiting for a device that is offline |

A device on the mobile network needs a **public** address. `server.py` shows
the machine's local one when `--public` is not given, and says so; run it on
a server that has one, or forward the port on your router.

---

## What is in here

| | |
|---|---|
| `flevio/protocol.py` | Frames, records, CRC, varints. No I/O, no dependencies. **This file is the specification.** |
| `flevio/catalog.py` | What event 17 and element 205 mean, with units and scaling. |
| `flevio/params.py`, `params.json` | Every configuration parameter: name, unit, range, default, choices, access. |
| `flevio/server.py` | A working asyncio server: TCP, UDP, acknowledgements, downlink commands. |
| `flevio/cmdqueue.py` | Commands for devices that are offline: held, sent when they are back, matched to their answers. |
| `flevio/sinks.py` | Two storage handlers — JSON lines and SQLite — that commit before they acknowledge. |
| `flevio/device.py` | The encoder, for tests and the simulator. A server never needs it. |
| `flevio/simulator.py` | A truck that drives around, queues what you do not acknowledge, and answers commands. |
| `docs/PROTOCOL.md` | The byte-level specification, with a worked example, MQTT, commands and settings. |
| `examples/` | `server.py` (TCP and UDP) and `mqtt_server.py` (MQTT). |
| `tests/` | The decoder against a corpus produced by the firmware's C encoder, and more. |

## The smallest server that works

```python
import asyncio
from flevio.server import Handler, Server

class MyHandler(Handler):
    async def on_records(self, session, records):
        for r in records:
            print(session.imei, r.ts, r.event, r.lat, r.lon, r.io)
        return len(records)        # how many you STORED

asyncio.run(Server(MyHandler()).serve())
```

## The one rule

`on_records` returns **how many records you have durably stored**, counted
from the first. The device marks exactly that many as delivered and keeps the
rest to send again.

```python
async def on_records(self, session, records):
    try:
        await db.insert(records)   # commits
    except DatabaseError:
        return 0                   # the device keeps every one of them
    return len(records)
```

* Return `len(records)` **only after the write has committed.**
* Return `0` when your database is down. The device holds tens of thousands
  of records in flash and will offer them again in seconds. Your endpoint can
  be down for an afternoon and lose nothing.
* Return `3` when three went in and the fourth failed. The device re-sends
  from the fourth.

Acknowledging before the write commits is the only way to lose data with this
protocol. Until the server says otherwise, **the device is the only copy**.
(The two example servers acknowledge what they have shown, because they store
nothing — the line to change is marked.)

The other side of that promise: a device re-sends anything it was not told
about, so **the same record will arrive twice**. Make your write idempotent on
`imei + ts + event`. `flevio/sinks.py` does this with a `UNIQUE` constraint;
copy that, whatever your database. Prove both with the simulator:

```python
# fleet.py
import asyncio
from flevio.server import Server
from flevio.sinks import SqliteSink

async def main():
    async with SqliteSink("fleet.db") as sink:
        await Server(sink).serve()

asyncio.run(main())
```

```bash
python3 fleet.py
python3 -m flevio.simulator --drop 0.3 --backlog 500 --speed 3000
```

A third of the packets never arrive. Count the rows at the end: nothing is
missing, and nothing is doubled.

## Talking back to a device

```python
session = server.sessions["865341041238314"]
print(await session.send_command("GETSTATUS"))
print(await session.send_command("EVENTS SERVER OFF IDLING,SPEEDING"))
print(await session.send_command("SETPARAMS 140=30;"))
```

The text is the same language the device accepts over SMS and from the driver
application. Without a password in front it runs at the device's *user* level
— status, event masks, periods, thresholds. Put the configuration password
first to reach *protected* parameters such as the APN and the server address.
Some events (ignition, power, crash, VIN, bus, configuration and firmware
changes) are **essential** and the device will refuse to have them switched
off; `flevio.catalog.is_essential()` tells you which before you ask.

Commands are not retried by the device. If one times out because the truck was
in a tunnel, send it again yourself — or queue it, as `mqtt_server.py` does.

**GETODO** is the call to make when a driver changes duty status: one line
of `key=value` pairs, answered from what the device has right now:

```
odo=100341.9 odo_src=bus hours=2.5 hours_src=bus ign=1 eng=1 spd=0 lat=40.71275 lon=-74.00597 fix_age=3 utc=1790380990
```

`odo` km and `hours` engine hours, one decimal; `*_src` says whether the
value came from the vehicle bus (`bus`) or is the device's own count (`gps`,
`calc`); `-` is a value the vehicle has not given; `fix_age` is how old the
position is in seconds; `utc` is the device's clock in unix seconds. It works
on every transport — TCP, UDP, MQTT, SMS.

## Settings

Every configuration parameter — the ids in `GETPARAMS` and `SETPARAMS` — is
in `flevio/params.json`, generated from the same registry the device's own
settings are built from. `flevio.params` reads it, and checks a value before
it goes out:

```python
from flevio import params

params.get("move_send_period_s")                 # Param(id=145, unit='s', default=120, ...)
params.setparams({"move_send_period_s": 300})    # 'SETPARAMS 145=300;'
params.setparams({32: "mqtt"}, password="...")   # '... SETPARAMS 32=2;' - protected
params.parse("140=60;145=120;")                  # {140: '60', 145: '120'} - a GETPARAMS answer
params.describe(32, "2")                         # "32 srv1_protocol = mqtt (...)   [Protocol]"
params.find("check-in")                          # parameters that mention it
```

Values are numbers except for text — yes/no is `1`/`0`, a choice is its
number. Each parameter says who may change it: `user` from any channel,
`protected` after the configuration password, `locked` only through device
management. Passwords are never sent back without the configuration password.

## Events and data elements

Every event code and every IO element id, with units, scaling and a
description, is in `flevio/catalog.py`:

```python
from flevio import catalog

catalog.event_name(17)              # 'SHOCK'
catalog.is_essential(17)            # True - cannot be switched off
catalog.IO[205].to_physical(128)    # 88.0   (coolant is degC + 40 on the wire)
catalog.decode_io(record.io)        # {'ignition': 1, 'ext_voltage': 13.82, ...}
catalog.decode_ext(record.ext)      # {'ain1': 12480, 'ble0_temp': -1250, 'ble0_mac': 'c47c8d6a1234'}
```

Elements 100–199 are assigned by the **vehicle database**, not by firmware, so
a device may send an id this table has not heard of yet — that is the point of
the database: decoding a new signal on a new truck does not need a new
firmware build. `decode_io` keeps unknown ids under `io_<id>` rather than
dropping them. Never drop an element you do not recognise.

Flags worth handling before you file anything by time:

* **`record.dated`** — a tracker has no battery-backed clock. Between power-up
  and its first fix it still writes records, and those carry *seconds since
  boot* in `ts`, with element 248 (`time_src`) set to 0. File them by arrival
  time, not by `ts`, or you will have trips in 1970.
* **element 249 (`simulated`)** — the record came from the ELD Simulator. A
  server that ignores it stores invented mileage as real.
* **element 244 (`pos_src`) = 1** — the position was *reckoned*, not
  measured: the device was in a tunnel or a covered yard and built it from
  the distance the vehicle bus reported and a heading estimated from lateral
  acceleration. Element 243 (`pos_err_m`) is the radius it believes that
  position is good to, typically a few hundred metres. These records have no
  altitude and no satellite count, on purpose. Draw them as a circle rather
  than a point, and never let one become a mileage or hours-of-service fact
  without the radius beside it.

## Transports

**TCP** (default). The device connects, sends `HELLO` once, then `DATA` frames
with rising sequence numbers, and `PING` while idle. The connection carries the
identity, so `DATA` frames have no IMEI in them. Answer `HELLO` with
`ACK(0, 1)` to accept the device or `ACK(0, 0)` to refuse it — refusing is how
you keep a device you have never provisioned out of your database.

**UDP**. No handshake and no acknowledgements: every datagram carries the IMEI
and the device fires and forgets. Cheaper, and anyone can forge a datagram —
if you run UDP in production, check the IMEI against a device you know, or put
it behind a VPN.

**ACK mode off** (`srv1_ack_mode = 0`). The device sends nothing back and
treats a frame as delivered once the modem reports it sent. One small frame
cheaper per batch; a record in a connection that dies mid-flight is gone.
`session.acked` tells your handler which mode it is in.

**MQTT** (`srv1_protocol = 2`). The device publishes the same frames to
your broker, one frame per message, under `srv1_topic` (default `flevio`):

```
    <prefix>/<imei>/hello    HELLO frame, retained: who the unit is
    <prefix>/<imei>/status   "online", retained; "offline" is the last will
    <prefix>/<imei>/data     DATA frames, QoS 1, one frame per message
    <prefix>/<imei>/cmd      subscribed: a command as plain text or a CMD frame
    <prefix>/<imei>/reply    the answer: text for text, a REPLY frame for a frame
```

No ACK to send — the broker's PUBACK is the receipt — so subscribe with a
persistent session (a fixed client id, clean session off) if you must see
what arrived while you were down; `mqtt_server.py` does.

Commands are **not** kept by the broker for a device that is offline: the
device takes them at QoS 0, so that a command is never run twice. Keep them on
your side and send them when the device's `status` says `online`.
`flevio/cmdqueue.py` does exactly that — one command in flight per device,
answers matched by id, actions such as `RESET` never repeated — and
`mqtt_server.py` uses it: a command typed for an offline device waits and
goes out when the device comes back.

**TLS** on MQTT is the device's `srv1_tls` (param 36): 1 encrypts, 2 also
checks the broker's certificate. `mqtt_server.py` uses TLS unless `--plain`;
`--ca` names a private authority.

## Running the tests

```bash
./run_tests.sh              # or: python3 -m pytest tests/
```

`tests/corpus.json` is not written by hand. It is the output of the firmware's
own C encoder, covering deltas, removed elements, negative values, a changed
VIN, a clock that steps backwards, records with no fix and records with no
date. `tests/test_protocol.py` decodes it and compares every field. If that
passes, this decoder reads a real device exactly as the device meant it.

`tests/test_server.py` runs the simulator against the server with a third of
the frames dropped and a quarter of the batches refused, and checks that every
record produced still reaches the store. `tests/test_cmdqueue.py` covers the
command queue, and `tests/test_params.py` the parameter table and its
agreement with the event catalogue.

## Porting to another language

Read `docs/PROTOCOL.md` and `flevio/protocol.py`; between them they are the
whole contract. Then run your implementation against `tests/corpus.json` — the
same bytes, the same expected fields. Four things catch people out:

1. **CRC-16/IBM**, polynomial 0xA001 reflected, init 0, no final xor.
   `crc16("123456789") == 0xBB3D`. Check this first; if it is wrong, every
   frame is rejected at both ends and nothing else can be debugged.
2. **Zigzag before varint** for anything signed — timestamps in delta records,
   position differences, element values.
3. **Deltas never cross a packet.** The first record of every frame is
   absolute. Carry state forward inside one frame only.
4. **A delta position is a difference from the last record that *had* one.**
   After a tunnel, the next fix is absolute again.

`flevio/params.json` is plain JSON; any language can load it for parameter
names, ranges and defaults.

## Licence

MIT. Build your product on it.
