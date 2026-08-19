from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import uvicorn

from .app import create_app
from .config import HubConfig
from .db import HubDatabase
from .discovery import SessionDiscoverer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Agent Hub local control plane")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="启动本地 Web UI")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8766)
    serve.add_argument("--scan-interval", type=float, default=5.0)
    serve.add_argument("--db", type=Path)

    scan = subparsers.add_parser("scan", help="只读扫描现有 session")
    scan.add_argument("--json", action="store_true")

    sessions = subparsers.add_parser("sessions", help="列出注册表中的 session")
    sessions.add_argument("--json", action="store_true")
    sessions.add_argument("--db", type=Path)

    subparsers.add_parser("doctor", help="检查 runtime 与零侵入配置")
    subparsers.add_parser(
        "shim-preview",
        help="打印未来可选的 shell shim；不会修改任何配置",
    )
    return parser


def config_from_args(args: argparse.Namespace) -> HubConfig:
    base = HubConfig.from_env()
    return HubConfig(
        host=getattr(args, "host", base.host),
        port=getattr(args, "port", base.port),
        scan_interval=getattr(args, "scan_interval", base.scan_interval),
        db_path=getattr(args, "db", None) or base.db_path,
        codex_home=base.codex_home,
        tcodex_home=base.tcodex_home,
        codex_history_limit=base.codex_history_limit,
        tmux_socket_name=base.tmux_socket_name,
        enable_public_runtimes=base.enable_public_runtimes,
    )


def print_sessions(items: list[dict], as_json: bool) -> None:
    if as_json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return
    if not items:
        print("No sessions.")
        return
    print(
        f"{'RUNTIME':9} {'PRESENCE':10} {'STATE':18} "
        f"{'NAME':28} {'PROJECT':18} {'PID':>7}"
    )
    for item in items:
        name = (item.get("effective_name") or "-")[:28]
        project = (item.get("project") or "-")[:18]
        pid = item.get("pid") or "-"
        print(
            f"{item['runtime'][:9]:9} {item['presence'][:10]:10} "
            f"{item['attach_state'][:18]:18} {name:28} {project:18} {str(pid):>7}"
        )


def shim_preview() -> str:
    return """# Agent Hub shim preview — 当前不会自动安装
# 设计目标：以后仍输入原命令，但 session 默认具备 latent attach 能力。
# 在完成 app-server/PTY 稳定性验证前，请不要把下面内容加入 ~/.zshrc。

# claude()  { command agenthub-shim claude  -- "$@"; }
# tclaude() { command agenthub-shim tclaude -- "$@"; }
# codex()   { command agenthub-shim codex   -- "$@"; }
# tcodex()  { command agenthub-shim tcodex  -- "$@"; }
"""


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = config_from_args(args)

    if args.command == "serve":
        uvicorn.run(
            create_app(config),
            host=config.host,
            port=config.port,
            log_level="info",
        )
        return 0

    if args.command == "scan":
        result = SessionDiscoverer(config).discover()
        if args.json:
            print(
                json.dumps(
                    {
                        "sessions": result.records,
                        "diagnostics": result.diagnostics,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            db = HubDatabase(config.db_path)
            db.replace_discovery(result.records, result.scanned_runtimes)
            print_sessions(db.list_sessions(), False)
            for item in result.diagnostics:
                print(f"[{item['level']}] {item['runtime']}: {item['message']}")
        return 0

    if args.command == "sessions":
        print_sessions(HubDatabase(config.db_path).list_sessions(), args.json)
        return 0

    if args.command == "doctor":
        result = SessionDiscoverer(config).discover()
        print("Agent Hub doctor")
        print(f"- DB: {config.db_path}")
        print("- Mode: observe-only（不修改 shell，不接管现有 session）")
        print(f"- Sessions discovered: {len(result.records)}")
        for item in result.diagnostics:
            print(f"- [{item['level']}] {item['runtime']}: {item['message']}")
        return 0

    if args.command == "shim-preview":
        print(shim_preview())
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
