# Agent Hub 独立目录迁移

2026-08-17，Agent Hub 从 `/home/luyi/generation` 迁移到独立目录：

```text
/home/luyi/agenthub/
├── agent_hub/
├── agent_hub_vscode/
└── docs/
```

Hub 后端源码、SQLite、worker 状态、测试、VS Code 扩展和 Agent Hub/tmux 相关文档均归入该目录。被管理 session 的 `cwd` 仍指向其实际项目，例如 generation session 继续使用 `/home/luyi/generation`；这不是 Agent Hub 自身文件残留。

正式后端入口：

```bash
cd /home/luyi/agenthub
env AGENTHUB_TMUX_SOCKET=agenthub \
  bash agent_hub/run.sh --host 127.0.0.1 --port 8766
```

VS Code 扩展从 0.1.3 起默认使用 `/home/luyi/agenthub` 作为 `agentHub.serverProjectPath`。

## 完成状态

- 正式 SQLite、备份、runtime log 和 worker 状态已迁入 `agent_hub/data/`。
- coordinator 已从 `/home/luyi/agenthub` 启动。
- generation 项目的 managed session 保留原 cwd、workspace ID 和 runtime ID。
- VS Code Remote 扩展已安装为 `luyi-local.agent-hub@0.1.3`。
- Machine settings 已设置：

  ```json
  {
    "agentHub.autoStartServer": true,
    "agentHub.serverProjectPath": "/home/luyi/agenthub"
  }
  ```

- 迁移后 Python 单元测试 40/40、VS Code 静态测试 4/4、随机隔离 tmux 的真实 E2E 24/24 通过。
- 原 `test` session 在新目录成功 resume，并返回 `AGENTHUB-MOVED-OK`。
- `/home/luyi/generation` 中的 Agent Hub 后端、扩展和 21 份专属文档已移除。

## 0.1.4 UI 消息生命周期修复

迁移后发现 VS Code UI 可能把轮询结果发送到隐藏的 fallback Webview，导致可见聊天窗口只保留本地乐观插入的 `Thinking…`。0.1.4 做了以下修复：

- 只注册一个 `agenthub.chatView`，移除共享 provider 的 fallback Webview。
- 发送请求显式携带 `sessionUid` 和 `requestId`，不再依赖 provider 的全局选中状态。
- 增加 `sendStarted`、`sendAccepted`、`sendFailed` acknowledgement；10 秒无 acknowledgement 时停止 Thinking 并显示错误。
- stale view、server offline 和 POST 失败都会回滚本地 Thinking。
- polling 增加 singleflight，避免并发/乱序 refresh。
- VS Code Remote 已安装 `luyi-local.agent-hub@0.1.4`。

## 0.1.6 tmux gen 会话横条与 agent-attn

顶部新增只读发现的 `gen 活跃会话` 横条：

- 数据源仅为默认 tmux server 的 `gen` session。
- 通过 `/home/luyi/tools/agent-attn/detect.py` 识别 Claude/tclaude/Codex/tcodex pane；普通 zsh 和 dead/关闭窗口不显示。
- 横条按 tmux window index 排序；点击标签在 VS Code integrated terminal 中 attach 精确的 tmux window ID。
- 每个标签的 `⋯` 菜单支持修改显示名称。名称保存在 tmux window option `@agenthub_name`，不会执行 `rename-window`。
- 提醒状态完全复用 `/home/luyi/tools/agent-attn/README.md` 的协议：
  - 红色：`blocked` / `@attn_manual=red`，表示待处理。
  - 黄色：`done` / `@attn_manual=yellow`，表示待验收。
  - 运行中与空闲分别显示状态点。
  - 清除提醒会 unset `@attn_manual`，恢复自动检测。
- 关闭的 tmux window 会在下一轮刷新中自动从横条消失。
- 默认 tmux 集成不包含 kill/new/respawn/select/send-keys；实现期间默认 tmux server PID 保持 `2511053`。

验收结果：Python 测试 46/46、VS Code 静态测试 6/6、真实 `gen` 只读交叉验证通过；当时 8 个 `gen` window 中显示 7 个 Agent window，并排除普通 zsh window。

## 0.1.7 tmux 原生会话图形 Chat

顶部 `gen` 标签现在不是 Terminal 快捷入口，而是原 tmux Agent 的图形 Chat 入口：

