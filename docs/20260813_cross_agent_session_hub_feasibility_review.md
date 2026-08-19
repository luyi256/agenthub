# 跨 Agent Session Hub 可行性复审

> 日期：2026-08-13
> 结论等级：**有条件可行，不是“任意现有 session 无侵入互通”**
>
> 针对“session 已经开始，之后才决定让它加入协作”的使用习惯，进一步方案见 `agentdocs/cross_agent_session_late_binding_design.md`。核心从 managed-first 修订为 **instrumented-by-default、coordinated-on-demand**：一次性替换启动入口，让每个未来 session 默认具备潜在控制通道，但只有用户点击连接时才真正加入协作。

## 1. 修订后的结论

原方案的方向——统一 Agent Hub、事件总线、artifact registry、runtime adapter 和 Web UI——仍然成立，但最初表述过于乐观。必须明确区分以下两种目标：

1. **Hub-managed sessions**：session 从一开始就由 Hub 启动、输入、订阅输出和管理生命周期。这一模式可行，而且本机已经验证 tclaude stream-json 与 tcodex app-server 的关键链路。
2. **任意已经独立打开的 sessions**：Hub 后来发现它们，并在不重启、不改变启动方式的情况下可靠写入消息、获取实时输出。这一模式只对部分新版 Claude session 有原生支持；对现有 tclaude 和独立 Codex/tcodex TUI 不具备统一、稳定的方案。

因此，产品承诺应改成：

> Agent Hub 可以可靠编排自己管理的 Claude/Codex/tclaude/tcodex session；对外部已打开 session，第一版只保证发现和状态展示，是否可发送消息由 runtime capability 决定。

## 2. 本机验证结果

### 2.1 Claude 原生跨 session 消息当前并未真正可用

官方跨 session messaging 要求 Claude Code 版本至少为 2.1.224，并且依赖 feature flag evaluation。以下环境变量会让该功能保持关闭：

```text
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC
DISABLE_TELEMETRY
DO_NOT_TRACK
DISABLE_GROWTHBOOK
```

本机检查结果：

- 公共 Claude Code 为 2.1.226，版本满足要求；
- 当前所有公共 Claude session 都带有 `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`；
- 当前所有 tclaude session 还带有 `DISABLE_TELEMETRY=1` 和 `DO_NOT_TRACK=1`；
- tclaude 的上游 Claude Code 版本是 2.1.154，低于 2.1.224。

所以：

- 当前已开的公共 Claude session 虽能被 `claude agents --json` 列出，但不能据此认定已启用原生跨 session messaging；
- 当前 tclaude session 不能使用新版原生 cross-session messaging；
- `claude agents`/Agent View 和 cross-session messaging 是相关但不同的能力，前者存在不代表后者可用。

原生 Claude 消息本身还有这些边界：

- 只发送纯文本，不传 conversation history 或文件；
- recipient 可能按 inbound policy 对消息执行 delivered、held 或 refused；
- idle recipient 会自动开始一轮，running recipient 在 tool call 之间读取消息，不会中断正在执行的 tool；
- 消息不能代表用户批准权限，也不能修改配置；
- accepted queue 和 held queue 有容量限制，不能作为 durable event bus；
- 跨容器、不同文件系统、provider 不支持或 feature flag 关闭时不可用。

因此，即使将来打开该能力，也只能把它作为 Claude adapter 的一种**消息投递通道**，不能代替 Hub 自己的任务状态与持久事件存储。

### 2.2 tclaude 的 managed stream-json 模式已验证可行

本机实测命令使用 tclaude 2.1.154 的：

```text
-p
--input-format stream-json
--output-format stream-json
```

在同一个持久进程中连续写入两个 user message，分别得到 `A` 和 `B`，两次返回的 `session_id` 相同，且第一轮结束后进程仍保持运行。

这证明：

- Hub 可以把 tclaude 作为持久、可多轮、可流式读取的 managed agent；
- 不依赖 tclaude 原生 cross-session messaging；
- Hub 可以为它提供统一消息和 UI。

但这个方式的代价是：用户应通过 Hub UI/CLI 继续该 session，而不是同时在另一个独立 tclaude TUI 中写入。

### 2.3 tcodex app-server 的核心链路已验证

通过 `tcodex -- app-server` 已实测：

