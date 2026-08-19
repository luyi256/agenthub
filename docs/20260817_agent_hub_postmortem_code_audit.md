# Agent Hub 事后只读代码审计

审计时间：2026-08-17（Asia/Beijing）。审计范围为 `agent_hub/config.py`、`project_tmux.py`、`session_worker.py`、`worker_client.py`、`runtime_manager.py`、`app.py`、`db.py` 与 `agent_hub_vscode/src`；额外只读查看了 `AGENTS.md`、相关 README、命名模块和测试源码以核对设计意图。**没有执行任何 tmux 命令、进程清理、E2E、单元测试、编译或服务启动，也没有修改核心代码；本文件是唯一写入。** 目录不是 Git worktree，且审计期间 `config.py`、`runtime_manager.py` 等文件出现过并发更新，因此以下行号和结论以 2026-08-17 11:36:59 的最终复核快照为准。

## 1. 结论摘要

| 等级 | 数量 | 结论 |
|---|---:|---|
| P0 | 0 | 没有发现生产代码中无条件访问用户 default tmux socket、无条件 `kill-server` 或绕过审批直接执行的路径。若实际环境把 `AGENTHUB_TMUX_SOCKET` 配成 `default`，则 P1-01 会立即升级为现场 P0。 |
| P1 | 8 | 专属 socket 约束未端到端封闭；worker 初次创建和恢复重拉不是事务式清理；同一 session 恢复无 singleflight；worker 连接异常后可能永久卡在 stale live；未知审批会被先删除再报错；扩展 auto-start 存在硬编码和并发 `respawn-pane -k` 风险。 |
| P2 | 7 | Hub 重启时 active turn 内存态恢复不完整；重连映射存在短暂丢事件窗口；启动恢复串行且扩展等待过短；alias 只改数据库；审批 action 未校验；WorkerClient 超时资源回收不完整；Open tmux 回退命名与后端不完全一致。 |

对题目中 tmux 安全问题的直接回答是：**Python 后端的生产 tmux 调用都通过 `env -u TMUX tmux -L <config socket>`，这一部分合格；VS Code auto-start 的三个 tmux 子进程也显式清空 `TMUX` 并使用 `-L agenthub`。但 `Open Project tmux` 没有清空 `TMUX`，扩展把 socket 硬编码为 `agenthub`，而后端允许环境变量修改且不拒绝 `default`，所以“所有调用都使用同一个受保护专属 socket并清空 TMUX”目前不成立。** 审计范围内生产代码没有 `kill-server`；有精确目标的 `kill-window` 和 `respawn-pane -k`，后者只在扩展的 coordinator auto-start 路径。

## 2. P0

**未发现 P0。** `project_tmux.py` 的 `_has_session()` 和 `_tmux()` 均显式使用 `env -u TMUX` 与 `tmux -L`（`agent_hub/project_tmux.py:271-305`），扩展 auto-start 的 `has-session`、`respawn-pane`、`new-session` 也显式使用 `env -u TMUX` 与 `-L agenthub`（`agent_hub_vscode/src/extension.ts:328-367`）。生产范围内没有 `kill-server`。但配置层没有禁止 socket 名 `default`，故仍有下述 P1 配置型高风险。

## 3. P1

### P1-01：专属 tmux socket 不是不可破坏约束，配置可直接落到用户 default socket

**位置：** `agent_hub/config.py:22,46-48`；`agent_hub/project_tmux.py:222-230,271-305`；`agent_hub_vscode/src/extension.ts:86-115,328-367`。

后端默认 socket 是 `agenthub`，所有后端调用也正确带 `-L self.tmux_socket_name`，但 `AGENTHUB_TMUX_SOCKET` 没有非空、保留名或格式校验。若配置为 `default`，`tmux -L default` 就是用户默认 server，后端的 `has-session`、`new-session`、`new-window` 和失败/恢复路径中的 `kill-window` 都会进入默认 socket；若配置为空则会变成运行期故障。与此同时，扩展把 socket 固定写成 `agenthub`，既不读取后端配置，也不从 `/api/health` 获取实际 socket，导致自定义 socket 时 coordinator、worker 和 Open tmux 视图发生分叉。

**影响：** 错误环境变量可把精确 `kill-window` 变成对用户默认 server 的破坏操作；自定义合法 socket 则会令扩展看不到后端创建的 workspace/session。建议启动时拒绝 `""`、`default` 及不符合白名单的值；由后端 health/snapshot 返回实际 socket，扩展只消费该值；最好使用独立 socket 路径或带 UID 的不可配置默认值，并在执行 kill 前校验 session 前缀与 server ownership marker。

