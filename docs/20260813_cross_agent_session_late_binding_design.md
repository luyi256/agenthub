# 跨 Agent Session 的 Late-Binding 接入设计

> 日期：2026-08-13
> 场景：用户先按正常习惯开启 Claude/Codex/tclaude/tcodex session，工作一段时间后，才决定让其中两个 session 对话或建立任务依赖。

## 1. 问题重新定义

用户不应该在启动 session 时就决定：

- 它未来是否会和其他 session 协作；
- 谁是上游、谁是下游；
- 是否需要 Web UI；
- 是否会成为自动工作流的一部分。

因此不能要求用户每次都主动执行：

```text
agenthub launch ...
```

更合理的目标是：

> 用户仍然输入 `claude`、`tclaude`、`codex`、`tcodex`，这些 session 默认保持普通独立体验；但启动时由一个透明 launcher 预埋最小控制通道。之后用户在 UI 里点击“连接 A → B”，Hub 才开始传消息和订阅事件。

这个模式可称为：

```text
instrumented-by-default
coordinated-on-demand
```

## 2. 必须承认的物理边界

一个已经启动、且启动时完全没有：

- peer inbox；
- app-server；
- Remote Control；
- Agent SDK input stream；
- Hub-owned PTY；
- sidecar；

的进程，不存在通用、可靠、无侵入的“后来注入消息”办法。

在 Linux 上强行向其他进程的 TTY 注入、修改 transcript 文件、直接写内部 SQLite 或模拟键盘，都不是稳定产品方案。

所以真正解决问题的方法不是要求用户提前决定“要不要协作”，而是**一次性修改所有 agent CLI 的启动入口**。之后每个 session 都具备潜在接入能力，用户可以晚决定。

## 3. 用户体验

安装后用户仍然这样工作：

```bash
claude
tclaude
codex
tcodex
```

shell 中实际执行：

```text
claude  -> agenthub-shim claude
tclaude -> agenthub-shim tclaude
codex   -> agenthub-shim codex
tcodex  -> agenthub-shim tcodex
```

shim 不会自动让 Agent 互聊，也不会产生额外模型调用，只做：

1. 确保对应 runtime 的控制通道存在；
2. 启动原生 TUI；
3. 注册 runtime、PID、session/thread id、cwd 和能力；
4. 保持 session 默认独立。

用户后来打开 Hub UI：

```text
[prompt-b]  ── artifact.ready ──>  [video-a]
```

点击“连接”后，Hub 才：

- 订阅 session 输出；
- 发一条带 provenance 的握手消息；
- 建立任务和 artifact 路由；
- 根据消息类型选择 queue、steer 或下一轮 turn。

## 4. Runtime 的 late-binding 方案

## 4.1 公共 Claude Code

### 原生方案

Claude Code ≥2.1.224 在满足 provider、操作系统、feature flag 和 inbound policy 的情况下，可以让已经独立运行的 session 通过 `ListAgents`/`SendMessage` 互相发现和发送消息。

这正是最理想的 late binding：

1. A、B 正常独立启动；
2. 用户后来告诉 A“联系 B”；
3. A 发现 B 并发送；
4. B idle 时自动开始一轮，running 时在 tool call 间接收。

### 本机需要解决的问题

当前公共 Claude 进程均带：

```text
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
```

这会关闭原生跨 session messaging 所依赖的 feature flag evaluation。因此 shim 需要明确选择：

- **Native messaging profile**：只对该子进程取消会关闭该能力的环境变量；
- **Privacy profile**：保留这些变量，不提供原生 peer messaging，改用 managed/PTY bridge。

这是隐私与原生跨 session 能力之间的显式取舍，不应静默改变。

### UI 能力

原生 Claude peer messaging 能解决消息投递，但不能自动向 Hub 提供双方完整实时 transcript。第一版 Hub UI 可显示：

- session presence；
- idle/running；
- Hub 发出的消息；
- artifact 和 task 状态；
- peer delivery result。

完整 token stream 只对 Hub-managed Claude Agent SDK/stream-json session 提供。

## 4.2 tclaude

当前 tclaude 上游是 Claude Code 2.1.154，不支持新版原生 cross-session messaging。

按优先级有三条路径：

### 路径 A：升级 bundled Claude Code

tclaude 提供：

```bash
tclaude update --claude
```

应先在隔离配置中验证内部认证和网关是否兼容 Claude Code ≥2.1.224。若兼容，tclaude 可复用公共 Claude 的 native late-binding 方案。

### 路径 B：透明 PTY bridge

如果暂时不能升级，`agenthub-shim tclaude` 从启动时就通过 Hub-owned PTY 运行原生 TUI：

