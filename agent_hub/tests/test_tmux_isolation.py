from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class TmuxIsolationTests(unittest.TestCase):
    def test_production_tmux_calls_use_dedicated_socket(self) -> None:
        project_tmux = (ROOT / "agent_hub" / "project_tmux.py").read_text()
        self.assertIn('"env",\n                "-u",\n                "TMUX",', project_tmux)
        self.assertIn('"-L",\n                    self.tmux_socket_name,', project_tmux)

        extension = (
            ROOT / "agent_hub_vscode" / "src" / "extension.ts"
        ).read_text()
        self.assertIn('shellPath: "/usr/bin/env"', extension)
        self.assertIn(
            '"-u",\n        "TMUX",\n        "tmux",\n        "-L",\n        this.serverSocketName,',
            extension,
        )
        self.assertIn(
            'const socketName =\n      this.serverSocketName ?? bootstrapTmuxSocketName(configuration);',
            extension,
        )
        self.assertNotIn('shellPath: "tmux"', extension)

    def test_no_unscoped_kill_server_in_product_or_tests(self) -> None:
        targets = [
            ROOT / "agent_hub" / "project_tmux.py",
            ROOT / "agent_hub" / "runtime_manager.py",
            ROOT / "agent_hub" / "session_worker.py",
            ROOT / "agent_hub_vscode" / "src",
        ]
        violations: list[str] = []
        for base in targets:
            paths = [base] if base.is_file() else base.rglob("*")
            for path in paths:
                if not path.is_file() or path.suffix not in {
                    ".py",
                    ".ts",
                    ".js",
                    ".sh",
                }:
                    continue
                text = path.read_text(errors="replace")
                for match in re.finditer(r"tmux[^\n]{0,200}kill-server", text):
                    snippet = match.group(0)
                    if "-L" not in snippet:
                        violations.append(f"{path.relative_to(ROOT)}: {snippet}")
        self.assertEqual([], violations)

    def test_default_tmux_gen_integration_is_narrow_and_user_scoped(self) -> None:
        gen_tmux = (ROOT / "agent_hub" / "gen_tmux.py").read_text()
        self.assertIn('session_name: str = "gen"', gen_tmux)
        self.assertIn('"@agenthub_name"', gen_tmux)
        self.assertIn('"@attn_manual"', gen_tmux)
        for command in (
            "kill-session",
            "kill-server",
            "new-window",
            "new-session",
            "respawn-pane",
            "select-window",
        ):
            self.assertNotIn(command, gen_tmux)
        self.assertIn('def close_window(self, window_id: str)', gen_tmux)
        self.assertIn('"kill-window"', gen_tmux)
        self.assertIn("self._verify_window(window_id)", gen_tmux)
        self.assertIn('"load-buffer"', gen_tmux)
        self.assertIn('"paste-buffer"', gen_tmux)
        self.assertIn('"send-keys"', gen_tmux)
        self.assertIn('"Enter"', gen_tmux)

    def test_e2e_hard_codes_random_non_default_socket(self) -> None:
        e2e = (
            ROOT / "agent_hub" / "tests" / "e2e" / "full_matrix.py"
        ).read_text()
        self.assertIn('self.socket_name = f"ah-e2e-', e2e)
        self.assertIn('if self.socket_name in {"default", "agenthub"}', e2e)
        self.assertIn('"env",\n                "-u",\n                "TMUX",', e2e)
        self.assertIn('self.tmux("kill-server", check=False)', e2e)
        self.assertIn('"user default tmux server PID unchanged"', e2e)


if __name__ == "__main__":
    unittest.main()
