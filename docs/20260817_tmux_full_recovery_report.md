# tmux 全量恢复报告（2026-08-17）

## 结论

事故前最后一份完整快照记录于 **2026-08-17 10:59:03（北京时间）**，当时共有 **17 个 tmux session、34 个 window**。现已重建为同样的基线拓扑：

- 用户默认 socket：15 个 session、32 个 window；
- Agent Hub 专属 socket `tmux -L agenthub`：2 个 session、2 个 window；
- 合计：**17 个 session、34 个 window**；
- 所有恢复后的 pane 均为存活状态，没有 `pane_dead=1`；
- 默认 tmux server PID 始终为 `2511053`；
- 现有 `gen` 的四个 pane PID 保持为 `2511432 / 2511438 / 2511442 / 2511446`，恢复其他 session 时没有重启或覆盖 `gen`。

恢复期间用户又在 `gen` 中主动新开了一个名为 `[tmux]` 的 tclaude window（session ID
`057b850c-bfb6-4bb3-a1a2-94ac282224e1`）。该窗口不属于事故前基线，恢复流程没有修改或删除它。因此当前实时状态是
**17 个 session、35 个 window**：34 个恢复基线窗口，加 1 个恢复期间新增的用户窗口。

旧 server 的内存对象已经被销毁，因此以下内容不能位级恢复：scrollback、分屏布局、焦点、未回车输入、shell 内存变量和当时的窗口索引细节。可持久化的 Claude/tclaude/Codex/tcodex 对话已按 session/thread ID 恢复；无法唯一映射 ID 的窗口只恢复了 cwd 和 shell 骨架，避免错误 resume 或双 writer。

## 当前查看方式

```bash
# 普通历史 session
env -u TMUX tmux ls

# Agent Hub 已迁到专属 socket，避免再影响普通 tmux
env -u TMUX tmux -L agenthub ls
```

## 已恢复拓扑

### 默认 tmux socket

| Session | Window |
|---|---|
| `1` | `plan-judge`、`shots-judge`、`chat-web`、`chat-web-codex`、`shell-chat-web`、`verifier`、`filmops`、`shell` |
| `ccm` | `ccm`、`aihot` |
| `data` | `data-web`、`script-revise`、`lapian-workflow` |
| `gen` | 基线：`shell`、`enhance`、`routing`、`recovery`；另有恢复期间新增的用户窗口 `[tmux]` |
| `multi` | `data-web`、`multiref` |
| `paper` | `icml`、`image-book`、`agentic-rl` |
| `scripts` | `daqing`、`r2-clean` |
| `test` | `unknown-tclaude` |
| `aesthetic_web_smoke` | `web` |
| `aesthetic_web_smoke2` | `web` |
| `creative-agent-data-web-local` | `annotation` |
| `lapian-local` | `lapian` |
| `pilot_subset_web` | `web` |
| `pilot_firsttest_web` | `web` |
| `pilot_pkg_web` | `web` |

### Agent Hub 专属 socket

| Session | Window |
|---|---|
| `agenthub-mvp` | `server` |
| `ah-generation-9c577f41` | `hub` |

## 主要 Agent 恢复映射

