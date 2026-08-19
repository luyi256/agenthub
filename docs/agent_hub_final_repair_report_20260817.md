# Agent Hub 最终修复报告（2026-08-17）

## 结论

Agent Hub 后端和 VS Code 扩展已经完成修复、隔离测试与正式部署。正式协调器运行在独立 tmux socket `agenthub`，默认 tmux server PID 在全过程中保持 `2511053`，未被停止、重启或接管。VS Code 扩展已升级并注册为 `luyi-local.agent-hub@0.1.2`；当前窗口需执行一次 `Developer: Reload Window` 才会加载新扩展代码。

## 主要修复

- worker 创建、恢复和失败回滚改为事务式处理，并用 `@agenthub_worker_id` 校验窗口所有权，避免误杀其他 tmux window。
- coordinator 重启后可通过 Unix socket 重连现有 worker；断线、runtime exit 和事件回放只作用于匹配 worker。
- 每个 session 的恢复使用 singleflight，避免并发发送时重复启动 worker。
- WorkerClient 增加连接健康状态、失败清理、pending request 清理及断线 callback。
- Codex/tcodex 审批仅接受已知 method 和 `accept/decline`，未知 server request 返回 JSON-RPC method-not-found。
- 新建 managed session 强制使用 `tmux-worker`，API 和 RuntimeManager 双层拒绝 `use_tmux:false`。
- 历史 `app-server`/`stream-json` managed session 在下一次发送时迁移到 tmux worker，不再进入旧直连路径，并保留原 `session_uid`、`runtime_id` 与消息历史。
- `/api/health` 返回真实 `tmux_socket_name`；managed chat session 正确计入 `messageable`。
- VS Code 扩展从 health 获取并校验 tmux socket；所有 tmux 命令使用 `/usr/bin/env -u TMUX tmux -L <socket>`。
- 扩展 auto-start 改为 singleflight，禁止对存活 pane 使用 `respawn-pane -k`，并限制为 loopback HTTP。

## 测试

- Python 单元测试：40/40 通过。
- VS Code TypeScript/static 测试：4/4 通过。
- 随机隔离 socket `ah-e2e-3906680-71ad48` 完整真实 E2E：24/24 通过。
- E2E 覆盖：tclaude/tcodex 创建与多轮对话、四路并发、重复 alias 无泄漏、审批 allow/deny、coordinator 离线完成与重启回放、启动失败清理、默认 tmux 隔离。
- 正式环境回归：
  - 既有 `test` session 在 coordinator 部署后返回 `AGENTHUB-POST-DEPLOY-OK`。
  - 新建 tcodex 返回 `FORMAL-TCODEX-OK`。
  - 新建 tclaude 返回 `FORMAL-TCLAUDE-OK`。
  - `test` session 滚动切换到新版 worker 后 resume 成功，返回 `ROLLING-WORKER-UPGRADE-OK`。

## 正式部署状态

- 后端：`http://127.0.0.1:8766`。
- dedicated tmux socket：`agenthub`。
- coordinator tmux session：`agenthub-mvp`。
- workspace tmux session：`ah-generation-9c577f41`。
- VSIX：`agent_hub_vscode/agent-hub-0.1.2.vsix`。
- VSIX SHA-256：`0383de7247829656257b6093a2cdf86acfc3c0661368932baf6c71899ea4651d`。
- 扩展注册版本：`luyi-local.agent-hub@0.1.2`。
- `agentHub.autoStartServer` 暂时保持 `false`，避免旧 Extension Host 在 Reload 前参与生命周期管理。

## 会话状态与升级策略

- 原有 managed session 的数据库记录和消息历史保留。
- `test` 已切换到新版 worker并保持在线。
- 其余旧 worker 已安全停止；用户下一次在对应 session 发送消息时，Hub 会用原 runtime ID 自动 resume，并创建新版 worker。
- 测试产生的 tcodex/tclaude 诊断 session、窗口和运行文件已清理。

## 用户剩余操作

在 VS Code 命令面板执行一次：

```text
Developer: Reload Window
```

Reload 后可直接在右侧 Agent Hub 中新建 tcodex/tclaude session、发送消息，并使用 `Open Project tmux` 查看对应项目的 dedicated tmux。
