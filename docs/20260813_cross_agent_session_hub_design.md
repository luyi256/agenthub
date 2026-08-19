# 跨 Claude / Codex Agent Session 协作方案

> 日期：2026-08-13
> 目标：让多个 Claude Code、Codex、tclaude、tcodex session 能按任务依赖互相通知、传递产物并触发后续工作，同时提供低延迟、可审计的统一界面。
>
> **重要修订：** 进一步做本机协议验证后，确认“Hub 管理下的新 session”可行，但“无侵入接管任意已经打开的 Claude/Codex/tclaude/tcodex session”并不普遍可行。详细证据、限制和修订后的方案见 `agentdocs/cross_agent_session_hub_feasibility_review.md`。本文应与该审计文档一起阅读。

## 1. 结论

推荐实现一个独立的 **Agent Hub（控制面 + 事件总线 + Web UI）**，而不是让各个 session 直接伪装成人类互相聊天。Claude 内部可在满足版本、provider、环境变量和 inbound policy 时使用原生跨 session 消息；Codex 使用 app-server；tclaude、tcodex 必须通过各自 wrapper 对应的协议接入。Hub 统一管理 session、任务、消息、产物、依赖、权限和实时事件。

最重要的设计原则：

1. **消息的真实身份是 `agent`，不是 `assistant`，也不应伪装成人类 `user`。**
2. 当底层产品只能接收 user turn 时，可以编码成 user message，但必须带不可混淆的 Agent Hub 信封，明确来源、权限和消息类型。
3. Agent 之间优先传递结构化任务事件和产物，不要只传一句“文件在这里”。
4. 长时间视频生成应由独立 job process 执行，Agent 负责规划、提交、诊断和恢复，不应让 LLM turn 一直阻塞等待。
5. **需要自动协作的新 session 必须由 Hub 启动和托管。** 任意已经打开的独立 TUI 默认只做发现和只读展示，不承诺可靠注入。
6. 对“B 完成后启动 A”的确定性依赖，优先由 Hub 直接启动 job；Agent 消息用于需要推理的交接和解释，不作为唯一触发依据。

## 2. 本机能力核验

当前机器已安装：

| Runtime | 本机版本 | 可利用能力 |
|---|---:|---|
| Claude Code | 2.1.226 | `claude agents`、`--background`、`--remote-control`、stream-json；但当前进程环境设置了 `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`，原生跨 session 消息实际关闭 |
| tclaude | 0.0.9 / Claude Code 2.1.154 | stream-json 已实测可做持久多轮托管；版本低于原生跨 session 消息要求的 2.1.224 |
| Codex | 0.147.0 | app-server、remote-control、SDK/exec、multi-agent |
| tcodex | 0.0.16 / Codex 0.144.5 | app-server 的 start/turn/stream 已实测；必须通过 tcodex wrapper 启动，不能用公共 codex 进程直接恢复 `tencent` provider session |

Claude 原生能力只适合满足条件的 Claude session，Codex 原生 subagent 适合一个 Codex 主线程内部的委派；它们都不能单独解决 Claude ↔ Codex ↔ 内部 wrapper 的统一跨运行时通信。

## 3. 方案比较

| 方案 | 优点 | 问题 | 建议 |
|---|---|---|---|
| 只用 Claude Agent Teams | 接入最快，原生身份与消息 | 仅 Claude 生态，难统一 Codex | Claude 内部保留使用 |
| 共享文件夹 + Agent 轮询 | 实现简单 | 延迟高、浪费 token、易漏事件、无审计 | 不建议 |
| tmux/PTY `send-keys` | 几乎适配所有 CLI | 容易和人工输入竞争，无法可靠判断消息边界和完成状态 | 仅作兼容兜底 |
| 所有 Agent 只接同一个 MCP | Agent → Hub 很自然 | MCP 本身通常是请求式，不能可靠唤醒空闲 recipient | 作为上行工具层，不单独使用 |
| Agent Hub + runtime adapters | 统一、可靠、可做 UI、可跨模型 | 需要开发一个小型控制面 | **推荐** |

