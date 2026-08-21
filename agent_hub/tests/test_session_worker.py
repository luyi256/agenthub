from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from agent_hub.session_worker import ClaudeRuntime, CodexRuntime


class FakeWorker:
    def __init__(self) -> None:
        self.config = {
            "runtime": "tcodex",
            "cwd": "/tmp",
            "native_name": "test",
        }
        self.state: dict[str, Any] = {
            "status": "running",
            "pending_approvals": {},
        }
        self.persist_count = 0
        self.emitted: list[tuple[str, dict[str, Any]]] = []

    async def persist_state(self) -> None:
        self.persist_count += 1

    async def emit(self, method: str, params: dict[str, Any]) -> None:
        self.emitted.append((method, params))


class RecordingCodexRuntime(CodexRuntime):
    def __init__(self, worker: FakeWorker):
        super().__init__(worker)  # type: ignore[arg-type]
        self.writes: list[dict[str, Any]] = []
        self.write_error: Exception | None = None

    async def write(self, payload: dict[str, Any]) -> None:
        if self.write_error:
            raise self.write_error
        self.writes.append(payload)


class CodexApprovalTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self) -> tuple[FakeWorker, RecordingCodexRuntime]:
        worker = FakeWorker()
        return worker, RecordingCodexRuntime(worker)

    async def test_known_server_request_becomes_persisted_approval(self) -> None:
        worker, runtime = self.runtime()
        await runtime._handle_server_request(
            {
                "id": 41,
                "method": "item/commandExecution/requestApproval",
                "params": {"command": "echo ok"},
            }
        )

        self.assertEqual(len(runtime.approvals), 1)
        approval_id, approval = next(iter(runtime.approvals.items()))
        self.assertEqual(approval["runtime_request_id"], 41)
        self.assertEqual(
            worker.state["pending_approvals"][approval_id],
            {
                "method": "item/commandExecution/requestApproval",
                "params": {"command": "echo ok"},
            },
        )
        self.assertEqual(worker.state["status"], "waiting_approval")
        self.assertEqual(worker.persist_count, 1)
        self.assertEqual(
            worker.emitted[0][0], "approval.requested"
        )

    async def test_unknown_server_request_receives_rpc_error(self) -> None:
        worker, runtime = self.runtime()
        await runtime._handle_server_request(
            {
                "id": "server-9",
                "method": "account/login/request",
                "params": {"reason": "new protocol"},
            }
        )

        self.assertEqual(runtime.approvals, {})
        self.assertEqual(worker.state["pending_approvals"], {})
        self.assertEqual(worker.persist_count, 0)
        self.assertEqual(
            runtime.writes,
            [
                {
                    "id": "server-9",
                    "error": {
                        "code": -32601,
                        "message": (
                            "Unsupported server request: account/login/request"
                        ),
                    },
                }
            ],
        )

    async def test_resolve_validates_action_without_mutating_state(self) -> None:
        worker, runtime = self.runtime()
        await runtime._approval_request(
            {
                "id": 7,
                "method": "item/fileChange/requestApproval",
                "params": {"path": "a.py"},
            }
        )
        approval_id = next(iter(runtime.approvals))
        before = dict(runtime.approvals[approval_id])
        persist_count = worker.persist_count

        with self.assertRaisesRegex(
            RuntimeError, "不支持的 approval action"
        ):
            await runtime.resolve_approval(approval_id, "approve")

        self.assertEqual(runtime.approvals[approval_id], before)
        self.assertIn(
            approval_id, worker.state["pending_approvals"]
        )
        self.assertEqual(worker.persist_count, persist_count)
        self.assertEqual(len(runtime.writes), 0)

    async def test_resolve_write_failure_keeps_approval_retryable(self) -> None:
        worker, runtime = self.runtime()
        await runtime._approval_request(
            {
                "id": 8,
                "method": "execCommandApproval",
                "params": {"command": "touch no"},
            }
        )
        approval_id = next(iter(runtime.approvals))
        persist_count = worker.persist_count
        runtime.write_error = BrokenPipeError("closed")

        with self.assertRaises(BrokenPipeError):
            await runtime.resolve_approval(approval_id, "decline")

        self.assertIn(approval_id, runtime.approvals)
        self.assertIn(
            approval_id, worker.state["pending_approvals"]
        )
        self.assertEqual(worker.state["status"], "waiting_approval")
        self.assertEqual(worker.persist_count, persist_count)

    async def test_resolve_success_pops_after_write_and_preserves_others(
        self,
    ) -> None:
        worker, runtime = self.runtime()
        await runtime._approval_request(
            {
                "id": 9,
                "method": "item/commandExecution/requestApproval",
                "params": {"command": "first"},
            }
        )
        first_id = next(iter(runtime.approvals))
        await runtime._approval_request(
            {
                "id": 10,
                "method": "applyPatchApproval",
                "params": {"patch": "second"},
            }
        )
        second_id = next(
            key for key in runtime.approvals if key != first_id
        )

        result = await runtime.resolve_approval(first_id, "accept")

        self.assertEqual(result, {"decision": "accept"})
        self.assertEqual(
            runtime.writes[-1],
            {"id": 9, "result": {"decision": "accept"}},
        )
        self.assertNotIn(first_id, runtime.approvals)
        self.assertIn(second_id, runtime.approvals)
        self.assertEqual(
            set(worker.state["pending_approvals"]), {second_id}
        )
        self.assertEqual(worker.state["status"], "waiting_approval")

        result = await runtime.resolve_approval(second_id, "decline")
        self.assertEqual(result, {"decision": "denied"})
        self.assertEqual(runtime.approvals, {})
        self.assertEqual(worker.state["pending_approvals"], {})
        self.assertEqual(worker.state["status"], "idle")

    async def test_corrupt_restored_approval_is_not_removed(self) -> None:
        worker, runtime = self.runtime()
        runtime.approvals["restored"] = {
            "runtime_request_id": 11,
            "method": "unknownApproval",
            "params": {},
        }
        runtime._sync_pending_approval_state()
        persist_count = worker.persist_count

        with self.assertRaisesRegex(
            RuntimeError, "尚不支持 approval 类型"
        ):
            await runtime.resolve_approval("restored", "accept")

        self.assertIn("restored", runtime.approvals)
        self.assertIn(
            "restored", worker.state["pending_approvals"]
        )
        self.assertEqual(runtime.writes, [])
        self.assertEqual(worker.persist_count, persist_count)

    async def test_request_timeout_removes_pending(self) -> None:
        _, runtime = self.runtime()
        with self.assertRaises(asyncio.TimeoutError):
            await runtime.request("thread/read", {}, timeout=0.01)
        self.assertEqual(runtime.pending, {})

    async def test_send_failure_restores_active_state(self) -> None:
        worker, runtime = self.runtime()

        async def failed_request(
            method: str,
            params: dict[str, Any],
            *,
            timeout: float = 120,
        ) -> dict[str, Any]:
            del method, params, timeout
            raise RuntimeError("turn start failed")

        runtime.request = failed_request  # type: ignore[method-assign]
        with self.assertRaisesRegex(RuntimeError, "turn start failed"):
            await runtime.send("hello", "message-1")

        self.assertIsNone(runtime.active_message_id)
        self.assertIsNone(worker.state["active_message_id"])
        self.assertEqual(worker.state["active_text"], "")
        self.assertEqual(worker.state["status"], "error")

    async def test_send_uses_configured_model_and_effort(self) -> None:
        worker, runtime = self.runtime()
        worker.config.update(
            {"model": "gpt-5.6-luna", "reasoning_effort": "high"}
        )
        runtime.runtime_id = "thread-1"
        captured: dict[str, Any] = {}

        async def successful_request(
            method: str,
            params: dict[str, Any],
            *,
            timeout: float = 120,
        ) -> dict[str, Any]:
            del timeout
            captured["method"] = method
            captured["params"] = params
            return {"turn": {"id": "turn-1"}}

        runtime.request = successful_request  # type: ignore[method-assign]
        await runtime.send("hello", "message-1")

        self.assertEqual(captured["method"], "turn/start")
        self.assertEqual(captured["params"]["model"], "gpt-5.6-luna")
        self.assertEqual(captured["params"]["effort"], "high")

    async def test_start_configures_selected_model_and_effort(self) -> None:
        worker, runtime = self.runtime()
        worker.config.update(
            {"model": "gpt-5.6-luna", "reasoning_effort": "high"}
        )
        calls: list[tuple[str, dict[str, Any]]] = []

        async def request(
            method: str,
            params: dict[str, Any],
            *,
            timeout: float = 120,
        ) -> dict[str, Any]:
            del timeout
            calls.append((method, params))
            if method == "initialize":
                return {"userAgent": "codex/0.144.5"}
            if method == "thread/start":
                return {
                    "thread": {"id": "thread-selected"},
                    "model": "gpt-5.6-luna",
                    "reasoningEffort": "xhigh",
                }
            return {}

        runtime.request = request  # type: ignore[method-assign]
        runtime.notify = AsyncMock()  # type: ignore[method-assign]
        runtime.process = SimpleNamespace(pid=123)

        with unittest.mock.patch(
            "agent_hub.session_worker.asyncio.create_subprocess_exec",
            AsyncMock(return_value=runtime.process),
        ), unittest.mock.patch.object(
            runtime, "_read_stdout", AsyncMock()
        ), unittest.mock.patch.object(
            runtime, "_read_stderr", AsyncMock()
        ):
            details = await runtime.start()

        start = next(params for method, params in calls if method == "thread/start")
        self.assertEqual(start["model"], "gpt-5.6-luna")
        self.assertEqual(
            start["config"]["model_reasoning_effort"], "high"
        )
        self.assertEqual(details["reasoning_effort"], "high")

    def test_claude_command_uses_configured_model_and_effort(self) -> None:
        worker = FakeWorker()
        worker.config.update(
            {
                "runtime": "tclaude",
                "model": "claude-opus-5",
                "reasoning_effort": "high",
            }
        )
        command = ClaudeRuntime(worker).command()  # type: ignore[arg-type]

        self.assertIn("--model", command)
        self.assertEqual(command[command.index("--model") + 1], "claude-opus-5")
        self.assertIn("--effort", command)
        self.assertEqual(command[command.index("--effort") + 1], "high")

    async def test_active_codex_turn_uses_native_steer(self) -> None:
        worker, runtime = self.runtime()
        runtime.runtime_id = "thread-1"
        runtime.active_message_id = "message-1"
        runtime.active_turn_id = "turn-1"

        async def successful_request(
            method: str,
            params: dict[str, Any],
            *,
            timeout: float = 120,
        ) -> dict[str, Any]:
            del timeout
            self.assertEqual(method, "turn/steer")
            self.assertEqual(
                params,
                {
                    "threadId": "thread-1",
                    "expectedTurnId": "turn-1",
                    "input": [{"type": "text", "text": "补充"}],
                },
            )
            return {"turnId": "turn-1"}

        runtime.request = successful_request  # type: ignore[method-assign]
        result = await runtime.steer("补充")

        self.assertEqual(result["delivery"], "steer")
        self.assertEqual(result["turn_id"], "turn-1")
        self.assertEqual(
            worker.state["last_steer"]["delivery"], "steer"
        )

    async def test_turn_completion_clears_active_turn_id(self) -> None:
        worker, runtime = self.runtime()
        runtime.active_message_id = "message-1"
        runtime.active_turn_id = "turn-1"
        worker.state.update(
            {
                "active_message_id": "message-1",
                "active_turn_id": "turn-1",
                "active_text": "done",
            }
        )
        event = {
            "method": "turn/completed",
            "params": {
                "turn": {
                    "id": "turn-1",
                    "status": "completed",
                }
            },
        }
        line = (json.dumps(event) + "\n").encode()
        runtime.process = SimpleNamespace(
            stdout=asyncio.StreamReader(),
            stderr=None,
        )
        runtime.process.stdout.feed_data(line)
        runtime.process.stdout.feed_eof()
        worker.runtime_exited = AsyncMock()  # type: ignore[attr-defined]

        await runtime._read_stdout()

        self.assertIsNone(runtime.active_turn_id)
        self.assertIsNone(worker.state["active_turn_id"])

    async def test_parallel_resolve_writes_only_once(self) -> None:
        _, runtime = self.runtime()
        runtime.active_message_id = "message-2"
        await runtime._approval_request(
            {
                "id": 12,
                "method": "item/commandExecution/requestApproval",
                "params": {"command": "echo once"},
            }
        )
        approval_id = next(iter(runtime.approvals))

        results = await asyncio.gather(
            runtime.resolve_approval(approval_id, "accept"),
            runtime.resolve_approval(approval_id, "accept"),
            return_exceptions=True,
        )

        self.assertEqual(
            sum(isinstance(result, RuntimeError) for result in results), 1
        )
        self.assertEqual(
            runtime.writes.count(
                {"id": 12, "result": {"decision": "accept"}}
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()
