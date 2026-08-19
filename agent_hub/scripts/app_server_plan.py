#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path


def main() -> int:
    home = Path.home()
    plan = {
        "mode": "plan-only",
        "note": "This script does not start daemons or modify configuration.",
        "runtimes": {
            "codex": {
                "executable": "codex",
                "codex_home": str(home / ".codex"),
                "daemon_start": "CODEX_HOME=$HOME/.codex codex app-server daemon start",
                "tui_connect": "CODEX_HOME=$HOME/.codex codex --remote unix://",
            },
            "tcodex": {
                "executable": "tcodex",
                "codex_home": str(home / ".tcodex"),
                "daemon_start": "CODEX_HOME=$HOME/.tcodex tcodex -- app-server daemon start",
                "tui_connect": "CODEX_HOME=$HOME/.tcodex tcodex -- --remote unix://",
            },
        },
    }
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
