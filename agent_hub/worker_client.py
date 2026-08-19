from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any, Awaitable, Callable


WorkerEventHandler = Callable[[str, dict[str, Any]], Awaitable[None]]
WorkerDisconnectHandler = Callable[[str], Awaitable[None]]


class WorkerClient:
    def __init__(
        self,
        worker_id: str,
        socket_path: str,
        event_handler: WorkerEventHandler,
        disconnect_handler: WorkerDisconnectHandler | None = None,
    ):
        self.worker_id = worker_id
        self.socket_path = socket_path
        self.event_handler = event_handler
        self.disconnect_handler = disconnect_handler
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.reader_task: asyncio.Task[None] | None = None
        self.pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.next_id = 1
        self.write_lock = asyncio.Lock()
        self._healthy = False
        self._connection_generation = 0
        self._ready_generations: set[int] = set()
        self._suppressed_disconnects: set[int] = set()
        self._notified_disconnects: set[int] = set()
        self._disconnect_tasks: set[asyncio.Task[None]] = set()

    @property
    def is_healthy(self) -> bool:
        return bool(
            self._healthy
            and self.writer
            and not self.writer.is_closing()
            and self.reader_task
            and not self.reader_task.done()
        )

    async def connect(self, timeout: float = 30.0) -> dict[str, Any]:
        await self.close()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        last_error: Exception | None = None
        while loop.time() < deadline:
            try:
                remaining = max(0.01, deadline - loop.time())
                reader, writer = await asyncio.wait_for(
                    asyncio.open_unix_connection(self.socket_path),
                    timeout=remaining,
                )
                self._connection_generation += 1
                generation = self._connection_generation
                self.reader = reader
                self.writer = writer
                self._healthy = True
                self.reader_task = asyncio.create_task(
                    self._read_loop(reader, writer, generation)
                )
                details = await self.request(
                    "describe",
                    {},
                    timeout=max(0.01, deadline - loop.time()),
                )
                if not self.is_healthy:
                    raise RuntimeError(
                        f"worker {self.worker_id} 在 describe 后断开"
                    )
                self._ready_generations.add(generation)
                return details
            except asyncio.CancelledError:
                await self.close()
                raise
            except Exception as error:
                last_error = error
                await self.close()
                remaining = deadline - loop.time()
                if remaining > 0:
                    await asyncio.sleep(min(0.15, remaining))
        raise RuntimeError(
            f"worker {self.worker_id} 未能启动：{last_error or 'timeout'}"
        )

    async def _read_loop(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        generation: int,
    ) -> None:
        error: Exception = RuntimeError(
            f"worker {self.worker_id} connection closed"
        )
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "id" in message:
                    future = self.pending.pop(int(message["id"]), None)
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(
                                RuntimeError(str(message["error"]))
                            )
                        else:
                            future.set_result(message.get("result") or {})
                    continue
                method = message.get("method")
                if method:
                    await self.event_handler(
                        method, message.get("params") or {}
                    )
        except asyncio.CancelledError:
            error = RuntimeError(f"worker {self.worker_id} client closed")
            raise
        except Exception as read_error:
            error = RuntimeError(
                f"worker {self.worker_id} connection failed: {read_error}"
            )
        finally:
            self._fail_pending(error)
            if not writer.is_closing():
                writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            if self.writer is writer:
                self._healthy = False
                self.reader = None
                self.writer = None
                if self.reader_task is asyncio.current_task():
                    self.reader_task = None
            intentional = generation in self._suppressed_disconnects
            self._suppressed_disconnects.discard(generation)
            if not intentional and generation in self._ready_generations:
                task = asyncio.create_task(
                    self._notify_disconnect(generation)
                )
                self._disconnect_tasks.add(task)
                task.add_done_callback(self._disconnect_tasks.discard)

    def _fail_pending(self, error: Exception) -> None:
        pending = list(self.pending.values())
        self.pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(error)

    async def _notify_disconnect(self, generation: int) -> None:
        if (
            not self.disconnect_handler
            or generation in self._notified_disconnects
        ):
            return
        self._notified_disconnects.add(generation)
        with contextlib.suppress(Exception):
            await self.disconnect_handler(self.worker_id)

    async def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        if not self.is_healthy or not self.writer:
            raise RuntimeError(f"worker {self.worker_id} 未连接")
        writer = self.writer
        request_id = self.next_id
        self.next_id += 1
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self.pending[request_id] = future
        payload = {"id": request_id, "method": method, "params": params}
        try:
            async with self.write_lock:
                if writer is not self.writer or writer.is_closing():
                    raise RuntimeError(f"worker {self.worker_id} 连接已替换")
                writer.write(
                    (json.dumps(payload, ensure_ascii=False) + "\n").encode()
                )
                await writer.drain()
        except Exception:
            if writer is self.writer and not writer.is_closing():
                self._healthy = False
                writer.close()
            raise
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            if self.pending.get(request_id) is future:
                self.pending.pop(request_id, None)

    async def close(self) -> None:
        generation = self._connection_generation
        task = self.reader_task
        writer = self.writer
        if task or writer:
            self._suppressed_disconnects.add(generation)
        self._healthy = False
        self._fail_pending(RuntimeError(f"worker {self.worker_id} client closed"))
        if writer and not writer.is_closing():
            writer.close()
        if writer:
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        current = asyncio.current_task()
        if task and task is not current and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self.reader_task is task and task is not current:
            self.reader_task = None
        if self.writer is writer:
            self.reader = None
            self.writer = None
        disconnect_tasks = [
            disconnect_task
            for disconnect_task in self._disconnect_tasks
            if disconnect_task is not current
        ]
        if disconnect_tasks:
            await asyncio.gather(
                *disconnect_tasks, return_exceptions=True
            )

    @staticmethod
    def socket_exists(path: str) -> bool:
        return Path(path).exists()
