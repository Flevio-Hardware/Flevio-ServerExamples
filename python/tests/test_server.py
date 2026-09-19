"""
The server and the simulator against each other, including the ugly parts.

The interesting test is :func:`test_nothing_is_lost_when_frames_are_dropped`.
It runs a device that throws a third of its packets away and a server that
refuses to store a quarter of what arrives, and then checks that every single
record produced by the truck is in the database at the end. That is the
promise the acknowledgement protocol makes, and it is the one worth testing.

    python3 tests/test_server.py
"""

from __future__ import annotations

import asyncio
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from flevio import device as dev  # noqa: E402
from flevio import protocol as p  # noqa: E402
from flevio.server import Handler, Server, Session  # noqa: E402
from flevio.simulator import Simulator, Truck  # noqa: E402


class Collector(Handler):
    """Stores records, and can be told to misbehave."""

    def __init__(self, refuse_rate: float = 0.0, seed: int = 7) -> None:
        self.records = []
        self.hellos = []
        self.refuse_rate = refuse_rate
        self.rng = random.Random(seed)

    async def on_hello(self, session, hello) -> bool:
        self.hellos.append(hello)
        return True

    async def on_records(self, session: Session, records) -> int:
        n = len(records)
        if self.refuse_rate and self.rng.random() < self.refuse_rate:
            n = self.rng.randint(0, len(records))     # a partial store
        self.records += [(session.imei, r) for r in records[:n]]
        return n


async def _free_port() -> int:
    s = await asyncio.start_server(lambda *_: None, "127.0.0.1", 0)
    port = s.sockets[0].getsockname()[1]
    s.close()
    await s.wait_closed()
    return port


async def _with_server(handler, body, udp: bool = False):
    port = await _free_port()
    server = Server(handler, host="127.0.0.1", tcp_port=port,
                    udp_port=port if udp else None)
    task = asyncio.get_running_loop().create_task(server.serve())
    await asyncio.sleep(0.15)
    try:
        return await body(server, port)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# --------------------------------------------------------------------------


