from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_hub.config import HubConfig
from agent_hub.project_tmux import ProjectTmuxManager, WorkerLaunch


class RecordingTmuxManager(ProjectTmuxManager):
    def __init__(self, config: HubConfig, *, fail_on: str | None = None):
        super().__init__(config)
        self.fail_on = fail_on
        self.commands: list[tuple[list[str], bool]] = []
        self.owned_target: str | None = None

    def ensure_workspace(self, workspace: object) -> None:
        del workspace

    def _tmux(
        self, args: list[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append((list(args), check))
        if self.fail_on and args[0] == self.fail_on:
            raise subprocess.CalledProcessError(1, args, stderr="injected")
        return subprocess.CompletedProcess(args, 0, "", "")

    def _owned_worker_window_target(
        self, launch: WorkerLaunch
    ) -> str | None:
        del launch
        return self.owned_target


class ProjectTmuxLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = HubConfig(
            db_path=self.root / "data" / "agenthub.sqlite3",
            tmux_socket_name="ah-e2e-unit-lifecycle",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _launch(self, manager: ProjectTmuxManager) -> WorkerLaunch:
        return manager.launch_worker(
            runtime="tcodex",
            cwd=str(self.root),
            native_name="lifecycle-test",
            permission_profile="safe",
            workspace_id="abcd1234",
            workspace_name="unit",
            resume_runtime_id="thread-1",
            model="gpt-5.6-luna",
            reasoning_effort="high",
        )

    def test_new_window_failure_removes_config_without_kill(self) -> None:
        manager = RecordingTmuxManager(self.config, fail_on="new-window")

        with self.assertRaises(subprocess.CalledProcessError):
            self._launch(manager)

        self.assertEqual(list(manager.worker_dir.glob("*.json")), [])
        self.assertEqual(list(manager.run_dir.glob("*")), [])
        self.assertEqual(list(manager.state_dir.glob("*")), [])
        self.assertFalse(
            any(args[0] == "kill-window" for args, _ in manager.commands)
        )
        config_files = list(manager.worker_dir.glob("*.json"))
        self.assertEqual(config_files, [])

    def test_worker_config_preserves_model_selection(self) -> None:
        manager = RecordingTmuxManager(self.config)

        launch = self._launch(manager)
        config = __import__("json").loads(
            Path(launch.config_path).read_text(encoding="utf-8")
        )

        self.assertEqual(config["model"], "gpt-5.6-luna")
        self.assertEqual(config["reasoning_effort"], "high")

    def test_post_create_failure_kills_exact_window_and_removes_files(
        self,
    ) -> None:
        manager = RecordingTmuxManager(
            self.config, fail_on="set-window-option"
        )

        with self.assertRaises(subprocess.CalledProcessError):
            self._launch(manager)

        kill_commands = [
            args for args, _ in manager.commands if args[0] == "kill-window"
        ]
        self.assertEqual(len(kill_commands), 1)
        self.assertEqual(list(manager.worker_dir.glob("*.json")), [])

    def test_cleanup_uses_owned_window_id_after_rename(self) -> None:
        manager = RecordingTmuxManager(self.config)
        launch = WorkerLaunch(
            worker_id="wrk_123456",
            socket_path=str(self.root / "worker.sock"),
            state_path=str(self.root / "worker-state.json"),
            config_path=str(self.root / "worker-config.json"),
            tmux_session="ah-unit-abcd1234",
            tmux_window="old-name-123456",
        )
        for value in (
            launch.socket_path,
            launch.state_path,
            launch.config_path,
        ):
            Path(value).write_text("x", encoding="utf-8")
        manager.owned_target = "@42"

        manager.cleanup_launch(launch)

        kill_commands = [
            args for args, _ in manager.commands if args[0] == "kill-window"
        ]
        self.assertEqual(kill_commands, [["kill-window", "-t", "@42"]])
        for value in (
            launch.socket_path,
            launch.state_path,
            launch.config_path,
        ):
            self.assertFalse(Path(value).exists())

    def test_cleanup_never_kills_unowned_window(self) -> None:
        manager = RecordingTmuxManager(self.config)
        launch = WorkerLaunch(
            worker_id="wrk_123456",
            socket_path="",
            state_path="",
            config_path="",
            tmux_session="ah-unit-abcd1234",
            tmux_window="reused-name-123456",
        )

        manager.cleanup_launch(launch)

        self.assertFalse(
            any(args[0] == "kill-window" for args, _ in manager.commands)
        )


if __name__ == "__main__":
    unittest.main()