- `initialize` 成功；
- `thread/start` 成功；
- `turn/start` 成功；
- `item/agentMessage/delta` 能实时返回；
- `turn/completed` 能可靠标记完成；
- 用 app-server 恢复一个已结束的 tcodex session 成功。

这证明 Hub 管理 tcodex thread 和构建实时 UI 是可行的。

但同时发现以下重要限制：

1. **必须使用匹配的 wrapper。** 用公共 `codex app-server` 恢复 tcodex session 会报 `Model provider tencent not found`；改用 `tcodex -- app-server` 才成功。
2. **协议必须按 executable 探测。** 当前 tcodex 0.144.5 接受的 sandbox 值是 `read-only`，不能照搬其他版本示例中的不同枚举形式。
3. **thread inventory 不能简单依赖默认 list。** 在当前状态库中，默认 `thread/list` 返回空；加 `useStateDbOnly=true` 后才能看到已有 tcodex threads。
4. **多 client 的事件订阅有 owner 语义。** 实测 client A 创建 thread，client B 发起 `turn/start` 后，delta 和 completed notification 仍发给 client A，而不是 client B。因此 Hub 必须成为 managed thread 的创建者和主订阅者，不能假设任意后接入 client 都能得到完整流。
5. **独立运行中的 TUI 不能视为安全可接管。** app-server 只知道自己进程内 loaded threads；状态数据库中的 `notLoaded` 不等于另一个 TUI 进程没有正在使用该 thread。由第二个进程 resume 并写入可能形成双写或 transcript 竞争。

### 2.4 Codex 与 tcodex 的状态目录必须隔离

当前执行环境中：

```text
CODEX_HOME=/home/luyi/.tcodex
```

这意味着直接调用公共 `codex` 时，也可能读写 tcodex 的 state DB、sessions 和配置。由于两者版本和 provider 不同，这是高风险配置。

Hub 必须显式固定：

```text
public codex  -> executable=codex,  CODEX_HOME=/home/luyi/.codex
internal     -> executable=tcodex, CODEX_HOME=/home/luyi/.tcodex
```

并且：

- 不允许两个不同版本的 app-server 同时写同一个 CODEX_HOME；
- 每个 adapter 启动时记录 executable path、wrapper version、upstream version、schema hash；
- 使用该 executable 自己生成的 app-server schema；
- session metadata 中保存 runtime family，禁止用另一 family resume。

## 3. 原方案中不够可行或不够严谨的部分

### 3.1 “发现 session”不等于“能控制 session”

`claude agents --json` 可以列出 Claude/Agent View session，Codex state DB 也能列出历史 thread，但这只说明存在 session metadata，不说明：

- recipient 有可写入的 transport；
- Hub 能订阅该 session 的实时输出；
- session 当前没有另一个 writer；
- recipient 会接受消息；
- Hub 可以回答 recipient 的审批请求。

第一版 UI 应把 session 明确分成：

```text
managed      Hub 拥有输入、输出和生命周期
messageable  可发消息，但 Hub 不拥有完整输出流
observable   只能看状态/元数据
offline      历史 session
```

不能把四者都显示成同样可操作。

### 3.2 不能承诺接管任意独立 Codex/tcodex TUI

对于已经在另一个终端启动的 standalone Codex/tcodex：

- 没有已验证的通用 peer inbox；
- 启动第二个 app-server resume 同一 thread 可能造成双 writer；
- state DB 的 status 不是跨进程实时锁；
- tmux `send-keys` 虽可模拟人类输入，但无法可靠判断 composer 状态、审批弹窗、turn 边界和输出结构。

可接受的策略只有三种：

1. 要自动协作的 session 从一开始由 Hub 创建；
2. 用户先关闭 standalone session，再由 Hub resume；
3. 兼容模式下用 Hub-owned PTY/tmux 启动，Hub 从一开始就是唯一终端 owner。

不应支持“后来无侵入附着并写入一个仍在运行的 Codex TUI”。

### 3.3 MCP 不是异步 push

标准 MCP 是 Agent 主动调用工具。recipient idle 时不会主动调用 `await_event`，所以单独配置 Agent Hub MCP 不能唤醒 session。

MCP 适合：

- Agent → Hub：发布 artifact、提交 task、发消息；
- Agent 查询 Hub 状态；
- Agent 获取结构化 artifact metadata。

Hub → Agent 仍需要：

