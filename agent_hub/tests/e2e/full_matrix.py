#!/usr/bin/env python3
"""Destructive Agent Hub E2E tests, isolated from the user's default tmux.

Run only with:
    AGENTHUB_E2E_ALLOW=1 python -m agent_hub.tests.e2e.full_matrix

Every tmux command is pinned to a random `-L ah-e2e-*` socket and clears TMUX.
The suite records the user's default tmux server PID and verifies it is unchanged.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
PYTHON = Path(
    os.environ.get(
        "AGENTHUB_PYTHON",
        "/home/luyi/creative-agent/creative-agent-mcp/.venv/bin/python",
    )
)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


class E2E:
    def __init__(self) -> None:
        if os.environ.get("AGENTHUB_E2E_ALLOW") != "1":
            raise RuntimeError("Set AGENTHUB_E2E_ALLOW=1 to run E2E tests")
        self.stamp = f"{int(time.time())}-{os.getpid()}"
        self.base = Path(
            tempfile.mkdtemp(prefix=f"agenthub-e2e-{self.stamp}-")
        )
        self.db = self.base / "hub.sqlite3"
        self.port = self._free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.socket_name = f"ah-e2e-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        if self.socket_name in {"default", "agenthub"}:
            raise RuntimeError("unsafe tmux socket")
        self.server: subprocess.Popen[bytes] | None = None
        self.server_log = self.base / "server.log"
        self.checks: list[Check] = []
        self.created: list[dict[str, Any]] = []
        self.default_tmux_pid_before = self._default_tmux_server_pid()

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    @staticmethod
    def _default_tmux_server_pid() -> int | None:
        output = subprocess.run(
            ["lsof", "-t", "/tmp/tmux-1000/default"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.splitlines()
        return int(output[0]) if output else None

    def tmux(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        if self.socket_name in {"default", "agenthub"}:
            raise RuntimeError("refusing unsafe tmux socket")
        return subprocess.run(
            [
                "env",
                "-u",
                "TMUX",
                "tmux",
                "-L",
                self.socket_name,
                *args,
            ],
            capture_output=True,
            text=True,
            check=check,
        )

    def add(self, name: str, ok: bool, detail: Any = "") -> None:
        check = Check(name=name, ok=bool(ok), detail=str(detail))
        self.checks.append(check)
        print(("PASS" if check.ok else "FAIL"), name, check.detail, flush=True)

    def start_server(
        self, *, restricted_path: bool = False, new_base: Path | None = None
    ) -> None:
        if self.server is not None:
            raise RuntimeError("server already running")
        base = new_base or self.base
        env = os.environ.copy()
        env.pop("TMUX", None)
        env.update(
            {
                "AGENTHUB_DB": str(base / "hub.sqlite3"),
                "AGENTHUB_TMUX_SOCKET": self.socket_name,
                "AGENTHUB_ENABLE_PUBLIC_RUNTIMES": "0",
                "PYTHONPATH": str(ROOT),
            }
        )
        if restricted_path:
            env["PATH"] = "/usr/bin:/bin"
        log = self.server_log.open("ab")
        self.server = subprocess.Popen(
            [
                str(PYTHON),
                "-m",
                "agent_hub.cli",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--scan-interval",
                "30",
            ],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self._wait_health()

    def stop_server(self) -> None:
        if self.server and self.server.poll() is None:
            self.server.terminate()
            try:
                self.server.wait(8)
            except subprocess.TimeoutExpired:
                self.server.kill()
                self.server.wait()
        self.server = None

    def _wait_health(self, timeout: float = 25) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                status, body = self.request("GET", "/api/health", timeout=2)
                if status == 200 and body.get("ok"):
                    return
            except Exception:
                pass
            time.sleep(0.15)
        raise RuntimeError("server health timeout")

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        timeout: float = 150,
    ) -> tuple[int, dict[str, Any]]:
        payload = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.url + path,
            data=payload,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            raw = error.read().decode(errors="replace")
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"raw": raw}
            return error.code, parsed

    def create(
        self,
        runtime: str,
        alias: str,
        *,
        permission: str = "read-only",
        workspace_id: str | None = None,
        workspace_name: str | None = None,
        cwd: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        status, body = self.request(
            "POST",
            "/api/managed-sessions",
            {
                "runtime": runtime,
                "cwd": cwd or str(ROOT),
                "alias": alias,
                "title": alias,
                "role": "e2e",
                "permission_profile": permission,
                "workspace_id": workspace_id or f"e2e{self.stamp}",
                "workspace_name": workspace_name or f"ah-e2e-main-{self.stamp}",
                "use_tmux": True,
            },
        )
        if status == 201:
            self.created.append(body)
        return status, body

    def messages(self, uid: str) -> dict[str, Any]:
        return self.request("GET", f"/api/sessions/{uid}/messages")[1]

    def send(self, uid: str, text: str) -> tuple[int, dict[str, Any]]:
        return self.request(
            "POST",
            f"/api/sessions/{uid}/messages",
            {"text": text},
            timeout=30,
        )

    def wait_reply(
        self, uid: str, expected: str | None = None, timeout: float = 180
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        deadline = time.monotonic() + timeout
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            data = self.messages(uid)
            assistants = [
                item for item in data["messages"] if item["role"] == "assistant"
            ]
            last = assistants[-1] if assistants else None
            if last and last["status"] != "streaming":
                if expected and expected not in last["content"]:
                    raise AssertionError(
                        f"expected {expected!r}, got {last!r}"
                    )
                return last, data
            time.sleep(0.35)
        raise TimeoutError(f"reply timeout: {last!r}")

    def wait_approval(self, uid: str, timeout: float = 100) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = self.messages(uid)
            if data["approvals"]:
                return data["approvals"][0]
            assistants = [
                item for item in data["messages"] if item["role"] == "assistant"
            ]
            if assistants and assistants[-1]["status"] != "streaming":
                raise AssertionError(
                    f"turn ended without approval: {assistants[-1]!r}"
                )
            time.sleep(0.3)
        raise TimeoutError("approval timeout")

    def stop_worker(self, session: dict[str, Any]) -> None:
        socket_path = (session.get("managed_config") or {}).get("socket_path")
        if not socket_path or not Path(socket_path).exists():
            return
        code = (
            "import asyncio,json,sys\n"
            "async def main():\n"
            " r,w=await asyncio.open_unix_connection(sys.argv[1])\n"
            " w.write((json.dumps({'id':1,'method':'stop','params':{}})+'\\n').encode())\n"
            " await w.drain(); await asyncio.wait_for(r.readline(),3)\n"
            " w.close(); await w.wait_closed()\n"
            "asyncio.run(main())\n"
        )
        subprocess.run(
            [sys.executable, "-c", code, socket_path],
            timeout=7,
            check=False,
        )

    def run(self) -> None:
        self.start_server()
        status, error = self.request(
            "POST",
            "/api/managed-sessions",
            {
                "runtime": "tcodex",
                "cwd": f"/tmp/does-not-exist-{uuid.uuid4().hex}",
                "alias": f"e2e/invalid-{self.stamp}",
                "permission_profile": "safe",
                "workspace_id": self.stamp,
                "workspace_name": f"ah-e2e-invalid-{self.stamp}",
                "use_tmux": True,
            },
        )
        self.add(
            "invalid cwd returns JSON 400",
            status == 400 and isinstance(error.get("error"), str),
            error,
        )

        for runtime in ("claude", "codex"):
            status, error = self.create(
                runtime, f"e2e/public-{runtime}-{self.stamp}"
            )
            self.add(
                f"public {runtime} rejected until explicitly enabled",
                status == 400 and "尚未" in error.get("error", ""),
                error,
            )

        runtime_sessions: dict[str, dict[str, Any]] = {}
        for runtime in ("tclaude", "tcodex"):
            status, session = self.create(
                runtime, f"e2e/{runtime}-{self.stamp}"
            )
            self.add(
                f"{runtime} create",
                status == 201,
                session.get("runtime_id") or session.get("error"),
            )
            if status != 201:
                continue
            runtime_sessions[runtime] = session
            for turn in (1, 2):
                token = f"{runtime.upper()}-TURN-{turn}-OK"
                self.send(
                    session["session_uid"],
                    f"只回复 {token}，不要其他内容。",
                )
                reply, _ = self.wait_reply(
                    session["session_uid"], token, 180
                )
                self.add(
                    f"{runtime} turn {turn}",
                    reply["status"] == "completed",
                    reply["content"],
                )

        duplicate_alias = f"e2e/duplicate-{self.stamp}"
        status, first = self.create("tclaude", duplicate_alias)
        if status != 201:
            raise AssertionError(first)
        tmux_session = first["managed_config"]["tmux_session"]
        windows_before = self.tmux(
            "list-windows", "-t", tmux_session, "-F", "#{window_name}"
        ).stdout.splitlines()
        status, error = self.create("tcodex", duplicate_alias)
        time.sleep(0.5)
        windows_after = self.tmux(
            "list-windows", "-t", tmux_session, "-F", "#{window_name}"
        ).stdout.splitlines()
        self.add(
            "duplicate alias returns 409 without worker leak",
            status == 409 and windows_before == windows_after,
            error,
        )

        concurrent_id = f"e2e-concurrent-{self.stamp}"
        concurrent_name = f"ah-e2e-concurrent-{self.stamp}"

        def create_concurrent(index: int) -> tuple[int, dict[str, Any]]:
            return self.create(
                "tcodex",
                f"e2e/concurrent-{self.stamp}-{index}",
                workspace_id=concurrent_id,
                workspace_name=concurrent_name,
            )

        started = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            concurrent_results = list(
                pool.map(create_concurrent, range(4))
            )
        elapsed = time.monotonic() - started
        self.add(
            "same-workspace four tcodex concurrent create",
            all(status == 201 for status, _ in concurrent_results),
            f"{elapsed:.2f}s {[status for status, _ in concurrent_results]}",
        )
        if all(status == 201 for status, _ in concurrent_results):
            session_name = concurrent_results[0][1]["managed_config"][
                "tmux_session"
            ]
            windows = self.tmux(
                "list-windows",
                "-t",
                session_name,
                "-F",
                "#{window_name}",
            ).stdout.splitlines()
            self.add(
                "concurrent workspace has one hub plus four workers",
                len(windows) == 5 and windows.count("hub") == 1,
                windows,
            )
            for index, (_, session) in enumerate(concurrent_results):
                token = f"CONCURRENT-{index}-OK"
                self.send(session["session_uid"], f"只回复 {token}。")
                reply, _ = self.wait_reply(
                    session["session_uid"], token, 180
                )
                self.add(
                    f"concurrent session {index} chats",
                    reply["status"] == "completed",
                    reply["content"],
                )

        status, approval_session = self.create(
            "tcodex",
            f"e2e/approval-{self.stamp}",
            permission="safe",
        )
        if status == 201:
            target = Path.home() / f"agenthub-e2e-{self.stamp}.tmp"
            target.unlink(missing_ok=True)
            self.send(
                approval_session["session_uid"],
                (
                    "You must call the exec_command tool exactly once to run: "
                    f"touch {target} . Set sandbox_permissions to "
                    "require_escalated so a human approval is requested. "
                    "Do not use another command or alternate path. If denied, "
                    "reply exactly APPROVAL-DENIED."
                ),
            )
            approval = self.wait_approval(approval_session["session_uid"])
            self.request(
                "POST",
                f"/api/approvals/{approval['approval_id']}",
                {"action": "decline"},
            )
            reply, _ = self.wait_reply(
                approval_session["session_uid"], timeout=180
            )
            self.add(
                "approval deny prevents command",
                not target.exists(),
                reply["content"],
            )
            self.send(
                approval_session["session_uid"],
                (
                    "You must call the exec_command tool exactly once to run: "
                    f"touch {target} . Set sandbox_permissions to "
                    "require_escalated so a human approval is requested. "
                    "Do not use another command or alternate path. If allowed "
                    "and the command succeeds, reply exactly APPROVAL-ALLOW-OK."
                ),
            )
            approval = self.wait_approval(approval_session["session_uid"])
            self.request(
                "POST",
                f"/api/approvals/{approval['approval_id']}",
                {"action": "accept"},
            )
            reply, _ = self.wait_reply(
                approval_session["session_uid"],
                "APPROVAL-ALLOW-OK",
                180,
            )
            self.add(
                "approval allow resumes command",
                target.exists(),
                reply["content"],
            )
            target.unlink(missing_ok=True)
        else:
            self.add("approval session create", False, approval_session)

        offline = runtime_sessions.get("tclaude")
        if offline:
            send_status, pending_message = self.send(
                offline["session_uid"],
                "请等待6秒，然后只回复 OFFLINE-REPLAY-OK。",
            )
            if send_status != 202:
                raise AssertionError(pending_message)
            expected_message_id = pending_message["message_id"]
            time.sleep(1)
            self.stop_server()
            state_path = Path(offline["managed_config"]["state_path"])
            deadline = time.monotonic() + 120
            state: dict[str, Any] = {}
            while time.monotonic() < deadline:
                state = json.loads(state_path.read_text())
                completion = state.get("last_completion") or {}
                if completion.get("message_id") == expected_message_id:
                    break
                time.sleep(0.5)
            self.add(
                "worker completes while coordinator is offline",
                (
                    state.get("last_completion", {})
                    .get("text", "")
                    .strip()
                    == "OFFLINE-REPLAY-OK"
                ),
                state.get("status"),
            )
            self.start_server()
            reply, _ = self.wait_reply(
                offline["session_uid"], "OFFLINE-REPLAY-OK", 30
            )
            self.add(
                "coordinator replays offline completion",
                reply["status"] == "completed",
                reply["content"],
            )

        stopped = runtime_sessions.get("tclaude")
        if stopped:
            self.stop_worker(stopped)
            time.sleep(2)
            self.stop_server()
            self.start_server()
            time.sleep(2)
            snapshot = self.request("GET", "/api/snapshot")[1]
            current = next(
                session
                for session in snapshot["sessions"]
                if session["session_uid"] == stopped["session_uid"]
            )
            self.add(
                "restart does not relaunch stopped history",
                current["presence"] == "offline" and current["pid"] is None,
                {
                    "presence": current["presence"],
                    "status": current["status"],
                    "pid": current["pid"],
                },
            )

        with sqlite3.connect(self.db) as connection:
            counts = {
                table: connection.execute(
                    f"SELECT count(*) FROM {table}"
                ).fetchone()[0]
                for table in ("sessions", "messages", "approvals")
            }
        self.add(
            "database records messages and sessions",
            counts["sessions"] >= 8 and counts["messages"] >= 12,
            counts,
        )

        self._failure_cleanup_test()

    def _failure_cleanup_test(self) -> None:
        self.stop_server()
        failure_base = self.base / "failure"
        failure_base.mkdir()
        original_db = self.db
        self.db = failure_base / "hub.sqlite3"
        self.start_server(restricted_path=True, new_base=failure_base)
        status, error = self.create(
            "tcodex",
            f"e2e/failure-{self.stamp}",
            workspace_id=f"failure-{self.stamp}",
            workspace_name=f"ah-e2e-failure-{self.stamp}",
        )
        time.sleep(1)
        sessions = self.tmux(
            "list-sessions", "-F", "#{session_name}", check=False
        ).stdout.splitlines()
        failure_sessions = [
            name for name in sessions if "failure" in name
        ]
        windows: list[str] = []
        for name in failure_sessions:
            windows.extend(
                self.tmux(
                    "list-windows",
                    "-t",
                    name,
                    "-F",
                    "#{window_name}",
                    check=False,
                ).stdout.splitlines()
            )
        leaked_sockets = list((failure_base / "run").glob("*.sock"))
        leaked_states = list((failure_base / "worker-state").glob("*.json"))
        leaked_configs = list((failure_base / "workers").glob("*.json"))
        self.add(
            "runtime startup failure returns JSON and removes worker resources",
            (
                status == 400
                and isinstance(error.get("error"), str)
                and all(name == "hub" for name in windows)
                and not leaked_sockets
                and not leaked_states
                and not leaked_configs
            ),
            {
                "status": status,
                "error": error,
                "windows": windows,
                "sockets": leaked_sockets,
                "states": leaked_states,
                "configs": leaked_configs,
            },
        )
        self.stop_server()
        self.db = original_db
        self.start_server()

    def cleanup(self) -> None:
        try:
            if self.server and self.server.poll() is None:
                snapshot = self.request("GET", "/api/snapshot")[1]
                for session in snapshot.get("sessions", []):
                    if session.get("managed"):
                        self.stop_worker(session)
        except Exception as error:
            print("cleanup worker error:", error, file=sys.stderr)
        self.stop_server()
        time.sleep(1)
        self.tmux("kill-server", check=False)
        after = self._default_tmux_server_pid()
        self.add(
            "user default tmux server PID unchanged",
            after == self.default_tmux_pid_before,
            {
                "before": self.default_tmux_pid_before,
                "after": after,
            },
        )
        shutil.rmtree(self.base, ignore_errors=True)


def main() -> int:
    suite = E2E()
    try:
        suite.run()
    except Exception as error:
        suite.add("unhandled suite exception", False, repr(error))
    finally:
        suite.cleanup()
    report = {
        "socket": suite.socket_name,
        "checks": [check.__dict__ for check in suite.checks],
        "passed": sum(check.ok for check in suite.checks),
        "failed": sum(not check.ok for check in suite.checks),
    }
    print("E2E_SUMMARY", json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
