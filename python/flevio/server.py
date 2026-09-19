"""
A working data server: TCP, UDP, acknowledgements and downlink commands.

This is the part you are meant to replace, or subclass, or read and then
write your own. It handles the transport correctly - framing, sequence
numbers, partial acknowledgements, commands and their replies - and hands
you decoded records through one small interface:

    import asyncio
    from flevio.server import Handler, Server

    class MyHandler(Handler):
        async def on_records(self, session, records):
            for r in records:
                print(session.imei, r.ts, r.event, r.lat, r.lon)
            return len(records)          # all stored

    asyncio.run(Server(MyHandler()).serve())

Everything else - a database, a queue, an HTTP API - goes in your handler.

**The one rule that matters.** :meth:`Handler.on_records` returns how many
records you have *durably stored*, counted from the first. The device marks
exactly that many as delivered and keeps the rest to send again. So:

* return ``len(records)`` only after the write has committed;
* return ``0`` when your database is down - the device will hold the data
  and retry, which is exactly what you want;
* return ``3`` when the first three went in and the fourth failed - the
  device re-sends from the fourth.

Returning ``len(records)`` before the write commits is the one way to lose
data with this protocol. The device is the only copy until you say otherwise,
and it has flash for tens of thousands of records.

**TCP and UDP.** On TCP the device opens a connection, sends HELLO once and
then DATA frames with rising sequence numbers; the connection carries the
identity, so DATA frames have no IMEI in them. On UDP there is no handshake:
every datagram carries the IMEI and nothing is acknowledged - the device
fires and forgets. Both land in the same handler; ``session.transport_name``
says which, and ``session.acked`` says whether an answer is expected.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional

from . import protocol as p
from .protocol import DataBatch, DecodeError, Frame, FrameParser, Hello, Record

__all__ = ["Handler", "Session", "Server", "LoggingHandler"]

log = logging.getLogger("flevio.server")


# --------------------------------------------------------------------------
# What a handler sees
# --------------------------------------------------------------------------


class Session:
    """One device talking to us: identity, and a way to talk back.

    A TCP session lives as long as the connection. A UDP "session" lives for
    one datagram and cannot be sent commands, because there is nothing to
    send them down - the device does not listen on UDP.
    """

    def __init__(self, transport_name: str, peer: str) -> None:
        self.transport_name = transport_name
        self.peer = peer
        self.imei: Optional[str] = None
        self.hello: Optional[Hello] = None
        self.acked = False
        """The device asked for acknowledgements (``srv1_ack_mode`` is on).

        When this is False the device does not read our answers at all, so
        whatever :meth:`Handler.on_records` returns, the records are gone
        from its queue. Store them or lose them.
        """
        self.connected_at = time.time()
        self.last_seq: Optional[int] = None
        self.records_in = 0

        self._writer: Optional[asyncio.StreamWriter] = None
        self._next_cmd_id = 1
        self._pending: Dict[int, asyncio.Future] = {}

    # --- talking to the device --------------------------------------------

    def can_send(self) -> bool:
        """False for UDP, and for a TCP session whose socket has closed."""
        return self._writer is not None and not self._writer.is_closing()

    async def send_command(self, text: str, timeout: float = 30.0) -> str:
        """Send a command and wait for the device's answer.

        The text is the SMS command language: ``GETSTATUS``, ``GETGPS``,
        ``EVENTS SERVER OFF IDLING,SPEEDING``, ``SETPARAMS 140=30;``. Put the
        configuration password in front to reach protected parameters.

        Raises :class:`ConnectionError` on UDP or a closed socket, and
        :class:`asyncio.TimeoutError` if the device does not answer in time -
        a truck in a tunnel is the usual reason, and the command is simply
        lost. Nothing on the device retries it; queue it again yourself.
        """
        if not self.can_send():
            raise ConnectionError("no open connection to %s" % (self.imei or self.peer))
        cmd_id = self._next_cmd_id
        self._next_cmd_id = self._next_cmd_id % 0xFFFF + 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[cmd_id] = fut
        try:
            self._write(p.command(cmd_id, text))
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(cmd_id, None)

    def send_time(self, unix_ms: Optional[int] = None) -> None:
        """Offer the device our clock. Harmless; ignored once it has GNSS."""
        if self.can_send():
            self._write(p.time_sync(unix_ms if unix_ms is not None else int(time.time() * 1000)))

    # --- internals ---------------------------------------------------------

    def _write(self, data: bytes) -> None:
        assert self._writer is not None
        self._writer.write(data)

    def _resolve_reply(self, cmd_id: int, text: str) -> None:
        fut = self._pending.pop(cmd_id, None)
        if fut and not fut.done():
            fut.set_result(text)
        else:
            log.info("%s: unsolicited reply #%d: %s", self.imei, cmd_id, text)

    def __repr__(self) -> str:
        return "<Session %s %s %s>" % (self.transport_name, self.imei or "?", self.peer)


# --------------------------------------------------------------------------
# The interface you implement
# --------------------------------------------------------------------------


class Handler:
    """Override what you need; every method has a safe default.

    Nothing here blocks the protocol except :meth:`on_records`, which the
    server waits for before answering - by design, so that the answer can
    tell the truth about what was stored.
    """

    async def on_hello(self, session: Session, hello: Hello) -> bool:
        """A device introduced itself. Return False to refuse it.

        Refusing sends ``ACK(0, 0)`` and closes the connection: that is how
        you keep a device you have never provisioned out of your database.
        The default accepts everyone, which is right for an example and
        wrong for production.
        """
        return True

    async def on_records(self, session: Session, records: List[Record]) -> int:
        """Store these. Return how many you stored, from the first.

        See the module docstring: this return value is the only thing
        standing between a network failure and lost data.
        """
        return len(records)

    async def on_reply(self, session: Session, cmd_id: int, text: str) -> None:
        """The answer to a command nobody was waiting for any more."""

    async def on_connect(self, session: Session) -> None:
        """A TCP connection opened, before the HELLO arrives."""

    async def on_disconnect(self, session: Session) -> None:
        """The connection ended, for whatever reason."""

    async def on_ping(self, session: Session) -> None:
        """A keepalive. The server has already answered it."""

    async def on_bad_frame(self, session: Session, error: Exception, frame: Optional[Frame]) -> None:
        """A frame passed its CRC but would not decode.

        Rare and worth an alarm: it means the two sides disagree about the
        format. The server has already declined to acknowledge it, so the
        device will send it again.
        """
        log.warning("%s: %s", session, error)


class LoggingHandler(Handler):
    """Prints what arrives. Useful on its own for a bench session."""

    def __init__(self, store=None) -> None:
        self.store = store

    async def on_hello(self, session: Session, hello: Hello) -> bool:
        log.info(
            "HELLO %s serial=%s fw=%s cfg=%d ack=%s",
            hello.imei, hello.serial, hello.fw, hello.cfg_revision, hello.want_ack,
        )
        return True

    async def on_records(self, session: Session, records: List[Record]) -> int:
        from . import catalog

        for r in records:
            where = "%.6f,%.6f" % (r.lat, r.lon) if r.has_fix else "no fix"
            log.info(
                "%s %s %-13s %s %s",
                session.imei,
                time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(r.ts)) if r.dated else "uptime %ds" % r.ts,
                catalog.event_name(r.event),
                where,
                " ".join(catalog.describe(k, v) for k, v in sorted(r.io.items())),
            )
        if self.store is not None:
            return await self.store(session, records)
        return len(records)


# --------------------------------------------------------------------------
# The server
# --------------------------------------------------------------------------


class Server:
    """Listens on TCP and UDP and drives one :class:`Handler`.

    ``sessions`` maps IMEI to the live TCP session, which is how you reach a
    device to send it a command::

        s = server.sessions.get("860000000000001")
        if s:
            print(await s.send_command("GETSTATUS"))
    """

    def __init__(
        self,
        handler: Optional[Handler] = None,
        host: str = "0.0.0.0",
        tcp_port: int = 5600,
        udp_port: Optional[int] = 5600,
        idle_timeout: float = 600.0,
    ) -> None:
        self.handler = handler or LoggingHandler()
        self.host = host
        self.tcp_port = tcp_port
        self.udp_port = udp_port
        self.idle_timeout = idle_timeout
        """Close a connection that has said nothing for this long.

        The device pings well inside its own keepalive period, so silence
        this long is a half-open socket - the far end went away without a
        FIN, which on a mobile network is the normal way for a connection to
        end. Without this the server slowly fills with dead sockets.
        """
        self.sessions: Dict[str, Session] = {}
        self._tcp: Optional[asyncio.AbstractServer] = None

    # --- lifecycle ---------------------------------------------------------

    async def serve(self) -> None:
        """Run until cancelled."""
        self._tcp = await asyncio.start_server(self._handle_tcp, self.host, self.tcp_port)
        log.info("TCP listening on %s:%d", self.host, self.tcp_port)

        udp_transport = None
        if self.udp_port:
            loop = asyncio.get_running_loop()
            udp_transport, _ = await loop.create_datagram_endpoint(
                lambda: _UdpProtocol(self), local_addr=(self.host, self.udp_port)
            )
            log.info("UDP listening on %s:%d", self.host, self.udp_port)

        try:
            async with self._tcp:
                await self._tcp.serve_forever()
        finally:
            if udp_transport is not None:
                udp_transport.close()

    # --- TCP ---------------------------------------------------------------

    async def _handle_tcp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = "%s:%d" % writer.get_extra_info("peername")[:2]
        session = Session("tcp", peer)
        session._writer = writer
        parser = FrameParser()
        log.debug("connection from %s", peer)
        await self.handler.on_connect(session)

        try:
            while True:
                try:
                    data = await asyncio.wait_for(reader.read(4096), self.idle_timeout)
                except asyncio.TimeoutError:
                    log.info("%s: idle for %ds, closing", session, int(self.idle_timeout))
                    break
                if not data:
                    break
                parser.feed(data)
                for frame in parser.frames():
                    if not await self._dispatch(session, frame):
                        return
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            if session.imei and self.sessions.get(session.imei) is session:
                del self.sessions[session.imei]
            for fut in session._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("connection closed"))
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            await self.handler.on_disconnect(session)

    async def _dispatch(self, session: Session, frame: Frame) -> bool:
        """Handle one frame. False means "close the connection"."""
        if frame.kind == p.K_HELLO:
            try:
                hello = p.decode_hello(frame.payload)
            except DecodeError as e:
                await self.handler.on_bad_frame(session, e, frame)
                return False
            session.hello = hello
            session.imei = hello.imei
            session.acked = hello.want_ack

            if not await self.handler.on_hello(session, hello):
                session._write(p.ack(0, 0))
                log.info("refused %s", hello.imei)
                return False

            # One device, one connection. A tracker that reconnects after a
            # network drop leaves the old socket half-open on our side; the
            # new HELLO is the reliable signal to drop it.
            old = self.sessions.get(hello.imei)
            if old is not None and old is not session and old.can_send():
                log.info("%s reconnected, closing the previous socket", hello.imei)
                old._writer.close()
            self.sessions[hello.imei] = session
            session._write(p.ack(0, 1))
            return True

        if frame.kind == p.K_DATA:
            try:
                batch = p.decode_data(frame.payload)
            except DecodeError as e:
                await self.handler.on_bad_frame(session, e, frame)
                return True          # no ACK: the device will send it again
            await self._take(session, batch)
            return True

        if frame.kind == p.K_PING:
            # Answered before the handler runs: a ping is a question about
            # the socket, and the honest answer costs six bytes.
            if session.acked:
                session._write(p.ack(0, 1))
            await self.handler.on_ping(session)
            return True

        if frame.kind == p.K_REPLY:
            try:
                cmd_id, text = p.decode_reply(frame.payload)
            except DecodeError as e:
                await self.handler.on_bad_frame(session, e, frame)
                return True
            if cmd_id in session._pending:
                session._resolve_reply(cmd_id, text)
            else:
                await self.handler.on_reply(session, cmd_id, text)
            return True

        log.warning("%s: unexpected frame %s", session, frame.kind_name)
        return True

    async def _take(self, session: Session, batch: DataBatch) -> None:
        if session.imei is None:
            session.imei = batch.imei
        session.last_seq = batch.seq

        try:
            accepted = await self.handler.on_records(session, batch.records)
        except Exception:
            # A handler that raised has stored nothing we can prove. Say
            # zero and let the device keep its copy.
            log.exception("%s: handler failed on seq %d", session, batch.seq)
            accepted = 0

        accepted = max(0, min(int(accepted), len(batch.records)))
        session.records_in += accepted
        if session.acked and session.can_send():
            session._write(p.ack(batch.seq, accepted))
        if accepted < len(batch.records):
            log.warning(
                "%s: stored %d of %d from seq %d",
                session.imei, accepted, len(batch.records), batch.seq,
            )


class _UdpProtocol(asyncio.DatagramProtocol):
    """UDP: one datagram, one batch, no answer.

    A device configured for UDP does not wait for anything, so there is no
    session to keep and no acknowledgement to send. Everything the device
    sends is in the datagram, IMEI included. That also means anyone can forge
    one: if you run UDP in production, check the IMEI against a device you
    know and consider the source address, or put it behind a VPN.
    """

    def __init__(self, server: Server) -> None:
        self.server = server

    def datagram_received(self, data: bytes, addr) -> None:
        asyncio.get_running_loop().create_task(self._handle(data, addr))

    async def _handle(self, data: bytes, addr) -> None:
        peer = "%s:%d" % addr[:2]
        parser = FrameParser()
        parser.feed(data)
        for frame in parser.frames():
            if frame.kind != p.K_DATA:
                log.debug("udp %s: ignoring %s", peer, frame.kind_name)
                continue
            session = Session("udp", peer)
            try:
                batch = p.decode_data(frame.payload)
            except DecodeError as e:
                await self.server.handler.on_bad_frame(session, e, frame)
                continue
            session.imei = batch.imei
            await self.server._take(session, batch)
