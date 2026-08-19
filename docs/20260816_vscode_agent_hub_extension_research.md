# Agent Hub VS Code 扩展方案调研

> 日期：2026-08-16
> 结论：**VS Code 扩展方案可行，而且比继续打磨独立 HTML 更适合当前需求。**
> 本轮只完成调研与架构收敛，尚未安装扩展、修改 VS Code 配置或改变现有 CLI 使用方式。

## 1. 用户需求重新归纳

核心需求不是做一个 session dashboard，而是：

1. 日常工作入口就是 VS Code 右侧 AI Chat；
2. 对话天然归属于当前 VS Code 项目，不需要再猜 session 对应哪个目录；
3. 新建 Claude、tclaude、Codex、tcodex 对话时自动命名并显示 runtime；
4. 对话关闭 VS Code 后仍继续存在；
5. 每个项目都有自己的 tmux session，新对话在该项目的 tmux 中运行；
6. 能从 VS Code 打开对应 tmux，检查进程、日志或故障；
7. 将来可以让不同 Agent session 互发消息、传 artifact、建立依赖；
8. 不能影响现在直接在终端运行 `claude/tclaude/codex/tcodex` 的方式。

之前的独立 HTML 将“session 管理”做成了主任务，导致界面接近运维 dashboard，而不是 ChatGPT、Claude Code、Codex 一类的对话产品。VS Code 扩展能让“当前项目”和“当前编辑上下文”天然明确，因此应改走扩展路线。

## 2. 本机环境结论

本机已经具备实施条件：

- VS Code Remote SSH 正在运行，当前存在远程 Extension Host；
- 当前 CLI 可见 VS Code 版本为 1.126.0，正在运行的远程 server 中包含 1.129.1，机器上还缓存了 1.131–1.133；
- 已安装 `anthropic.claude-code 2.1.233`；
- 该 Claude 扩展本机 manifest 已经使用：
  - `viewsContainers.secondarySidebar`；
  - `type: "webview"` 的右侧栏视图；
  - `Claude Code: Open in Side Bar`；
  - 自定义 webview UI；
- tmux 3.4 已安装并长期使用；
- 已验证可通过命令为指定 cwd 创建 tmux session/window；
- `@vscode/vsce` 3.9.2 可用于打包 VSIX；
- Node.js 22 可用。

这说明“像其他 AI Chat 插件一样放在右侧栏”不是推测，本机现有 Claude Code 扩展就是直接先例。

## 3. UI 技术路线比较

### 3.1 推荐：Secondary Sidebar + Webview View

扩展贡献一个独立的右侧栏 View Container：

```json
{
  "contributes": {
    "viewsContainers": {
      "secondarySidebar": [
        {
          "id": "agenthub.sidebar",
          "title": "Agent Hub",
          "icon": "media/agenthub.svg"
        }
      ]
    },
    "views": {
      "agenthub.sidebar": [
        {
          "type": "webview",
          "id": "agenthub.chatView",
          "name": "Agent Hub"
        }
      ]
    }
  }
}
```

扩展端使用稳定 API：

```ts
vscode.window.registerWebviewViewProvider(...)
```

优点：

- 可实现完整、现代的聊天 UI；
- 可以在右侧 Secondary Sidebar 常驻；
- 不依赖 GitHub Copilot；
- 可以展示 runtime selector、session picker、流式消息、tool call、approval、artifact；
- 能使用 VS Code 主题变量，视觉上自然融入编辑器；
- 可以提供 `Open in Editor` 命令，将同一会话放大到 editor tab；
- 可与现有 Claude Code 扩展共存，由用户切换右侧栏容器。

Webview 不应该继续沿用当前 HTML 的三栏 dashboard，而应设计成单列 chat：

```text
┌─────────────────────────────┐
│ project · generation   ＋ ⋯ │
│ [video-agent ▾] [tcodex]    │
├─────────────────────────────┤
│                             │
│ user message                │
│                             │
│ assistant response          │
│ tool activity ▸             │
│ approval card               │
│ artifact chip               │
│                             │
├─────────────────────────────┤
│ # file / @ agent / prompt   │
│ [model] [mode]       Send   │
└─────────────────────────────┘
```

Session 列表不再永久占据一列，而是使用顶部 picker 或抽屉。这更符合 Claude Code/Codex/ChatGPT 的交互。

### 3.2 不推荐作为核心：Chat Participant API

Chat Participant API 可以注册 `@agenthub`，让用户在 VS Code 内建 Chat 中调用扩展。但它更适合“一个领域助手”，不适合本项目的核心需求：

- 多 runtime；
- 独立 session 生命周期；
- session picker；
- tmux 映射；
- 审批 UI；
- Agent-to-Agent link；
- 每个对话不同 cwd、权限和模型。

