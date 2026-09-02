"""
ESP32 connection manager supporting both Serial (USB) and TCP (WiFi) transports.
Exposes a unified async interface: connect(), disconnect(), send(), receive().
Incoming messages are dispatched to registered async callbacks.
"""
import asyncio
import logging
from abc import ABC, abstractmethod
from typing import AsyncGenerator, Callable, Awaitable, Optional
import serial.tools.list_ports
import serial_asyncio

from .protocol import decode, encode, cmd_ping, cmd_set_telemetry

log = logging.getLogger(__name__)

MessageCallback = Callable[[dict], Awaitable[None]]


class _Transport(ABC):
    @abstractmethod
    async def connect(self) -> None: ...
    @abstractmethod
    async def disconnect(self) -> None: ...
    @abstractmethod
    async def write(self, data: bytes) -> None: ...
    @abstractmethod
    async def read_lines(self) -> AsyncGenerator: ...


# ── Serial transport ──────────────────────────────────────────────────────────

class SerialTransport(_Transport):
    def __init__(self, port: str, baudrate: int = 115200):
        self.port = port
        self.baudrate = baudrate
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None

    async def connect(self) -> None:
        self._reader, self._writer = await serial_asyncio.open_serial_connection(
            url=self.port, baudrate=self.baudrate
        )
        log.info("Serial connected on %s @ %d baud", self.port, self.baudrate)

    async def disconnect(self) -> None:
        if self._writer:
            self._writer.close()
            self._writer = None

    async def write(self, data: bytes) -> None:
        if self._writer:
            self._writer.write(data)
            await self._writer.drain()

    async def read_lines(self):
        while self._reader:
            try:
                line = await self._reader.readline()
                if line:
                    yield line.decode("utf-8", errors="replace")
            except (asyncio.IncompleteReadError, ConnectionResetError):
                break


# ── TCP transport ─────────────────────────────────────────────────────────────

class TcpTransport(_Transport):
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
        log.info("TCP connected to %s:%d", self.host, self.port)

    async def disconnect(self) -> None:
        if self._writer:
            self._writer.close()
            await self._writer.wait_closed()
            self._writer = None

    async def write(self, data: bytes) -> None:
        if self._writer:
            self._writer.write(data)
            await self._writer.drain()

    async def read_lines(self):
        while self._reader:
            try:
                line = await self._reader.readline()
                if line:
                    yield line.decode("utf-8", errors="replace")
            except (asyncio.IncompleteReadError, ConnectionResetError):
                break


# ── Connection manager ────────────────────────────────────────────────────────

