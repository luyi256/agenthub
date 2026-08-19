from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .naming import session_uid


WINDOW_ID_RE = re.compile(r"^@[0-9]+$")
NAME_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,40}$")
STATE_PRIORITY = {
    "blocked": 4,
    "red": 4,
    "done": 3,
    "yellow": 3,
    "busy": 2,
    "idle": 1,
}
GENERIC_WINDOW_NAMES = {
    "bash",
    "claude",
    "node",
    "python",
    "python3",
    "shell",
    "sh",
    "tcodex",
    "tclaude",
    "zsh",
    "[tmux]",
}


class GenTmuxService:
    """Narrow integration with the user's existing default-tmux `gen` session.

    The service never creates or respawns anything in the default tmux server.
    Mutations are limited to a verified live `gen` Agent window:
    - an Agent Hub-only display label (`@agenthub_name`);
    - the existing agent-attn manual marker (`@attn_manual`);
    - explicit user-requested closure of that exact window.
    Opening a window is handled by the VS Code integrated terminal; this
    backend never changes the active default-tmux client.
    """

    def __init__(
        self,
        *,
        session_name: str = "gen",
        tmux_socket_name: str | None = None,
        attn_dir: Path = Path.home() / "tools" / "agent-attn",
        cache_seconds: float = 2.5,
    ):
        self.session_name = session_name
        self.tmux_socket_name = tmux_socket_name
        self.attn_dir = attn_dir
        self.attn_state_path = attn_dir / "state" / "attn.json"
        self.detect_path = attn_dir / "detect.py"
        self.cache_seconds = cache_seconds
        self._cache_lock = threading.Lock()
        self._cache_at = 0.0
        self._cache: dict[str, Any] | None = None

    def snapshot(self, *, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        with self._cache_lock:
            if (
                not force
                and self._cache is not None
                and now - self._cache_at < self.cache_seconds
            ):
                return self._cache
            result = self._build_snapshot()
            self._cache = result
            self._cache_at = now
            return result

    def rename(self, window_id: str, name: str | None) -> dict[str, Any]:
        self._verify_window(window_id)
        normalized = (name or "").strip()
        if normalized:
            if not NAME_RE.fullmatch(normalized):
                raise ValueError("名称需为 1–40 个可见字符")
            result = self._tmux(
                "set-window-option",
                "-t",
                window_id,
                "@agenthub_name",
                normalized,
            )
        else:
            result = self._tmux(
                "set-window-option",
                "-t",
                window_id,
                "-u",
                "@agenthub_name",
            )
        self._ensure_success(result, "设置窗口名称")
        self._invalidate()
        return self._find_window(window_id)

    def set_attn(self, window_id: str, action: str) -> dict[str, Any]:
        self._verify_window(window_id)
        if action in {"red", "yellow"}:
            result = self._tmux(
                "set-window-option",
                "-t",
                window_id,
                "@attn_manual",
                action,
            )
        elif action == "clear":
            result = self._tmux(
                "set-window-option",
                "-t",
                window_id,
                "-u",
                "@attn_manual",
            )
        else:
            raise ValueError("action 仅支持 red、yellow 或 clear")
        self._ensure_success(result, "设置 tmux attn 标记")
        self._invalidate()
        return self._find_window(window_id)

    def get_window(self, window_id: str) -> dict[str, Any]:
        if not WINDOW_ID_RE.fullmatch(window_id):
            raise ValueError("非法 tmux window id")
        return self._find_window(window_id)

    def close_window(self, window_id: str) -> dict[str, Any]:
        window = self.get_window(window_id)
        self._verify_window(window_id)
        result = self._tmux(
            "kill-window",
            "-t",
            window_id,
        )
        self._ensure_success(result, "关闭 tmux gen 窗口")
        self._invalidate()
        return {
            "window_id": window_id,
            "closed": True,
            "session_uid": window.get("adopted_session_uid")
            or window.get("session_uid"),
        }

    def send_text(
        self,
        window_id: str,
        text: str,
        *,
        allow_busy: bool = False,
    ) -> dict[str, Any]:
        """Paste one user message into an exact Agent pane in tmux gen."""
        text = text.strip()
        if not text:
            raise ValueError("消息不能为空")
        window = self.get_window(window_id)
        allowed_states = {"idle", "done", "busy"} if allow_busy else {"idle", "done"}
        if window["state"] not in allowed_states:
            raise ValueError(
                "tmux gen 会话正在运行或等待交互，请先完成当前操作"
            )
        if not window.get("agent_present"):
            raise RuntimeError("原 tmux Agent 已退出，请刷新后重试")
        self._verify_window(window_id)
        agent_pgid = int(window.get("agent_pgid") or 0)
        if agent_pgid <= 1:
            raise RuntimeError("无法确定原 Agent 的前台进程组")
        current_pgid = self._foreground_pgid(window["pane_pid"])
        if current_pgid != agent_pgid:
            raise RuntimeError("Agent 前台进程已变化，请刷新后重试")
        buffer_name = f"agenthub-{uuid.uuid4().hex[:12]}"
        loaded = self._tmux_with_input(
            text,
            "load-buffer",
            "-b",
            buffer_name,
            "-",
        )
        self._ensure_success(loaded, "写入 tmux 消息缓冲区")
        pasted = self._tmux(
            "paste-buffer",
            "-b",
            buffer_name,
            "-t",
            window["pane_id"],
            "-d",
        )
        self._ensure_success(pasted, "粘贴消息到 tmux Agent")
        # Codex/Claude TUIs consume bracketed-paste asynchronously. Sending
        # Enter in the same scheduler tick can leave the text in the editor
        # without submitting it.
        time.sleep(0.5)
        submitted = self._tmux(
            "send-keys",
            "-t",
            window["pane_id"],
            "Enter",
        )
        self._ensure_success(submitted, "提交 tmux Agent 消息")
        self._invalidate()
        return {
            **window,
            "submitted": True,
            "delivery": "queued" if window["state"] == "busy" else "turn",
        }

    def bind_chat(
        self,
        window_id: str,
        *,
        session_uid_value: str,
        runtime: str,
        runtime_id: str,
    ) -> None:
        self._verify_window(window_id)
        for key, value in (
            ("@agenthub_session_uid", session_uid_value),
            ("@agenthub_runtime", runtime),
            ("@agenthub_runtime_id", runtime_id),
        ):
            result = self._tmux(
                "set-window-option",
                "-t",
                window_id,
                key,
                value,
            )
            self._ensure_success(result, "绑定图形 Chat")
        self._invalidate()

    def _find_window(self, window_id: str) -> dict[str, Any]:
        for window in self.snapshot(force=True)["windows"]:
            if window["window_id"] == window_id:
                return window
        raise RuntimeError("窗口已关闭或不再是 Agent session")

    def _verify_window(self, window_id: str) -> None:
        if not WINDOW_ID_RE.fullmatch(window_id):
            raise ValueError("非法 tmux window id")
        result = self._tmux(
            "display-message",
            "-p",
            "-t",
            window_id,
            "#{session_name}\t#{window_id}\t#{pane_dead}",
        )
        self._ensure_success(result, "读取 tmux 窗口")
        session_name, _, pane_dead = (
            result.stdout.rstrip("\n").split("\t", 2)
        )
        if session_name != self.session_name:
            raise ValueError("只允许操作 tmux gen 中的窗口")
        if pane_dead == "1":
            raise ValueError("该 tmux 窗口已经关闭")
        live_agent_ids = {
            window["window_id"]
            for window in self.snapshot(force=True)["windows"]
        }
        if window_id not in live_agent_ids:
            raise ValueError("该窗口不是 tmux gen 中正在运行的 Agent session")

    def _build_snapshot(self) -> dict[str, Any]:
        window_rows = self._window_rows()
        active_window_id = self._active_window_id()
        detected = self._detect_agent_panes()
        detected_by_pane = {
            item.get("pane"): item
            for item in detected
            if item.get("session") == self.session_name
        }
        attn_state = self._load_attn_state()
        styled = attn_state.get("styled_windows") or {}
        grouped: dict[str, dict[str, Any]] = {}

        for row in window_rows:
            adopted_session_uid = row["adopted_session_uid"].strip()
            if row["pane_dead"] == "1":
                continue
            agent = detected_by_pane.get(row["pane_id"])
            if not agent:
                continue
            if (
                agent.get("kind") in {"codex", "tcodex"}
                and not self._rollout_is_open(agent)
            ):
                # Before the first real prompt, detector fallback may find an
                # older same-cwd rollout. Relay requires the rollout currently
                # owned by this exact Codex process.
                continue
            window_id = row["window_id"]
            current = grouped.get(window_id)
            candidate_state = self._window_state(
                (agent or {}).get("state") or "idle",
                row["manual_attn"],
                styled.get(window_id),
            )
            if current is None:
                runtime = (
                    agent.get("kind") or row["adopted_runtime"] or "agent"
                )
                runtime_id = (
                    self._runtime_id(agent, runtime)
                    or row["adopted_runtime_id"]
                )
                if not runtime_id:
                    continue
                grouped[window_id] = {
                    "tmux_session": self.session_name,
                    "window_id": window_id,
                    "window_index": int(row["window_index"]),
                    "tmux_name": row["window_name"],
                    "display_name": self._display_name(row, agent),
                    "custom_name": row["custom_name"] or None,
                    "active": (
                        window_id == active_window_id
                        or row["window_active"] == "1"
                    ),
                    "pane_id": row["pane_id"],
                    "pane_pid": int(row["pane_pid"]),
                    "cwd": row["pane_current_path"],
                    "command": row["pane_current_command"],
                    "title": row["pane_title"],
                    "runtime": runtime,
                    "runtime_id": runtime_id,
                    "session_uid": session_uid(runtime, runtime_id),
                    "adopted_session_uid": adopted_session_uid or None,
                    "agent_present": True,
                    "pane_dead": False,
                    "rollout_path": agent.get("rollout_path"),
                    "agent_pid": agent.get("agent_pid"),
                    "agent_pgid": (
                        self._agent_pgid(agent, row)
                    ),
                    "permission_profile": self._permission_profile(
                        agent, row
                    ),
                    "state": candidate_state,
                    "manual_attn": row["manual_attn"] or None,
                    "attn_source": (
                        "manual"
                        if row["manual_attn"] in {"red", "yellow"}
                        else (
                            "automatic"
                            if candidate_state in {"blocked", "done"}
                            else None
                        )
                    ),
                    "pane_count": 1,
                }
                continue
            current["pane_count"] += 1
            if STATE_PRIORITY.get(candidate_state, 0) > STATE_PRIORITY.get(
                current["state"], 0
            ):
                current["state"] = candidate_state
                current["pane_id"] = row["pane_id"]
                current["pane_pid"] = int(row["pane_pid"])
                current["runtime"] = (
                    agent.get("kind") or current["runtime"]
                )
                current["title"] = row["pane_title"]

        windows = sorted(
            grouped.values(), key=lambda item: item["window_index"]
        )
        return {
            "tmux_session": self.session_name,
            "available": bool(window_rows),
            "windows": windows,
            "attn": {
                "available": self.detect_path.exists(),
                "state_file": str(self.attn_state_path),
                "poll_seconds": int(os.environ.get("ATTN_POLL", "3")),
                "hooked_server_pid": attn_state.get("hooked_server_pid"),
            },
        }

    @staticmethod
    def _runtime_id(agent: dict[str, Any], runtime: str) -> str | None:
        if runtime in {"claude", "tclaude"}:
            value = agent.get("sid")
            return str(value) if value else None
        rollout_path = agent.get("rollout_path")
        if runtime in {"codex", "tcodex"} and rollout_path:
            try:
                home = Path(
                    agent.get("codex_home")
                    or (
                        Path.home()
                        / (".tcodex" if runtime == "tcodex" else ".codex")
                    )
                )
                state_db = home / "state_5.sqlite"
                if state_db.is_file():
                    connection = sqlite3.connect(
                        f"file:{state_db}?mode=ro", uri=True
                    )
                    try:
                        rows = connection.execute(
                            """
                            SELECT id FROM threads
                            WHERE rollout_path = ? AND source = 'cli'
                            """,
                            (str(Path(rollout_path).resolve()),),
                        ).fetchall()
                    finally:
                        connection.close()
                    if len(rows) == 1 and rows[0][0]:
                        database_id = str(rows[0][0])
                        with Path(rollout_path).open(
                            errors="replace"
                        ) as source:
                            first = json.loads(source.readline())
                        payload = first.get("payload") or {}
                        file_id = payload.get("id")
                        if file_id and str(file_id) != database_id:
                            return None
                        return database_id
                with Path(rollout_path).open(errors="replace") as source:
                    first = json.loads(source.readline())
                payload = first.get("payload") or {}
                value = payload.get("id") or payload.get("session_id")
                return str(value) if value else None
            except Exception:
                return None
        return None

    def _agent_pgid(
        self,
        agent: dict[str, Any],
        row: dict[str, str],
    ) -> int | None:
        foreground = self._foreground_pgid(int(row["pane_pid"]))
        if foreground and foreground > 1:
            return foreground
        pid = int(agent.get("agent_pid") or row["pane_pid"])
        try:
            return os.getpgid(pid)
        except (ProcessLookupError, PermissionError):
            return None

    def _permission_profile(
        self,
        agent: dict[str, Any],
        row: dict[str, str],
    ) -> str:
        pids = {
            int(row["pane_pid"]),
            int(agent.get("agent_pid") or 0),
            int(self._foreground_pgid(int(row["pane_pid"])) or 0),
        }
        command = " ".join(
            self._cmdline(pid) for pid in pids if pid > 1
        )
        if any(
            flag in command
            for flag in (
                "--yolo",
                "--dangerously-skip-permissions",
                "--dangerously-bypass-approvals-and-sandbox",
            )
        ):
            return "full-access"
        return "safe"

    @staticmethod
    def _rollout_is_open(agent: dict[str, Any]) -> bool:
        pid = int(agent.get("agent_pid") or 0)
        raw_path = agent.get("rollout_path")
        if pid <= 1 or not raw_path:
            return False
        target = str(Path(raw_path).resolve())
        try:
            for fd in Path(f"/proc/{pid}/fd").iterdir():
                try:
                    if str(fd.resolve()) == target:
                        return True
                except (FileNotFoundError, OSError):
                    continue
        except (FileNotFoundError, PermissionError):
            return False
        return False

    @staticmethod
    def _cmdline(pid: int) -> str:
        try:
            return (
                Path(f"/proc/{pid}/cmdline")
                .read_bytes()
                .replace(b"\0", b" ")
                .decode(errors="replace")
            )
        except Exception:
            return ""

    @staticmethod
    def _foreground_pgid(pane_pid: int) -> int | None:
        try:
            fields = Path(f"/proc/{pane_pid}/stat").read_text().rsplit(")", 1)[
                1
            ].split()
            return int(fields[5])
        except Exception:
            return None

    def _active_window_id(self) -> str | None:
        result = self._tmux(
            "display-message",
            "-p",
            "-t",
            self.session_name,
            "#{window_id}",
        )
        if result.returncode != 0:
            return None
        value = result.stdout.strip()
        return value if WINDOW_ID_RE.fullmatch(value) else None

    def _window_rows(self) -> list[dict[str, str]]:
        fields = [
            "session_name",
            "window_id",
            "window_index",
            "window_name",
            "window_active",
            "@agenthub_name",
            "@attn_manual",
            "@agenthub_session_uid",
            "@agenthub_runtime",
            "@agenthub_runtime_id",
            "pane_id",
            "pane_dead",
            "pane_pid",
            "pane_current_path",
            "pane_current_command",
            "pane_title",
        ]
        fmt = "\t".join(f"#{{{field}}}" for field in fields)
        result = self._tmux(
            "list-panes",
            "-s",
            "-t",
            self.session_name,
            "-F",
            fmt,
        )
        if result.returncode != 0:
            return []
        rows: list[dict[str, str]] = []
        for line in result.stdout.splitlines():
            values = line.split("\t")
            if len(values) != len(fields):
                continue
            row = dict(zip(fields, values))
            if row.pop("session_name") != self.session_name:
                continue
            row["custom_name"] = row.pop("@agenthub_name")
            row["manual_attn"] = row.pop("@attn_manual")
            row["adopted_session_uid"] = row.pop(
                "@agenthub_session_uid"
            )
            row["adopted_runtime"] = row.pop("@agenthub_runtime")
            row["adopted_runtime_id"] = row.pop(
                "@agenthub_runtime_id"
            )
            rows.append(row)
        return rows

    def _detect_agent_panes(self) -> list[dict[str, Any]]:
        if not self.detect_path.exists():
            return []
        code = """
import importlib.util
import json
import sys

path = sys.argv[1]
socket_name = sys.argv[2] if len(sys.argv) > 2 else ""
spec = importlib.util.spec_from_file_location("agent_attn_detect", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
if socket_name:
    original_run = module.subprocess.run
    def socket_run(command, *args, **kwargs):
        if isinstance(command, list) and command and command[0] == "tmux":
            command = ["tmux", "-L", socket_name, *command[1:]]
        return original_run(command, *args, **kwargs)
    module.subprocess.run = socket_run
print(json.dumps(module.snapshot(), ensure_ascii=False))
"""
        try:
            result = subprocess.run(
                [
                    "env",
                    "-u",
                    "TMUX",
                    "python3",
                    "-c",
                    code,
                    str(self.detect_path),
                    self.tmux_socket_name or "",
                ],
                capture_output=True,
                text=True,
                timeout=8,
            )
            if result.returncode != 0:
                return []
            value = json.loads(result.stdout or "[]")
            return value if isinstance(value, list) else []
        except Exception:
            return []

    def _load_attn_state(self) -> dict[str, Any]:
        try:
            value = json.loads(self.attn_state_path.read_text())
            if not isinstance(value, dict):
                return {}
            server_pid = self._default_server_pid()
            hooked_pid = value.get("hooked_server_pid")
            if server_pid and hooked_pid and str(hooked_pid) != server_pid:
                # agent-attn is tmux-server-aware too. During the short window
                # after a default tmux restart, ignore styles saved for the old
                # server rather than showing stale blocked/done indicators.
                return {
                    "styled_windows": {},
                    "done_panes": {},
                    "hooked_server_pid": server_pid,
                }
            return value
        except Exception:
            return {}

    def _default_server_pid(self) -> str | None:
        result = self._tmux("display-message", "-p", "#{pid}")
        if result.returncode != 0:
            return None
        value = result.stdout.strip()
        return value if value.isdigit() else None

    @staticmethod
    def _window_state(
        detected: str,
        manual: str,
        styled: str | None,
    ) -> str:
        if manual == "red":
            return "blocked"
        if manual == "yellow":
            return "done"
        if styled in {"blocked", "red"}:
            return "blocked"
        if styled in {"done", "yellow"}:
            return "done"
        return detected if detected in {"blocked", "busy", "idle"} else "idle"

    @staticmethod
    def _display_name(
        row: dict[str, str],
        agent: dict[str, Any],
    ) -> str:
        custom = row["custom_name"].strip()
        if custom:
            return custom
        tmux_name = row["window_name"].strip()
        if tmux_name and tmux_name.lower() not in GENERIC_WINDOW_NAMES:
            return tmux_name
        title = re.sub(r"^[^\w\u4e00-\u9fff]+", "", row["pane_title"]).strip()
        if title and title.lower() not in {"generation", "claude code"}:
            return title[:40]
        runtime = agent.get("kind") or "agent"
        return f"{runtime}-{row['window_index']}"

    def _invalidate(self) -> None:
        with self._cache_lock:
            self._cache = None
            self._cache_at = 0.0

    def _tmux(self, *args: str) -> subprocess.CompletedProcess[str]:
        command = ["env", "-u", "TMUX", "tmux"]
        if self.tmux_socket_name:
            command += ["-L", self.tmux_socket_name]
        return subprocess.run(
            [*command, *args],
            capture_output=True,
            text=True,
            timeout=6,
        )

    def _tmux_with_input(
        self,
        text: str,
        *args: str,
    ) -> subprocess.CompletedProcess[str]:
        command = ["env", "-u", "TMUX", "tmux"]
        if self.tmux_socket_name:
            command += ["-L", self.tmux_socket_name]
        return subprocess.run(
            [*command, *args],
            input=text,
            capture_output=True,
            text=True,
            timeout=6,
        )

    @staticmethod
    def _ensure_success(
        result: subprocess.CompletedProcess[str],
        operation: str,
    ) -> None:
        if result.returncode == 0:
            return
        raise RuntimeError(
            result.stderr.strip()
            or result.stdout.strip()
            or f"{operation}失败"
        )