可在后期把 `@agenthub` 作为快捷入口，例如：

```text
@agenthub ask generation/video to inspect the failed clips
```

但不应作为主界面。

### 3.3 不采用：VS Code 原生 Chat Sessions Provider

VS Code 源码已经有 `chatSessions`/`ChatSessionItemController`，从能力上非常接近需求，但截至本次调研，该 API 仍属于 proposed API。普通扩展使用 proposed API 需要特殊启动参数，且官方不建议发布到 Marketplace。

因此不能把第一版建立在这个 API 上。GitHub/第一方扩展可以使用，并不代表我们的普通 VSIX 可以稳定依赖。

结论：

```text
第一版：稳定 Webview View
以后 API 稳定：再评估迁移到原生 Chat Sessions
```

## 4. Remote SSH 与项目归属

扩展必须声明：

```json
{
  "extensionKind": ["workspace"]
}
```

这样在 Remote SSH 中扩展逻辑运行于远程 workspace Extension Host，能够直接：

- 访问远程项目文件；
- 调用远程的 `tmux`；
- 运行 tclaude/tcodex；
- 连接远程 Agent Hub socket；
- 使用远程 cwd。

不能依赖：

```ts
process.cwd()
```

本机当前远程 Extension Host 的 `PWD` 是 `/home/luyi`，并不一定等于用户正在查看的 `/home/luyi/generation`。项目必须从 VS Code API 获取：

```ts
vscode.workspace.workspaceFolders
vscode.workspace.workspaceFile
```

### Workspace identity

建议生成稳定 workspace ID：

```text
sha256(
  remoteAuthority
  + sorted(canonical workspace folder URIs)
  + workspace file URI
)
```

单目录项目：

```text
projectSlug = generation
workspaceId = 5bb738bb
```

多 root workspace：

- 优先使用 `.code-workspace` 文件名；
- hash 包含所有 folder URI；
- 新对话时允许选择运行 cwd；
- 默认 cwd 为当前 active editor 所属 workspace folder。

## 5. tmux 架构

### 5.1 一个项目一个 tmux session

命名：

```text
ah-<project-slug>-<workspace-hash>
```

示例：

```text
ah-generation-5bb738bb
ah-filmops-2137cd6b
```

不能只用 basename，因为不同路径可能同名。

### 5.2 每个 Agent 对话一个 tmux window

结构：

```text
ah-generation-5bb738bb
├─ hub
├─ prompt-a83f
├─ video-c21d
└─ reviewer-17bc
```

- `hub`：当前项目的 bridge/registry；
- 其他 window：一个 window 对应一个 Agent session worker；
- window name 使用 alias slug + 短 UID，避免重名；
- worker 的 cwd 始终是 VS Code 对应项目目录；
- tmux 设置 `remain-on-exit`，失败后保留退出码和日志。

### 5.3 为什么不能直接把普通 TUI 当聊天传输层

VS Code 稳定 Terminal API 可以创建 terminal、执行 shell integration command、读取由 shell integration 启动的 command stream，但不能可靠读取和操纵任意长时间运行的全屏 TUI。

所以不能采用：

```text
Webview
  ↕ 模拟键盘
VS Code Terminal
  ↕ 抓屏/解析 ANSI
Claude/Codex TUI
```

这种方案会在审批弹窗、alternate screen、输入法、resize 和并发输入时失效。

正确方案是：

```text
Webview
  ↕ postMessage
VS Code extension host
  ↕ Unix socket / JSON-RPC
Agent Hub daemon
  ↕ structured protocol
tmux worker
  ├─ Claude/tclaude stream-json
  └─ Codex/tcodex app-server
```

tmux 负责：

- 生命周期；
- 进程存活；
- inspect/debug；
- VS Code 断线后的继续运行。

结构化协议负责：

- 用户消息；
- token delta；
- tool call；
- approval；
- turn completed；
- session resume。

### 5.4 Open Project tmux

扩展提供命令：

```text
Agent Hub: Open Project tmux
Agent Hub: Open Current Session Window
```

用稳定 Terminal API 创建 VS Code integrated terminal：

```ts
vscode.window.createTerminal({
  name: `Agent Hub · ${project}`,
  shellPath: "tmux",
  shellArgs: ["attach-session", "-t", tmuxSession],
  cwd: workspaceFolder.uri
});
```

或者直接切到某个 window：

```text
tmux attach-session -t ah-generation-5bb738bb \; select-window -t video-c21d
```

注意：tmux window 默认展示 worker 的结构化日志，不是另一个可并发输入的 Agent TUI。避免 Webview 和 Terminal 同时成为 writer。

