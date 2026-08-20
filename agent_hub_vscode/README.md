# Agent Hub for VS Code

VS Code Remote SSH workspace extension for project-scoped Claude/Codex conversations.

## User experience

- Opens as an Agent Hub chat in VS Code's right secondary sidebar.
- Shows live `tmux gen` sessions and Hub-created sessions in one top tab bar.
- Keeps stopped sessions in a compact history picker; selecting one restores its
  transcript, and sending a message relaunches its worker automatically.
- Selecting a tab keeps the whole conversation in the graphical Chat; tmux is
  only the background persistence layer.
- Uses the current VS Code workspace as the default cwd.
- Creates one tmux session per workspace:

  ```text
  ah-<workspace-name>-<workspace-hash>
  ```

- Creates one tmux window per Agent Hub conversation.
- Supports Claude, tclaude, Codex, and tcodex.
- Codex/tcodex approvals are shown inside chat.
- Assistant responses are rendered as safe Markdown.
- Plan updates, progress commentary, and CLI tool calls are shown inline with
  the chat. Tool rows stay collapsed by default, show only the first output
  line, and fetch full parameters/results only when expanded.
- Scrolling up stays anchored when polling brings new messages; a button shows
  when new content is available below.
- A manual collaboration panel can send an explicitly labelled user message
  from the current session to another idle session.
- Every top tab has an explicit close control. Closing stops the exact managed
  worker or verified imported `tmux gen` window and removes it from the UI
  while preserving the historical transcript in the coordinator database.
- Closing VS Code or losing SSH does not stop workers.
- Existing terminal sessions and existing CLI commands are not modified.
- The coordinator-reported `tmux_socket_name` is the source of truth for every
  interactive tmux action.

## Commands

- `Agent Hub: Open Chat`
- `Agent Hub: New Session`
- `Agent Hub: Refresh Sessions`
- `Agent Hub: Open Logs`

Opening a tmux terminal remains available only as a debug fallback from the
per-session `⋯` menu.

## Build

```bash
cd /home/luyi/agenthub/agent_hub_vscode
npm install
npm run build
npm run package
```

Result:

```text
agent-hub-0.2.7.vsix
```

## Install on Remote SSH host

Use VS Code's **Extensions: Install from VSIX...** and select the file, or run the remote VS Code CLI with an active IPC socket:

```bash
code --install-extension agent-hub-0.2.7.vsix --force
```

After installing or updating, reload the VS Code window if the new view is not yet visible.

## Backend

The extension connects to:

```text
http://127.0.0.1:8766
```

The default coordinator runs in:

```text
dedicated tmux socket: tmux -L agenthub
tmux session: agenthub-mvp
```

The extension can auto-start it from `/home/luyi/agenthub`.

### tmux safety and auto-start

- The hidden debug terminal action uses
  `/usr/bin/env -u TMUX tmux -L <socket-from-health>` and opens the exact
  `tmux_session`/`tmux_window` returned in managed session metadata. It does
  not reproduce the backend workspace naming algorithm.
- Auto-start parses the host and port from `agentHub.serverUrl`, and is
  permitted only for loopback HTTP addresses such as `127.0.0.1`,
  `localhost`, or `::1`.
- Concurrent refresh/create calls share one auto-start attempt. An existing
  live coordinator pane is never killed or force-respawned; a dead pane may
  be respawned, and the extension waits up to about 30 seconds for health.
- `agentHub.tmuxSocketName` is only the offline bootstrap socket. If the
  backend uses a custom `AGENTHUB_TMUX_SOCKET`, configure the same value here.
  As soon as health is reachable, the server-reported value replaces it.
- With `agentHub.autoStartServer=false`, the extension only connects to the
  configured URL and never invokes tmux to start the coordinator.

## Verification

```bash
npm run typecheck
npm run build
npm run test:static
npm run package
```

The static test suite never creates, attaches, respawns, or kills a tmux
session.

## Architecture

```text
VS Code Secondary Sidebar Webview
        ↕
Remote Extension Host
        ↕ HTTP
Agent Hub coordinator
        ↕ Unix sockets
workspace tmux session
        ├─ hub window
        ├─ Claude/tclaude stream-json worker
        └─ Codex/tcodex app-server worker
```

The tmux worker or verified `gen` relay is the runtime's only writer. The
integrated tmux terminal is for exceptional inspection and native interactive
pickers, not normal chat input.
