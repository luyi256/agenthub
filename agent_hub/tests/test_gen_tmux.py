from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from agent_hub.gen_tmux import GenTmuxService


class GenTmuxServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "state").mkdir()
        (self.root / "detect.py").write_text("# test")
        (self.root / "state" / "attn.json").write_text(
            json.dumps(
                {
                    "styled_windows": {"@2": "done"},
                    "hooked_server_pid": "123",
                }
            )
        )
        self.service = GenTmuxService(
            attn_dir=self.root,
            cache_seconds=0,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_snapshot_only_includes_live_detected_gen_agents(self) -> None:
        rows = [
            {
                "window_id": "@1",
                "window_index": "1",
                "window_name": "rubric",
                "window_active": "1",
                "custom_name": "",
                "manual_attn": "",
                "adopted_session_uid": "",
                "adopted_runtime": "",
                "adopted_runtime_id": "",
                "pane_id": "%1",
                "pane_dead": "0",
                "pane_pid": "101",
                "pane_current_path": "/home/luyi/generation",
                "pane_current_command": "node",
                "pane_title": "generation",
            },
            {
                "window_id": "@2",
                "window_index": "2",
                "window_name": "node",
                "window_active": "0",
                "custom_name": "Prompt 优化",
                "manual_attn": "",
                "adopted_session_uid": "",
                "adopted_runtime": "",
                "adopted_runtime_id": "",
                "pane_id": "%2",
                "pane_dead": "0",
                "pane_pid": "102",
                "pane_current_path": "/home/luyi/generation",
                "pane_current_command": "node",
                "pane_title": "generation",
            },
            {
                "window_id": "@3",
                "window_index": "3",
                "window_name": "zsh",
                "window_active": "0",
                "custom_name": "",
                "manual_attn": "",
                "adopted_session_uid": "",
                "adopted_runtime": "",
                "adopted_runtime_id": "",
                "pane_id": "%3",
                "pane_dead": "0",
                "pane_pid": "103",
                "pane_current_path": "/home/luyi/generation",
                "pane_current_command": "zsh",
                "pane_title": "shell",
            },
        ]
        detected = [
            {
                "pane": "%1",
                "session": "gen",
                "kind": "tcodex",
                "state": "busy",
                "rollout_path": str(self.root / "codex-1.jsonl"),
            },
            {
                "pane": "%2",
                "session": "gen",
                "kind": "tclaude",
                "state": "idle",
                "sid": "claude-session-2",
            },
        ]
        (self.root / "codex-1.jsonl").write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {"session_id": "codex-session-1"},
                }
            )
            + "\n"
        )
        with patch.object(self.service, "_window_rows", return_value=rows), patch.object(
            self.service, "_detect_agent_panes", return_value=detected
        ), patch.object(
            self.service, "_default_server_pid", return_value="123"
        ), patch.object(
            self.service, "_rollout_is_open", return_value=True
        ):
            snapshot = self.service.snapshot(force=True)
        self.assertEqual(
            [item["window_id"] for item in snapshot["windows"]],
            ["@1", "@2"],
        )
        self.assertEqual(snapshot["windows"][0]["display_name"], "rubric")
        self.assertEqual(snapshot["windows"][0]["state"], "busy")
        self.assertEqual(snapshot["windows"][1]["display_name"], "Prompt 优化")
        self.assertEqual(snapshot["windows"][1]["state"], "done")

    def test_mutations_are_scoped_to_verified_gen_window(self) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_tmux(*args: str) -> CompletedProcess[str]:
            calls.append(args)
            if args[0] == "display-message":
                return CompletedProcess(args, 0, "gen\t@2\t0\n", "")
            return CompletedProcess(args, 0, "", "")

        with patch.object(self.service, "_tmux", side_effect=fake_tmux), patch.object(
            self.service,
            "_find_window",
            return_value={"window_id": "@2"},
        ), patch.object(
            self.service,
            "snapshot",
            return_value={"windows": [{"window_id": "@2"}]},
        ):
            self.service.rename("@2", "Prompt 优化")
            self.service.set_attn("@2", "red")
            self.service.set_attn("@2", "clear")

        self.assertIn(
            (
                "set-window-option",
                "-t",
                "@2",
                "@agenthub_name",
                "Prompt 优化",
            ),
            calls,
        )
        self.assertIn(
            ("set-window-option", "-t", "@2", "@attn_manual", "red"),
            calls,
        )
        self.assertIn(
            ("set-window-option", "-t", "@2", "-u", "@attn_manual"),
            calls,
        )

    def test_bind_chat_uses_tmux_window_options_only(self) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_tmux(*args: str) -> CompletedProcess[str]:
            calls.append(args)
            if args[0] == "display-message":
                return CompletedProcess(args, 0, "gen\t@2\t0\n", "")
            return CompletedProcess(args, 0, "", "")

        with patch.object(self.service, "_tmux", side_effect=fake_tmux), patch.object(
            self.service,
            "snapshot",
            return_value={"windows": [{"window_id": "@2"}]},
        ):
            self.service.bind_chat(
                "@2",
                session_uid_value="ses_unit",
                runtime="tcodex",
                runtime_id="thread-1",
            )

        self.assertIn(
            (
                "set-window-option",
                "-t",
                "@2",
                "@agenthub_session_uid",
                "ses_unit",
            ),
            calls,
        )

    def test_unbind_chat_clears_all_window_identity_options(self) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_tmux(*args: str) -> CompletedProcess[str]:
            calls.append(args)
            return CompletedProcess(args, 0, "", "")

        with patch.object(
            self.service, "_verify_window"
        ), patch.object(
            self.service, "_tmux", side_effect=fake_tmux
        ):
            self.service.unbind_chat("@2")

        self.assertEqual(
            calls,
            [
                (
                    "set-window-option",
                    "-t",
                    "@2",
                    "-u",
                    "@agenthub_session_uid",
                ),
                (
                    "set-window-option",
                    "-t",
                    "@2",
                    "-u",
                    "@agenthub_runtime",
                ),
                (
                    "set-window-option",
                    "-t",
                    "@2",
                    "-u",
                    "@agenthub_runtime_id",
                ),
            ],
        )
        self.assertNotIn("rename-window", {arg for call in calls for arg in call})

    def test_close_window_kills_only_verified_gen_window(self) -> None:
        calls: list[tuple[str, ...]] = []
        window = {
            "window_id": "@2",
            "session_uid": "ses_unit",
        }

        def fake_tmux(*args: str) -> CompletedProcess[str]:
            calls.append(args)
            return CompletedProcess(args, 0, "", "")

        with patch.object(
            self.service, "get_window", return_value=window
        ), patch.object(
            self.service, "_verify_window"
        ), patch.object(
            self.service, "_tmux", side_effect=fake_tmux
        ):
            result = self.service.close_window("@2")

        self.assertTrue(result["closed"])
        self.assertEqual(calls, [("kill-window", "-t", "@2")])

    def test_send_text_uses_buffer_paste_then_enter(self) -> None:
        calls: list[tuple[str, ...]] = []
        inputs: list[str] = []
        window = {
            "window_id": "@2",
            "window_index": 2,
            "pane_id": "%2",
            "pane_pid": 102,
            "agent_pgid": 202,
            "agent_present": True,
            "state": "idle",
        }

        def fake_tmux(*args: str) -> CompletedProcess[str]:
            calls.append(args)
            return CompletedProcess(args, 0, "", "")

        def fake_tmux_input(
            text: str, *args: str
        ) -> CompletedProcess[str]:
            inputs.append(text)
            calls.append(args)
            return CompletedProcess(args, 0, "", "")

        with patch.object(
            self.service, "get_window", return_value=window
        ), patch.object(
            self.service, "_verify_window"
        ), patch.object(
            self.service, "_foreground_pgid", return_value=202
        ), patch.object(
            self.service, "_tmux", side_effect=fake_tmux
        ), patch.object(
            self.service, "_tmux_with_input", side_effect=fake_tmux_input
        ), patch(
            "agent_hub.gen_tmux.time.sleep"
        ):
            result = self.service.send_text(
                "@2", "继续处理", verify_submission=False
            )

        self.assertTrue(result["submitted"])
        self.assertEqual(inputs, ["继续处理"])
        self.assertEqual(calls[0][0], "load-buffer")
        self.assertEqual(
            calls[1],
            ("paste-buffer", "-p", "-b", calls[0][2], "-t", "%2", "-d"),
        )
        self.assertEqual(calls[2], ("send-keys", "-t", "%2", "Enter"))

    def test_send_text_can_queue_while_busy_when_explicitly_allowed(
        self,
    ) -> None:
        calls: list[tuple[str, ...]] = []
        window = {
            "window_id": "@2",
            "window_index": 2,
            "pane_id": "%2",
            "pane_pid": 102,
            "agent_pgid": 202,
            "agent_present": True,
            "state": "busy",
        }

        def fake_tmux(*args: str) -> CompletedProcess[str]:
            calls.append(args)
            return CompletedProcess(args, 0, "", "")

        with patch.object(
            self.service, "get_window", return_value=window
        ), patch.object(
            self.service, "_verify_window"
        ), patch.object(
            self.service, "_foreground_pgid", return_value=202
        ), patch.object(
            self.service, "_tmux", side_effect=fake_tmux
        ), patch.object(
            self.service,
            "_tmux_with_input",
            return_value=CompletedProcess([], 0, "", ""),
        ), patch(
            "agent_hub.gen_tmux.time.sleep"
        ):
            result = self.service.send_text(
                "@2",
                "运行中补充",
                allow_busy=True,
                verify_submission=False,
            )

        self.assertTrue(result["submitted"])
        self.assertEqual(result["delivery"], "queued")
        self.assertEqual(calls[-1], ("send-keys", "-t", "%2", "Enter"))

    def test_send_text_fails_when_tui_does_not_confirm_submission(
        self,
    ) -> None:
        window = {
            "window_id": "@2",
            "window_index": 2,
            "pane_id": "%2",
            "pane_pid": 102,
            "agent_pgid": 202,
            "agent_present": True,
            "runtime": "tcodex",
            "runtime_id": "thread-2",
            "rollout_path": None,
            "state": "idle",
        }

        with patch.object(
            self.service, "get_window", return_value=window
        ), patch.object(
            self.service, "_verify_window"
        ), patch.object(
            self.service, "_foreground_pgid", return_value=202
        ), patch.object(
            self.service,
            "_tmux",
            return_value=CompletedProcess([], 0, "", ""),
        ), patch.object(
            self.service,
            "_tmux_with_input",
            return_value=CompletedProcess([], 0, "", ""),
        ), patch.object(
            self.service,
            "_verify_text_submission",
            side_effect=RuntimeError("TUI 未确认"),
        ), patch(
            "agent_hub.gen_tmux.time.sleep"
        ):
            with self.assertRaisesRegex(RuntimeError, "TUI 未确认"):
                self.service.send_text("@2", "未提交消息")

    def test_refuses_window_outside_gen(self) -> None:
        with patch.object(
            self.service,
            "_tmux",
            return_value=CompletedProcess([], 0, "paper\t@9\t0\n", ""),
        ), patch.object(
            self.service,
            "snapshot",
            return_value={"windows": []},
        ):
            with self.assertRaisesRegex(ValueError, "只允许操作 tmux gen"):
                self.service.set_attn("@9", "yellow")

    def test_refuses_non_agent_window_inside_gen(self) -> None:
        with patch.object(
            self.service,
            "_tmux",
            return_value=CompletedProcess([], 0, "gen\t@9\t0\n", ""),
        ), patch.object(
            self.service,
            "snapshot",
            return_value={"windows": []},
        ):
            with self.assertRaisesRegex(ValueError, "不是.*Agent session"):
                self.service.rename("@9", "shell")

    def test_stale_attn_state_from_old_tmux_server_is_ignored(self) -> None:
        (self.root / "state" / "attn.json").write_text(
            json.dumps(
                {
                    "styled_windows": {"@2": "done"},
                    "done_panes": {"%2": {"window_id": "@2"}},
                    "hooked_server_pid": "old-server",
                }
            )
        )
        with patch.object(
            self.service,
            "_default_server_pid",
            return_value="new-server",
        ):
            state = self.service._load_attn_state()
        self.assertEqual(state["styled_windows"], {})
        self.assertEqual(state["done_panes"], {})
        self.assertEqual(state["hooked_server_pid"], "new-server")


if __name__ == "__main__":
    unittest.main()
