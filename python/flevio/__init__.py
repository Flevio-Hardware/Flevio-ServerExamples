"""
Flevio Compact Protocol (codec 0xF2) - reference server, in plain Python.

An FE-OT100 tracker talks this protocol to whatever server you point it at.
This package is the whole of the server side: a decoder, a catalogue of what
every number means, a working asyncio server, two storage sinks, and a
simulator so you can build against it before a unit is on your desk.

    from flevio.server import Handler, Server

    class MyHandler(Handler):
        async def on_records(self, session, records):
            ...                       # store them
            return len(records)       # only after they are stored

    asyncio.run(Server(MyHandler()).serve())

Modules, in the order they are worth reading:

* :mod:`flevio.protocol`  - frames, records, CRC, varints. No dependencies,
  no I/O, nothing to configure. If you are porting to another language, this
  file is the specification.
* :mod:`flevio.catalog`   - what event 17 and element 205 mean, with units.
* :mod:`flevio.params`    - every configuration parameter: GETPARAMS and
  SETPARAMS, checked before they go out.
* :mod:`flevio.server`    - TCP and UDP, acknowledgements, commands.
* :mod:`flevio.cmdqueue`  - commands for devices that are offline (MQTT).
* :mod:`flevio.sinks`     - JSON lines and SQLite, both committing before
  they acknowledge.
* :mod:`flevio.device`    - the encoder, for tests and the simulator.
* :mod:`flevio.simulator` - a truck that drives around and reports.

Python 3.8 or newer. Nothing outside the standard library.

MIT licensed - build your product on it.
"""

__version__ = "1.0.0"

#: The protocol this package speaks. A device announces it as the first byte
#: of every frame.
CODEC = 0xF2

from . import catalog, protocol  # noqa: E402,F401

__all__ = ["catalog", "protocol", "CODEC", "__version__"]
