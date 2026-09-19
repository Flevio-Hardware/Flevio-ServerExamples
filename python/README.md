# Flevio Python Server

A reference data server for the **Flevio Compact Protocol (codec 0xF2)** — the
wire format an [FE-OT100](https://firsteld.com) ELD tracker speaks to whatever
server you point it at.

It is complete, tested against the firmware's own encoder, and written to be
read: the decoder is one file with no dependencies, the server is another, and
between them they cover every frame, every event and every data element the
device can send. Take the whole thing and build your product on it, or take
`flevio/protocol.py` alone and port it to your language.

Python 3.8 or newer. **No dependencies** — standard library only.

```bash
git clone https://github.com/Flevio-Hardware/Flevio-ServerExamples
cd Flevio-ServerExamples/python

python3 examples/01_print_records.py        # a server on :5600
python3 -m flevio.simulator                 # a truck, in another terminal
```

You should see a truck drive from Manhattan towards Newark, reporting every
simulated minute, with engine RPM, coolant, odometer and VIN decoded.

---

## What is in here

| | |
|---|---|
| `flevio/protocol.py` | Frames, records, CRC, varints. No I/O, no dependencies. **This file is the specification.** |
| `flevio/catalog.py` | What event 17 and element 205 mean, with units and scaling. |
| `flevio/server.py` | A working asyncio server: TCP, UDP, acknowledgements, downlink commands. |
| `flevio/sinks.py` | Two storage handlers — JSON lines and SQLite — that commit before they acknowledge. |
| `flevio/device.py` | The encoder, for tests and the simulator. A server never needs it. |
| `flevio/simulator.py` | A truck that drives around, queues what you do not acknowledge, and answers commands. |
| `docs/PROTOCOL.md` | The byte-level specification, with a worked example. |
| `tests/` | The decoder against a corpus produced by the firmware's C encoder. |
| `examples/` | Four servers, from six lines to one you could leave running. |

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

The other side of that promise: a device re-sends anything it was not told
about, so **the same record will arrive twice**. Make your write idempotent on
`imei + ts + event`. The SQLite sink does this with a `UNIQUE` constraint;
copy that, whatever your database.

Prove both with the simulator:

```bash
python3 examples/02_sqlite_server.py fleet.db
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
in a tunnel, queue it again yourself.

## Events and data elements

Every event code and every IO element id, with units, scaling and a
description, is in `flevio/catalog.py`:

```python
from flevio import catalog

catalog.event_name(17)              # 'SHOCK'
catalog.is_essential(17)            # True - cannot be switched off
catalog.IO[205].to_physical(128)    # 88.0   (coolant is degC + 40 on the wire)
catalog.decode_io(record.io)        # {'ignition': 1, 'ext_voltage': 13.82, ...}
```

Elements 100–199 are assigned by the **vehicle database**, not by firmware, so
a device may send an id this table has not heard of yet — that is the point of
the database: decoding a new signal on a new truck does not need a new
firmware build. `decode_io` keeps unknown ids under `io_<id>` rather than
dropping them. Never drop an element you do not recognise.

Two flags are worth handling before you file anything by time:

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

**TLS** is not in this example. The device can speak it; how you provision
certificates is your decision, and a bad default here would be worse than
none.

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
record produced still reaches the store.

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

## Licence

MIT. Build your product on it.
