# Contributing

## Branches

- `main`: tested and deployable.
- `feat/<topic>`: new behavior.
- `fix/<topic>`: bug fixes.
- `docs/<topic>`: documentation-only changes.

Keep branches short-lived and rebase or merge the latest `main` before final verification.

## Required checks

Run from the repository root:

```bash
PY="${AGENTHUB_PYTHON:-python3}"

$PY -m unittest discover -s agent_hub/tests -p 'test_*.py'
cd agent_hub_vscode
npm ci
npm test
```

Changes that touch tmux lifecycle, runtime messaging, approvals, or session recovery also require an isolated `ah-e2e-*` test. Never run destructive tests on the default tmux socket or the production `agenthub` socket.

## Commit rules

- Use concise imperative subjects, for example `fix: preserve chat width on resize`.
- Keep product code, tests, and the relevant documentation in the same commit.
- Do not commit generated `dist/`, `.vsix`, SQLite, worker state, sockets, logs, credentials, or local recovery exports.
- Before pushing, inspect `git diff --cached` and check the staged file-size list.

## Release procedure

1. Update `agent_hub_vscode/package.json`, its lockfile, the changelog, and documentation.
2. Run backend tests, extension tests, browser regression, and isolated tmux E2E where applicable.
3. Package the extension with `npm run package`.
4. Install and verify the exact VSIX on the remote Extension Host.
5. Confirm the default tmux server PID is unchanged.
6. Commit source changes, create an annotated tag such as `v0.2.5`, and push the branch and tag.

VSIX files are release artifacts and should be attached to a GitHub Release rather than committed to Git.
