from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import HubConfig
from .naming import ascii_slug


@dataclass(frozen=True)
class WorkspaceIdentity:
    workspace_id: str
    name: str
    cwd: str
    tmux_session: str


@dataclass(frozen=True)
class WorkerLaunch:
    worker_id: str
    socket_path: str
    state_path: str
    config_path: str
    tmux_session: str
    tmux_window: str


class ProjectTmuxManager:
    def __init__(self, config: HubConfig):
        self.config = config
        self.tmux_socket_name = config.tmux_socket_name
        self.data_dir = config.db_path.parent
        self.worker_dir = self.data_dir / "workers"
        self.run_dir = self.data_dir / "run"
        self.state_dir = self.data_dir / "worker-state"
        self.lock_dir = Path.home() / ".agenthub" / "locks"
        for path in (
            self.worker_dir,
            self.run_dir,
            self.state_dir,
            self.lock_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        self._workspace_lock = threading.RLock()

    @staticmethod
    def identify_workspace(
        cwd: str,
        *,
        workspace_id: str | None = None,
        workspace_name: str | None = None,
    ) -> WorkspaceIdentity:
        canonical = str(Path(cwd).expanduser().resolve())
        digest = (
            re.sub(r"[^a-f0-9]", "", (workspace_id or "").lower())[:10]
            or hashlib.sha256(canonical.encode()).hexdigest()[:10]
        )
        name = workspace_name or Path(canonical).name or "workspace"
        slug = ascii_slug(name, fallback="workspace", max_length=30)
        return WorkspaceIdentity(
            workspace_id=digest,
            name=name,
            cwd=canonical,
            tmux_session=f"ah-{slug}-{digest[:8]}",
        )

    def ensure_workspace(self, workspace: WorkspaceIdentity) -> None:
        with self._workspace_lock:
            if self._has_session(workspace.tmux_session):
                return
            keeper = (
                "printf '\\033[1;36mAgent Hub workspace\\033[0m\\n"
                f"name: {self._display_escape(workspace.name)}\\n"
                f"cwd: {self._display_escape(workspace.cwd)}\\n"
                "This tmux session is managed by the Agent Hub VS Code extension.\\n'; "
                "exec tail -f /dev/null"
            )
            completed = self._tmux(
                [
                    "new-session",
                    "-d",
                    "-s",
                    workspace.tmux_session,
                    "-n",
                    "hub",
                    "-c",
                    workspace.cwd,
                    keeper,
                ],
                check=False,
            )
            if completed.returncode != 0 and not self._has_session(
                workspace.tmux_session
            ):
                raise RuntimeError(
                    completed.stderr.strip()
                    or f"无法创建 tmux session {workspace.tmux_session}"
                )
            self._tmux(
                [
                    "set-window-option",
                    "-t",
                    f"{workspace.tmux_session}:hub",
                    "remain-on-exit",
                    "on",
                ],
                check=False,
            )

    def launch_worker(
        self,
        *,
        runtime: str,
        cwd: str,
        native_name: str,
        permission_profile: str,
        workspace_id: str | None,
        workspace_name: str | None,
        resume_runtime_id: str | None = None,
    ) -> WorkerLaunch:
        workspace = self.identify_workspace(
            cwd,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
        )
        self.ensure_workspace(workspace)
        worker_id = f"wrk_{uuid.uuid4().hex}"
        short = worker_id[-6:]
        window_name = self._window_name(native_name, short)
        socket_path = self.run_dir / f"{worker_id}.sock"
        state_path = self.state_dir / f"{worker_id}.json"
        config_path = self.worker_dir / f"{worker_id}.json"
        launch = WorkerLaunch(
            worker_id=worker_id,
            socket_path=str(socket_path),
            state_path=str(state_path),
            config_path=str(config_path),
            tmux_session=workspace.tmux_session,
            tmux_window=window_name,
        )
        worker_config: dict[str, Any] = {
            "worker_id": worker_id,
            "runtime": runtime,
            "cwd": workspace.cwd,
            "native_name": native_name,
            "permission_profile": permission_profile,
            "resume_runtime_id": resume_runtime_id,
            "socket_path": str(socket_path),
            "state_path": str(state_path),
            "workspace_id": workspace.workspace_id,
            "workspace_name": workspace.name,
            "tmux_session": workspace.tmux_session,
            "tmux_window": window_name,
            "startup_lock_path": str(
                self.lock_dir / f"{runtime}-startup.lock"
            ),
        }
        python = os.environ.get(
            "AGENTHUB_PYTHON",
            "/home/luyi/creative-agent/creative-agent-mcp/.venv/bin/python",
        )
        package_root = str(Path(__file__).resolve().parent.parent)
        command = " ".join(
            shlex.quote(part)
            for part in [
                "env",
                f"PYTHONPATH={package_root}",
                python,
                "-m",
                "agent_hub.session_worker",
                "--config",
                str(config_path),
            ]
        )
        window_created = False
        try:
            self._atomic_json(config_path, worker_config)
            self._tmux(
                [
                    "new-window",
                    "-d",
                    "-t",
                    workspace.tmux_session,
                    "-n",
                    window_name,
                    "-c",
                    workspace.cwd,
                    command,
                ]
            )
            window_created = True
            self._tmux(
                [
                    "set-window-option",
                    "-q",
                    "-t",
                    f"{workspace.tmux_session}:{window_name}",
                    "@agenthub_worker_id",
                    worker_id,
                ]
            )
            self._tmux(
                [
                    "set-window-option",
                    "-t",
                    f"{workspace.tmux_session}:{window_name}",
                    "remain-on-exit",
                    "on",
                ],
                check=False,
            )
            return launch
        except Exception:
            with contextlib.suppress(Exception):
                if window_created or self.worker_window_owned(launch):
                    self.kill_worker_window(
                        launch.tmux_session,
                        launch.tmux_window,
                    )
            self._cleanup_worker_files(launch)
            raise

    def rename_window(self, launch: WorkerLaunch, alias: str) -> str:
        new_name = self._window_name(alias, launch.worker_id[-6:])
        self._tmux(
            [
                "rename-window",
                "-t",
                f"{launch.tmux_session}:{launch.tmux_window}",
                new_name,
            ],
            check=False,
        )
        return new_name

    def kill_worker_window(self, tmux_session: str, tmux_window: str) -> None:
        target = (
            tmux_window
            if tmux_window.startswith("@")
            else f"{tmux_session}:{tmux_window}"
        )
        self._tmux(
            [
                "kill-window",
                "-t",
                target,
            ],
            check=False,
        )

    def cleanup_launch(self, launch: WorkerLaunch) -> None:
        target = self._owned_worker_window_target(launch)
        if target:
            self.kill_worker_window(launch.tmux_session, target)
        self._cleanup_worker_files(launch)

    def worker_window_owned(self, launch: WorkerLaunch) -> bool:
        return self._owned_worker_window_target(launch) is not None

    def _owned_worker_window_target(self, launch: WorkerLaunch) -> str | None:
        if not launch.tmux_session or not launch.tmux_window:
            return None
        completed = self._tmux(
            [
                "display-message",
                "-p",
                "-t",
                f"{launch.tmux_session}:{launch.tmux_window}",
                "#{@agenthub_worker_id}\t#{window_name}",
            ],
            check=False,
        )
        if completed.returncode == 0:
            owner, _, actual_name = completed.stdout.strip().partition("\t")
            if owner == launch.worker_id:
                return launch.tmux_window
            if (
                not owner
                and launch.tmux_session.startswith("ah-")
                and actual_name == launch.tmux_window
                and actual_name.endswith(f"-{launch.worker_id[-6:]}")
            ):
                return launch.tmux_window
        listed = self._tmux(
            [
                "list-windows",
                "-t",
                launch.tmux_session,
                "-F",
                "#{window_id}\t#{@agenthub_worker_id}",
            ],
            check=False,
        )
        if listed.returncode == 0:
            for line in listed.stdout.splitlines():
                window_id, _, owner = line.partition("\t")
                if window_id and owner == launch.worker_id:
                    return window_id
        return None

    @staticmethod
    def _cleanup_worker_files(launch: WorkerLaunch) -> None:
        for value in (
            launch.socket_path,
            launch.state_path,
            launch.config_path,
        ):
            if not value:
                continue
            path = Path(value)
            candidates = {
                path,
                path.with_suffix(path.suffix + ".tmp"),
                path.with_suffix(".tmp"),
            }
            for candidate in candidates:
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    pass

    def list_workspace_windows(self, tmux_session: str) -> list[dict[str, str]]:
        completed = self._tmux(
            [
                "list-windows",
                "-t",
                tmux_session,
                "-F",
                "#{window_index}\t#{window_name}\t#{pane_current_path}\t#{pane_dead}",
            ],
            check=False,
        )
        if completed.returncode != 0:
            return []
        rows = []
        for line in completed.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 4:
                rows.append(
                    {
                        "index": parts[0],
                        "name": parts[1],
                        "cwd": parts[2],
                        "dead": parts[3],
                    }
                )
        return rows

    def _has_session(self, name: str) -> bool:
        return (
            subprocess.run(
                [
                    "env",
                    "-u",
                    "TMUX",
                    "tmux",
                    "-L",
                    self.tmux_socket_name,
                    "has-session",
                    "-t",
                    name,
                ],
                capture_output=True,
                text=True,
            ).returncode
            == 0
        )

    def _tmux(
        self, args: list[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return self._run(
            [
                "env",
                "-u",
                "TMUX",
                "tmux",
                "-L",
                self.tmux_socket_name,
                *args,
            ],
            check=check,
        )

    @staticmethod
    def _run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=check,
        )

    @staticmethod
    def _window_name(value: str, short: str) -> str:
        slug = ascii_slug(value, fallback="chat", max_length=22)
        return f"{slug}-{short}"

    @staticmethod
    def _display_escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace("'", "'\\''")

    @staticmethod
    def _atomic_json(path: Path, value: dict[str, Any]) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(path)