### P1-02：`Open Project tmux` 没有清空 `TMUX`，且与可配置后端 socket 不一致

**位置：** `agent_hub_vscode/src/extension.ts:86-115`；对照 `agent_hub/config.py:46-48`。

Open tmux 使用 VS Code integrated terminal 直接执行 `tmux -L agenthub ...`，没有像其他路径那样通过 `env -u TMUX`，也没有给 terminal 设置移除 `TMUX` 的环境。若 Remote Extension Host/VS Code 是从 tmux 内启动，子终端会继承 `TMUX`，tmux 可能拒绝嵌套 attach；即使成功，硬编码 socket 也无法跟随后端 `AGENTHUB_TMUX_SOCKET`。

**影响：** 这是用户可见的排障入口，事故时可能恰好打不开或打开错误 server。建议 terminal 使用 shell wrapper `env -u TMUX tmux -L <server-reported-socket> ...`，或通过 terminal `env` 显式删除 `TMUX`；不要复制 socket 常量。

### P1-03：初次 worker 创建在获得 `WorkerLaunch` 前不是事务式，`new-window` 失败会遗留配置

**位置：** `agent_hub/project_tmux.py:76-117,136-189`；`agent_hub/runtime_manager.py:490-507,543-552`。

`launch_worker()` 先写 `workers/<worker_id>.json`（`project_tmux.py:142-159`），再执行 `new-window`（`177-189`）。如果 `new-window` 抛异常，函数不会返回 `WorkerLaunch`，而 `create_session()` 的清理 `try/except` 是在调用成功返回之后才开始，因此 `runtime_manager.py:543-551` 无法覆盖这类失败。新建 workspace 时 `hub` keeper session 也可能已留下；keeper session是否保留可以是设计选择，但孤立 worker config 明确不是。

**影响：** tmux server 异常、目标 session 瞬时消失、窗口创建失败时会产生不可追踪配置垃圾，且调用方无法精确回滚。建议把 `launch_worker()` 自身改为事务：记录 `config_written/window_created`，任何异常都删除 config/socket/state，并仅在窗口确实创建后执行精确 kill；或先构造 `WorkerLaunch`，由统一 context manager 管理 commit/rollback。

### P1-04：worker 恢复/重拉失败不清理，新旧 dead window 会持续累积

**位置：** `agent_hub/project_tmux.py:190-199,222-242`；`agent_hub/runtime_manager.py:756-830,1001-1013`。

worker window 开启了 `remain-on-exit`。当 worker 正常报 `runtime.error/exited` 时，manager 只移除 live/client 并更新 DB，没有清理对应 window、socket/state/config（`runtime_manager.py:1001-1013`）。下一次恢复若旧 socket 已消失，会直接创建新 window（`767-786`），不会 kill 旧 dead window。恢复中新建的 `launch` 如果 `client.connect()` 或 `_wait_for_worker_runtime()` 失败，也没有与初次创建相同的 `cleanup_launch()`；尤其 `798-800` 在 `launch is not None` 时直接抛出，`830` 之后的初始化失败同样无 finally。

**影响：** 每次 runtime 崩溃或恢复失败都可能留下 dead tmux window和 worker 文件；DB随后指向新 window，旧资源失去引用，长期运行会不断堆积。建议恢复路径也使用统一 launch transaction；在“socket 缺失但 DB 有旧 tmux target”时先验证 pane dead/worker ownership，再清理旧 window；用 tmux `window_id`/`pane_id` 而不是可重复名称作为持久目标。

### P1-05：同一 session 的 `ensure_live()` 没有 per-UID singleflight，可并发拉起两个 resume worker

**位置：** `agent_hub/runtime_manager.py:660-708,756-876`；对照 workspace 级锁 `agent_hub/project_tmux.py:53,76-78`。

当 Hub 刚重启、session 尚未进入 `self.sessions`，两个并发 send/approval 都可进入 `ensure_live()`，各自调用 `_connect_or_relaunch_worker()`。这里没有 per-session lock、in-flight task 或二次检查；若 socket 缺失，两边都可能创建新 window，并让两个 runtime process 同时 resume 同一 runtime ID，最后相互覆盖 `worker_clients`、`worker_to_uid` 和 `sessions`。`ProjectTmuxManager` 的锁只保证 workspace session 创建，不保证一个 session 只拉一个 worker。

