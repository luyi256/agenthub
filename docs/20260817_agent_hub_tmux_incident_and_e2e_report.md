# Agent Hub tmux 事故复盘与完整 E2E 报告

> 日期：2026-08-17
> 事故影响：用户默认 tmux server 被测试清理脚本误杀，`gen` session 的 tmux 内存状态丢失。
> 当前状态：事故前的 17 个 session / 34 个 window 已按持久化证据完成结构性重建；Agent Hub 已迁移到专属 tmux socket；隔离 E2E 全量通过。旧 scrollback、分屏布局和 shell 内存无法位级恢复，详见 `tmux_full_recovery_report_20260817.md`。

## 1. 事故结论

事故不是资源耗尽、OOM、磁盘满或 tmux 3.4 自身随机崩溃。直接原因是黑盒测试 subagent 的 cleanup：

```bash
env TMUX_TMPDIR="$ROOT/tmux" tmux kill-server
```

该进程继承了现有：

```text
TMUX=/tmp/tmux-1000/default,...
```

tmux 优先使用 `TMUX` 指向的 server，`TMUX_TMPDIR` 没有覆盖已经存在的 `TMUX`，因此 `kill-server` 实际命中了用户默认 server，而不是临时测试 server。

这是测试隔离设计错误，也是主 agent 未在委派任务中明确要求：

```bash
env -u TMUX tmux -L <random-e2e-socket> ...
```

造成的责任事故。

## 2. 现场证据

事故后现场：

- 默认 socket `/tmp/tmux-1000/default` 仍残留，但客户端报 `server exited unexpectedly`；
- 原 server PID `1084637` 一度还在，但已经没有 pane 子进程；
- 唯一 client 是卡住的 `tmux at -t gen`；
- 无 OOM、无进程上限、无 FD 上限、无 inode 耗尽；
- 磁盘约 91% 使用，但仍有约 10 GB，不是直接触发条件；
- subagent rollout 中明确存在上述 `tmux kill-server` 命令；
- 执行时间和 `gen` 丢失时间一致。

## 3. 用户环境恢复

默认 tmux server 已恢复，当前：

```text
gen
├─ shell
├─ enhance
├─ routing
└─ recovery
```

恢复来源：

- `enhance`：tcodex thread `019ffa7d-21f0-7dd1-b8e4-fe3b6c5cd560`
- `routing`：tclaude session `f81a898f-c8ae-4f3b-8623-bbfdac11ba53`
- `shell`：`/home/luyi/generation` 登录 shell
- `recovery`：恢复说明和当前仍在 VS Code 独立终端运行的 session 信息

另外两个在事故后仍由 VS Code integrated terminal 持有、没有重复 resume：

- Agent Hub tcodex thread `019ffad2-e9af-7133-aeb6-7fc2df84cb0a`
- rubric tcodex thread `01a00dad-20a6-7680-b935-63e928023a87`

持久 conversation 数据没有丢失；丢失的是旧 tmux server 的窗口布局和 scrollback 内存。

## 4. 永久隔离修复

### 4.1 Agent Hub 专属 socket

Agent Hub 不再使用用户默认 tmux：

```bash
tmux -L agenthub
```

正式 coordinator：

```text
tmux -L agenthub
└─ agenthub-mvp
```

用户默认：

```text
tmux default socket
└─ gen
```

两者物理隔离。

### 4.2 所有产品 tmux 调用清空 `TMUX`

Python launcher 统一执行：

```text
env -u TMUX tmux -L agenthub ...
```

VS Code 扩展：

- Open Project tmux：`tmux -L agenthub attach-session ...`
- auto-start：`env -u TMUX tmux -L agenthub ...`

### 4.3 测试使用随机 socket

E2E 每轮创建：

```text
tmux -L ah-e2e-<pid>-<random>
```

并硬性拒绝：

```text
default
agenthub
```

测试开始记录默认 tmux server PID 和 `gen` pane PID；结束逐字比较。

### 4.4 静态防回归

新增：

```text
agent_hub/tests/test_tmux_isolation.py
```

