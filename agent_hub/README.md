# Agent Hub backend

当前版本采用**混合模式**：

- 对已经存在的 Claude、tclaude、Codex、tcodex CLI session：保持零侵入，只读发现和命名；
- 对 `tmux gen` 中经过精确 runtime 校验的原生 session：可导入图形 Chat，并通过原 TUI 的输入机制继续对话；
- 对用户从 Agent Hub 页面点击“新建对话”创建的 session：由 Hub 管理，可持续聊天、查看 Plan/工具调用、处理审批以及在运行中追加消息。

它不会：

- 修改 `~/.zshrc`、Claude/Codex 配置或环境变量；
- 自动接管、关闭或替换任何外部已有 session；
- 改变原有 `claude/tclaude/codex/tcodex` 命令行为；
- 在用户没有点击“新建对话”时启动模型 runtime；
- 修改 `~/.codex` 或 `~/.tcodex` 的数据库。

Codex thread 信息通过 SQLite `mode=ro` 读取；Claude session 通过 `agents --json` 读取。

## 图形对话

页面右上角点击“新建对话”，选择 runtime、cwd、alias、标题、角色和权限：

- Codex/tcodex：Hub 按 runtime 启动匹配 wrapper 的 app-server，并用 `turn/start` 流式通信；
- Claude/tclaude：Hub 启动持久 `stream-json` session；
- Codex/tcodex 的命令和文件审批会在聊天区显示允许/拒绝按钮；
- Codex/tcodex 运行中追加使用原生 `turn/steer`；
- Claude/tclaude 运行中追加进入 Hub 持久化下一轮队列，当前回复结束后自动执行；
- Plan、commentary 和工具调用会显示在 Chat 中，工具结果默认折叠；
- Hub 重启后，下一条消息会尝试 resume 原 session；
- 聊天记录、alias 和角色保存在独立 Agent Hub SQLite。

`full-access` 会绕过大部分审批，只应用于用户明确以该权限新建的 Hub session。

## 启动

```bash
cd /home/luyi/agenthub
bash agent_hub/run.sh --host 127.0.0.1 --port 8766
```

打开：

```text
http://127.0.0.1:8766/
```

## 命令

```bash
PY=/home/luyi/creative-agent/creative-agent-mcp/.venv/bin/python

$PY -m agent_hub.cli doctor
$PY -m agent_hub.cli scan
$PY -m agent_hub.cli sessions
$PY -m agent_hub.cli shim-preview
```

`shim-preview` 只打印未来启动层示例，不会安装。

另外提供两个**默认只输出、不执行**的脚本：

```bash
$PY agent_hub/scripts/generate_shim_snippet.py
$PY agent_hub/scripts/app_server_plan.py
```

即使给 `generate_shim_snippet.py` 加 `--write`，它也只写独立文件，不会修改或 source `~/.zshrc`。

## 数据

默认数据库：

```text
agent_hub/data/agenthub.sqlite3
```

可通过 `AGENTHUB_DB` 或 `--db` 修改。

## 仍有限制

- Claude/tclaude 原生交互式 picker 尚不能全部结构化到网页中；
- 导入的原 TUI busy 输入属于 runtime best-effort 行为；
- context/cost 指标和 artifact registry 尚未形成统一产品面。