| 位置 | Runtime ID | 恢复情况 |
|---|---|---|
| `1:plan-judge` | Claude `3e30215a-7224-42ef-9042-c485786689f6` | 已恢复，并通过 `direnv` 恢复 Anthropic 网关环境 |
| `1:shots-judge` | tclaude `e9cba90e-96d1-4fe0-8b81-0d0ae7e1f5b7` | 已恢复 |
| `1:chat-web` | tclaude `b790e7b2-3bea-457d-b32d-8a2ca3758b64` | 已恢复 |
| `1:chat-web-codex` | tcodex `019fa28c-b8b6-7231-a1f9-b5ddffa8b6e1` | 已恢复 |
| `1:verifier` | tcodex `019f8ecc-cea2-7352-95c6-5bf91b28f5e9` | 已恢复 |
| `1:filmops` | tcodex `019fa801-4284-7dc0-9006-9a9b04d6037c` | 已恢复 |
| `ccm:ccm` | tcodex `019fb881-8dcd-7991-b8c1-b8e8e96f4132` | 已恢复 |
| `ccm:aihot` | tclaude `789877f3-dd13-4ace-b685-70fc3762cd60` | 已恢复 |
| `data:script-revise` | tcodex `019fd677-dc0d-7013-9179-23c297b1f1a9` | 已恢复 |
| `data:lapian-workflow` | tcodex `019fda42-0c0b-7360-8c00-ec205c1ce144` | 已恢复 |
| `multi:data-web` | tcodex `019fd658-8294-7681-872e-9bcc8b14e034` | 已恢复 |
| `multi:multiref` | tcodex `019fadbd-1835-7e91-80ec-a17a7bbdf812` | 已恢复 |
| `paper:icml` | tclaude `9b0e75ae-f5a5-40fe-81de-449bebf38aba` | 已恢复 |
| `paper:image-book` | Claude `35fc70a7-2de6-4a94-939c-895057a16496` | 已恢复，并通过 `direnv` 恢复网关环境 |
| `paper:agentic-rl` | Claude `11a038f3-88c6-4ef5-be0e-3bf18a3878ce` | 已恢复，并通过 `direnv` 恢复网关环境 |
| `scripts:daqing` | tcodex `019fcca7-986e-7670-9a96-bfea13e2f6b8` | 已恢复 |
| `scripts:r2-clean` | tcodex `019fccaa-6c49-7be3-8adc-3bb850b8a9c6` | 已恢复 |
| `gen:enhance` | tcodex `019ffa7d-21f0-7dd1-b8e4-fe3b6c5cd560` | 事故后已恢复，本轮未重复启动 |
| `gen:routing` | tclaude `f81a898f-c8ae-4f3b-8623-bbfdac11ba53` | 事故后已恢复，本轮未重复启动 |

更完整的进程/ID 映射见：

```text
agentdocs/tmux_recovered_runtime_map_20260817.tsv
```

## 有意未重复 resume 的会话

以下会话仍由 VS Code integrated terminal 持有，rollout 文件存在唯一写 FD，因此没有在 tmux 中再启动：

- tcodex `019ffad2-e9af-7133-aeb6-7fc2df84cb0a`，writer PID `2465237`；
- tcodex `01a00dad-20a6-7680-b935-63e928023a87`，writer PID `2467702`。

这样避免两个进程同时写同一个 conversation。

## 只能恢复窗口骨架的部分

- `data:data-web`：旧 tclaude PID `3185037` 可以确认，但持久 session ID 无法无歧义映射；现恢复到原 cwd 的 shell，并写明原因。
- `test:unknown-tclaude`：可以确认原来是 tclaude/Claude Code 窗口，但 ID 无法唯一确定；现恢复为原 cwd shell。
- `multi` 最终两个 window 的准确组合没有事故前最后一刻的 pane 清单；当前采用证据最强的 `creative-agent-data-web + MultiRef-Compass` 组合。
- `pilot_firsttest_web` 的原 `first_test` 目录后来已被清理；已重建站点入口并恢复 `:8852` 服务，但内容使用当前最终静态站，不是当时单样本临时目录的逐字副本。

## 服务验证

以下地址均已返回 HTTP 200：

```text
http://127.0.0.1:18850/annotation_workbench.html
http://127.0.0.1:18851/annotation_workbench.html
http://127.0.0.1:8851/viewer.html
http://127.0.0.1:8852/viewer.html
http://127.0.0.1:8853/annotation_workbench.html
http://127.0.0.1:8200/health
http://127.0.0.1:8780/api/inspiration
http://127.0.0.1:8766/api/health
```

## 证据与取证文件

```text
agentdocs/tmux_recovery_forensics_20260817_143350.txt
agentdocs/tmux_preincident_rollout_evidence.txt
agentdocs/tmux_pane_inventories_all.txt
agentdocs/tmux_incident_active_agents.tsv
agentdocs/tmux_recovered_runtime_map_20260817.tsv
agentdocs/agent_hub_tmux_incident_and_e2e_report_20260817.md
```

机器没有安装 `tmux-resurrect` 或 `tmux-continuum`，因此本次恢复依赖模型对话持久文件、Agent Hub SQLite、历史进程树、VS Code pty 日志和旧服务命令，而不是 tmux 原生快照。