- 单击标签会读取原 tcodex/Codex rollout 或 tclaude/Claude transcript，过滤 reasoning、tool、system、sidechain 等内部噪声，并在 Chat 区显示原历史。
- 从 Chat 发送消息时，后端通过 tmux named buffer → `paste-buffer` → Enter，把文本提交给原 TUI；不会停止、resume 或替换原 runtime。
- 回复继续写入原生历史文件；Agent Hub 轮询同一历史并把最终回答同步回 Chat。
- 原 tmux window、进程、runtime ID、上下文和 agent-attn 状态始终保留。
- `busy` 或 `blocked` 状态禁止发送新消息；blocked 提醒可从标签 `⋯` 菜单打开原 tmux 终端处理交互式问题或权限选择。
- tcodex runtime ID 由进程当前打开的 rollout 路径与 `state_5.sqlite.threads` 交叉校验；未打开真实 rollout 的首启 TUI 不会被误认成旧 session。
- 重复打开或刷新采用确定性 native message ID，历史导入和 relay 消息同步幂等，不重复显示。

隔离真实 E2E 已覆盖：

- tcodex：tmux 第一轮 → 图形 Chat 第二轮，同一 runtime ID，原 TUI 保持存活。
- tclaude：tmux 第一轮 → 图形 Chat 第二轮，同一 runtime ID，原 TUI 保持存活。
- 两轮测试均使用随机 `ah-e2e-*` tmux socket，默认 tmux server PID 保持不变。

## 0.2.0 统一图形 Chat、稳定滚动与会话协作

2026-08-18，VS Code 界面进一步收敛为日常唯一入口：

- `tmux gen` 原生会话和 Agent Hub 新建会话合并到顶部同一标签栏，标签显示名称、runtime 与运行/待处理/待验收状态。
- 已暂停的历史 managed session 收进顶部“↶”历史入口，不长期挤占标签栏；选中后可查看原对话，继续发送时自动恢复 worker。
- 点击标签后在下方显示完整图形 Chat；终端入口从主界面移除，只在 `⋯` 中保留为原生交互选择器的调试兜底。
- 轮询不再无条件把历史拉回底部。用户上翻时按首个可见 message 锚点保持位置；新回复到达时显示“↓ 1 条新消息”，点击后回到底部。
- assistant/user/system 消息使用本地 `markdown-it` 安全渲染，禁用 raw HTML，仅允许 http/https/mailto 链接；代码块支持复制。
- 新增“发送给其他会话”面板。目标收到的是明确标记来源的普通 user message，不伪装为 system/assistant；busy、blocked、unmanaged 或不支持 chat 的目标会被拒绝。
- `tmux gen` 标签和协作目标按当前 VS Code workspace 的 cwd 过滤；后端也要求 source/target 属于同一 workspace 或工作目录，避免跨项目误投递。
- runtime 发送入口新增 per-session singleflight，避免两个并发 handoff 同时穿透 `gen-tmux-relay` 的状态检查。

验收：后端单元测试 62/62、VS Code 静态测试 8/8 通过；Playwright 真实 Chromium 验证了顶部统一标签、Markdown 代码块、上翻后保持 `scrollTop=240`、新消息提示、历史会话入口及协作弹窗。隔离随机 tmux E2E 又验证了 tclaude→tcodex handoff 的真实投递与回复，默认 tmux PID 保持不变。新版 VSIX 为 `agent-hub-0.2.0.vsix`。

## 0.2.1 顶部滚动稳定与 CLI 活动时间线

2026-08-18，继续补齐与原 CLI 一致的过程可见性：

- 顶部会话标签栏记录用户手动横向滚动位置；轮询重绘不再调用 `scrollIntoView`，只有用户主动切换会话时才把选中标签移入可视区域。
- Chat 时间线新增 Codex/tcodex 的 `update_plan`、commentary、function/custom/web/tool-search 调用，以及 Claude/tclaude 的 Enter/ExitPlanMode 与 tool_use/tool_result。
- 工具调用默认折叠，只在横条显示工具名、状态和结果首个非空行；展开后才通过独立 API 懒加载完整参数与结果，避免轮询反复传输大段输出。
- 原始 reasoning/thinking 仍不展示；Plan 使用 Markdown 呈现，工具结果保持纯文本并经过转义。

验收：后端测试 69/69、VS Code 静态测试 10/10 通过；真实 Chromium 中顶部标签轮询前后 `scrollLeft` 均保持不变，工具默认折叠且只显示结果首行，首次展开才请求并显示完整参数/结果。JSONL 使用二进制 byte offset，正在写入的残缺尾行会在下次轮询重读，不会被跳过。