- Claude native peer message；
- Claude managed stream input；
- Claude Channels，但它仍是 research preview；
- Codex app-server `turn/start`/`turn/steer`；
- 或 Hub-owned PTY 兼容层。

### 3.4 “agent 身份”在部分 runtime 只是逻辑层，不是模型原生角色

Hub 数据库和 UI 中应该有 `actor_type=agent`。但在实际模型输入中：

- Claude native cross-session message 能保留 peer session 来源；
- tclaude stream-json 的稳定输入是 user message；
- Codex app-server 的稳定 `turn/start` 输入也是 user input。

所以对 tclaude/Codex 来说，`[AGENTHUB EVENT]` 只是带来源标记的 user turn，并不是一个强隔离的新模型角色。

这意味着：

- 不能只靠 prompt 中的 `authority=informational` 做安全控制；
- Hub 必须在模型外执行权限、预算、路径和命令白名单；
- agent message 不能批准高风险操作；
- 不要用 Codex `thread/inject_items` 伪造 assistant/developer history；该接口面向 raw Responses items，且不会自动开始新 turn。

原结论“不要用 assistant role”仍然正确，但“使用标记后的 user role”只是兼容传输，不是安全边界。

### 3.5 确定性工作流不应依赖 Agent 自己决定是否执行

“B 完成 prompt 后，通知 A 开始生成视频”其实包含两件事：

1. 确定性依赖：prompt artifact ready 后启动视频 job；
2. 需要推理的工作：检查 prompt、选择模型、诊断失败、调整参数。

第一件应由 Hub task engine 直接执行：

```text
artifact.ready -> validate -> acquire idempotency lock -> start video job
```

第二件才交给 A。

否则会遇到：

- A 收到消息但没有调用脚本；
- A 误解路径或参数；
- A 回答“已经开始”但进程实际没启动；
- 消息重复导致生成 job 启动两次；
- A context 太长、compact 或 crash 后丢失任务状态。

最终状态应以 process/job 事实为准，而不是 assistant 文本：

```text
delivery_ack  adapter 接收了消息
agent_ack     Agent 表示理解
job_started   OS process/queue 实际创建成功
job_done      process exit + manifest 校验成功
```

### 3.6 统一实时 UI 只对 managed sessions 有完整保证

Hub-owned tclaude stream-json 与 tcodex app-server 都能提供 token/tool/turn events，因此 managed session 的实时 UI 可行。

对于 unmanaged session：

- `claude agents --json` 只提供 presence/status，不提供完整 transcript stream；
- native Claude peer messages不会自动把双方完整对话复制给 Hub；
- 独立 Codex TUI 的 event stream 不归 Hub 所有；
- tail 私有 JSONL transcript 属于脆弱实现，并可能暴露 reasoning、secret 或不稳定内部格式。

因此 UI 应承诺：

- managed：完整实时 transcript、tool、approval、job stream；
- messageable：Hub 消息记录 + 基础状态；
- observable：session 名、cwd、runtime、idle/running；
- 不承诺 unmanaged session 的完整实时对话。

## 4. 还需补齐的工程细节

### 4.1 单写者与 session lease

每个 managed session 必须有：

```text
owner_instance_id
lease_expires_at
runtime_process_id
runtime_session_id
input_sequence
last_output_sequence
```

只有 lease owner 能写入 session。Hub crash 后由新实例在确认旧 process 不再持有输入通道后接管，不能只看数据库时间戳。

### 4.2 三层 ACK

不要把一个 `ack` 同时表示消息投递、Agent 理解和任务执行：

```text
transport_ack
agent_ack
execution_ack
```

对视频任务，只有 `execution_ack` 和真实 job pid/task id 才能解除依赖。

### 4.3 artifact readiness

`artifact.ready` 前必须：

1. 写临时文件；
2. flush、close，必要时 fsync；
3. schema 校验；
4. 计算 checksum；
5. 原子 rename；
6. 验证 recipient 的 sandbox/mount 可访问；
7. 在 Hub DB 中创建 immutable artifact version。

仅发送绝对路径仍不够，因为 recipient 可能位于不同容器、机器或只读 sandbox。

### 4.4 幂等与重复 job

事件总线使用 at-least-once 时必须给视频 job 设置：

```text
idempotency_key = workflow_id + artifact_sha256 + job_type
```

Hub 在数据库加唯一约束或分布式锁，防止重试消息启动多份相同生成任务。

