"""
A truck, without the truck.

Runs a plausible device against your server: it connects, says HELLO, drives
a route, turns the ignition on and off, reports periodically while moving,
raises the occasional harsh-braking event, answers commands, and - the part
that matters - keeps everything the server did not acknowledge and sends it
again. Build your server against this and the first real unit will be boring.

    python3 -m flevio.simulator --host 127.0.0.1 --port 5600

Useful switches:

    --no-ack        fire and forget, as ``srv1_ack_mode 0`` does
    --udp           datagrams instead of a connection
    --backlog 300   start with a queue, the way a device that has been out of
                    coverage for five hours does
    --drop 0.1      lose a tenth of the frames on purpose
    --speed 60      one simulated minute every second

The queue is the whole point. A device holds its records in flash until a
server says it has them, so a server that acknowledges what it has not stored
loses data silently. Run this with ``--drop`` against your handler and count
what you have at the end: the number must match.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import random
import time
from typing import Dict, List, Optional

from . import device as dev
from . import protocol as p
from .protocol import FrameParser, Record

log = logging.getLogger("flevio.simulator")

#: Manhattan to Newark, roughly. Any list of points will do.
ROUTE = [
    (40.712753, -74.005973),
    (40.718000, -74.020000),
    (40.722000, -74.040000),
    (40.726000, -74.070000),
    (40.730000, -74.100000),
    (40.735000, -74.130000),
    (40.736000, -74.168000),
    (40.740000, -74.175000),
]


def _bearing(a, b) -> int:
    dlon = math.radians(b[1] - a[1])
    y = math.sin(dlon) * math.cos(math.radians(b[0]))
    x = math.cos(math.radians(a[0])) * math.sin(math.radians(b[0])) - math.sin(
        math.radians(a[0])
    ) * math.cos(math.radians(b[0])) * math.cos(dlon)
    return int(math.degrees(math.atan2(y, x))) % 360


class Truck:
    """Produces records. No networking - that is :class:`Simulator`."""

    def __init__(self, imei: str, vin: str = "1FUJGLDR8CSBP1234", serial: str = "FT00001234") -> None:
        self.imei = imei
        self.vin = vin
        self.serial = serial
        self.t = int(time.time())
        self.leg = 0
        self.frac = 0.0
        self.ignition = False
        self.odometer_m = 432_105_000
        self.engine_s = 7_200 * 3600
        self.fuel = 78
        self.rng = random.Random(0xF2)

    # --- one simulated minute ------------------------------------------------

    def step(self, seconds: int = 60) -> List[Record]:
        """Advance the clock and return whatever the device would have
        written in that time."""
        out: List[Record] = []
        self.t += seconds

        if not self.ignition:
            # Parked. The device still says so every so often, and that is
            # all a parked truck should ever cost you.
            self.ignition = True
            out.append(self._rec(1, priority=1))          # IGN_ON
            return out

        moving = self.frac < len(ROUTE) - 1
        if not moving:
            self.ignition = False
            out.append(self._rec(2, priority=1, speed=0))  # IGN_OFF
            self.leg, self.frac = 0, 0.0
            return out

        speed = self.rng.randint(55, 95)
        self.frac += speed * seconds / 3600.0 / 3.0       # ~3 km per leg
        self.odometer_m += int(speed * seconds / 3.6)
        self.engine_s += seconds
        if self.rng.random() < 0.06:
            self.fuel = max(5, self.fuel - 1)

        out.append(self._rec(3, speed=speed))              # ON_PERIODIC
        if self.rng.random() < 0.05:
            out.append(self._rec(15, priority=1, speed=max(0, speed - 30),
                                 extra={209: self.rng.randint(420, 700)}))  # HARDBRAKE
        return out

    # --- building one record -------------------------------------------------

    def _position(self):
        i = min(int(self.frac), len(ROUTE) - 2)
        f = self.frac - i
        a, b = ROUTE[i], ROUTE[i + 1]
        return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f), _bearing(a, b)

    def _rec(self, event: int, priority: int = 0, speed: Optional[int] = None,
             extra: Optional[Dict[int, object]] = None) -> Record:
        (lat, lon), heading = self._position()
        io: Dict[int, object] = {
            1: 1 if self.ignition else 0,
            2: 1 if speed else 0,
            21: self.rng.randint(2, 5),
            66: 13_800 + self.rng.randint(-120, 120),
            67: 4_050,
            68: 100,
            200: 2,                                  # J1939
            201: (speed or 0) * 1000,
            202: self.odometer_m,
            203: self.engine_s,
            204: 700 if not speed else 1_200 + (speed or 0) * 6,
            205: 128,                                # 88 degC on the wire
            206: self.fuel,
            207: 1,
            250: self.vin,
            251: self.serial,
        }
        if extra:
            io.update(extra)
        return Record(
            ts=self.t, event=event, priority=priority,
            lat=round(lat, 6), lon=round(lon, 6),
            alt_m=self.rng.randint(-20, 90),
            heading_deg=heading,
            speed_kph=speed if speed is not None else 0,
            sats=self.rng.randint(8, 14),
            hdop=round(self.rng.uniform(0.6, 1.8), 1),
            io=io,
        )

    def power_up(self) -> Record:
        """The first record of a boot: why it started, and no date yet.

        ``time_src`` 0 says the timestamp is seconds since boot. A server
        that files this by ``ts`` puts it in 1970; a server that reads the
        flag dates it from arrival. Worth testing on purpose, which is why
        the simulator sends it.
        """
        return Record(ts=11, event=0, priority=1,
                      io={248: 0, 246: 1, 247: 0, 252: "2.0.0", 251: self.serial})


class Simulator:
    """The truck, plus a queue and a socket."""

    def __init__(self, host: str, port: int, imei: str, want_ack: bool = True,
                 udp: bool = False, drop: float = 0.0, backlog: int = 0,
                 speed: float = 60.0, fw: str = "2.0.0", cfg_revision: int = 41,
                 ack_timeout: float = 30.0) -> None:
        self.host, self.port, self.imei = host, port, imei
        self.want_ack, self.udp, self.drop = want_ack, udp, drop
        self.speed = speed
        self.ack_timeout = ack_timeout
        """How long to wait for an ACK before sending the batch again.

        The device does the same thing: an unanswered packet is not a lost
        packet, it is a packet whose fate is unknown, and the only safe move
        is to send it once more. That is why a server must be able to take
        the same record twice without counting it twice."""
        self.fw, self.cfg_revision = fw, cfg_revision
        self.truck = Truck(imei)
        self.queue: List[Record] = []
        self.seq = 1
        self.sent = 0
        self.confirmed = 0
        self._spare: List[p.Frame] = []

        self.queue.append(self.truck.power_up())
        for _ in range(backlog):
            self.queue += self.truck.step()

    # --- the interesting part ------------------------------------------------

    def _advance_seq(self) -> int:
        seq, self.seq = self.seq, self.seq % 0xFFFF + 1
        return seq

    async def run(self) -> None:
        if self.udp:
            await self._run_udp()
            return
        while True:
            try:
                await self._session()
            except (ConnectionError, OSError, asyncio.IncompleteReadError) as e:
                log.warning("connection lost (%s); retrying in 5 s", e)
            except asyncio.TimeoutError:
                log.warning("server went quiet; reconnecting")
            await asyncio.sleep(5)

    async def _session(self) -> None:
        reader, writer = await asyncio.open_connection(self.host, self.port)
        log.info("connected to %s:%d as %s", self.host, self.port, self.imei)
        writer.write(dev.hello_frame(self.imei, self.truck.serial, self.fw,
                                     self.cfg_revision, self.want_ack))
        await writer.drain()

        parser = FrameParser()
        self._spare = []
        if self.want_ack:
            frame = await self._next_frame(reader, parser, timeout=self.ack_timeout)
            seq, accepted = self._read_ack(frame)
            if accepted == 0:
                log.error("the server refused this IMEI; stopping")
                writer.close()
                return
            log.info("server accepted the HELLO")

        tick = asyncio.get_running_loop().create_task(self._tick())
        try:
            while True:
                if self.queue:
                    await self._send_one(reader, writer, parser)
                else:
                    writer.write(dev.ping_frame())
                    await writer.drain()
                    await self._drain_downlink(reader, writer, parser, wait=60 / self.speed)
        finally:
            tick.cancel()
            writer.close()

    async def _send_one(self, reader, writer, parser) -> None:
        batch = dev.split_records(self.queue[:dev.MAX_RECORDS])[0]
        seq = self._advance_seq()
        frame = dev.data_frame(seq, batch)
        self.sent += len(batch)

        if self.drop and random.random() < self.drop:
            log.info("dropping seq %d (%d records) on purpose", seq, len(batch))
        else:
            writer.write(frame)
            await writer.drain()

        if not self.want_ack:
            # Fire and forget: the records are gone from the queue whether or
            # not anyone stored them. This is what --no-ack costs.
            del self.queue[:len(batch)]
            return

        while True:
            try:
                f = await self._next_frame(reader, parser, timeout=self.ack_timeout)
            except asyncio.TimeoutError:
                # Nobody answered. Keep the records and try again - which is
                # exactly how a duplicate reaches a server, and why
                # de-duplication is not optional on the storage side.
                log.info("no answer to seq %d; sending it again", seq)
                self.sent -= len(batch)
                return
            if f.kind == p.K_ACK:
                got_seq, accepted = self._read_ack(f)
                if got_seq != seq:
                    continue                 # an answer to something older
                break
            await self._handle_downlink(f, writer)

        if accepted < len(batch):
            log.warning("server took %d of %d; keeping the rest", accepted, len(batch))
        del self.queue[:accepted]
        self.confirmed += accepted
        if accepted == 0:
            await asyncio.sleep(2)           # do not spin against a sick server

    async def _drain_downlink(self, reader, writer, parser, wait: float) -> None:
        try:
            f = await asyncio.wait_for(self._next_frame(reader, parser, timeout=None), wait)
        except asyncio.TimeoutError:
            return
        await self._handle_downlink(f, writer)

    async def _handle_downlink(self, f: p.Frame, writer) -> None:
        if f.kind == p.K_CMD:
            cmd_id = int.from_bytes(f.payload[:2], "big")
            text = f.payload[2:].decode("utf-8", "replace")
            log.info("command #%d: %s", cmd_id, text)
            writer.write(dev.reply_frame(cmd_id, self._answer(text)))
            await writer.drain()
        elif f.kind == p.K_TIME:
            log.info("server offered its clock; we have GNSS, ignoring")
        elif f.kind == p.K_ACK:
            pass                             # a late answer; harmless
        else:
            log.warning("unexpected %s from the server", f.kind_name)

    def _answer(self, text: str) -> str:
        """A believable reply. The real device's answers are richer."""
        head = text.strip().split()[0].upper() if text.strip() else ""
        if head == "GETSTATUS":
            return ("OK ign=%d moving=%d gsm=4 sats=11 vbat=4050 vext=13800 "
                    "queue=%d fw=%s" % (self.truck.ignition, 1, len(self.queue), self.fw))
        if head == "GETGPS":
            (lat, lon), hd = self.truck._position()
            return "OK %.6f,%.6f hdg=%d" % (lat, lon, hd)
        if head == "SETPARAMS":
            self.cfg_revision += 1
            return "OK applied=1 rejected=0 denied=0 cfg=%d" % self.cfg_revision
        if head == "EVENTS":
            return "OK events updated"
        return "ERR unknown command"

    async def _tick(self) -> None:
        """One simulated minute every ``60 / speed`` real seconds."""
        while True:
            await asyncio.sleep(60 / self.speed)
            self.queue += self.truck.step()

    # --- plumbing ------------------------------------------------------------

    @staticmethod
    def _read_ack(f: p.Frame):
        if f.kind != p.K_ACK or len(f.payload) < 3:
            raise ConnectionError("expected an ACK, got %s" % f.kind_name)
        return int.from_bytes(f.payload[:2], "big"), f.payload[2]

    async def _next_frame(self, reader, parser: FrameParser, timeout: Optional[float]):
        """One frame, waiting for the socket only when the buffer is dry.

        `FrameParser.frames()` hands over everything that is complete, so
        what it returns beyond the first has to be kept - a read that threw
        away the second frame of a packet would lose an ACK roughly whenever
        the server was quick.
        """
        while True:
            if self._spare:
                return self._spare.pop(0)
            pending = parser.frames()
            if pending:
                self._spare = pending[1:]
                return pending[0]
            chunk = reader.read(4096)
            data = await (asyncio.wait_for(chunk, timeout) if timeout else chunk)
            if not data:
                raise ConnectionError("the server closed the connection")
            parser.feed(data)

    async def _run_udp(self) -> None:
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, remote_addr=(self.host, self.port)
        )
        log.info("sending datagrams to %s:%d as %s", self.host, self.port, self.imei)
        tick = loop.create_task(self._tick())
        try:
            while True:
                if self.queue:
                    batch = dev.split_records(self.queue[:dev.MAX_RECORDS], self.imei)[0]
                    transport.sendto(dev.data_frame(self._advance_seq(), batch, self.imei))
                    # Nothing comes back, so the queue empties regardless.
                    del self.queue[:len(batch)]
                    self.sent += len(batch)
                await asyncio.sleep(60 / self.speed)
        finally:
            tick.cancel()
            transport.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Pretend to be an FE-OT100.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5600)
    ap.add_argument("--imei", default="865341041238314")
    ap.add_argument("--udp", action="store_true", help="datagrams, no acknowledgements")
    ap.add_argument("--no-ack", action="store_true", help="srv1_ack_mode 0: fire and forget")
    ap.add_argument("--drop", type=float, default=0.0, metavar="P",
                    help="lose this fraction of frames on purpose, 0..1")
    ap.add_argument("--backlog", type=int, default=0,
                    help="start with this many records already queued")
    ap.add_argument("--speed", type=float, default=60.0,
                    help="simulated minutes per real second (default 60)")
    ap.add_argument("--ack-timeout", type=float, default=30.0,
                    help="seconds to wait for an ACK before re-sending")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    sim = Simulator(a.host, a.port, a.imei, want_ack=not a.no_ack, udp=a.udp,
                    drop=a.drop, backlog=a.backlog, speed=a.speed,
                    ack_timeout=a.ack_timeout)
    try:
        asyncio.run(sim.run())
    except KeyboardInterrupt:
        log.info("sent %d, confirmed %d, still queued %d",
                 sim.sent, sim.confirmed, len(sim.queue))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
