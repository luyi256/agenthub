from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_hub.session_history import (
    _ACTIVITY_CACHE,
    _load_claude_history,
    _load_claude_activities,
    _load_claude_activity_detail,
    _load_codex_activities,
    _load_codex_activity_detail,
    _load_codex_history,
    _cached_jsonl_records,
)


class SessionHistoryTests(unittest.TestCase):
    def test_codex_uses_only_visible_user_and_agent_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout-test.jsonl"
            records = [
                {
                    "type": "session_meta",
                    "payload": {"session_id": "thread-1"},
                },
                {
                    "timestamp": "2026-08-17T01:00:00Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": "用户问题",
                    },
                },
                {
                    "timestamp": "2026-08-17T01:00:01Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "output": "不应展示",
                    },
                },
                {
                    "timestamp": "2026-08-17T01:00:02Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "phase": "commentary",
                        "message": "过程信息",
                    },
                },
                {
                    "timestamp": "2026-08-17T01:00:03Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "phase": "final_answer",
                        "message": "最终回答",
                    },
                },
            ]
            path.write_text(
                "\n".join(json.dumps(record) for record in records)
            )
            messages = _load_codex_history(path)
        self.assertEqual(
            [(item["role"], item["content"]) for item in messages],
            [("human", "用户问题"), ("assistant", "最终回答")],
        )

    def test_claude_deduplicates_thinking_only_and_text_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session-1.jsonl"
            records = [
                {
                    "type": "user",
                    "uuid": "u1",
                    "sessionId": "session-1",
                    "timestamp": "2026-08-17T01:00:00Z",
                    "message": {"content": "用户问题"},
                },
                {
                    "type": "assistant",
                    "uuid": "a-thinking",
                    "parentUuid": "u1",
                    "sessionId": "session-1",
                    "timestamp": "2026-08-17T01:00:01Z",
                    "message": {
                        "id": "message-1",
                        "content": [{"type": "thinking", "thinking": "..."}],
                    },
                },
                {
                    "type": "assistant",
                    "uuid": "a-text",
                    "parentUuid": "a-thinking",
                    "sessionId": "session-1",
                    "timestamp": "2026-08-17T01:00:02Z",
                    "message": {
                        "id": "message-1",
                        "content": [
                            {"type": "text", "text": "最终回答"}
                        ],
                    },
                },
                {
                    "type": "assistant",
                    "isSidechain": True,
                    "uuid": "sub",
                    "message": {
                        "id": "sub-message",
                        "content": [{"type": "text", "text": "子代理噪声"}],
                    },
                },
            ]
            path.write_text(
                "\n".join(json.dumps(record) for record in records)
            )
            messages = _load_claude_history(path)
        self.assertEqual(
            [(item["role"], item["content"]) for item in messages],
            [("human", "用户问题"), ("assistant", "最终回答")],
        )

    def test_codex_activities_join_plan_tools_and_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout-tools.jsonl"
            records = [
                {
                    "timestamp": "2026-08-18T01:00:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "update_plan",
                        "call_id": "plan-1",
                        "arguments": json.dumps(
                            {
                                "plan": [
                                    {
                                        "step": "读取数据",
                                        "status": "completed",
                                    },
                                    {
                                        "step": "生成结果",
                                        "status": "in_progress",
                                    },
                                ]
                            },
                            ensure_ascii=False,
                        ),
                    },
                },
                {
                    "timestamp": "2026-08-18T01:00:01Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "plan-1",
                        "output": "Plan updated",
                    },
                },
                {
                    "timestamp": "2026-08-18T01:00:02Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "tool-1",
                        "arguments": json.dumps({"cmd": "printf ok"}),
                    },
                },
                {
                    "timestamp": "2026-08-18T01:00:03Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "tool-1",
                        "output": (
                            "Chunk ID: one\nExit code: 0\nOutput:\nok\n"
                        ),
                    },
                },
                {
                    "timestamp": "2026-08-18T01:00:04Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "phase": "commentary",
                        "message": "正在校验结果。",
                    },
                },
            ]
            path.write_text(
                "\n".join(
                    json.dumps(record, ensure_ascii=False)
                    for record in records
                )
            )
            _ACTIVITY_CACHE.clear()
            activities = _load_codex_activities(path)

        self.assertEqual(
            [(item["kind"], item["name"]) for item in activities],
            [
                ("plan", "Plan"),
                ("tool", "exec_command"),
                ("commentary", "进度"),
            ],
        )
        self.assertEqual(activities[0]["status"], "completed")
        self.assertEqual(
            activities[0]["input"]["plan"][1]["status"],
            "in_progress",
        )
        self.assertEqual(activities[1]["status"], "completed")
        self.assertIn("Chunk ID: one", activities[1]["result"])

    def test_claude_activities_join_tool_result_and_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session-tools.jsonl"
            records = [
                {
                    "type": "user",
                    "uuid": "u1",
                    "sessionId": "session-tools",
                    "timestamp": "2026-08-18T01:00:00Z",
                    "message": {"content": "开始"},
                },
                {
                    "type": "assistant",
                    "uuid": "a1",
                    "parentUuid": "u1",
                    "sessionId": "session-tools",
                    "timestamp": "2026-08-18T01:00:01Z",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call-1",
                                "name": "Bash",
                                "input": {"command": "printf ok"},
                            }
                        ]
                    },
                },
                {
                    "type": "user",
                    "uuid": "u2",
                    "parentUuid": "a1",
                    "sessionId": "session-tools",
                    "timestamp": "2026-08-18T01:00:02Z",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "call-1",
                                "content": "ok\nsecond line",
                            }
                        ]
                    },
                },
                {
                    "type": "assistant",
                    "uuid": "a2",
                    "parentUuid": "u2",
                    "sessionId": "session-tools",
                    "timestamp": "2026-08-18T01:00:03Z",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "plan-1",
                                "name": "ExitPlanMode",
                                "input": {"plan": "# 计划\n\n1. 执行"},
                            }
                        ]
                    },
                },
                {
                    "type": "user",
                    "uuid": "u3",
                    "parentUuid": "a2",
                    "sessionId": "session-tools",
                    "timestamp": "2026-08-18T01:00:04Z",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "plan-1",
                                "content": "Plan accepted",
                            }
                        ]
                    },
                },
            ]
            path.write_text(
                "\n".join(
                    json.dumps(record, ensure_ascii=False)
                    for record in records
                )
            )
            _ACTIVITY_CACHE.clear()
            activities = _load_claude_activities(path)

        self.assertEqual(
            [(item["kind"], item["name"]) for item in activities],
            [("tool", "Bash"), ("plan", "Plan")],
        )
        self.assertEqual(activities[0]["result"], "ok\nsecond line")
        self.assertEqual(activities[1]["input"], "# 计划\n\n1. 执行")

    def test_activity_detail_reads_full_result_beyond_summary_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout-long-result.jsonl"
            long_result = "first line\n" + "x" * 25_000
            records = [
                {
                    "timestamp": "2026-08-18T01:00:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "tool-long",
                        "arguments": json.dumps({"cmd": "cat large.log"}),
                    },
                },
                {
                    "timestamp": "2026-08-18T01:00:01Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "tool-long",
                        "output": long_result,
                    },
                },
            ]
            path.write_text(
                "\n".join(json.dumps(record) for record in records)
            )
            _ACTIVITY_CACHE.clear()
            summary = _load_codex_activities(path)[0]
            detail = _load_codex_activity_detail(
                path,
                summary["activity_id"],
            )

        self.assertLess(len(summary["result"]), len(long_result))
        self.assertIsNotNone(detail)
        assert detail
        self.assertEqual(detail["result"], long_result)

    def test_claude_detail_extracts_text_blocks_from_tool_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session-array-result.jsonl"
            records = [
                {
                    "type": "assistant",
                    "uuid": "a1",
                    "sessionId": "session-array-result",
                    "timestamp": "2026-08-18T01:00:00Z",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tool-1",
                                "name": "Read",
                                "input": {"file_path": "/tmp/a"},
                            }
                        ]
                    },
                },
                {
                    "type": "user",
                    "uuid": "u1",
                    "parentUuid": "a1",
                    "sessionId": "session-array-result",
                    "timestamp": "2026-08-18T01:00:01Z",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "tool-1",
                                "content": [
                                    {"type": "text", "text": "line one"},
                                    {"type": "image", "source": "hidden"},
                                    {"type": "text", "text": "line two"},
                                ],
                            }
                        ]
                    },
                },
            ]
            path.write_text(
                "\n".join(json.dumps(record) for record in records)
            )
            _ACTIVITY_CACHE.clear()
            summary = _load_claude_activities(path)[0]
            detail = _load_claude_activity_detail(
                path,
                summary["activity_id"],
            )

        self.assertEqual(summary["result"], "line one\nline two")
        self.assertIsNotNone(detail)
        assert detail
        self.assertEqual(detail["result"], "line one\nline two")

    def test_incomplete_jsonl_tail_is_replayed_after_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout-partial.jsonl"
            first = json.dumps(
                {
                    "timestamp": "2026-08-18T01:00:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "tool-partial",
                        "arguments": "{}",
                    },
                }
            )
            second = json.dumps(
                {
                    "timestamp": "2026-08-18T01:00:01Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "tool-partial",
                        "output": "complete output",
                    },
                }
            )
            split_at = len(second) // 2
            path.write_text(first + "\n" + second[:split_at])
            _ACTIVITY_CACHE.clear()
            initial = _cached_jsonl_records(path)
            with path.open("a") as output:
                output.write(second[split_at:] + "\n")
            completed = _cached_jsonl_records(path)
            activities = _load_codex_activities(path)

        self.assertEqual(len(initial), 1)
        self.assertEqual(len(completed), 2)
        self.assertEqual(activities[0]["result"], "complete output")


if __name__ == "__main__":
    unittest.main()
