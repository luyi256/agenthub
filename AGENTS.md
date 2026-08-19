# AGENTS.md — Agent Hub

> 独立的跨 Claude/Codex/tclaude/tcodex session 控制面与 VS Code Chat 扩展。

## 目录

- `agent_hub/`：Python 后端、session worker、发现/命名/消息/审批、测试及运行数据。
- `agent_hub_vscode/`：VS Code workspace extension，右侧栏 Chat UI。
- `docs/`：设计、审计、事故复盘、恢复证据与部署报告。

## 运行

```bash
cd /home/luyi/agenthub
AGENTHUB_TMUX_SOCKET=agenthub bash agent_hub/run.sh --host 127.0.0.1 --port 8766
```

正式服务必须使用独立 tmux socket：`env -u TMUX tmux -L agenthub ...`。禁止对默认 tmux 使用 `kill-server` 或无范围清理；只有用户在 UI 明确确认“关闭会话”后，才允许对经过 runtime/window ID 双重校验的单个 `gen` window 执行 `kill-window`。所有 E2E 使用随机 `ah-e2e-*` socket。

## 测试

```bash
PY=/home/luyi/creative-agent/creative-agent-mcp/.venv/bin/python
$PY -m unittest discover -s agent_hub/tests -p 'test_*.py'
cd agent_hub_vscode && npm test
```

## 数据与工作区

- Hub 自身数据默认位于 `/home/luyi/agenthub/agent_hub/data/`。
- session 的 `cwd` 是被操作项目目录，不随 Hub 源码迁移。例如 generation 项目的对话仍使用 `/home/luyi/generation`。
- VS Code 扩展默认 coordinator 项目路径为 `/home/luyi/agenthub`。