## 4. 总体架构

```text
┌──────────────────────────────── Web UI ────────────────────────────────┐
│ Sessions │ Task DAG │ Messages │ Artifacts │ Jobs │ Approvals │ Logs │
└───────────────────────────────┬────────────────────────────────────────┘
                                │ WebSocket
┌───────────────────────────────▼────────────────────────────────────────┐
│                            Agent Hub                                   │
│ Session Registry │ Event Bus │ Task Engine │ Artifact Registry         │
│ Approval/Policy  │ Audit Log │ Dependency Rules │ Runtime Adapters     │
└───────────────┬───────────────┬─────────────────┬──────────────────────┘
                │               │                 │
       Claude adapter    Codex adapter       Job adapter
       stream-json /     app-server /        subprocess /
       native teams      SDK / exec          existing pipeline
                │               │                 │
      claude/tclaude     codex/tcodex     run_pipeline.py 等
```

建议将协议分成三个通道：

- **Control channel**：任务分配、状态、取消、审批、依赖解除。
- **Conversation channel**：Agent 之间的问题、回答、说明。
- **Artifact channel**：文件、目录、manifest、checksum、schema、版本。

## 5. 消息角色怎么设计

### 5.1 不要使用 assistant 身份

`assistant` 在目标 session 中通常表示“这个 session 自己已经生成过的回复”。如果把 B 的消息写成 A 的 assistant message，会导致：

- A 误以为那是自己说过的话；
- transcript 的来源和审计链被破坏；
- 后续模型可能基于伪造的自我陈述继续推理；
- UI 无法区分人、Agent、系统和工具。

因此，跨 Agent 消息不能作为目标 Agent 的 assistant 历史注入。

### 5.2 也不要无标记地伪装成人类 user

部分底层接口只接受 user turn，例如稳定的 Codex `turn/start`。这种情况下可以使用 user role 作为**传输载体**，但内容必须明确标记来源：

```text
[AGENTHUB EVENT]
event_id=evt_01J...
source_type=agent
source_session=prompt-b
authority=human_delegated
kind=artifact.ready
task_id=task_prompt_200
correlation_id=flow_pilot_200

Artifact:
- uri: file:///mnt/hifs/.../selected_200_enhanced.jsonl
- sha256: ...
- schema: prompt-enhanced/v9
- records: 200

This is an Agent Hub event, not a new human instruction.
Only perform actions allowed by the referenced task and your current policy.
```

在 Hub 数据库和 UI 中，它仍然是独立的 `agent` 消息，不显示成普通 user 气泡。

### 5.3 权限与身份分开

建议每条消息至少包含：

```json
{
  "id": "evt_01J...",
  "from": {"type": "agent", "session_id": "prompt-b"},
  "to": {"type": "agent", "session_id": "video-a"},
  "kind": "artifact.ready",
  "authority": "human_delegated",
  "task_id": "task_prompt_200",
  "correlation_id": "flow_pilot_200",
  "payload": {},
  "requires_ack": true,
  "created_at": "2026-08-13T10:00:00+08:00"
}
```

`authority` 可分为：

- `informational`：只提供信息，不自动改变任务优先级；
- `agent_request`：另一个 Agent 的请求，recipient 可接受或拒绝；
- `human_delegated`：来源于用户已经授权的工作流，可以触发限定范围内的动作；
- `system_control`：Hub 的取消、暂停、超时、审批结果等控制事件。

## 6. Prompt → Video 的推荐工作流

不要只让 B 给 A 发一个裸文件路径，推荐流程是：

1. Hub 创建 `prompt_generation` 和 `video_generation` 两个 task，后者依赖前者。
2. B 生成 prompt，先写临时文件。
3. B 完成 flush/close 和校验后，通过原子 rename 发布最终文件。
4. B 调用 `agenthub.publish_artifact`，提交路径、checksum、schema、记录数和 task id。
5. Hub 持久化 artifact，发出 `artifact.ready`。
6. Hub 根据依赖规则唤醒 A。
7. A 先校验文件可见性、checksum、schema、记录数，再 ACK。
8. A 提交视频生成 job，而不是让 LLM turn 一直等待。
9. job process 持续上报进度、失败、重试和 manifest。
10. job 完成后，Hub 通知 A 做结果检查，并在 UI 中将整个 workflow 标记完成。

