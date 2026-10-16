from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any


class TdlibProcessError(RuntimeError):
    pass


class TdlibError(TdlibProcessError):
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        message = str(payload.get("message") or "TDLib request failed")
        code = payload.get("code")
        super().__init__(f"TDLib error{f' {code}' if code is not None else ''}: {message}")


class TdlibClient:
    """Async JSON client for the C++ sidecar, one JSON object per line."""

    def __init__(self, binary: Path, tdjson: Path):
        self.binary = binary
        self.tdjson = tdjson
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._updates: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._send_status: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._send_status_backlog: deque[dict[str, Any]] = deque()
        self._counter = 0
        self._write_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._process is not None:
            return
        if not self.binary.is_file():
            raise TdlibProcessError(f"sidecar binary not found: {self.binary}")
        if not self.tdjson.is_file():
            raise TdlibProcessError(f"tdjson library not found: {self.tdjson}")
        self._process = await asyncio.create_subprocess_exec(
            str(self.binary),
            "--tdjson",
            str(self.tdjson),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def close(self) -> None:
        process = self._process
        if process is None:
            return
        try:
            await self._send_raw({"@type": "sidecar.shutdown"})
        except Exception:
            pass
        if self._reader_task is not None:
            try:
                await asyncio.wait_for(self._reader_task, timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._reader_task.cancel()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        self._process = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(TdlibProcessError("TDLib sidecar stopped"))
        self._pending.clear()

    async def _send_raw(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise TdlibProcessError("TDLib sidecar is not running")
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        async with self._write_lock:
            process.stdin.write(line.encode("utf-8"))
            await process.stdin.drain()

    async def send(self, payload: dict[str, Any]) -> None:
        await self._send_raw(payload)

    async def request(self, payload: dict[str, Any], timeout: float = 60) -> dict[str, Any]:
        self._counter += 1
        request_id = f"neno-{self._counter}"
        request = dict(payload)
        request["@extra"] = request_id
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future
        try:
            await self._send_raw(request)
            response = await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(request_id, None)
        if response.get("@type") == "error":
            raise TdlibError(response)
        return response

    async def wait_message_send_result(self, message_id: int, timeout: float = 20) -> dict[str, Any]:
        """Wait for TDLib's final acknowledgement for one sendMessage result.

        sendMessage returning a message object only means TDLib accepted the
        request. The update stream carries the network result, so callers can
        distinguish a confirmed send from an explicit failure without blindly
        duplicating an uncertain message.
        """
        def matches(update: dict[str, Any]) -> bool:
            update_type = str(update.get("@type") or "")
            if update_type not in {"updateMessageSendSucceeded", "updateMessageSendFailed"}:
                return False
            candidate = update.get("old_message_id")
            try:
                return int(candidate) == int(message_id)
            except (TypeError, ValueError):
                return False

        for _ in range(len(self._send_status_backlog)):
            update = self._send_status_backlog.popleft()
            if matches(update):
                return update
            self._send_status_backlog.append(update)

        while True:
            update = await asyncio.wait_for(self._send_status.get(), timeout=timeout)
            if matches(update):
                return update
            self._send_status_backlog.append(update)

    async def _read_loop(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            try:
                payload = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            request_id = payload.get("@extra")
            if request_id is not None and request_id in self._pending:
                future = self._pending[request_id]
                if not future.done():
                    future.set_result(payload)
                continue
            update_type = str(payload.get("@type") or "")
            if update_type in {
                "updateMessageSendAcknowledged",
                "updateMessageSendSucceeded",
                "updateMessageSendFailed",
            }:
                observer = getattr(self, 'send_status_observer', None)
                if observer is not None:
                    try:
                        observer(payload)
                    except Exception:
                        import logging
                        logging.exception('Could not persist Telegram send receipt')
                await self._send_status.put(payload)
            else:
                await self._updates.put(payload)
        error = TdlibProcessError("TDLib sidecar exited")
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)

    async def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        while await process.stderr.readline():
            # TDLib writes diagnostics to stderr. Keep the pipe drained but do
            # not forward protocol internals or credentials into gateway logs.
            pass

    async def updates(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            yield await self._updates.get()