## 0.2.2 窗口标签随侧栏宽度伸缩

顶部会话窗口由固定宽度改为弹性布局：会话少时自动平分并填满侧栏，会话增加时逐步收缩到可读最小宽度，超过容量后才出现横向滚动。真实 Chromium 已覆盖 260/320/410/640/900px 五种宽度和 1/2/3/5/10 个窗口；例如 410px 侧栏下 1/2/3 个窗口宽度分别约为 358/178/118px，均无无效空白或提前溢出。

## 0.2.3 对话区由宽变窄的收缩修复

长代码块、超长单词或宽表格曾把 CSS Grid 的隐式列撑到约 4,014px；之后即使把 VS Code 右侧栏缩窄，body 本身变窄但 header、对话区和输入框仍保留旧的 min-content 宽度。现已把根网格列改为 `minmax(0, 1fr)`，并为每个直接子项、transcript、tool detail、composer 增加完整的 `min-width:0/max-width:100%` 约束。Chromium 在同一个 Webview 中完成 `900→640→410→320→260→220→410→900px` 动态回归，所有阶段 `body.scrollWidth` 都严格等于当前 viewport 宽度。

## 0.2.4 缩窄后左侧遮挡修复

当用户曾横向滚动顶部标签、代码块或表格后再缩窄侧栏，浏览器会保留旧的 `scrollLeft`，导致当前选中窗口和代码内容进入负坐标，看起来像左侧被遮挡。现已增加根尺寸 `ResizeObserver`：尺寸变化后归零页面与 transcript 横向偏移，收窄时归零代码/表格内部偏移，并重新将选中会话标签调整到可视区域。强制把所有横向容器滚到最右后再从 900px 缩到 260px 的 Chromium 回归中，`conversationTabs.scrollLeft=0`、`transcript.scrollLeft=0`，负坐标元素数量为 0。

## 0.2.5 运行中追加消息

Chat 输入框在 Agent 运行时保持可用，并按 runtime 的真实能力路由：

- Codex/tcodex managed worker 使用 app-server 原生 `turn/steer`，携带 `expectedTurnId`，补充消息进入同一个 active turn。
- Claude/tclaude managed worker 使用 Agent Hub 持久化 next-turn 队列；当前回复结束后自动启动下一轮，避免 stream-json 多个 `result` 与 assistant message 错配。
- 原生 `tmux gen` TUI 在 busy 时通过原 TUI 输入队列 best-effort 投递；blocked/等待交互状态仍拒绝普通消息，避免误触审批或选择器。
- UI 在 running 状态下按钮显示“追加”，可连续发送多条；消息分别显示“已追加到当前任务”“已排到下一轮”或“已交给当前 Agent 排队处理”。

随机隔离 tmux 的真实 E2E 已验证：tcodex 的补充 token 出现在同一 turn 最终回复中；tclaude 的第二条消息先以 `queued` 持久化，首轮结束后自动生成第二轮并完成。E2E 前后默认 tmux server PID 不变。

## 0.2.6 会话关闭

顶部每个会话标签新增 `×`，`⋯` 菜单也提供“关闭会话”。关闭前由 VS Code modal 二次确认；managed session 会停止并清理其精确 worker/tmux window，导入的 `tmux gen` session 会在 runtime/window ID 校验后关闭精确 window。聊天记录保留在 SQLite，但 session 标记为 `closed`，不会再出现在顶部或历史恢复入口，也不会因发送消息而自动 resume。隔离 E2E 验证 worker window 从 workspace tmux 中消失、hub keeper window 保留、closed 状态持久化且默认 tmux PID 不变。

## 0.2.8 模型选择、窗口重绑与文字选择

- 新建会话可选择模型与推理强度；不选择时仍沿用 runtime 默认配置。选择项会写入 tmux worker 配置，在 worker 重启/恢复后继续保留。
- 对话输入框左下角及会话标题区显示实际模型与推理强度。
- `tmux gen` window ID 被复用或原绑定已关闭时，后端会核对 runtime、pane、rollout 与数据库配置并重新绑定，避免活窗口继续指向 closed session。
- transcript 轮询遇到用户正在选择文字时会延后 DOM 更新，结束选择后再刷新，避免局部选择突然扩成整条消息。