### 4.5 Agent 间 prompt injection

B 的自然语言、prompt 文件甚至 artifact metadata 都是不可信输入。禁止：

- 把 B 给出的 shell string 直接执行；
- 将 artifact 内文本拼进系统权限规则；
- 允许 B 通过消息修改 A 的 sandbox、approval policy 或 developer instructions；
- 允许跨 Agent 消息代替人类审批。

Hub 只执行结构化、schema 验证后的 intent，并将命令构造成固定 argv。

### 4.6 人工指令与自动消息冲突

当用户正在给 A 输入，而自动 dependency 同时触发时，需要明确优先级：

```text
human interrupt/cancel > approval response > system control
> existing task continuation > new agent request > informational message
```

消息可排队，但不能在未知 composer 状态下直接插入终端。

### 4.7 成本和循环控制

跨 Agent 对话需要：

- `hop_count`；
- `max_hops`；
- workflow token/cost budget；
- 每 session 并发 turn 上限；
- 相同消息去重；
- 对话 TTL；
- cycle detection。

Claude 原生通道自身会节流循环，但 Claude ↔ Codex 的 Hub 消息必须自己实现。

## 5. 修订后的技术架构

```text
                    ┌────────── Web UI ──────────┐
                    │ managed transcript / jobs │
                    │ observable session status │
                    └─────────────┬──────────────┘
                                  │
┌─────────────────────────────────▼─────────────────────────────────┐
│ Agent Hub                                                       │
│ durable tasks │ event log │ artifact registry │ policy/approval │
│ idempotency   │ leases    │ runtime inventory │ WebSocket fanout│
└──────────┬────────────────────────┬───────────────────────┬───────┘
           │                        │                       │
 Claude-managed adapter      Codex-managed adapter       Job runner
 stream-json / SDK           matching app-server        run_pipeline
           │                        │
 claude or tclaude           codex or tcodex
 dedicated env/config        dedicated CODEX_HOME
```

关键变化：

- Hub 是 managed session 的唯一 writer；
- runtime adapter 必须绑定具体 executable 和配置目录；
- peer messages 只是 transport，durable truth 在 Hub；
- 确定性 dependency 直接触发 job；
- unmanaged sessions 不承诺完整控制。

## 6. 推荐 MVP

先不要做“所有已经打开的 session 都能互相聊天”，而是做一个严格可验证的最小闭环：

1. Hub 启动 `prompt-b`：tclaude stream-json managed session；
2. Hub 启动 `video-a`：tcodex app-server managed thread；
3. B 调用 Hub MCP 发布 prompt artifact；
4. Hub 校验并直接创建 video job；
5. Hub 同时向 A 发送结构化事件，让 A 监控和诊断；
6. Web UI 展示 B、artifact、job、A 的统一 timeline；
7. 以 process pid、manifest 和 checksum 判定成功；
8. 用户所有输入从 Hub UI 进入，避免双 writer。

该 MVP 已有足够本机证据支持，主要未知点只剩：

- tclaude/tcodex 长时间运行数小时后的稳定性；
- approval callback 的完整 UI；
- wrapper 升级后的协议兼容；
- 大日志流下的 backpressure；
- Agent crash 后 session resume 的恢复语义。

这些应通过 pilot 测试解决，而不是靠架构假设。

## 7. 最终判断

| 目标 | 判断 |
|---|---|
| Hub-managed Claude/Codex/tclaude/tcodex 互通 | **可行，核心协议已本机验证** |
| Prompt artifact 完成后自动启动视频生成 | **可行，且应由 Hub 直接触发** |
| managed sessions 的统一实时 Web UI | **可行** |
| 新版、功能已启用的 Claude session 之间直接通信 | **可行，但当前本机 session 实际被环境变量关闭** |
| 当前 tclaude session 使用原生 cross-session messaging | **不可行，版本和环境均不满足** |
| 无侵入接管任意正在运行的 Codex/tcodex TUI | **不应承诺，存在双写和事件订阅问题** |
| unmanaged sessions 的完整统一实时 transcript | **不可靠** |
| 用 assistant role 伪造其他 Agent 消息 | **不可行且设计错误** |

所以答案不是“原方案完全没问题”，而是：

> **控制面架构可行，但产品边界必须收紧为 managed-first；工作流触发必须确定性化；现有独立 session 的接入只能按 runtime 分级支持。**
