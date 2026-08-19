#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path


SNIPPET = r'''# Agent Hub optional shim — generated file, not automatically sourced.
# Current implementation remains pass-through until runtime adapters are enabled.
#
# To opt in later, source this file explicitly from a temporary shell first:
#   source ~/.config/agenthub/shim.zsh
#
# Do not add it to ~/.zshrc until `agenthub doctor` reports the adapter ready.

agenthub_real_command() {
  command "$@"
}

# Future wrappers are intentionally disabled in the MVP:
# claude()  { agenthub_real_command claude "$@"; }
# tclaude() { agenthub_real_command tclaude "$@"; }
# codex()   { agenthub_real_command codex "$@"; }
# tcodex()  { agenthub_real_command tcodex "$@"; }
'''


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate an opt-in Agent Hub shell snippet without modifying shell config."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path.home() / ".config" / "agenthub" / "shim.zsh",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write the standalone snippet. It is never added to ~/.zshrc automatically.",
    )
    args = parser.parse_args()
    if not args.write:
        print(SNIPPET)
        print(f"# Planned output: {args.output}")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(SNIPPET, encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
