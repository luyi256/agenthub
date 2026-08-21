from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from agent_hub.config import HubConfig
from agent_hub.db import HubDatabase
from agent_hub.project_tmux import WorkerLaunch
from agent_hub.runtime_manager import (
    LiveSession,
    RuntimeBusyError,
    RuntimeManager,
)


class FakeTmuxManager:
    def __init__(self, root: Path):
        self.root = root
        self.launch_count = 0
        self.launch_kwargs: list[dict[str, Any]] = []
        self.cleaned: list[WorkerLaunch] = []

    def launch_worker(self, **kwargs: Any) -> WorkerLaunch:
        self.launch_kwargs.append(dict(kwargs))
        self.launch_count += 1
        worker_id = f"wrk_new_{self.launch_count}"
        return WorkerLaunch(
            worker_id=worker_id,
            socket_path=str(self.root / f"{worker_id}.sock"),
            state_path=str(self.root / f"{worker_id}.state.json"),
            config_path=str(self.root / f"{worker_id}.config.json"),
            tmux_session="ah-unit-abcd1234",
            tmux_window=f"unit-{worker_id[-6:]}",
        )

    def cleanup_launch(self, launch: WorkerLaunch) -> None:
        self.cleaned.append(launch)


class FakeWorkerClient:
    connect_gate: asyncio.Event | None = None
    connect_error: Exception | None = None
    connect_outcomes: list[dict[str, Any] | Exception] = []
    details: dict[str, Any] = {}
    request_handler: Any = None
    requests: list[tuple[str, dict[str, Any]]] = []
    created: list["FakeWorkerClient"] = []

    def __init__(
        self,
        worker_id: str,
        socket_path: str,
        event_handler: Any,
        disconnect_handler: Any = None,
    ):
        self.worker_id = worker_id
        self.socket_path = socket_path
        self.event_handler = event_handler
        self.disconnect_handler = disconnect_handler
        self.closed = False
        self._healthy = False
        type(self).created.append(self)

    @property
    def is_healthy(self) -> bool:
        return self._healthy and not self.closed

    async def connect(self, timeout: float = 30) -> dict[str, Any]:
        del timeout
        if type(self).connect_gate:
            await type(self).connect_gate.wait()
        if type(self).connect_outcomes:
            outcome = type(self).connect_outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            self._healthy = True
            return dict(outcome)
        if type(self).connect_error:
            raise type(self).connect_error
        self._healthy = True
        return dict(type(self).details)

    async def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float = 120,
    ) -> dict[str, Any]:
        del timeout
        type(self).requests.append((method, dict(params)))
        if type(self).request_handler:
            result = type(self).request_handler(method, params)
            if asyncio.iscoroutine(result):
                return await result
            return result
        if method == "describe":
            return dict(type(self).details)
        return {}

    async def close(self) -> None:
        self.closed = True
        self._healthy = False

    @staticmethod
    def socket_exists(path: str) -> bool:
        return Path(path).exists()


class FakeGenTmux:
    def __init__(self) -> None:
        self.send_count = 0
        self.sent_texts: list[str] = []
        self.window = {
            "window_id": "@9",
            "runtime": "tcodex",
            "runtime_id": "thread-1",
            "state": "idle",
            "rollout_path": None,
        }

    def get_window(self, window_id: str) -> dict[str, Any]:
        assert window_id == "@9"
        return dict(self.window)

    def send_text(
        self,
        window_id: str,
        text: str,
        *,
        allow_busy: bool = False,
        verify_submission: bool = True,
    ) -> dict[str, Any]:
        del verify_submission
        assert window_id == "@9"
        self.send_count += 1
        self.sent_texts.append(text)
        return {
            **self.window,
            "submitted": True,
            "delivery": (
                "queued"
                if allow_busy or self.window["state"] == "busy"
                else "turn"
            ),
        }


class RuntimeManagerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = HubConfig(
            db_path=self.root / "agenthub.sqlite3",
            tmux_socket_name="ah-e2e-unit-runtime",
        )
        self.db = HubDatabase(self.config.db_path)
        self.broadcasts: list[dict[str, Any]] = []

        async def broadcast(event: dict[str, Any]) -> None:
            self.broadcasts.append(event)

        self.manager = RuntimeManager(self.config, self.db, broadcast)
        self.fake_tmux = FakeTmuxManager(self.root)
        self.manager.tmux = self.fake_tmux  # type: ignore[assignment]
        FakeWorkerClient.connect_gate = None
        FakeWorkerClient.connect_error = None
        FakeWorkerClient.connect_outcomes = []
        FakeWorkerClient.request_handler = None
        FakeWorkerClient.requests = []
        FakeWorkerClient.details = {
            "runtime_id": "thread-1",
            "runtime_version": "1.0",
            "status": "idle",
            "pid": 12,
            "supports_approvals": True,
            "pending_approvals": {},
        }
        FakeWorkerClient.created = []
        self.worker_patch = patch(
            "agent_hub.runtime_manager.WorkerClient",
            FakeWorkerClient,
        )
        self.worker_patch.start()

    async def asyncTearDown(self) -> None:
        self.worker_patch.stop()
        self.temporary.cleanup()

    def _register_session(
        self,
        *,
        worker_id: str = "wrk_old",
        socket_path: str | None = None,
    ) -> dict[str, Any]:
        return self.db.register_managed_session(
            runtime="tcodex",
            runtime_id="thread-1",
            runtime_version="1.0",
            native_name="unit-session",
            alias=None,
            user_title=None,
            role=None,
            cwd=str(self.root),
            transport="tmux-worker",
            managed_config={
                "permission_profile": "safe",
                "worker_id": worker_id,
                "socket_path": socket_path
                or str(self.root / f"{worker_id}.sock"),
                "state_path": str(self.root / f"{worker_id}.state.json"),
                "config_path": str(self.root / f"{worker_id}.config.json"),
                "tmux_session": "ah-unit-abcd1234",
                "tmux_window": f"unit-{worker_id[-6:]}",
            },
            capabilities={"chat": True, "tmux": True},
        )

    async def test_create_session_rejects_use_tmux_false(self) -> None:
        with self.assertRaisesRegex(ValueError, "use_tmux 必须为 true"):
            await self.manager.create_session(
                runtime="tcodex",
                cwd=str(self.root),
                alias="unit-direct",
                title=None,
                role=None,
                permission_profile="safe",
                use_tmux=False,
            )

        self.assertEqual(self.fake_tmux.launch_count, 0)
        self.assertEqual(self.db.list_sessions(), [])
        self.assertEqual(self.manager.codex_adapters, {})
        self.assertEqual(self.manager.claude_sessions, {})

    async def test_create_session_defaults_to_tmux_worker(self) -> None:
        session = await self.manager.create_session(
            runtime="tcodex",
            cwd=str(self.root),
            alias="unit-default",
            title=None,
            role=None,
            permission_profile="safe",
        )

        self.assertEqual(session["transport"], "tmux-worker")
        self.assertEqual(self.fake_tmux.launch_count, 1)
        self.assertEqual(self.manager.codex_adapters, {})
        self.assertEqual(self.manager.claude_sessions, {})

    async def test_create_session_forwards_model_to_tmux_worker(self) -> None:
        session = await self.manager.create_session(
            runtime="tcodex",
            cwd=str(self.root),
            alias="unit-model",
            title=None,
            role=None,
            permission_profile="safe",
            model="gpt-5.6-luna",
            reasoning_effort="high",
        )

        launch = self.fake_tmux.launch_kwargs[0]
        self.assertEqual(launch["model"], "gpt-5.6-luna")
        self.assertEqual(launch["reasoning_effort"], "high")
        self.assertEqual(
            session["managed_config"]["model"], "gpt-5.6-luna"
        )
        self.assertEqual(
            session["managed_config"]["reasoning_effort"], "high"
        )

    async def test_ensure_live_migrates_historical_app_server_session(
        self,
    ) -> None:
        historical = self.db.register_managed_session(
            runtime="tcodex",
            runtime_id="thread-1",
            runtime_version="0.9",
            native_name="historical-session",
            alias="historical/app-server",
            user_title="Historical session",
            role="prompt",
            cwd=str(self.root),
            transport="app-server",
            managed_config={
                "permission_profile": "safe",
                "ephemeral": False,
            },
            capabilities={"chat": True, "approvals": True},
            pid=None,
        )
        message = self.db.add_message(
            historical["session_uid"],
            "human",
            "保留这条历史消息",
        )

        live = await self.manager.ensure_live(historical["session_uid"])

        self.assertEqual(live.transport, "tmux-worker")
        self.assertEqual(live.session_uid, historical["session_uid"])
        self.assertEqual(live.runtime_id, historical["runtime_id"])
        self.assertEqual(self.manager.codex_adapters, {})
        self.assertEqual(self.manager.claude_sessions, {})
        self.assertEqual(self.fake_tmux.launch_count, 1)
        self.assertEqual(
            self.fake_tmux.launch_kwargs[0]["resume_runtime_id"],
            historical["runtime_id"],
        )

        migrated = self.db.get_session(historical["session_uid"])
        assert migrated
        self.assertEqual(migrated["session_uid"], historical["session_uid"])
        self.assertEqual(migrated["runtime_id"], historical["runtime_id"])
        self.assertEqual(migrated["transport"], "tmux-worker")
        self.assertEqual(
            migrated["managed_config"]["worker_id"],
            "wrk_new_1",
        )
        self.assertTrue(migrated["capabilities"]["tmux"])
        self.assertEqual(
            self.db.get_message(message["message_id"])["content"],
            "保留这条历史消息",
        )

    async def test_gen_tmux_relay_sends_to_existing_tui(
        self,
    ) -> None:
        gen_tmux = FakeGenTmux()
        self.manager.gen_tmux = gen_tmux  # type: ignore[assignment]
        session = self.db.register_managed_session(
            runtime="tcodex",
            runtime_id="thread-1",
            runtime_version="1.0",
            native_name="gen-import",
            alias="gen/9",
            user_title="Imported gen window",
            role="imported-tmux-gen",
            cwd=str(self.root),
            transport="gen-tmux-relay",
            managed_config={
                "permission_profile": "safe",
                "workspace_id": "abcd123456",
                "workspace_name": "unit",
                "source_tmux_window_id": "@9",
            },
            capabilities={"chat": True, "tmux": True},
        )

        history_calls = 0

        async def fake_sync(
            stored_session: dict[str, Any],
            *,
            rollout_path: str | None = None,
            relay_id: str | None = None,
            baseline: set[str] | None = None,
        ) -> list[dict[str, Any]]:
            del relay_id, baseline
            nonlocal history_calls
            history_calls += 1
            if history_calls < 2:
                return []
            messages = self.db.list_messages(stored_session["session_uid"])
            assistant = next(
                item
                for item in reversed(messages)
                if item["role"] == "assistant"
            )
            self.db.sync_message(
                assistant["message_id"],
                content="原 TUI 回复",
                status="completed",
                metadata_patch={"native_message_id": "hist_reply"},
            )
            return [
                {
                    "message_id": "hist_reply",
                    "role": "assistant",
                    "content": "原 TUI 回复",
                    "status": "completed",
                    "metadata": {},
                }
            ]

        self.manager.sync_gen_relay_history = fake_sync  # type: ignore[method-assign]
        assistant = await self.manager.send_message(
            session["session_uid"], "继续处理"
        )
        task = self.manager._gen_reply_tasks[session["session_uid"]]
        await asyncio.wait_for(task, timeout=2)

        self.assertEqual(gen_tmux.send_count, 1)
        self.assertEqual(gen_tmux.sent_texts, ["继续处理"])
        self.assertEqual(self.fake_tmux.launch_count, 0)
        stored = self.db.get_message(assistant["message_id"])
        assert stored
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored["content"], "原 TUI 回复")

    async def test_gen_relay_does_not_match_reply_to_old_failed_placeholder(
        self,
    ) -> None:
        gen_tmux = FakeGenTmux()
        self.manager.gen_tmux = gen_tmux  # type: ignore[assignment]
        session = self.db.register_managed_session(
            runtime="tcodex",
            runtime_id="thread-1",
            runtime_version="1.0",
            native_name="gen-import",
            alias="gen/old-failure",
            user_title="Imported gen window",
            role="imported-tmux-gen",
            cwd=str(self.root),
            transport="gen-tmux-relay",
            managed_config={"source_tmux_window_id": "@9"},
            capabilities={"chat": True},
        )
        self.db.add_message(
            session["session_uid"],
            "assistant",
            "旧超时",
            status="failed",
            metadata={"relay": "tmux-gen", "relay_id": "old"},
        )
        current = self.db.add_message(
            session["session_uid"],
            "assistant",
            "",
            status="streaming",
            metadata={"relay": "tmux-gen", "relay_id": "current"},
        )
        native = {
            "message_id": "hist_new_reply",
            "role": "assistant",
            "content": "新回答",
            "status": "completed",
            "created_at": "2026-08-20T00:00:00+00:00",
            "updated_at": "2026-08-20T00:00:00+00:00",
            "metadata": {},
        }

        with patch(
            "agent_hub.runtime_manager.load_runtime_history",
            return_value=[native],
        ):
            await self.manager.sync_gen_relay_history(
                session,
                relay_id="current",
                baseline=set(),
            )

        messages = self.db.list_messages(session["session_uid"])
        old = next(
            item
            for item in messages
            if (item.get("metadata") or {}).get("relay_id") == "old"
        )
        matched = self.db.get_message(current["message_id"])
        assert matched
        self.assertEqual(old["status"], "failed")
        self.assertEqual(old["content"], "旧超时")
        self.assertEqual(matched["status"], "completed")
        self.assertEqual(matched["content"], "新回答")

    async def test_busy_gen_tmux_session_rejects_send(self) -> None:
        gen_tmux = FakeGenTmux()
        gen_tmux.window["state"] = "busy"
        self.manager.gen_tmux = gen_tmux  # type: ignore[assignment]
        session = self.db.register_managed_session(
            runtime="tcodex",
            runtime_id="thread-1",
            runtime_version="1.0",
            native_name="gen-import",
            alias="gen/8",
            user_title="Imported gen window",
            role="imported-tmux-gen",
            cwd=str(self.root),
            transport="gen-tmux-relay",
            managed_config={
                "permission_profile": "safe",
                "source_tmux_window_id": "@9",
            },
            capabilities={"chat": True},
        )

        result = await self.manager.send_message(
            session["session_uid"], "运行中补充"
        )

        self.assertEqual(result["delivery"], "runtime_queued")
        self.assertEqual(gen_tmux.send_count, 1)
        self.assertEqual(gen_tmux.sent_texts, ["运行中补充"])
        self.assertEqual(self.fake_tmux.launch_count, 0)

    async def test_blocked_gen_tmux_session_still_rejects_send(self) -> None:
        gen_tmux = FakeGenTmux()
        gen_tmux.window["state"] = "blocked"
        self.manager.gen_tmux = gen_tmux  # type: ignore[assignment]
        session = self.db.register_managed_session(
            runtime="tcodex",
            runtime_id="thread-1",
            runtime_version="1.0",
            native_name="gen-import",
            alias="gen/blocked",
            user_title="Blocked gen window",
            role="imported-tmux-gen",
            cwd=str(self.root),
            transport="gen-tmux-relay",
            managed_config={"source_tmux_window_id": "@9"},
            capabilities={"chat": True},
        )

        with self.assertRaisesRegex(RuntimeBusyError, "等待交互"):
            await self.manager.send_message(
                session["session_uid"],
                "不应进入确认界面",
            )

        self.assertEqual(gen_tmux.send_count, 0)

    async def test_concurrent_gen_relay_send_is_singleflight(self) -> None:
        gen_tmux = FakeGenTmux()
        self.manager.gen_tmux = gen_tmux  # type: ignore[assignment]
        session = self.db.register_managed_session(
            runtime="tcodex",
            runtime_id="thread-1",
            runtime_version="1.0",
            native_name="gen-import",
            alias="gen/7",
            user_title="Imported gen window",
            role="imported-tmux-gen",
            cwd=str(self.root),
            transport="gen-tmux-relay",
            managed_config={
                "permission_profile": "safe",
                "source_tmux_window_id": "@9",
            },
            capabilities={"chat": True},
        )
        entered = asyncio.Event()
        release = asyncio.Event()
        sync_calls = 0

        async def fake_sync(
            stored_session: dict[str, Any],
            *,
            rollout_path: str | None = None,
            relay_id: str | None = None,
            baseline: set[str] | None = None,
        ) -> list[dict[str, Any]]:
            del stored_session, rollout_path, relay_id, baseline
            nonlocal sync_calls
            sync_calls += 1
            if sync_calls == 1:
                entered.set()
                await release.wait()
            return []

        self.manager.sync_gen_relay_history = fake_sync  # type: ignore[method-assign]
        first = asyncio.create_task(
            self.manager.send_message(session["session_uid"], "第一条")
        )
        await entered.wait()
        second = asyncio.create_task(
            self.manager.send_message(session["session_uid"], "第二条")
        )
        await asyncio.sleep(0)
        self.assertFalse(second.done())

        release.set()
        await first
        second_result = await second

        self.assertEqual(second_result["delivery"], "runtime_queued")
        self.assertEqual(gen_tmux.sent_texts, ["第一条", "第二条"])
        monitor = self.manager._gen_reply_tasks.pop(
            session["session_uid"]
        )
        monitor.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await monitor

    async def test_active_tcodex_worker_uses_native_steer(self) -> None:
        session = self._register_session()
        client = FakeWorkerClient(
            "wrk_active",
            str(self.root / "active.sock"),
            lambda method, params: asyncio.sleep(0),
        )
        client._healthy = True
        live = LiveSession(
            session_uid=session["session_uid"],
            runtime="tcodex",
            runtime_id="thread-1",
            transport="tmux-worker",
            active_message_id="msg_active",
            active_turn_id="turn-1",
            worker_id="wrk_active",
        )
        self.manager.sessions[session["session_uid"]] = live
        self.manager.worker_clients[session["session_uid"]] = client
        self.manager.worker_to_uid["wrk_active"] = session["session_uid"]
        self.db.add_message(
            session["session_uid"],
            "assistant",
            status="streaming",
            message_id="msg_active",
        )
        FakeWorkerClient.request_handler = (
            lambda method, params: {
                "accepted": True,
                "delivery": "steer",
                "turn_id": "turn-1",
            }
            if method == "steer"
            else {}
        )

        result = await self.manager.send_message(
            session["session_uid"],
            "补充约束",
        )

        self.assertEqual(result["delivery"], "steered")
        self.assertIn(
            ("steer", {"text": "补充约束"}),
            FakeWorkerClient.requests,
        )
        human = self.db.list_messages(session["session_uid"])[-1]
        self.assertEqual(human["role"], "human")
        self.assertEqual(human["metadata"]["delivery"], "steer")

    async def test_active_claude_worker_queues_durable_next_turn(self) -> None:
        session = self.db.register_managed_session(
            runtime="tclaude",
            runtime_id="claude-1",
            runtime_version="1.0",
            native_name="claude-unit",
            alias="claude/unit",
            user_title=None,
            role=None,
            cwd=str(self.root),
            transport="tmux-worker",
            managed_config={"permission_profile": "safe"},
            capabilities={"chat": True},
        )
        live = LiveSession(
            session_uid=session["session_uid"],
            runtime="tclaude",
            runtime_id="claude-1",
            transport="tmux-worker",
            active_message_id="msg_active",
            worker_id="wrk_claude",
        )
        client = FakeWorkerClient(
            "wrk_claude",
            str(self.root / "claude.sock"),
            lambda method, params: asyncio.sleep(0),
        )
        client._healthy = True
        self.manager.sessions[session["session_uid"]] = live
        self.manager.worker_clients[session["session_uid"]] = client
        self.manager.worker_to_uid["wrk_claude"] = session["session_uid"]
        self.db.add_message(
            session["session_uid"],
            "assistant",
            status="streaming",
            message_id="msg_active",
        )

        result = await self.manager.send_message(
            session["session_uid"],
            "下一轮继续检查",
        )

        self.assertEqual(result["delivery"], "hub_queued")
        queued = self.db.get_message(result["message_id"])
        assert queued
        self.assertEqual(queued["status"], "queued")
        self.assertTrue(queued["metadata"]["queued_followup"])
        self.assertFalse(
            any(method == "steer" for method, _ in FakeWorkerClient.requests)
        )

    async def test_queued_followup_starts_after_active_turn_completes(
        self,
    ) -> None:
        session = self._register_session()
        client = FakeWorkerClient(
            "wrk_queue",
            str(self.root / "queue.sock"),
            lambda method, params: asyncio.sleep(0),
        )
        client._healthy = True
        live = LiveSession(
            session_uid=session["session_uid"],
            runtime="tcodex",
            runtime_id="thread-1",
            transport="tmux-worker",
            worker_id="wrk_queue",
        )
        self.manager.sessions[session["session_uid"]] = live
        self.manager.worker_clients[session["session_uid"]] = client
        self.manager.worker_to_uid["wrk_queue"] = session["session_uid"]
        queued = self.db.add_message(
            session["session_uid"],
            "human",
            "排队任务",
            status="queued",
            metadata={
                "delivery": "queued",
                "queued_followup": True,
            },
        )
        FakeWorkerClient.request_handler = (
            lambda method, params: {
                "accepted": True,
                "turn": {"id": "turn-next"},
            }
            if method == "send"
            else {}
        )

        await self.manager._drain_queued_followups(
            session["session_uid"]
        )

        stored = self.db.get_message(queued["message_id"])
        assert stored
        self.assertEqual(stored["status"], "completed")
        self.assertFalse(stored["metadata"]["queued_followup"])
        self.assertEqual(live.active_turn_id, "turn-next")
        self.assertIn(
            (
                "send",
                {
                    "text": "排队任务",
                    "message_id": live.active_message_id,
                },
            ),
            FakeWorkerClient.requests,
        )
        assistants = [
            item
            for item in self.db.list_messages(session["session_uid"])
            if item["role"] == "assistant"
        ]
        self.assertEqual(len(assistants), 1)
        self.assertEqual(assistants[0]["status"], "streaming")

    async def test_close_managed_session_stops_worker_and_cleans_window(
        self,
    ) -> None:
        session = self._register_session()
        client = FakeWorkerClient(
            "wrk_old",
            session["managed_config"]["socket_path"],
            lambda method, params: asyncio.sleep(0),
        )
        client._healthy = True
        live = LiveSession(
            session_uid=session["session_uid"],
            runtime="tcodex",
            runtime_id="thread-1",
            transport="tmux-worker",
            worker_id="wrk_old",
        )
        self.manager.sessions[session["session_uid"]] = live
        self.manager.worker_clients[session["session_uid"]] = client
        self.manager.worker_to_uid["wrk_old"] = session["session_uid"]
        launch = self.manager._worker_launch_from_config(
            session["managed_config"]
        )
        assert launch
        self.manager._worker_launches["wrk_old"] = launch
        with patch.object(
            self.manager,
            "_cleanup_worker_resources",
            wraps=self.manager._cleanup_worker_resources,
        ) as cleanup:
            closed = await self.manager.close_session(
                session["session_uid"]
            )

        self.assertEqual(closed["status"], "closed")
        self.assertIn(("stop", {}), FakeWorkerClient.requests)
        self.assertNotIn(session["session_uid"], self.manager.sessions)
        cleanup.assert_awaited_once()

    async def test_per_session_singleflight_launches_once(self) -> None:
        session = self._register_session()
        gate = asyncio.Event()
        FakeWorkerClient.connect_gate = gate

        tasks = [
            asyncio.create_task(
                self.manager.ensure_live(session["session_uid"])
            )
            for _ in range(4)
        ]
        for _ in range(50):
            if self.fake_tmux.launch_count:
                break
            await asyncio.sleep(0)
        self.assertEqual(self.fake_tmux.launch_count, 1)
        gate.set()
        lives = await asyncio.gather(*tasks)

        self.assertTrue(all(live is lives[0] for live in lives))
        self.assertEqual(self.fake_tmux.launch_count, 1)
        self.assertEqual(len(FakeWorkerClient.created), 1)

    async def test_missing_socket_cleans_old_owned_worker_before_launch(
        self,
    ) -> None:
        session = self._register_session()

        live = await self.manager.ensure_live(session["session_uid"])

        self.assertEqual(live.worker_id, "wrk_new_1")
        self.assertEqual(
            [launch.worker_id for launch in self.fake_tmux.cleaned],
            ["wrk_old"],
        )

    async def test_failed_new_launch_is_cleaned_and_not_bound(self) -> None:
        session = self._register_session()
        FakeWorkerClient.connect_error = RuntimeError("connect failed")

        with self.assertRaisesRegex(RuntimeError, "connect failed"):
            await self.manager.ensure_live(session["session_uid"])

        self.assertEqual(
            [launch.worker_id for launch in self.fake_tmux.cleaned],
            ["wrk_old", "wrk_new_1"],
        )
        self.assertNotIn(session["session_uid"], self.manager.sessions)
        self.assertNotIn(
            session["session_uid"], self.manager.worker_clients
        )
        self.assertEqual(self.manager.worker_to_uid, {})
        self.assertEqual(self.manager._worker_event_buffers, {})

    async def test_stale_existing_socket_falls_back_to_one_clean_relaunch(
        self,
    ) -> None:
        socket_path = self.root / "wrk_old.sock"
        socket_path.touch()
        session = self._register_session(socket_path=str(socket_path))
        FakeWorkerClient.connect_outcomes = [
            RuntimeError("stale socket"),
            dict(FakeWorkerClient.details),
        ]

        live = await self.manager.ensure_live(session["session_uid"])

        self.assertEqual(live.worker_id, "wrk_new_1")
        self.assertEqual(self.fake_tmux.launch_count, 1)
        self.assertEqual(
            [launch.worker_id for launch in self.fake_tmux.cleaned],
            ["wrk_old"],
        )
        self.assertEqual(len(FakeWorkerClient.created), 2)
        self.assertTrue(FakeWorkerClient.created[0].closed)

    async def test_restore_active_message_and_flush_buffered_completion(
        self,
    ) -> None:
        session = self._register_session()
        message = self.db.add_message(
            session["session_uid"],
            "assistant",
            status="streaming",
        )
        FakeWorkerClient.details.update(
            {
                "active_message_id": message["message_id"],
                "active_text": "partial",
                "status": "running",
            }
        )
        original_wait = self.manager._wait_for_worker_runtime

        async def wait_with_completion(
            client: FakeWorkerClient,
            details: dict[str, Any],
            *,
            timeout: float = 100,
        ) -> dict[str, Any]:
            ready = await original_wait(
                client, details, timeout=timeout
            )
            await client.event_handler(
                "turn.completed",
                {
                    "message_id": message["message_id"],
                    "text": "done",
                    "status": "completed",
                },
            )
            return ready

        self.manager._wait_for_worker_runtime = wait_with_completion  # type: ignore[method-assign]

        live = await self.manager.ensure_live(session["session_uid"])

        self.assertIsNone(live.active_message_id)
        stored = self.db.get_message(message["message_id"])
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored["content"], "partial")

    async def test_disconnect_invalidates_only_matching_worker(self) -> None:
        session = self._register_session()
        live = await self.manager.ensure_live(session["session_uid"])
        worker_id = live.worker_id
        assert worker_id

        await self.manager.handle_worker_disconnect(worker_id)

        self.assertNotIn(session["session_uid"], self.manager.sessions)
        self.assertNotIn(
            session["session_uid"], self.manager.worker_clients
        )
        stored = self.db.get_session(session["session_uid"])
        self.assertEqual(stored["presence"], "offline")
        self.assertEqual(stored["status"], "stopped")

        replacement = FakeWorkerClient(
            "wrk_replacement",
            str(self.root / "replacement.sock"),
            lambda method, params: asyncio.sleep(0),
        )
        replacement._healthy = True
        replacement_live = LiveSession(
            session_uid=session["session_uid"],
            runtime="tcodex",
            runtime_id="thread-1",
            transport="tmux-worker",
            worker_id="wrk_replacement",
        )
        self.manager.sessions[session["session_uid"]] = replacement_live
        self.manager.worker_clients[session["session_uid"]] = replacement
        self.manager.worker_to_uid["wrk_replacement"] = session["session_uid"]
        self.manager.worker_to_uid[worker_id] = session["session_uid"]

        await self.manager.handle_worker_disconnect(worker_id)

        self.assertIs(
            self.manager.sessions[session["session_uid"]],
            replacement_live,
        )
        self.assertIs(
            self.manager.worker_clients[session["session_uid"]],
            replacement,
        )


if __name__ == "__main__":
    unittest.main()
