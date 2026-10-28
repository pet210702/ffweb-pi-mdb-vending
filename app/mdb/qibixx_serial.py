from __future__ import annotations

import asyncio
import logging
from typing import Any

import serial

from app.mdb.base import MdbDriver, MdbEventCallback, parse_qibixx_line

logger = logging.getLogger(__name__)


class QibixxSerialMdbDriver(MdbDriver):
    """Async serial driver for Qibixx MDB-USB Interface / Qibixx MDB Pi HAT."""

    def __init__(self, port: str = "/dev/ttyACM0", baudrate: int = 115200) -> None:
        self.port = port
        self.baudrate = baudrate
        self._serial: serial.Serial | None = None
        self._on_event: MdbEventCallback | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._running = False
        self._connected = False
        self._last_error: str | None = None
        self._last_rx: str | None = None
        self._last_tx: str | None = None
        self._write_lock = asyncio.Lock()

    async def start(self, on_event: MdbEventCallback) -> None:
        self._on_event = on_event
        self._running = True
        self._reader_task = asyncio.create_task(self._io_loop(), name="qibixx-serial-loop")

    async def stop(self) -> None:
        self._running = False
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        await asyncio.to_thread(self._close_serial)

    def _close_serial(self) -> None:
        if self._serial is not None:
            try:
                if self._serial.is_open:
                    self._serial.write(b"C,0\n")
                    self._serial.flush()
            except Exception:
                pass
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
        self._connected = False

    async def send_command(self, command: str) -> None:
        clean_cmd = command.strip()
        self._last_tx = clean_cmd
        async with self._write_lock:
            if not self._serial or not self._serial.is_open:
                logger.warning("Cannot send MDB command '%s': serial port disconnected", clean_cmd)
                return
            logger.info("MDB TX -> %s", clean_cmd)
            await asyncio.to_thread(self._blocking_write, f"{clean_cmd}\n".encode("ascii"))

    def _blocking_write(self, data: bytes) -> None:
        if self._serial and self._serial.is_open:
            self._serial.write(data)
            self._serial.flush()

    def _blocking_open(self) -> serial.Serial:
        return serial.Serial(self.port, baudrate=self.baudrate, timeout=0.25)

    def _blocking_readline(self) -> str:
        if not self._serial or not self._serial.is_open:
            return ""
        raw = self._serial.readline()
        if not raw:
            return ""
        return raw.decode("utf-8", errors="replace").strip()

    async def _io_loop(self) -> None:
        while self._running:
            try:
                if self._serial is None or not self._serial.is_open:
                    logger.info("Opening Qibixx MDB serial port %s @ %d...", self.port, self.baudrate)
                    self._serial = await asyncio.to_thread(self._blocking_open)
                    self._connected = True
                    self._last_error = None
                    # Query firmware version and enable Cashless Peripheral mode
                    await self.send_command("V")
                    await self.enable_cashless()

                line = await asyncio.to_thread(self._blocking_readline)
                if not line:
                    continue

                self._last_rx = line
                logger.info("MDB RX <- %s", line)
                event = parse_qibixx_line(line)
                if event and self._on_event:
                    await self._on_event(event)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._connected = False
                self._last_error = str(exc)
                logger.warning("Qibixx MDB serial error (%s); retrying in 3s...", exc)
                await asyncio.to_thread(self._close_serial)
                await asyncio.sleep(3.0)

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "mode": "qibixx",
            "port": self.port,
            "baudrate": self.baudrate,
            "connected": self._connected,
            "last_rx": self._last_rx,
            "last_tx": self._last_tx,
            "last_error": self._last_error,
        }