后期可增加“移交到原生 TUI”，但必须先让 web worker 释放 writer。

## 6. 推荐进程架构

### 6.1 全局 coordinator

复用当前 Python Agent Hub 后端，但逐渐将其变成 headless coordinator：

```text
tmux: agenthub-control
└─ agenthubd
```

职责：

- session registry；
- alias；
- message/event store；
- Agent-to-Agent routing；
- artifact registry；
- approval；
- 跨项目 link；
- worker heartbeat。

不再把独立 HTML 作为主要产品。

### 6.2 Project bridge

每个项目 tmux 的 `hub` window：

```text
python -m agent_hub.project_bridge \
  --workspace-id 5bb738bb \
  --cwd /home/luyi/generation
```

职责：

- 向全局 coordinator 注册 workspace；
- 创建/停止 project worker；
- 保持 tmux session；
- 汇总 worker heartbeat；
- extension reload 后重新连接。

### 6.3 Session worker

每个会话一个 worker：

```text
python -m agent_hub.session_worker \
  --config ~/.agenthub/workspaces/5bb738bb/sessions/<uid>.json
```

Runtime adapter：

| Runtime | Worker 内部 transport |
|---|---|
| Claude | `claude -p --input-format stream-json --output-format stream-json` |
| tclaude | `tclaude -- -p ... stream-json` |
| Codex | `codex app-server` stdio JSON-RPC |
| tcodex | `tcodex -- app-server` stdio JSON-RPC |

每个 worker 是对应 runtime 的唯一 writer。

### 6.4 Extension 与 daemon 通信

推荐 Unix domain socket：

```text
~/.agenthub/run/control.sock
```

不要让 Webview 直接访问 socket。通信路径：

```text
Webview postMessage
→ Extension Host
→ Node net/http client over Unix socket
→ Agent Hub
```

原因：

- Webview 有 CSP；
- Remote SSH 中浏览器端 localhost 与远程 localhost 概念不同；
- 不需要公开端口；
- 可以用文件权限限制同用户访问；
- Extension Host 本身就在远端。

socket 路径需要足够短，Linux UDS 有长度限制。

## 7. VS Code UI 详细设计

### Header

```text
generation                         ＋ ⋯
video · tcodex · running              ●
```

- 当前 workspace 固定显示；
- session picker 显示 alias、runtime、状态；
- `+` 新建会话；
- `⋯` 包含 open tmux、rename、fork、stop、link agent。

### Transcript

只显示 Chat 产品需要的内容：

- Human message；
- Assistant Markdown；
- reasoning summary 折叠；
- tool call 折叠；
- command output 折叠；
- approval card；
- artifact/file chip；
- error/retry。

不显示 dashboard metric cards。

### Composer

```text
Type a message…

# context   @ agent   / command
[tcodex] [safe] [model]       Send
```

- Enter 发送、Shift+Enter 换行，可配置；
- `#` 选择当前文件、selection、问题、terminal output；
- `@` 引用其他 Agent session；
- 权限、model、effort 放 composer footer；
- active editor 和 selection 可自动作为可移除的 context chip。

### Session drawer

Session drawer 按当前 workspace 展示：

```text
Running
  video        tcodex     generating
  prompt       tclaude    idle

Recent
  reviewer     codex      yesterday
```

外部终端 session 可放到一个单独的 “External sessions” 区，仅用于发现和未来迁移，不与 Hub-managed chat 混在一起。

### Hidden view 的事件处理

Webview 被隐藏时可能暂停，也不能假设每个 token delta 都能实时送达。因此：

- daemon 是最终状态源；
- extension 对 hidden view 不缓存无限 token；
- view 再次 visible 时请求 transcript snapshot；
- delta 只用于可见状态的低延迟体验；
- 消息完成后保存最终 assistant message。

## 8. Session 创建流程

用户点击 `+`：

```text
Runtime: tcodex
Name: video
CWD: /home/luyi/generation
Permission: Safe
Model: default
```

后台：

1. Extension 通过 VS Code API得到 workspace identity；
2. 确保 `ah-generation-5bb738bb` 存在；
3. 写 session config 文件，权限 `0600`；
4. `tmux new-window` 创建 `video-c21d`；
5. worker 启动对应 runtime；
6. worker 向 coordinator 注册；
7. Webview 打开新对话；
8. 用户第一条消息开始模型 turn。

Extension 本身不拥有 runtime process。VS Code 窗口关掉后，tmux worker 继续运行。

## 9. Session 命名

延续此前四层模型：

```text
session_uid     不可变
runtime_id      Claude session ID / Codex thread ID
alias           用户稳定名称，例如 generation/video
display_title   当前任务摘要
```