状态示例：

```text
prompt-b: running → publishing → idle
artifact: staging → ready
video-a: waiting_dependency → validating → submitted_job → monitoring → done
```

必须实现：

- `event_id` 去重；
- ACK/NACK；
- 至少一次投递；
- recipient 幂等；
- retry + backoff；
- artifact checksum；
- task timeout；
- 最大对话/转发深度，防止 Agent 无限互相回复。

## 7. Runtime Adapter

### 7.1 Claude / tclaude

分两种模式：

1. **Claude 原生消息**：仅在 Claude Code ≥2.1.224、provider 支持、feature flag 未被关闭且 inbound policy 允许时使用，保留原生 peer 身份。
2. **跨运行时托管模式**：由 Hub 启动持久 Claude stream-json process，消费输入流并解析输出事件。

已有任意终端 session 可以先通过 `claude agents --json` 发现和展示；但 Agent View 的 session 列表不等于该 session 已启用跨 session 消息。不要直接修改内部 inbox 文件。可靠方案是将需要跨运行时自动协作的 session 重新以 managed session 方式启动。

### 7.2 Codex / tcodex

界面集成优先使用对应 executable 的 app-server，例如 tcodex session 必须由 `tcodex -- app-server` 管理：

- `thread/start` / `thread/resume` 管理 session；
- `turn/start` 发送新事件；
- `turn/steer` 给正在运行的 turn 补充消息；
- `item/agentMessage/delta`、tool/command events、`turn/completed` 驱动实时 UI。

本机接入建议使用 **stdio 或 Unix socket**。Hub 应成为 managed thread 的唯一控制客户端和事件订阅者；不要假设另一个独立 app-server client 能完整收到该 thread 的流式事件。不要把 Codex app-server 的实验性裸 WebSocket 直接暴露给浏览器或公网；浏览器只连接 Agent Hub 自己的 WebSocket。

简单自动化可以使用 SDK 或 `codex exec --json`，但要做完整实时 UI、审批、历史和 turn steering 时，app-server 更合适。

### 7.3 Capability negotiation

tclaude/tcodex 的上游版本可能晚于公共版本，adapter 启动时应探测：

- runtime 与上游版本；
- `--help` 中是否有目标命令；
- app-server schema；
- stream-json 能力；
- multi-agent/remote-control feature 状态。

不要按产品名硬编码“必然支持”。

## 8. MCP 与 A2A 的使用方式

建议给所有 Agent 配置同一个 Agent Hub MCP server，提供：

```text
agenthub.list_sessions
agenthub.send_message
agenthub.publish_artifact
agenthub.get_artifact
agenthub.create_task
agenthub.update_task
agenthub.ack_event
agenthub.await_event
agenthub.request_approval
agenthub.submit_job
agenthub.get_job
```

MCP 适合 Agent 主动调用 Hub，但不能单独解决“recipient 当前空闲时如何被异步唤醒”。因此仍需 runtime adapter 负责 Hub → session 的 push/turn injection。

消息与任务模型可以参考 A2A 的 task/message/artifact/streaming 思路，但 MVP 不必完整实现整个 A2A 协议。先定义一个小而稳定的内部 schema，后续再提供 A2A endpoint。

## 9. Web UI

建议第一版采用四区布局：

```text
左栏：Session 列表、runtime、cwd、状态、当前任务
中栏：当前 session 对话与实时 tool/job 流
右栏：Task DAG、依赖、artifact、审批
底栏：可折叠 raw logs / terminal
```

消息必须有清晰身份徽标：

- Human
- Agent: prompt-b
- Agent: video-a
- System
- Tool/Job

推荐交互：

