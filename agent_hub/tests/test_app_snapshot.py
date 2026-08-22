from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from starlette.requests import Request

from agent_hub.app import (
    HubService,
    _activity_summary,
    api_activity_detail,
    api_create_managed_session,
    api_search_messages,
)
from agent_hub.config import HubConfig


class AppSnapshotTests(unittest.TestCase):
    def test_managed_chat_sessions_count_as_messageable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = HubService(
                HubConfig(
                    db_path=Path(tmp) / "hub.sqlite3",
                    tmux_socket_name="ah-unit-test",
                )
            )
            service.db.register_managed_session(
                runtime="tcodex",
                runtime_id="thread-1",
                runtime_version="1.0.0",
                native_name="unit-test",
                alias="unit/test",
                user_title="Unit test",
                role=None,
                cwd=tmp,
                transport="tmux-worker",
                managed_config={"permission_profile": "safe"},
                capabilities={"chat": True, "tmux": True},
            )
            snapshot = service.snapshot()
            self.assertEqual(snapshot["counts"]["messageable"], 1)


class FakeRuntimeManager:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create_session(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "session_uid": "ses_unit",
            "transport": "tmux-worker",
        }


class ManagedSessionApiTmuxPolicyTests(
    unittest.IsolatedAsyncioTestCase
):
    def _request(
        self,
        body: dict[str, Any],
        manager: FakeRuntimeManager,
    ) -> Request:
        payload = json.dumps(body).encode()
        delivered = False

        async def receive() -> dict[str, Any]:
            nonlocal delivered
            if delivered:
                return {
                    "type": "http.request",
                    "body": b"",
                    "more_body": False,
                }
            delivered = True
            return {
                "type": "http.request",
                "body": payload,
                "more_body": False,
            }

        app = SimpleNamespace(
            state=SimpleNamespace(
                hub=SimpleNamespace(runtime_manager=manager)
            )
        )
        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/managed-sessions",
                "headers": [(b"content-type", b"application/json")],
                "app": app,
            },
            receive,
        )

    async def test_api_rejects_use_tmux_false_without_calling_manager(
        self,
    ) -> None:
        manager = FakeRuntimeManager()
        response = await api_create_managed_session(
            self._request(
                {
                    "runtime": "tcodex",
                    "cwd": "/tmp",
                    "use_tmux": False,
                },
                manager,
            )
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("use_tmux 必须为 true", response.body.decode())
        self.assertEqual(manager.calls, [])

    async def test_api_defaults_use_tmux_to_true(self) -> None:
        manager = FakeRuntimeManager()
        response = await api_create_managed_session(
            self._request(
                {
                    "runtime": "tcodex",
                    "cwd": "/tmp",
                },
                manager,
            )
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(manager.calls), 1)
        self.assertIs(manager.calls[0]["use_tmux"], True)

    async def test_api_forwards_optional_model_selection(self) -> None:
        manager = FakeRuntimeManager()
        response = await api_create_managed_session(
            self._request(
                {
                    "runtime": "tcodex",
                    "cwd": "/tmp",
                    "model": "gpt-5.6-luna",
                    "reasoning_effort": "high",
                },
                manager,
            )
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(manager.calls[0]["model"], "gpt-5.6-luna")
        self.assertEqual(manager.calls[0]["reasoning_effort"], "high")


class ActivityApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_summary_hides_details_but_keeps_first_line(self) -> None:
        summary = _activity_summary(
            {
                "activity_id": "act-1",
                "kind": "tool",
                "name": "exec_command",
                "status": "completed",
                "input": {"cmd": "printf ok"},
                "result": "Chunk ID: abc\nExit code: 0\nOutput:\nok",
                "created_at": "2026-08-18T01:00:00Z",
            }
        )
        self.assertIsNone(summary["input"])
        self.assertIsNone(summary["result"])
        self.assertEqual(summary["result_preview"], "Chunk ID: abc")
        self.assertTrue(summary["has_details"])

    async def test_activity_detail_returns_full_tool_payload(self) -> None:
        session = {
            "session_uid": "ses-1",
            "runtime": "tcodex",
            "runtime_id": "thread-1",
            "managed_config": {},
        }
        hub = SimpleNamespace(
            db=SimpleNamespace(
                get_session=lambda uid: session if uid == "ses-1" else None
            )
        )
        app = SimpleNamespace(state=SimpleNamespace(hub=hub))
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/sessions/ses-1/activities/act-1",
                "path_params": {
                    "uid": "ses-1",
                    "activity_id": "act-1",
                },
                "headers": [],
                "app": app,
            }
        )
        activity = {
            "activity_id": "act-1",
            "kind": "tool",
            "name": "exec_command",
            "status": "completed",
            "input": {"cmd": "printf ok"},
            "result": "ok\nsecond line",
            "created_at": "2026-08-18T01:00:00Z",
        }
        with mock.patch(
            "agent_hub.app.load_runtime_activity_detail",
            return_value=activity,
        ):
            response = await api_activity_detail(request)

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.body)
        self.assertEqual(payload["input"]["cmd"], "printf ok")
        self.assertEqual(payload["result"], "ok\nsecond line")