**影响：** 可能产生双 writer、重复 resume、审批落到错误 worker、孤立窗口和状态覆盖。建议维护 `uid -> asyncio.Lock` 或 `uid -> reconnect Task`，进入锁后重新检查 live/connection；创建、send、resolve approval 都必须复用同一个 singleflight 结果。

### P1-06：worker Unix 连接突然断开时 manager 不失效 live，后续请求可能永久使用 stale client

**位置：** `agent_hub/worker_client.py:46-71,94-101`；`agent_hub/runtime_manager.py:660-663,734-739,1001-1013`。

`WorkerClient._read_loop()` 遇到 EOF 只给 pending future 设异常，没有向 `RuntimeManager` 发 disconnect 回调。只有 worker来得及发送 `runtime.error/exited` 时 manager 才移除 live/client；若 worker 被 SIGKILL、Python 崩溃或 socket 直接断开，`self.sessions` 仍保留。以后 `ensure_live()` 在 `660-663` 直接返回旧 live，send 持续使用已关闭 writer，而不会进入重连路径。

**影响：** 单个 session 可在 Hub 不重启的情况下永久不可恢复，且每次发送只新增失败消息。建议 WorkerClient 增加一次性 disconnect callback；EOF 时原子移除 `sessions/worker_clients/worker_to_uid`、标 DB offline，并让下次 `ensure_live()` 重连。`ensure_live()` 返回 cached live 前也应检查 client writer/reader task 状态。

### P1-07：未知 Codex server request 会被当审批，首次处理又先 `pop`，从而永久挂起 runtime

**位置：** `agent_hub/session_worker.py:346-368,414-436,491-511`。

Codex stdout 中任何同时含 `id` 和 `method` 的 server request 都进入 `_approval_request()`，没有先限定已支持审批 method。resolve 时又在校验 method 前执行 `self.approvals.pop()`（`494`）；若 method 不在四种已知类型中，`505-506` 抛错，但 in-memory runtime request 映射已经丢失，持久 `pending_approvals` 和 DB 仍显示 pending。用户再次点击会得到“不存在或已过期”，app-server 则一直等不到响应。

**影响：** app-server 新增 server request 类型或 method 名变化时，会冻结当前 turn，且 UI无法恢复。建议先 `get`、校验 method/action、成功写回 runtime 后再 `pop`；未知 method 应明确返回拒绝/unsupported RPC 响应或保存为可重试状态，不能悬挂。server request 分流应使用允许列表，非审批请求走独立 handler。

### P1-08：扩展 auto-start 非 singleflight，硬编码端口并对 coordinator 使用 `respawn-pane -k`

**位置：** `agent_hub_vscode/src/extension.ts:61-75,118-155,305-379,381-397`；`agent_hub_vscode/src/hubClient.ts:22-29`。

每次 `client()` 发现 health 失败都会直接调用 `startServer()`，没有共享 promise/mutex。Webview resolve、`ready`、visibility refresh 和 1.3 秒 polling 都能并发触发。若 `agenthub-mvp` 存在，所有调用都会执行 `respawn-pane -k agenthub-mvp:0.0`，相互杀掉刚启动的 coordinator。另一个独立问题是读取了可配置 `serverUrl`（`extension.ts:305-310`），但启动命令固定 `127.0.0.1:8766`（`325-327`）；自定义 URL 时必然启动到错误地址，随后继续检查自定义 URL并失败。三个 tmux 子进程本身正确使用专属 `-L agenthub` 和 `env -u TMUX`，不会碰 default socket，但会对 Agent Hub 自身形成重启风暴。

**影响：** Hub 启动慢、多个 VS Code window同时打开、serverUrl 非默认或短暂 health timeout 时，coordinator 可能被反复终止。建议 extension host 级维护一个 auto-start singleflight；启动 host/port从 `serverUrl` 解析；只有确认 pane dead或启动命令归属 Agent Hub 后才 respawn，检查并报告 tmux 返回码；优先使用独立 supervisor/lockfile，而不是 health 失败即 `-k`。

## 4. P2

### P2-01：Hub 重启后只恢复 DB message，不恢复 `LiveSession.active_message_id`

**位置：** `agent_hub/db.py:514-547`；`agent_hub/runtime_manager.py:443-452,830-876,902-930`；发送门控 `runtime_manager.py:710-721`。

