from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import HubConfig
from .naming import compact_title


DISABLING_CLAUDE_ENV = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "DISABLE_TELEMETRY",
    "DO_NOT_TRACK",
    "DISABLE_GROWTHBOOK",
}
VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


@dataclass
class DiscoveryResult:
    records: list[dict[str, Any]]
    diagnostics: list[dict[str, Any]]
    scanned_runtimes: list[str]


class SessionDiscoverer:
    def __init__(self, config: HubConfig):
        self.config = config
        self._boot_time = self._read_boot_time()
        self._clock_ticks = os.sysconf(os.sysconf_names["SC_CLK_TCK"])

    def discover(self) -> DiscoveryResult:
        records: list[dict[str, Any]] = []
        diagnostics: list[dict[str, Any]] = []
        scanned: list[str] = []

        for runtime in ("claude", "tclaude"):
            try:
                runtime_records, runtime_diagnostics = self._discover_claude(runtime)
                records.extend(runtime_records)
                diagnostics.extend(runtime_diagnostics)
                scanned.append(runtime)
            except Exception as error:
                diagnostics.append(
                    {
                        "runtime": runtime,
                        "level": "error",
                        "message": f"扫描失败：{error}",
                    }
                )

        for runtime, home in (
            ("codex", self.config.codex_home),
            ("tcodex", self.config.tcodex_home),
        ):
            try:
                runtime_records, runtime_diagnostics = self._discover_codex(
                    runtime, home
                )
                records.extend(runtime_records)
                diagnostics.extend(runtime_diagnostics)
                scanned.append(runtime)
            except Exception as error:
                diagnostics.append(
                    {
                        "runtime": runtime,
                        "level": "error",
                        "message": f"扫描失败：{error}",
                    }
                )

        return DiscoveryResult(
            records=records,
            diagnostics=diagnostics,
            scanned_runtimes=scanned,
        )

    def _run_json(self, command: list[str], timeout: float = 5.0) -> Any:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                (completed.stderr or completed.stdout or "command failed").strip()
            )
        return json.loads(completed.stdout)

    def _version(self, runtime: str) -> str | None:
        command = [runtime, "--version"]
        if runtime == "tclaude":
            command = ["tclaude", "--version"]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        output = (completed.stdout + "\n" + completed.stderr).strip()
        if runtime == "tclaude":
            match = re.search(r"@anthropic-ai/claude-code\s+([0-9.]+)", output)
            if match:
                return match.group(1)
        if runtime == "tcodex":
            match = re.search(r"@openai/codex\s+([0-9.]+)", output)
            if match:
                return match.group(1)
        match = VERSION_RE.search(output)
        return match.group(0) if match else compact_title(output, 32)

    @staticmethod
    def _version_at_least(version: str | None, minimum: tuple[int, int, int]) -> bool:
        if not version:
            return False
        match = VERSION_RE.search(version)
        if not match:
            return False
        return tuple(map(int, match.groups())) >= minimum

    def _discover_claude(
        self, runtime: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        command = ["claude", "agents", "--json"]
        if runtime == "tclaude":
            command = ["tclaude", "--", "agents", "--json"]
        sessions = self._run_json(command)
        version = self._version(runtime)
        records: list[dict[str, Any]] = []
        disabled_count = 0
        for session in sessions:
            pid = session.get("pid")
            process_env = self._safe_process_env(pid)
            disabling = sorted(
                name
                for name in DISABLING_CLAUDE_ENV
                if self._truthy(process_env.get(name))
            )
            version_ok = self._version_at_least(version, (2, 1, 224))
            messageable = runtime == "claude" and version_ok and not disabling
            if disabling:
                disabled_count += 1
            runtime_id = session.get("sessionId") or f"process:{pid}"
            native_name = session.get("name")
            records.append(
                {
                    "runtime": runtime,
                    "runtime_id": runtime_id,
                    "runtime_version": version,
                    "native_name": native_name,
                    "discovered_title": native_name,
                    "cwd": session.get("cwd"),
                    "pid": pid,
                    "process_kind": session.get("kind") or "interactive",
                    "status": session.get("status") or "online",
                    "presence": "online",
                    "attach_state": "messageable" if messageable else "observable",
                    "capabilities": {
                        "observable": True,
                        "native_peer_messaging": messageable,
                        "full_stream": False,
                        "requires_handover": not messageable,
                    },
                    "metadata": {
                        "started_at_ms": session.get("startedAt"),
                        "messaging_disabled_by": disabling,
                        "version_supports_native_messaging": version_ok,
                    },
                }
            )
        diagnostics = [
            {
                "runtime": runtime,
                "level": "info",
                "message": f"发现 {len(records)} 个在线 session",
            }
        ]
        if disabled_count:
            diagnostics.append(
                {
                    "runtime": runtime,
                    "level": "warning",
                    "message": (
                        f"{disabled_count} 个 session 的原生跨会话消息被环境变量关闭"
                    ),
                }
            )
        if runtime == "tclaude" and not self._version_at_least(version, (2, 1, 224)):
            diagnostics.append(
                {
                    "runtime": runtime,
                    "level": "warning",
                    "message": f"Claude Code {version or 'unknown'} 低于跨会话消息要求",
                }
            )
        return records, diagnostics

    def _discover_codex(
        self, runtime: str, home: Path
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        state_path = home / "state_5.sqlite"
        if not state_path.exists():
            return [], [
                {
                    "runtime": runtime,
                    "level": "warning",
                    "message": f"未找到 {state_path}",
                }
            ]
        rows = self._read_codex_threads(state_path)
        processes = self._runtime_processes(runtime)
        claimed_thread_ids: set[str] = set()
        records: list[dict[str, Any]] = []
        version = self._version(runtime)

        for process in processes:
            matched = self._match_process_to_thread(process, rows, claimed_thread_ids)
            if matched:
                claimed_thread_ids.add(str(matched["id"]))
                records.append(
                    self._codex_record(
                        runtime,
                        version,
                        matched,
                        presence="online",
                        status="online",
                        pid=process["pid"],
                        attach_state="handover_required",
                        process=process,
                    )
                )
            else:
                runtime_id = f"process:{process['pid']}"
                records.append(
                    {
                        "runtime": runtime,
                        "runtime_id": runtime_id,
                        "runtime_version": version,
                        "native_name": None,
                        "discovered_title": f"{runtime} process {process['pid']}",
                        "cwd": process.get("cwd"),
                        "pid": process["pid"],
                        "process_kind": "interactive",
                        "status": "online",
                        "presence": "online",
                        "attach_state": "observable",
                        "capabilities": {
                            "observable": True,
                            "thread_id_known": False,
                            "full_stream": False,
                            "requires_handover": True,
                        },
                        "metadata": {
                            "process_started_at": process.get("started_at"),
                            "tty": process.get("tty"),
                            "mapping_confidence": "unmatched",
                        },
                    }
                )

        history_count = 0
        for row in rows:
            thread_id = str(row["id"])
            if thread_id in claimed_thread_ids:
                continue
            source = str(row.get("source") or "")
            if source not in {"cli", "vscode"}:
                continue
            records.append(
                self._codex_record(
                    runtime,
                    version,
                    row,
                    presence="history",
                    status="offline",
                    pid=None,
                    attach_state="offline",
                    process=None,
                )
            )
            history_count += 1
            if history_count >= self.config.codex_history_limit:
                break

        return records, [
            {
                "runtime": runtime,
                "level": "info",
                "message": (
                    f"发现 {len(processes)} 个在线进程，加载 {history_count} 条历史 thread"
                ),
            }
        ]

    def _codex_record(
        self,
        runtime: str,
        version: str | None,
        row: dict[str, Any],
        *,
        presence: str,
        status: str,
        pid: int | None,
        attach_state: str,
        process: dict[str, Any] | None,
    ) -> dict[str, Any]:
        preview = row.get("name") or row.get("title") or row.get("preview")
        metadata = {
            "source": row.get("source"),
            "model_provider": row.get("model_provider"),
            "model": row.get("model"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "recency_at": row.get("recency_at"),
            "rollout_path": row.get("rollout_path"),
            "mapping_confidence": "process_start" if process else "history",
        }
        if process:
            metadata.update(
                {
                    "process_started_at": process.get("started_at"),
                    "tty": process.get("tty"),
                }
            )
        return {
            "runtime": runtime,
            "runtime_id": str(row["id"]),
            "runtime_version": row.get("cli_version") or version,
            "native_name": row.get("name"),
            "discovered_title": compact_title(preview),
            "cwd": row.get("cwd"),
            "pid": pid,
            "process_kind": "interactive",
            "status": status,
            "presence": presence,
            "attach_state": attach_state,
            "capabilities": {
                "observable": True,
                "thread_id_known": True,
                "full_stream": False,
                "requires_handover": presence == "online",
                "can_resume_when_closed": True,
            },
            "metadata": metadata,
        }

    def _read_codex_threads(self, path: Path) -> list[dict[str, Any]]:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
        connection.row_factory = sqlite3.Row
        try:
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(threads)").fetchall()
            }
            wanted = [
                "id",
                "rollout_path",
                "created_at",
                "updated_at",
                "recency_at",
                "source",
                "model_provider",
                "model",
                "cwd",
                "title",
                "preview",
                "archived",
                "cli_version",
                "name",
            ]
            selected = [column for column in wanted if column in columns]
            order = "recency_at" if "recency_at" in columns else "updated_at"
            archived_clause = "WHERE archived = 0" if "archived" in columns else ""
            rows = connection.execute(
                f"""
                SELECT {', '.join(selected)}
                FROM threads
                {archived_clause}
                ORDER BY {order} DESC
                LIMIT ?
                """,
                (max(self.config.codex_history_limit * 4, 200),),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def _runtime_processes(self, runtime: str) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            try:
                raw = (entry / "cmdline").read_bytes()
                args = [part.decode(errors="replace") for part in raw.split(b"\0") if part]
                if not args:
                    continue
                command = " ".join(args[:4])
                launch_names = {Path(token).name for token in args[:3]}
                if runtime == "tcodex":
                    is_wrapper = bool(
                        launch_names.intersection({"tcodex", "tcodex-bin"})
                    )
                else:
                    is_wrapper = bool(
                        launch_names.intersection({"codex", "codex-bin"})
                    ) and not any("tcodex" in token for token in args[:4])
                if not is_wrapper:
                    continue
                cwd = os.readlink(entry / "cwd")
                tty = self._process_tty(entry)
                matches.append(
                    {
                        "pid": pid,
                        "cwd": cwd,
                        "started_at": self._process_start_time(entry),
                        "tty": tty,
                        "command": command,
                    }
                )
            except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
                continue
        matches.sort(key=lambda item: item["started_at"])
        return matches

    def _match_process_to_thread(
        self,
        process: dict[str, Any],
        rows: list[dict[str, Any]],
        claimed: set[str],
    ) -> dict[str, Any] | None:
        best: tuple[float, dict[str, Any]] | None = None
        for row in rows:
            thread_id = str(row["id"])
            if thread_id in claimed or row.get("cwd") != process.get("cwd"):
                continue
            if str(row.get("source") or "") != "cli":
                continue
            created_at = row.get("created_at")
            if not isinstance(created_at, (int, float)):
                continue
            distance = abs(float(created_at) - float(process["started_at"]))
            if distance <= 20 and (best is None or distance < best[0]):
                best = (distance, row)
        return best[1] if best else None

    def _process_start_time(self, proc_path: Path) -> float:
        parts = (proc_path / "stat").read_text().split()
        start_ticks = int(parts[21])
        return self._boot_time + start_ticks / self._clock_ticks

    @staticmethod
    def _process_tty(proc_path: Path) -> str | None:
        try:
            target = os.readlink(proc_path / "fd" / "0")
        except OSError:
            return None
        return target if target.startswith("/dev/") else None

    @staticmethod
    def _read_boot_time() -> float:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
        return time.time()

    @staticmethod
    def _safe_process_env(pid: int | None) -> dict[str, str]:
        if not pid:
            return {}
        try:
            raw = Path(f"/proc/{pid}/environ").read_bytes()
        except (OSError, PermissionError):
            return {}
        values: dict[str, str] = {}
        for item in raw.split(b"\0"):
            if b"=" not in item:
                continue
            key, value = item.split(b"=", 1)
            values[key.decode(errors="ignore")] = value.decode(errors="replace")
        return values

    @staticmethod
    def _truthy(value: str | None) -> bool:
        return (value or "").strip().lower() in {"1", "true", "yes", "on"}
