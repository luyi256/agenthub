from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_hub.worker_client import WorkerClient


class WorkerClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.socket_path = str(Path(self.temporary.name) / "worker.sock")
        self.servers: list[asyncio.AbstractServer] = []
        self.clients: list[WorkerClient] = []
        self.server_tasks: set[asyncio.Task[Any]] = set()

    async def asyncTearDown(self) -> None:
        for client in self.clients:
            await client.close()
        for server in self.servers:
            server.close()
            await server.wait_closed()
        if self.server_tasks:
            await asyncio.gather(
                *self.server_tasks, return_exceptions=True
            )
        self.temporary.cleanup()

    async def _start_server(self, handler: Any) -> None:
        async def tracked_handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            task = asyncio.current_task()
            if task:
                self.server_tasks.add(task)
            try:
                await handler(reader, writer)
            finally:
                if not writer.is_closing():
                    writer.close()
                await writer.wait_closed()
                if task:
                    self.server_tasks.discard(task)

        server = await asyncio.start_unix_server(
            tracked_handler, path=self.socket_path
        )
        self.servers.append(server)

    def _client(
        self,
        *,
        events: list[tuple[str, dict[str, Any]]] | None = None,
        disconnects: list[str] | None = None,
    ) -> WorkerClient:
        async def event_handler(
            method: str, params: dict[str, Any]
        ) -> None:
            if events is not None:
                events.append((method, params))

        async def disconnect_handler(worker_id: str) -> None:
            if disconnects is not None:
                disconnects.append(worker_id)

        client = WorkerClient(
            "worker-1",
            self.socket_path,
            event_handler,
            disconnect_handler if disconnects is not None else None,
        )
        self.clients.append(client)
        return client

    async def test_connect_health_events_and_disconnect_callback(self) -> None:
        release = asyncio.Event()

        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            request = json.loads(await reader.readline())
            writer.write(
                (
                    json.dumps(
                        {"id": request["id"], "result": {"status": "idle"}}
                    )
                    + "\n"
                    + json.dumps(
                        {"method": "runtime.ready", "params": {"pid": 7}}
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
            await release.wait()
            writer.close()
            await writer.wait_closed()

        await self._start_server(handler)
        events: list[tuple[str, dict[str, Any]]] = []
        disconnects: list[str] = []
        client = self._client(events=events, disconnects=disconnects)

        self.assertEqual(
            await client.connect(timeout=1), {"status": "idle"}
        )
        self.assertTrue(client.is_healthy)
        await asyncio.sleep(0)
        self.assertEqual(events, [("runtime.ready", {"pid": 7})])

        release.set()
        for _ in range(20):
            if disconnects:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(disconnects, ["worker-1"])
        self.assertFalse(client.is_healthy)

    async def test_request_timeout_removes_pending_without_disconnect(self) -> None:
        release = asyncio.Event()

        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            describe = json.loads(await reader.readline())
            writer.write(
                (
                    json.dumps({"id": describe["id"], "result": {"ok": True}})
                    + "\n"
                ).encode()
            )
            await writer.drain()
            await reader.readline()
            await release.wait()
            writer.close()
            await writer.wait_closed()

        await self._start_server(handler)
        disconnects: list[str] = []
        client = self._client(disconnects=disconnects)
        await client.connect(timeout=1)

        with self.assertRaises(asyncio.TimeoutError):
            await client.request("slow", {}, timeout=0.02)
        self.assertEqual(client.pending, {})
        self.assertTrue(client.is_healthy)
        self.assertEqual(disconnects, [])
        release.set()

    async def test_connect_describe_timeout_cleans_transport(self) -> None:
        connections: list[asyncio.StreamWriter] = []

        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            connections.append(writer)
            await reader.readline()
            await reader.read()

        await self._start_server(handler)
        disconnects: list[str] = []
        client = self._client(disconnects=disconnects)

        with self.assertRaises(RuntimeError):
            await client.connect(timeout=0.05)
        self.assertFalse(client.is_healthy)
        self.assertIsNone(client.reader)
        self.assertIsNone(client.writer)
        self.assertIsNone(client.reader_task)
        self.assertEqual(client.pending, {})
        self.assertEqual(disconnects, [])
        self.assertTrue(connections)

    async def test_reconnect_closes_old_connection_without_callback(self) -> None:
        first_closed = asyncio.Event()
        connection_count = 0

        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            nonlocal connection_count
            connection_count += 1
            index = connection_count
            describe = json.loads(await reader.readline())
            writer.write(
                (
                    json.dumps(
                        {"id": describe["id"], "result": {"connection": index}}
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
            await reader.read()
            if index == 1:
                first_closed.set()

        await self._start_server(handler)
        disconnects: list[str] = []
        client = self._client(disconnects=disconnects)

        self.assertEqual(
            await client.connect(timeout=1), {"connection": 1}
        )
        self.assertEqual(
            await client.connect(timeout=1), {"connection": 2}
        )
        await asyncio.wait_for(first_closed.wait(), timeout=1)
        self.assertTrue(client.is_healthy)
        self.assertEqual(disconnects, [])


if __name__ == "__main__":
    unittest.main()