启动先把 streaming message 标成 interrupted，再通过 worker state 把 DB同步回 streaming，这一思路正确；但随后创建的 `LiveSession` 没有从 `details.active_message_id` 恢复内存字段。HTTP 客户端若绕过只看 DB status 的扩展 UI，可在旧 turn 仍运行时通过 manager 的内存门控并提交第二条消息，最终由 worker拒绝并留下失败消息。建议 reconcile 同时恢复 live 的 active message/turn/busy state，并以 worker describe 作为发送前最终权威状态。

### P2-02：重连期间先接收事件、后登记 `worker_to_uid`，存在短暂丢事件窗口

**位置：** `agent_hub/runtime_manager.py:787-830,857-867,932-937`。

WorkerClient 在 `connect()` 时已经启动 reader task，但 `worker_to_uid[worker_id]` 到 `858-859` 才登记；期间到达的 delta/completion/approval 会因 `handle_worker_event()` 找不到 UID 而直接返回。`_reconcile_worker_state()` 可补回 connect/describe 快照之前的状态，却补不了“最后一次 describe 响应之后、映射登记之前”的事件。建议在建立 client 前登记 provisional mapping，失败时回滚；或者 WorkerClient 在 manager完成绑定前缓存 event。

### P2-03：Hub 启动恢复串行，多个 stale socket 可超过扩展 7.5 秒等待窗口

**位置：** `agent_hub/runtime_manager.py:443-452,794-797`；`agent_hub/worker_client.py:29-44`；`agent_hub/app.py:316-328`；`agent_hub_vscode/src/extension.ts:369-378`。

lifespan 在服务 ready 前串行遍历所有 tmux-worker；每个存在但不可连接的 stale socket 最多等 5 秒。扩展只轮询 50×150ms，即约 7.5 秒。两个 stale session 就可能让扩展误报“server did not start”，即使后端随后成功。建议恢复并发但带上限，先快速启动 HTTP 再后台 reconcile，或把扩展等待时间提高并显示 startup phase。

### P2-04：alias 数据库语义可靠，但 rename 不会同步 tmux window/runtime native name

**位置：** `agent_hub/app.py:221-241`；`agent_hub/db.py:368-382,784-831`；未接入的能力 `agent_hub/project_tmux.py:209-220`；扩展 handler `agent_hub_vscode/src/extension.ts:186-188,288-297`。

alias 会标准化且由 SQLite unique index兜底，并发重复 alias 在最终注册时会冲突并进入创建清理，核心唯一性合格。但 PATCH 只更新 DB，`rename_window()` 没有调用，runtime thread name也不更新；当前 webview脚本也没有实际 rename 控件触发已存在的 handler。结果是 UI alias、tmux window、Claude/Codex原生名称长期不一致。建议明确“alias仅展示名”并删除死接口，或在 PATCH 后以不可变 tmux window ID同步改名并更新 managed_config；不要依赖可变 window name作内部引用。

### P2-05：审批 action 未做允许列表校验，任意非 `accept` 值都静默按拒绝处理

**位置：** `agent_hub/app.py:200-210`；`agent_hub/session_worker.py:491-504`；`agent_hub/runtime_manager.py:1093-1145`。

当前扩展按钮只发送 `accept/decline`，正常 UI安全；但 API没有验证 action。拼写错误、旧客户端值或恶意请求都会被当成 decline，并在 DB写为 declined，而不是返回 400。此行为是 fail-closed，不会越权执行，但审计记录会误导。建议 API入口只接受枚举 `accept|decline`，manager和worker再次断言。

### P2-06：WorkerClient 的连接/请求超时没有完整释放 pending future、reader task和 writer

**位置：** `agent_hub/worker_client.py:29-44,46-71,73-97`；恢复调用 `agent_hub/runtime_manager.py:794-830`。

`request()` 的 `wait_for` 超时后，future 仍留在 `pending`，直到响应或连接关闭才移除；`connect()` 若 Unix connection 已建立但 `describe` 超时/报非 OSError，也不会主动关闭 writer或取消 reader task。恢复路径随后可能替换为新 client，旧 client留待 GC。建议 request 用 `try/finally` 移除 pending；connect每次失败都关闭本次 transport并等待 reader task退出；manager替换 client前显式 close旧实例。

### P2-07：Open tmux 无当前 session 时的回退 session 名与后端 slug 规则不完全一致

**位置：** `agent_hub_vscode/src/extension.ts:93-107,505-510`；`agent_hub/project_tmux.py:62-74`。