在当前项目 UI 内可简写成：

```text
video
prompt
reviewer
```

跨项目寻址时显示：

```text
generation/video
filmops/reviewer
```

tmux window：

```text
video-c21d
```

tmux window name 永远带短 UID，alias 改名不会破坏内部引用。

## 10. 安全和权限

### 现有 CLI

完全不修改：

```text
claude
tclaude
codex
tcodex
```

用户仍可按原方式使用。

### 插件创建的会话

只有点击扩展里的 `New Session` 才创建 tmux worker。

权限：

- `Read only`；
- `Safe/workspace write`；
- `Full access`。

Agent-to-Agent 消息永远不能作为人类审批。

### Webview

- 严格 Content Security Policy；
- nonce script；
- 不加载外部 CDN；
- Markdown HTML sanitize；
- extension 与 webview message 做 schema 校验；
- 不把凭据发到 webview；
- daemon socket 限当前 OS user；
- session config 文件 `0600`；
- tmux command 参数固定，alias 不直接拼 shell。

## 11. 主要风险

### 风险 1：每个 session 一个 worker 的资源消耗

Claude/tclaude 每会话一个进程是自然模型；Codex/tcodex 每会话一个 app-server 会比共享 app-server 更重。

第一版优先采用每会话一个 worker，以换取：

- 进程隔离；
- tmux window 一一对应；
- 简单 writer ownership；
- 容易停止/重启。

后续可优化为一个 workspace runtime daemon + 多 thread。

### 风险 2：Claude/tclaude 审批

当前 stream-json CLI 路线不如 Agent SDK 的 approval callback 完整。第一版：

- Codex/tcodex 支持网页审批；
- Claude/tclaude 默认 `acceptEdits` 或 plan；
- 更完整审批需要改为 Claude Agent SDK worker。

### 风险 3：VS Code 多版本

当前机器同时缓存多个 VS Code server 版本。扩展应：

- 使用稳定 API；
- `engines.vscode` 保守设置为 `^1.98.0`；
- 不使用 proposed API；
- 在实际 Remote SSH 窗口做 Extension Host 测试。

### 风险 4：多窗口同时打开同一 workspace

两个 VS Code 窗口可能对应同一个 workspace ID。必须：

- 共享同一 project tmux；
- coordinator 允许多个 UI subscribers；
- session worker 保持单 writer；
- alias 更新使用版本号或最后写入检测。

## 12. 实施建议

### Phase 1：VS Code UI 原型

新目录：

```text
agent_hub_vscode/
├─ package.json
├─ src/extension.ts
├─ src/hubClient.ts
├─ src/workspaceIdentity.ts
├─ webview/
│  ├─ index.tsx
│  └─ styles.css
└─ media/
```

功能：

- Secondary Sidebar；
- 当前项目标题；
- Hub session picker；
- transcript；
- composer；
- 创建 tcodex/tclaude session；
- reconnect；
- Open Project tmux。

先连接当前 Agent Hub 后端，验证 VS Code UX，不立即重构 worker。

### Phase 2：Project tmux worker

- global coordinator；
- workspace tmux manager；
- session worker；
- extension reload/reconnect；
- new session 一 window；
- worker logs。

### Phase 3：Agent collaboration

- `@agent`；
- Link 实际投递；
- artifact handoff；
- deterministic job trigger；
- cross-project routing。

### Phase 4：已有 CLI 迁移

- External sessions drawer；
- safe handover；
- close standalone writer；
- resume into worker；
- 保留 conversation history。

## 13. 最终判断

| 问题 | 结论 |
|---|---|
| 能否做成 VS Code 右侧 AI Chat 插件 | **可以，本机 Claude 扩展已经使用同一模式** |
| 是否需要继续使用独立 HTML 作为主入口 | **不需要；保留为调试/备用即可** |
| 能否识别当前 VS Code 项目 | **可以，必须使用 workspace API，不能使用 process.cwd** |
| 能否每个项目一个 tmux session | **可以** |
| 能否每个新对话一个 tmux window | **可以，推荐 session worker 模式** |
| VS Code 关闭后对话能否继续 | **可以，worker 由 tmux 持有** |
| 能否直接抓取普通 TUI 做聊天 UI | **不可靠，不采用** |
| 能否使用 VS Code 原生 Chat Sessions | **目前 proposed，不作为第一版依赖** |
| 是否影响现有 CLI | **不需要影响；插件只管理自己创建的 session** |

**推荐结论：停止继续打磨当前独立 HTML 的视觉界面，保留 Python Hub 作为后端，下一步开始实现 VS Code Secondary Sidebar 扩展原型。**