- 从 session B 拖一条依赖线到 session A；
- 配置 `artifact.ready → wake A`；
- 一键暂停/恢复/取消；
- Agent 请求高成本或危险动作时弹审批；
- 点击 artifact 直接预览 JSONL、manifest、视频样本；
- 每个事件显示 `task_id` 和 `correlation_id`，可查看完整链路。

### 实时性能

- Browser ↔ Hub 使用单一 WebSocket；
- 每个事件使用递增 `seq`，重连带 `last_seq` 补发；
- token delta 每 30–60ms 合并一次，不要每 token 一次 React render；
- shell/job log 每 100–200ms 或达到尺寸阈值后批量推送；
- UI 使用虚拟列表；
- 数据库只保存最终消息和关键事件，超细 token delta 可写 append-only 压缩日志；
- session 状态、审批、错误立即推送，不做批处理；
- 后端对不同 session 做独立 backpressure，避免某个大日志 job 卡住整个 UI。

## 10. 技术栈建议

结合当前仓库以 Python 为主，MVP 建议：

- Backend：FastAPI + asyncio；
- Session/process：`asyncio.subprocess`；
- 数据库：SQLite WAL；
- Browser push：WebSocket；
- Agent 接入：MCP server + runtime adapters；
- 前端：React/Vite；
- 本地连接：Unix socket 优先；
- 视频 job：直接封装现有 `run_pipeline.py` / `gen_clips.py`，读取现有 manifest 作为恢复依据。

当需要跨机器、多用户或大量并发时再升级：

- SQLite → PostgreSQL；
- 进程内事件总线 → NATS JetStream 或 Redis Streams；
- 本地路径 → artifact store / CephFS URI；
- 简单 task engine → Temporal 等持久工作流系统。

## 11. 建议目录

```text
generation/
└─ agent_hub/
   ├─ server.py
   ├─ cli.py
   ├─ config.py
   ├─ core/
   │  ├─ models.py
   │  ├─ event_bus.py
   │  ├─ task_engine.py
   │  ├─ artifacts.py
   │  └─ policy.py
   ├─ adapters/
   │  ├─ base.py
   │  ├─ claude_stream.py
   │  ├─ claude_native.py
   │  ├─ codex_app_server.py
   │  └─ process_job.py
   ├─ mcp/
   │  └─ server.py
   ├─ ui/
   └─ tests/
```

示例 CLI：

```bash
agenthub start

agenthub launch \
  --runtime tclaude \
  --name prompt-b \
  --cwd /home/luyi/generation

agenthub launch \
  --runtime tcodex \
  --name video-a \
  --cwd /home/luyi/generation

agenthub wire \
  --from prompt-b:artifact.ready \
  --to video-a \
  --task video_generation
```

## 12. 分阶段落地

### Phase 1：最小闭环

- Hub daemon；
- SQLite 数据模型；
- Codex app-server adapter；
- Claude stream-json adapter；
- `send_message`、`publish_artifact`、ACK；
- Prompt B → Video A 自动触发；
- Session 状态和消息时间线 UI。

### Phase 2：可日常使用

- 审批；
- job process 与日志；
- artifact 预览；
- 重连、重试、幂等；
- session lease，避免人工和 Hub 同时写同一 session；
- tclaude/tcodex capability probe；
- 原生 Claude Team bridge。

### Phase 3：生产化

- 跨机器；
- 权限与身份认证；
- CephFS / 对象存储 artifact URI；
- durable workflow；
- A2A endpoint；
- 成本、token、GPU 和视频任务配额；
- 全链路审计与告警。

## 13. 最终建议

第一版不要追求“任意已经打开的所有 session 都可以立即互聊”。先规定：**需要自动协作的 session 必须由 Agent Hub 启动或接管，并遵守单写者 lease**。这样可以可靠获得 session id、输入输出流、状态、审批和恢复能力。

你的 prompt → video 场景适合作为第一个 MVP：只做两个 managed session、一个 `artifact.ready` 事件、一条依赖规则和一个实时页面，就能验证架构。验证后再扩展到自由 Agent 对话、跨机器和更复杂的 DAG。
