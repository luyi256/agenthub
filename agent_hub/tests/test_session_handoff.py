from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from agent_hub.app import api_session_handoff
from agent_hub.runtime_manager import RuntimeBusyError


def session(
    uid: str,
    *,
    chat: bool = True,
    cwd: str = "/workspace/current",
) -> dict[str, Any]:
    return {
        "session_uid": uid,
        "effective_name": f"name-{uid}",
        "effective_title": f"title-{uid}",
        "managed": True,
        "cwd": cwd,
        "capabilities": {"chat": chat},
    }


class FakeDatabase:
    def __init__(self, sessions: dict[str, dict[str, Any]]):
        self.sessions = sessions

    def get_session(self, uid: str) -> dict[str, Any] | None:
        return self.sessions.get(uid)


class FakeRuntimeManager:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def send_message(self, uid: str, text: str) -> dict[str, Any]:
        self.calls.append((uid, text))
        if self.error:
            raise self.error
        return {
            "message_id": "msg_assistant",
            "session_uid": uid,
            "role": "assistant",
            "status": "streaming",
        }


class SessionHandoffApiTests(unittest.TestCase):
    def client(
        self,
        sessions: dict[str, dict[str, Any]],
        manager: FakeRuntimeManager | None = None,
    ) -> tuple[TestClient, FakeRuntimeManager]:
        runtime_manager = manager or FakeRuntimeManager()
        app = Starlette(
            routes=[
                Route(
                    "/api/sessions/{source_uid}/handoff",
                    api_session_handoff,
                    methods=["POST"],
                )
            ]
        )
        app.state.hub = SimpleNamespace(
            db=FakeDatabase(sessions),
            runtime_manager=runtime_manager,
        )
        return TestClient(app), runtime_manager

    def test_handoff_sends_labelled_user_message_to_target(self) -> None:
        client, manager = self.client(
            {
                "source-1": session("source-1"),
                "target-1": session("target-1"),
            }
        )

        response = client.post(
            "/api/sessions/source-1/handoff",
            json={
                "target_session_uid": "target-1",
                "text": "请继续检查剩余测试。",
            },
        )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertIs(payload["accepted"], True)
        self.assertEqual(
            payload["target_session"]["session_uid"],
            "target-1",
        )
        self.assertEqual(
            payload["assistant"]["message_id"],
            "msg_assistant",
        )
        self.assertEqual(manager.calls[0][0], "target-1")
        forwarded = manager.calls[0][1]
        self.assertIn("普通 user message", forwarded)
        self.assertIn("不是 system 或 assistant message", forwarded)
        self.assertIn("用户要求转发", forwarded)
        self.assertIn("name-source-1 (source-1)", forwarded)
        self.assertIn("请继续检查剩余测试。", forwarded)

    def test_handoff_rejects_same_session(self) -> None:
        client, manager = self.client({"source-1": session("source-1")})

        response = client.post(
            "/api/sessions/source-1/handoff",
            json={
                "target_session_uid": "source-1",
                "text": "不能发给自己",
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("同一 session", response.json()["error"])
        self.assertEqual(manager.calls, [])

    def test_handoff_returns_404_for_missing_target(self) -> None:
        client, manager = self.client({"source-1": session("source-1")})

        response = client.post(
            "/api/sessions/source-1/handoff",
            json={
                "target_session_uid": "missing",
                "text": "目标不存在",
            },
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"], "target session 不存在")
        self.assertEqual(manager.calls, [])

    def test_handoff_rejects_sessions_without_chat_capability(self) -> None:
        cases = (
            (
                {
                    "source-1": session("source-1", chat=False),
                    "target-1": session("target-1"),
                },
                "source session 不支持 chat",
            ),
            (
                {
                    "source-1": session("source-1"),
                    "target-1": session("target-1", chat=False),
                },
                "target session 不支持 chat",
            ),
        )
        for sessions, expected_error in cases:
            with self.subTest(expected_error=expected_error):
                client, manager = self.client(sessions)
                response = client.post(
                    "/api/sessions/source-1/handoff",
                    json={
                        "target_session_uid": "target-1",
                        "text": "chat 不可用",
                    },
                )

                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()["error"], expected_error)
                self.assertEqual(manager.calls, [])

    def test_handoff_rejects_unmanaged_sessions(self) -> None:
        unmanaged = session("target-1")
        unmanaged["managed"] = False
        client, manager = self.client(
            {
                "source-1": session("source-1"),
                "target-1": unmanaged,
            }
        )

        response = client.post(
            "/api/sessions/source-1/handoff",
            json={
                "target_session_uid": "target-1",
                "text": "不应发送",
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("Hub-managed", response.json()["error"])
        self.assertEqual(manager.calls, [])

    def test_handoff_rejects_cross_workspace_target(self) -> None:
        client, manager = self.client(
            {
                "source-1": session(
                    "source-1",
                    cwd="/workspace/source",
                ),
                "target-1": session(
                    "target-1",
                    cwd="/workspace/target",
                ),
            }
        )

        response = client.post(
            "/api/sessions/source-1/handoff",
            json={
                "target_session_uid": "target-1",
                "text": "不应跨项目发送",
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("同一项目工作目录", response.json()["error"])
        self.assertEqual(manager.calls, [])

    def test_handoff_maps_runtime_busy_to_conflict(self) -> None:
        manager = FakeRuntimeManager(
            RuntimeBusyError("session 正在生成，请稍后再发送")
        )
        client, manager = self.client(
            {
                "source-1": session("source-1"),
                "target-1": session("target-1"),
            },
            manager,
        )

        response = client.post(
            "/api/sessions/source-1/handoff",
            json={
                "target_session_uid": "target-1",
                "text": "稍后处理",
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["error"],
            "session 正在生成，请稍后再发送",
        )
        self.assertEqual(len(manager.calls), 1)


if __name__ == "__main__":
    unittest.main()