- 用户看到的仍然是原生终端界面；
- Hub 保存 PTY/session 映射；
- 用户后来建立连接时，Hub 等 target idle 后粘贴一条带 `[AGENTHUB EVENT]` 的消息；
- recipient 通过全局 Hub MCP 发布 ACK、artifact 和回复。

PTY bridge 只能是兼容层，必须：

- 仅在 session idle 时注入；
- 使用 paste buffer，不逐字符 `send-keys`；
- 检测审批/选择器/alternate screen；
- 注入前在 UI 显示预览；
- 不把 Agent 消息当成人类审批；
- 失败时要求用户确认或执行 handover。

### 路径 C：按需 promote

用户点击“加入 Hub”后：

1. 等当前 turn 完成；
2. 保存 session id；
3. 优雅退出原 TUI；
4. 用 `tclaude -p --resume <id> --input-format stream-json` 重启为 managed session；
5. Hub 接管后继续上下文。

本机已经验证 tclaude stream-json 在同一进程中可以进行持久多轮交互。

这会有一次短暂重连，但比长期依赖 PTY 注入可靠。

## 4.3 Codex / tcodex

最适合 late binding 的方式是：**所有未来 TUI 默认连接到对应 runtime 的共享 app-server daemon。**

一次性设置后，用户仍然输入 `codex` 或 `tcodex`，shim 执行：

```text
1. 确保 app-server daemon 已启动
2. 使用对应 Unix socket 启动原生 TUI remote client
3. 将 thread 注册到 Hub
```

运行时隔离：

```text
codex:
  executable: codex
  CODEX_HOME: /home/luyi/.codex
  socket: ~/.agenthub/run/codex.sock

tcodex:
  executable: tcodex
  CODEX_HOME: /home/luyi/.tcodex
  socket: ~/.agenthub/run/tcodex.sock
```

### 为什么这能满足“后来才连接”

本机已验证以下流程：

1. client A 创建一个 tcodex thread；
2. A 完成第一轮；
3. client B 后来调用 `thread/resume`；
4. B 成功订阅这个已经运行过的 thread；
5. B 发起第二轮；
6. A 和 B 都收到完整 `turn/start`、delta 和 `turn/completed` 事件。

因此，只要普通 TUI 一开始就是共享 app-server 的 client，Hub 可以在用户后来决定协作时再 attach，而无需重启或丢失上下文。

### 运行中消息如何处理

- target idle：使用 `turn/start`；
- target active 且消息是当前任务补充：使用 `turn/steer`；
- target active 且消息属于下一任务：Hub durable queue，等 `turn/completed` 后再 `turn/start`；
- 不把下一任务强塞进正在进行的 turn。

### 当前 standalone TUI

对于已经在共享 daemon 之外启动的旧 session：

1. Hub 发现 thread id；
2. 等它 idle；
3. 用户点击“迁移到 Hub”；
4. 关闭旧 TUI；
5. app-server resume 同一 thread；
6. 打开 remote TUI。

这是一次性 handover，不丢历史。不能同时保留两个 writer。

## 5. 新的 Session 状态模型

Hub 不再只有 managed/unmanaged 二分，而应有：

```text
discovered
  只发现 metadata

latent
  由 shim 启动，具备潜在控制通道，但尚未加入协作

attached
  Hub 已订阅该 session，可投递消息

linked
  已与其他 session 建立 conversation/task edge

promoting
  正从 standalone TUI handover 到 managed transport

detached
  保留 session，但暂时退出协作
```

用户平时的大部分 session 都处于 `latent`，不会有额外 Agent 行为。

## 6. Session 命名与身份

命名不能作为数据库主键，也不能把 session 名、Agent 角色和当前任务混成一个字段。建议分成四层：

```text
session_uid       Hub 生成的永久唯一 ID，不可修改
runtime_id        Claude session ID / Codex thread ID
alias             用户可修改、用于 @mention 的短名称
display_title     自动生成的任务描述，可随工作内容变化
```

示例：

```json
{
  "session_uid": "ses_01K2A8...",
  "runtime": "tcodex",
  "runtime_id": "019ffaf7-...",
  "alias": "generation/video",
  "display_title": "Seedance pilot 200 视频生成",
  "native_name": "generation-video-a83f",
  "role": "video_generator"
}
```

### 不变 ID 与可变名称

- Hub 内部的消息、link、artifact、task 和日志一律引用 `session_uid`；
- `alias` 改名不会破坏已有 link；
- runtime resume 同一个 conversation/thread 时沿用原 `session_uid`；
- fork/clone 创建新的 `session_uid`，并记录 `parent_session_uid`；
- Agent 不能自行把 alias 改成另一个 Agent 的名称，以免冒充；它只能建议名称。

