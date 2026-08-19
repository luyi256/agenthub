#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${AGENTHUB_PYTHON:-/home/luyi/creative-agent/creative-agent-mcp/.venv/bin/python}"

cd "$ROOT"
exec "$PY" -m agent_hub.cli serve "$@"
