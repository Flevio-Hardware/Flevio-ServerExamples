# Flevio Server Examples

Reference data servers for the **Flevio Compact Protocol (codec 0xF2)** — the
wire format an [FE-OT100](https://firsteld.com) ELD tracker speaks to whatever
server you point it at.

These are not sketches. Each one is a working server, tested against the
firmware's own encoder, and written to be read rather than merely run: take
the whole thing and build your product on it, or take the decoder alone and
port it to your language.

| | | |
|---|---|---|
| [`python/`](python/) | Python 3.8+, no dependencies (MQTT: `paho-mqtt`) | TCP, UDP and MQTT, acknowledgements, commands, every event, element and configuration parameter, a truck simulator, two example servers |

## Start here

```bash
git clone https://github.com/Flevio-Hardware/Flevio-ServerExamples
cd Flevio-ServerExamples/python

python3 examples/server.py                  # a server on :5600
python3 -m flevio.simulator                 # a truck, in another terminal
```

`examples/server.py` (TCP and UDP) and `examples/mqtt_server.py` (a broker of
yours) print how to point a device at them, show everything it sends and take
commands at a prompt. A truck drives from Manhattan towards Newark and reports every simulated
minute, with engine RPM, coolant temperature, odometer and VIN decoded. No
hardware required, and nothing to install first.

## The protocol

[`python/docs/PROTOCOL.md`](python/docs/PROTOCOL.md) is the byte-level
specification, with a worked example you can decode by hand.
[`python/flevio/protocol.py`](python/flevio/protocol.py) is the same thing as
code — one file, no dependencies, no I/O. If you are porting to another
language, that file is what to read.

Two things about it are worth knowing before you start, because both are
places a server can be written that appears to work and quietly loses data:

* **A batch is acknowledged by count, from the first record.** Your handler
  returns how many records it has *durably stored*, not how many it received.
  The device marks exactly that many delivered and keeps the rest. Returning
  the length of the list before your database commits is how a power cut
  becomes missing data.
* **Records are delta-coded against the one before them inside a packet.** An
  element whose value has not changed costs nothing on the wire and is
  *absent* from the record — which is not the same as the element having gone
  away. The decoder here reconstructs full records for you; a hand-rolled one
  that treats absence as removal will silently drop half the telemetry.

## Adding an example in another language

One directory per language, named for it, each self-contained and each with
its own README. The protocol document and the test corpus in
`python/tests/corpus.json` — produced by the firmware's C encoder, not written
by hand — are the shared reference. A port is right when it decodes that
corpus byte for byte.

## Licence

MIT. See [LICENSE](LICENSE). This is code meant to be copied into production
systems, and the licence is chosen to let you do that without asking.