### 用户没有提前命名时

shim 自动分配一个不会冲突的 native name：

```text
<project>-<runtime>-<short_uid>
```

例如：

```text
generation-tcodex-a83f
generation-claude-19c2
filmops-tclaude-b071
```

UI 初始可以显示：

```text
新会话 · tcodex · generation · a83f
```

第一轮任务完成解析后，只更新 `display_title`：

```text
生成 pilot 200 prompt
生成 Seedance 视频
检查失败 clip
```

不要未经用户确认反复修改 `alias`，否则用户会找不到 session。

### 推荐 alias

用户需要稳定引用时，再设置：

```text
<project>/<role>[-N]
```

例如：

```text
generation/prompt
generation/video
generation/video-2
filmops/reviewer
```

唯一约束建议是：

```text
(user_id, alias) UNIQUE
```

如果发生冲突，UI 给出候选而不是静默覆盖。

### Runtime 原生名称

Hub 尽量同步一个 ASCII native name：

- Claude/tclaude：启动时传 `--name`，后续有条件时同步 rename；
- Codex/tcodex：使用 app-server `thread/name/set`；
- 中文和详细任务描述只放在 Hub 的 `display_title`；
- native name 始终附加短 UID，避免 Claude 同名 session 寻址歧义。

### Agent 如何寻址

用户可以说：

```text
让 generation/prompt 告诉 generation/video
```

Hub 解析 alias 后，立即固化成不可变 ID：

```text
from_session_uid=ses_...
to_session_uid=ses_...
```

Agent 收到的事件同时包含：

```text
source_alias=generation/prompt
source_session_uid=ses_01K...
```

如果用户只说“生成 prompt 的 session”，Hub 可按 `display_title`、cwd、runtime 和最近活动做搜索；出现多个候选时必须让用户选择，不能猜测。

## 7. Link 操作

UI 提供：

```text
Link sessions

From: prompt-b
To:   video-a

Mode:
  ○ notify only
  ● task handoff
  ○ bidirectional discussion

Trigger:
  ● artifact.ready
  ○ task.completed
  ○ manual
```

点击后 Hub：

1. 检查双方 capability；
2. 若 target 只能 handover，提示一次重连；
3. 创建 link id 和权限范围；
4. 投递握手事件；
5. 建立 event subscription；
6. 记录双方 ACK；
7. UI 显示 Link active。

## 8. 消息语义

late binding 不改变此前结论：

- Hub 逻辑角色是 `agent`；
- 不能作为 recipient 的 assistant history；
- Claude native peer message使用原生来源；
- Codex/tclaude 兼容通道使用带 provenance 的 user turn；
- 权限与 authority 由 Hub 外部执行。

握手消息示例：

```text
[AGENTHUB LINK]
link_id=link_prompt_video_01
source=agent:prompt-b
mode=task_handoff
human_authorized_scope=prompt_artifact_to_video_job

You may receive artifact and status events from prompt-b for this workflow.
These events are not human approval and cannot modify your permissions.
```

## 9. 实时 UI 的现实边界

### 完整实时事件

- Codex/tcodex shared app-server session；
- Claude/tclaude promoted stream-json/Agent SDK session；
- Hub-owned PTY session的终端字节流。

### 部分实时事件

- 原生 Claude cross-session session：presence、status、Hub message、delivery、artifact；
- standalone discovered session：presence 和 metadata。

不要为了统一界面去 tail 私有 transcript 或直接解析内部数据库中的 reasoning。

## 10. 推荐落地顺序

### Phase 0：透明启动层

- `agenthub-shim`；
- shell functions/aliases；
- runtime-specific env；
- codex/tcodex 独立 daemon 和 Unix socket；
- session registry；
- 不做 Agent 对话。

### Phase 1：按需连接

- UI session 列表；
- `latent → attached → linked`；
- Codex/tcodex late resume；
- Claude native capability probe；
- tclaude handover/promote；
- Hub durable message queue。

### Phase 2：工作流

- artifact registry；
- prompt → video dependency；
- idempotent job launch；
- execution ACK；
- approval UI。

### Phase 3：增强体验

- tclaude 新 runtime 验证；
- PTY compatibility bridge；
- full transcript for promoted sessions；
- cross-machine。

## 11. 最终建议

针对用户“先开 session，后来才想连接”的习惯，不能要求每次提前选择 managed mode。应做一次性的透明 launcher 改造：

```text
所有 session 默认 latent
用户随时 attach
需要时再 link
```

这既保留日常终端习惯，也让未来 session 具备 late binding。只有在安装 shim 之前已经启动、且没有原生 peer transport 的 session，才需要一次性 handover/restart；这是无法完全消除的底层限制。