后端 `ascii_slug(..., max_length=30)`，扩展本地 `slug()` 截到 28 字符。workspace名较长且当前没有选中 session时，扩展推导的 `ah-...-<hash>` 可能与实际 workspace session不同。建议不要在扩展复制命名算法；由 snapshot/health 返回 workspace tmux target，或者提供后端 endpoint 做 identity→target解析。

## 5. 已确认的正确实现

1. **后端 tmux 调用集中且默认安全。** `_has_session()` 与 `_tmux()` 都显式 `env -u TMUX`、`tmux -L <socket>`，未发现绕过 helper 的后端生产调用（`agent_hub/project_tmux.py:271-305`）。
2. **生产代码没有 `kill-server`。** 仅有精确 `kill-window -t <session>:<window>`（`project_tmux.py:222-230`）和扩展 coordinator 的精确 `respawn-pane -k agenthub-mvp:0.0`（`extension.ts:338-352`）。
3. **初次创建在已经拿到 `WorkerLaunch` 后的失败清理较完整。** client stop/close 后调用 `cleanup_launch()`，会 kill worker window 并删除 socket/state/config（`runtime_manager.py:507-552`；`project_tmux.py:232-242`）。
4. **同一进程内 workspace session 创建有锁和二次存在检查。** `RLock` 包住 `has-session/new-session/再次 has-session`（`project_tmux.py:53,76-107`），同 workspace并发创建不应产生两个 keeper session。
5. **alias 唯一性最终由数据库保证。** 预检查之外还有 partial unique index和 IntegrityError转换（`db.py:71-72,368-382,404-472`），因此并发同 alias不会仅依赖 TOCTOU 预检查。
6. **已知 Codex审批的正常链路具备 worker持久化和 Hub重连补偿。** worker把 pending approvals写入 state（`session_worker.py:418-436`），Hub重连从 state重建 DB（`runtime_manager.py:921-930`）；Hub启动先 expire再重建不会把仍存活 worker的审批永久丢掉（`db.py:537-547`）。
7. **Hub正常关闭不会停止 tmux worker。** `stop_all()` 对 tmux-worker只关闭 coordinator client，不发 stop/kill，故 VS Code/Hub断开后 worker可继续运行（`runtime_manager.py:1247-1254`）。worker socket、config临时文件和 state临时文件均设置为 `0600`（`project_tmux.py:325-333`；`session_worker.py:550-557,684-692`）。
8. **runtime初始化有跨 worker进程锁。** 每个 runtime使用独立 `flock` startup lock，可降低同 runtime并发初始化的脆弱性（`session_worker.py:36-55,565-577`）。

## 6. 修复优先顺序

1. **先封死 socket 不变量：** 后端拒绝 `default/空值`，health返回实际 socket；扩展所有 tmux路径统一读取该值并清空 `TMUX`。
2. **把 worker launch/relaunch 做成同一个事务式 context manager：** 所有异常都 close client、kill精确 window ID、删 socket/state/config；处理 missing socket前先清旧 dead window。
3. **增加 per-session singleflight 与 disconnect callback：** 同 UID只允许一个 connect/resume任务，EOF立即失效 live并允许下次恢复。
4. **修审批状态机：** method/action允许列表、校验后再 pop、未知请求必须可拒绝或可重试，不能悬挂 app-server。
5. **修扩展 auto-start：** singleflight、从 serverUrl解析监听地址、检查 tmux返回码，避免 health失败即并发 `respawn-pane -k`。
6. **补重启状态恢复：** active message/busy state同步进 `LiveSession`，在 worker映射登记前缓存事件；HTTP服务先可用，再后台并发恢复历史 worker。

## 7. 验收建议

修复后应以独立随机 socket做专项验证，但不要把“测试使用了随机 socket”当成生产配置校验的替代。至少覆盖：`AGENTHUB_TMUX_SOCKET=default` 必须拒绝启动；从 tmux内启动 VS Code 后 Open tmux可用；自定义 socket时 create/auto-start/open三者一致；`new-window`、connect、runtime init各阶段注入失败均无 window/file泄漏；同 UID四路并发 send/approval只产生一个 resume worker；SIGKILL worker后下一条消息自动恢复；Hub离线期间 completion/approval在重连后完整回放；未知审批 method不会丢失 runtime request；自定义 `serverUrl`和慢启动不会触发 coordinator重启风暴。