def test_hello_and_a_batch():
    async def body(server, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(dev.hello_frame("865341041238314", "FT1", "2.0.0", 41))
        await writer.drain()
        parser = p.FrameParser()
        parser.feed(await reader.read(64))
        f = parser.frames()[0]
        assert f.kind == p.K_ACK and f.payload == b"\x00\x00\x01"

        recs = Truck("865341041238314").step() + Truck("x").step()
        writer.write(dev.data_frame(1, recs))
        await writer.drain()
        parser.feed(await reader.read(64))
        ack = [x for x in parser.frames() if x.kind == p.K_ACK][0]
        assert ack.payload[2] == len(recs), "the server should take them all"
        writer.close()
        await asyncio.sleep(0.05)
        assert len(handler.records) == len(recs)
        assert handler.hellos[0].imei == "865341041238314"

    handler = Collector()
    asyncio.run(_with_server(handler, body))


def test_a_refused_hello_closes_the_connection():
    class Picky(Handler):
        async def on_hello(self, session, hello) -> bool:
            return hello.imei == "111111111111111"

    async def body(server, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(dev.hello_frame("222222222222222"))
        await writer.drain()
        data = await reader.read(64)
        parser = p.FrameParser()
        parser.feed(data)
        assert parser.frames()[0].payload == b"\x00\x00\x00", "zero means refused"
        assert await reader.read(1) == b"", "and the socket should close"
        writer.close()

    asyncio.run(_with_server(Picky(), body))


def test_a_handler_that_raises_stores_nothing_and_says_so():
    """The one case where a bug in the customer's code must not lose data."""

    class Broken(Handler):
        async def on_records(self, session, records) -> int:
            raise RuntimeError("the database is on fire")

    async def body(server, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(dev.hello_frame("865341041238314"))
        await writer.drain()
        await reader.read(64)
        writer.write(dev.data_frame(5, Truck("x").step()))
        await writer.drain()
        parser = p.FrameParser()
        parser.feed(await reader.read(64))
        ack = [x for x in parser.frames() if x.kind == p.K_ACK][0]
        assert ack.payload[:2] == b"\x00\x05"
        assert ack.payload[2] == 0, "a handler that raised has stored nothing"
        writer.close()

    asyncio.run(_with_server(Broken(), body))


def test_a_command_gets_its_reply():
    async def body(server, port):
        sim = Simulator("127.0.0.1", port, "865341041238314", speed=6000)
        task = asyncio.get_running_loop().create_task(sim.run())
        for _ in range(100):
            await asyncio.sleep(0.05)
            if "865341041238314" in server.sessions:
                break
        session = server.sessions["865341041238314"]
        answer = await session.send_command("GETSTATUS", timeout=10)
        assert answer.startswith("OK ") and "fw=2.0.0" in answer
        task.cancel()

    asyncio.run(_with_server(Collector(), body))


def test_udp_datagrams_carry_their_own_identity():
    async def body(server, port):
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, remote_addr=("127.0.0.1", port)
        )
        recs = Truck("865341041238314").step()
        transport.sendto(dev.data_frame(9, recs, "865341041238314"))
        await asyncio.sleep(0.3)
        transport.close()
        assert len(handler.records) == len(recs)
        assert handler.records[0][0] == "865341041238314"

    handler = Collector()
    asyncio.run(_with_server(handler, body, udp=True))


def test_nothing_is_lost_when_frames_are_dropped():
    """A third of the packets never arrive and the server refuses a quarter
    of what does. Every record must still be in the store at the end."""

    async def body(server, port):
        sim = Simulator("127.0.0.1", port, "865341041238314",
                        drop=0.33, backlog=120, speed=100000, ack_timeout=0.5)
        produced = list(sim.queue)
        sim._tick = _no_more_records        # freeze the truck; drain the queue
        task = asyncio.get_running_loop().create_task(sim.run())
        for _ in range(600):
            await asyncio.sleep(0.05)
            if not sim.queue:
                break
        task.cancel()
        await asyncio.sleep(0.1)

        assert not sim.queue, "the device still holds %d records" % len(sim.queue)
        stored = {(r.ts, r.event) for _, r in handler.records}
        missing = [r for r in produced if (r.ts, r.event) not in stored]
        assert not missing, "%d records never made it" % len(missing)

    async def _no_more_records():
        await asyncio.sleep(3600)

    handler = Collector(refuse_rate=0.25)
    asyncio.run(_with_server(handler, body))


def test_fire_and_forget_loses_what_it_drops():
    """The other side of the same coin, stated plainly so nobody is
    surprised in production: with acknowledgements off, a dropped frame is
    gone. That is the trade ``srv1_ack_mode 0`` makes."""

    async def body(server, port):
        sim = Simulator("127.0.0.1", port, "865341041238314", want_ack=False,
                        drop=1.0, backlog=20, speed=100000)
        produced = len(sim.queue)
        sim._tick = _idle
        task = asyncio.get_running_loop().create_task(sim.run())
        for _ in range(200):
            await asyncio.sleep(0.02)
            if not sim.queue:
                break
        task.cancel()
        assert produced > 0 and not sim.queue
        assert not handler.records, "everything was dropped, and it is gone"

    async def _idle():
        await asyncio.sleep(3600)

    handler = Collector()
    asyncio.run(_with_server(handler, body))


def test_sqlite_sink_survives_duplicates():
    import tempfile

    from flevio.sinks import SqliteSink

    async def body(server, port):
        recs = Truck("865341041238314").step()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(dev.hello_frame("865341041238314", "FT1", "2.0.0", 41))
        await writer.drain()
        await reader.read(64)
        for seq in (1, 2):                 # the same batch twice, as a retry
            writer.write(dev.data_frame(seq, recs))
            await writer.drain()
            await reader.read(64)
        writer.close()
        await asyncio.sleep(0.2)
        rows = sink._db.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        assert rows == len(recs), "a retry must not double the rows"
        dev_rows = sink._db.execute("SELECT fw FROM devices").fetchone()
        assert dev_rows[0] == "2.0.0"

    with tempfile.TemporaryDirectory() as tmp:
        sink = SqliteSink(os.path.join(tmp, "t.db"))
        try:
            asyncio.run(_with_server(sink, body))
        finally:
            sink.close()


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok   %s" % name)
            except Exception as e:  # noqa: BLE001
                failed += 1
                print("FAIL %s: %r" % (name, e))
    print("\n%s" % ("all tests passed" if not failed else "%d failed" % failed))
    sys.exit(1 if failed else 0)