class SearchMessagesApiTests(unittest.IsolatedAsyncioTestCase):
    def _request(
        self,
        *,
        query: str,
        cwd: str,
        role: str | None = None,
    ) -> Request:
        database = SimpleNamespace(
            search_messages=lambda **kwargs: [
                {
                    "message_id": "msg-1",
                    "session_uid": "ses-1",
                    "content": "找到记录",
                    "excerpt": "找到记录",
                    "role": kwargs.get("role") or "assistant",
                }
            ]
        )
        app = SimpleNamespace(
            state=SimpleNamespace(hub=SimpleNamespace(db=database))
        )
        values = f"q={query}&cwd={cwd}"
        if role:
            values += f"&role={role}"
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/search/messages",
                "query_string": values.encode(),
                "headers": [],
                "app": app,
            }
        )

    async def test_search_messages_returns_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            response = await api_search_messages(
                self._request(query="记录", cwd=tmp, role="assistant")
            )

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.body)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["results"][0]["content"], "找到记录")

    async def test_search_messages_rejects_short_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            response = await api_search_messages(
                self._request(query="a", cwd=tmp)
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("至少需要 2 个字符", response.body.decode())


class FakeImportGenTmux:
    def __init__(self, cwd: str, rollout_path: str):
        self.cwd = cwd
        self.rollout_path = rollout_path
        self.bound: list[dict[str, str]] = []
        self.unbound: list[str] = []
        self.snapshot_window: dict[str, Any] | None = None

    def get_window(self, window_id: str) -> dict[str, Any]:
        return {
            "window_id": window_id,
            "window_index": 4,
            "display_name": "Prompt 优化",
            "runtime": "tcodex",
            "runtime_id": "thread-import",
            "cwd": self.cwd,
            "pane_id": "%4",
            "agent_pid": 123,
            "agent_pgid": 123,
            "rollout_path": self.rollout_path,
            "permission_profile": "full-access",
            "state": "idle",
        }

    def bind_chat(self, window_id: str, **kwargs: str) -> None:
        self.bound.append({"window_id": window_id, **kwargs})

    def unbind_chat(self, window_id: str) -> None:
        self.unbound.append(window_id)

    def snapshot(self, force: bool = False) -> dict[str, Any]:
        del force
        window = self.snapshot_window or self.get_window("@4")
        return {
            "tmux_session": "gen",
            "available": True,
            "windows": [dict(window)],
            "attn": {"available": True},
        }


class GenChatImportTests(unittest.IsolatedAsyncioTestCase):
    async def test_import_gen_chat_registers_session_and_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / "rollout-thread-import.jsonl"
            rollout.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "session_meta",
                                "payload": {"id": "thread-import"},
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-08-17T01:00:00Z",
                                "type": "event_msg",
                                "payload": {
                                    "type": "user_message",
                                    "message": "旧问题",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-08-17T01:00:01Z",
                                "type": "event_msg",
                                "payload": {
                                    "type": "agent_message",
                                    "message": "旧回答",
                                },
                            }
                        ),
                    ]
                )
            )
            service = HubService(
                HubConfig(
                    db_path=Path(tmp) / "hub.sqlite3",
                    tmux_socket_name="ah-unit-import",
                )
            )
            fake = FakeImportGenTmux(tmp, str(rollout))
            service.gen_tmux = fake  # type: ignore[assignment]
            service.runtime_manager.gen_tmux = fake  # type: ignore[assignment]

            result = await service.import_gen_chat("@4")
            again = await service.import_gen_chat("@4")

            self.assertEqual(
                result["session"]["transport"], "gen-tmux-relay"
            )
            self.assertEqual(
                [(m["role"], m["content"]) for m in result["messages"]],
                [("human", "旧问题"), ("assistant", "旧回答")],
            )
            self.assertEqual(len(again["messages"]), 2)
            self.assertEqual(
                result["session"]["managed_config"]["permission_profile"],
                "full-access",
            )
            self.assertEqual(fake.bound[0]["window_id"], "@4")

    async def test_gen_snapshot_exposes_bound_chat_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / "rollout-thread-import.jsonl"
            rollout.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": "thread-import"},
                    }
                )
            )
            service = HubService(
                HubConfig(
                    db_path=Path(tmp) / "hub.sqlite3",
                    tmux_socket_name="ah-unit-import-snapshot",
                )
            )
            fake = FakeImportGenTmux(tmp, str(rollout))
            service.gen_tmux = fake  # type: ignore[assignment]
            service.runtime_manager.gen_tmux = fake  # type: ignore[assignment]
            imported = await service.import_gen_chat("@4")

            fake.snapshot = lambda force=False: {
                "tmux_session": "gen",
                "available": True,
                "windows": [
                    {
                        **fake.get_window("@4"),
                        "adopted_session_uid": imported["session"][
                            "session_uid"
                        ],
                        "agent_pid": 123,
                    }
                ],
                "attn": {"available": True},
            }
            snapshot = await service.gen_snapshot(force=True)

            self.assertEqual(
                snapshot["windows"][0]["chat_session_uid"],
                imported["session"]["session_uid"],
            )
            self.assertEqual(
                snapshot["windows"][0]["chat_transport"],
                "gen-tmux-relay",
            )

    async def test_gen_snapshot_rebinds_stale_window_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / "rollout-thread-import.jsonl"
            rollout.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": "thread-import"},
                    }
                )
            )
            service = HubService(
                HubConfig(
                    db_path=Path(tmp) / "hub.sqlite3",
                    tmux_socket_name="ah-unit-stale-binding",
                )
            )
            fake = FakeImportGenTmux(tmp, str(rollout))
            fake.snapshot_window = {
                **fake.get_window("@4"),
                "adopted_session_uid": "ses_stale",
            }
            service.gen_tmux = fake  # type: ignore[assignment]
            service.runtime_manager.gen_tmux = fake  # type: ignore[assignment]

            snapshot = await service.gen_snapshot(force=True)

            expected_uid = result_uid = fake.bound[-1]["session_uid_value"]
            self.assertEqual(fake.unbound, ["@4"])
            self.assertEqual(
                snapshot["windows"][0]["chat_session_uid"],
                expected_uid,
            )
            self.assertEqual(result_uid, expected_uid)


if __name__ == "__main__":
    unittest.main()
