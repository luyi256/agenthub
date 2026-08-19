# Agent Hub

Agent Hub is a project-scoped control plane and VS Code Chat interface for persistent Claude, tclaude, Codex, and tcodex sessions. Runtimes stay alive in tmux while the user works through a single graphical interface.

## Repository layout

- `agent_hub/`: Python coordinator, tmux workers, CLI session discovery, message routing, approvals, history parsing, and tests.
- `agent_hub_vscode/`: VS Code Remote workspace extension for the right-side Chat interface.
- `docs/`: architecture, migration, audit, and incident reports.

Runtime databases, worker state, logs, extension bundles, and local forensic exports are intentionally excluded from Git.

## Start the coordinator

```bash
cd /home/luyi/agenthub
AGENTHUB_TMUX_SOCKET=agenthub \
  bash agent_hub/run.sh --host 127.0.0.1 --port 8766
```

The production coordinator must use the dedicated tmux socket `agenthub`. Never run destructive tmux commands against the user's default server.

## Test

```bash
PY=/home/luyi/creative-agent/creative-agent-mcp/.venv/bin/python

$PY -m unittest discover -s agent_hub/tests -p 'test_*.py'
cd agent_hub_vscode
npm test
```

Destructive end-to-end tests must use the random `ah-e2e-*` socket implemented in `agent_hub/tests/e2e/full_matrix.py`.

## Build the VS Code extension

```bash
cd agent_hub_vscode
npm ci
npm test
npm run package
```

The generated `dist/` directory and `.vsix` bundles are release artifacts and are not committed.

## Git workflow

- `main` must remain runnable and tested.
- Use short-lived branches such as `feat/...`, `fix/...`, or `docs/...`.
- Run both backend and extension tests before merging.
- Do not commit `agent_hub/data/`, credentials, SQLite files, worker configs, logs, VSIX files, or tmux recovery exports.
- Create version tags only after the exact packaged extension has passed browser and isolated tmux verification.