检查：

- 产品调用必须包含 `env -u TMUX`；
- 产品调用必须包含 `-L`；
- 不允许无 scope 的 `tmux kill-server`；
- E2E 必须随机 socket；
- E2E 必须验证默认 tmux PID 未变化。

## 5. 其他同时修复的问题

完整测试期间还发现并修复：

1. tcodex 多 worker 初始化共享状态库竞态：增加全局 runtime startup file lock；
2. 同 workspace 并发创建 tmux session 竞态：增加 workspace lock，并将“已存在”视为成功；
3. runtime 初始化失败残留 tmux window/socket/state/config：创建流程失败时事务式清理；
4. alias 冲突在 worker 启动后才发现：启动前预检 alias；
5. worker 初始化无 runtime id：增加 startup phase 和 90 秒 timeout；
6. coordinator 重启离线 completion 回放：按 message ID 从 worker state 同步；
7. stopped history 被 coordinator 自动拉起：启动时只连接仍存活 socket，不重启历史；
8. public Claude/Codex 在 Remote Extension Host 环境未稳定认证：默认 API 禁用，只开放 tclaude/tcodex；可显式 opt-in；
9. API 异常返回非 JSON 500：已覆盖错误 JSON contract；
10. VS Code 0.1.0 仍可能使用默认 tmux：0.1.1 已改专属 socket；在 Reload Window 前暂时设置 `agentHub.autoStartServer=false`。

## 6. 最终隔离 E2E 结果

命令：

```bash
AGENTHUB_E2E_ALLOW=1 \
  /home/luyi/creative-agent/creative-agent-mcp/.venv/bin/python \
  -m agent_hub.tests.e2e.full_matrix
```

最终全部通过：

1. 非法 cwd 返回 JSON 400
2. public Claude 默认拒绝
3. public Codex 默认拒绝
4. tclaude 创建
5. tclaude 第一轮
6. tclaude 第二轮
7. tcodex 创建
8. tcodex 第一轮
9. tcodex 第二轮
10. duplicate alias 返回 409，无 worker 泄漏
11. 同 workspace 四个 tcodex 并发创建
12. 同 workspace 只有一个 hub + 四个 worker window
13. 并发 session 0 对话
14. 并发 session 1 对话
15. 并发 session 2 对话
16. 并发 session 3 对话
17. approval deny 不执行命令
18. approval allow 后继续执行
19. coordinator 离线时 worker 完成
20. coordinator 重启后回放 completion
21. stopped history 不自动拉起
22. DB 正确记录 session/message/approval
23. runtime 启动失败返回 JSON 且清理 window/socket/state/config
24. 用户默认 tmux server PID 不变

安全断言：

```text
default tmux PID before = 2511053
default tmux PID after  = 2511053
gen panes before == gen panes after
```

`gen` pane PID 全程保持：

```text
shell    2511432
enhance  2511438
routing  2511442
recovery 2511446
```

## 7. VS Code 扩展状态

已安装：

```text
luyi-local.agent-hub 0.1.1
```

0.1.1 变化：

- 专属 `tmux -L agenthub`
- public runtimes 默认隐藏
- `agentHub.enablePublicRuntimes=false`

因为当前 VS Code Extension Host 可能仍载入旧的 0.1.0，需要执行一次：

```text
Developer: Reload Window
```

在 Reload 前已设置：

```json
"agentHub.autoStartServer": false
```

因此旧扩展不会再次自动创建默认 tmux session；正式后端已常驻专属 socket，不影响 Chat 使用。

## 8. 后续测试规则

从本事故起：

1. subagent 不得运行 tmux destructive commands，除非命令中显式出现 `env -u TMUX tmux -L ah-e2e-*`；
2. 测试不得依赖 `TMUX_TMPDIR` 隔离已连接 client；
3. 不允许在默认 socket 执行 `kill-server`；
4. 测试前后必须记录用户默认 server PID 和 session/pane 快照；
5. 出现任何变化立刻停止测试并优先恢复用户环境；
6. 完整 E2E 未通过不得交付。