class ESP32Connection:
    def __init__(self, settings):
        self._settings = settings
        self._transport: Optional[_Transport] = None
        self._callbacks: list[MessageCallback] = []
        self._reader_task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None
        self.connected = False
        self.connection_type: Optional[str] = None

        # ACK/response futures keyed by command type
        self._pending: dict[str, asyncio.Future] = {}
        # MOVE_DONE futures keyed by motor id (resolved with the settled
        # output-encoder angle). Used by the closed-loop precise positioning.
        self._move_done_futures: dict[int, asyncio.Future] = {}

    def add_callback(self, cb: MessageCallback) -> None:
        self._callbacks.append(cb)

    def remove_callback(self, cb: MessageCallback) -> None:
        self._callbacks.discard(cb) if hasattr(self._callbacks, 'discard') else None
        if cb in self._callbacks:
            self._callbacks.remove(cb)

    async def connect_serial(self, port: str) -> None:
        await self._do_connect(SerialTransport(port, self._settings.serial_baudrate), "serial")

    async def connect_tcp(self, host: str) -> None:
        await self._do_connect(TcpTransport(host, self._settings.esp32_tcp_port), "tcp")

    async def _do_connect(self, transport: _Transport, conn_type: str) -> None:
        if self.connected:
            await self.disconnect()
        self._transport = transport
        await self._transport.connect()
        self.connected = True
        self.connection_type = conn_type
        self._reader_task = asyncio.create_task(self._read_loop())
        self._ping_task = asyncio.create_task(self._ping_loop())
        # Ask ESP32 to use our preferred telemetry rate
        await self.send_raw(cmd_set_telemetry(self._settings.telemetry_interval_ms))

    async def disconnect(self) -> None:
        self.connected = False
        if self._ping_task:
            self._ping_task.cancel()
        if self._reader_task:
            self._reader_task.cancel()
        if self._transport:
            await self._transport.disconnect()
        self._transport = None
        self.connection_type = None
        log.info("ESP32 disconnected")

    async def send_raw(self, data: bytes) -> None:
        if not self._transport:
            raise RuntimeError("Not connected")
        await self._transport.write(data)

    def arm_move_done(self, motor: int) -> asyncio.Future:
        """Create (or replace) a future that resolves when the next MOVE_DONE
        for `motor` arrives, carrying that motor's settled output-encoder angle
        (or None if the encoder read failed). Call this *before* sending the
        MOVE/JOG so the reply can't be missed."""
        loop = asyncio.get_event_loop()
        old = self._move_done_futures.get(motor)
        if old and not old.done():
            old.cancel()
        fut: asyncio.Future = loop.create_future()
        self._move_done_futures[motor] = fut
        return fut

    async def send_command(self, data: bytes, expect_ack_for: Optional[str] = None) -> Optional[dict]:
        """Send a command and optionally wait for an ACK response."""
        await self.send_raw(data)
        if expect_ack_for:
            loop = asyncio.get_event_loop()
            fut: asyncio.Future = loop.create_future()
            self._pending[expect_ack_for] = fut
            try:
                return await asyncio.wait_for(fut, timeout=self._settings.command_timeout_s)
            except asyncio.TimeoutError:
                self._pending.pop(expect_ack_for, None)
                log.warning("Timeout waiting for ACK on %s", expect_ack_for)
                return None
        return None

    async def _read_loop(self) -> None:
        try:
            async for line in self._transport.read_lines():
                if not line.strip():
                    continue
                try:
                    msg = decode(line)
                except Exception as e:
                    log.warning("Bad JSON from ESP32: %s — %s", line.strip(), e)
                    continue
                await self._dispatch(msg)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error("Reader loop error: %s", e)
        finally:
            if self.connected:
                self.connected = False
                await self._notify_disconnect()

    async def _dispatch(self, msg: dict) -> None:
        msg_type = msg.get("type", "")

        # Resolve pending ACK futures
        if msg_type == "ACK":
            cmd = msg.get("cmd")
            if cmd in self._pending:
                fut = self._pending.pop(cmd)
                if not fut.done():
                    fut.set_result(msg)
        elif msg_type == "PONG":
            if "PING" in self._pending:
                fut = self._pending.pop("PING")
                if not fut.done():
                    fut.set_result(msg)
        elif msg_type == "MOVE_DONE":
            # Resolve any waiter for this motor with its settled angle. Done
            # before the callback fan-out so the closed loop and the existing
            # scan-controller event both see it.
            motor = msg.get("motor")
            fut = self._move_done_futures.pop(motor, None)
            if fut and not fut.done():
                fut.set_result(msg.get("final_angle"))

        for cb in self._callbacks:
            try:
                await cb(msg)
            except Exception as e:
                log.error("Callback error: %s", e)

    async def _notify_disconnect(self) -> None:
        msg = {"type": "DISCONNECTED"}
        for cb in self._callbacks:
            try:
                await cb(msg)
            except Exception:
                pass

    async def _ping_loop(self) -> None:
        try:
            while self.connected:
                await asyncio.sleep(self._settings.ping_interval_s)
                if self.connected:
                    await self.send_raw(cmd_ping())
        except asyncio.CancelledError:
            pass


def list_serial_ports() -> list[dict]:
    return [
        {"port": p.device, "description": p.description}
        for p in serial.tools.list_ports.comports()
    ]
