# dispatcher：常驻调度器

> 把"主持人手动派单、盯进度、催 worker、汇总"变成常驻进程 `host/dispatcher.py`。
> 原则：想介入就介入，想自动化就自动化——所有进程只通过实例 `state/` 下的文件协作。
> 数据契约见 `docs/contracts.md` §8，本文是运维与排障手册。

## 1. 跑起来

```bash
export CAMPUS_INSTANCE=~/campus-apply-instance

uv run host/dispatcher.py                # 常驻，默认 30s 一轮
uv run host/dispatcher.py --once         # 只跑一轮（排查用）
uv run host/dispatcher.py --dry-run --once   # 只打印将做的动作
uv run host/dispatcher.py status         # 作业表一屏
uv run host/dispatcher.py install-plist --dry-run   # 打印 launchd plist（不安装）
uv run host/dispatcher.py install-plist --dest ~/Library/LaunchAgents   # 只写文件，load 自己来
```

单实例锁：`state/dispatcher.lock`（flock 非阻塞），已在跑时新进程直接退出。
launchd 用 `install-plist` 生成的 plist（RunAtLoad + KeepAlive），脚本**不会替你 load**。

## 2. 作业状态机

```
            approve/auto 入队
                ↓
             queued ────────────┐
                ↓ 派单成功        │ registry/tracker 命中已投
           dispatched           ↓
                ↓ pane 非 idle  skipped
             filling ←──────────────┐
                ↓                   │ handoff resolved（dispatcher 通知 worker 继续）
        ┌─ gate_wait（等用户动手）───┘
        ↓       ↓ 字段表 log/fields/<id>.md 出现 或 submit_confirm handoff
        │   review_wait ── 用户 submit 指令 ──→ submitting
        │                                          ↓ outcome/回执
        └→ 终局：submitted → verified（写 registry）/ skipped / blocked / failed
        任意状态 → held（用户 hold/takeover，resume 回到之前状态）
```

dispatcher 只认这些证据，**不解析 pane 屏幕输出**：

| 证据 | 谁写 | 效果 |
|---|---|---|
| `~/.seedmux/team/tasks/<T>/meta.json` status | smx-team | `replied:done/blocked/failed` → 结合 outcome 定终局 |
| `state/outcomes/<id>.json` | worker | 终局真源（§9.2）：`submitted/blocked/skipped/failed`；旧写法 `verified`→verified、`unfit`/`need_materials`→blocked（保留 reason） |
| `log/fields/<id>.md` | worker | 停在提交前 → review_wait |
| `state/handoffs/*.json` 未 resolved | worker（经 notify.py 或夜间手写） | → gate_wait；resolved → 回 filling 并通知 worker |
| pane idle/working/exited | `smx-team panes --json` | 判空转与 pane 消失（桥不可用当轮跳过，不误判） |

worker 的完整行为约定写在 `templates/worker-task.md`（dispatcher 每单渲染一次，
产物留档 `state/task-files/<id>.md`），权限段落按 redlines §1 + `intent.yaml` 算好注入。

## 3. 自动化档位（state/autonomy.json）

文件不存在时 dispatcher 自动建默认 `{mode: auto, tier_modes: {冲=supervised, 常规=auto, 练手=auto}, max_parallel: 5, daily_cap: 50}`（广撒网策略，项目约定）。

| mode | 入队 | 派单 | 提交档位 |
|---|---|---|---|
| `manual` | 只有用户 approve 的建作业 | 同左 | 一律 user（停在提交前） |
| `supervised` | 只建 approve 过的 | 批准即派 | 按 intent.permissions |
| `auto` | 按 tier_modes 给每档单独定：某档可独立 auto | 自动派 | 同上 |

- `paused: true`：不派新单；在跑的照常监测（不追加干预）。
- `max_parallel`：worker pane 池上限（活 pane 数）；`daily_cap`：每天派单数上限。
- 派单顺序：截止日近的优先 → 冲→常规→练手 → fit 分数高优先。
- **防重投**：派单前查 `state/registry.jsonl`（同 employer_key 有 submitted/verified → skipped；
  作业还没识别门户时按"任意门户"从严匹配）+ `tracker.md` 已投列（公司主体命中 → skipped）。
- 提交档位计算（结果注入工单）：公司在 `user_confirm_companies` → user；
  练手档且 `practice=auto` → worker；公司层级在 `user_confirm_tiers` → user；否则取 `default`。

## 4. 用户指令（state/commands.jsonl，追加写）

格式见 contracts §8.3。telegram bot / web / CLI 都只往里追加；dispatcher 按 id 去重消费，
结果写 `state/commands.done.jsonl`（同 id 必不重做，重启安全）。

| action | target | args | 效果 |
|---|---|---|---|
| approve | 公告 id | 可带 company | 建作业/把 skipped 撤销回 queued |
| skip | 公告 id | — | 终结为 skipped；已派的会通知 worker 收手 |
| hold / resume | 公告 id | — | 暂停 ↔ 恢复原状态（通知 worker）；带 `handoff_id` 的 resume 兼容为 refresh |
| refresh | 公告 id | `handoff_id=…` | 「重新获取」：在跑作业→转 worker 刷新二维码；parked→关掉旧 handoff 重派（优先回原 pane 保登录态） |
| takeover | 公告 id | — | 通知 worker `handOff()` 并停止，作业转 held；parked 作业直接转 held 并关 handoff |
| submit | 公告 id | — | review_wait/gate_wait/held → submitting，通知 worker 核验 |
| mode | — | `mode=auto|supervised|manual` | 改 autonomy.mode |
| pause / unpause | — | — | 改 autonomy.paused |
| reply | 公告 id | `text=…` | 单行转发给 worker；长文本自动落 `state/inbox/` 传路径；parked 作业会先重派再转达 |

`reply / submit / refresh / takeover` 是时效指令（用户拿着手机在等）：dispatcher 在 30 秒大循环
之外以 1 秒短轮询（`--cmd-poll`）只消费这四类，其余指令仍按轮处理。

CLI 手动下指令：`uv run host/dispatcher.py cmd approve --target 10001`
（`mode` 指令也可以 `cmd mode --target auto`）。

## 5. 自停巡检（nudge）

worker 长任务会自己停下（devin 约 1 小时截断）也不回执。每轮巡检：
pane idle 且作业还在 `dispatched/filling/submitting` 超过 `--nudge-after-min`（默认 20 分钟）
→ 发单行 team-msg 催它续跑；同一作业最多 `--max-nudges`（默认 3）次，之后 `blocked` + Bark 通知。
`gate_wait / review_wait / held` 期间**绝不 nudge**（worker 在等人，催了会误操作）。

## 5.5 关卡兜底（contracts §8.7）

- **parked**：handoff `status=expired` 且 `refresh_count ≥ max_refresh` → 作业保持 `gate_wait`
  并打 `parked` 标记，pane 释放给下一家（parked 不占 max_parallel、不查 pane 死活、不 nudge），
  Bark 通知一次。用户点「重新获取」（refresh 指令）→ 关旧 handoff、清 task_id 回 queued 重派，
  优先 assign 回原 pane。parked 作业上收到 reply 文本 → 同样重派并把话带给新 worker。
- **scanned 无跳转**：handoff `status=scanned` 超 60 秒未 resolved → 给 worker 发单行消息
  让它刷新页面检测登录态（每条 handoff 只发一次，记 `scanned_check_at`）。
- **同门户卡关**：同 portal 24 小时内 parked ≥3 → 该门户记入 `state/dispatcher-state.json`
  的 `portal_pauses`，后续 queued 作业转 `held`（`hold_reason=portal_pause:<portal>`），
  Bark 通知 + 战报标出；用户 resume 任一被暂停作业即解除暂停，其余同门户 held 作业自动恢复。
  门户识别：job.portal > handoff.portal > 投递链接域名映射（PORTAL_DOMAINS）> 域名本身。
- **parked 超 48 小时** → `skipped:gate_timeout` + Bark；公告截止前 2 天仍 parked →
  `deadline` 事件提醒一次（记 `deadline_reminded`）。
- **bot 心跳**：`state/tgbot.heartbeat` mtime 超 2 分钟（或该有 bot 却从没写过心跳）→
  Bark 通知，每小时最多一次。handoff 5 分钟无响应的二次提醒由 tgbot 负责，dispatcher 不发。

## 6. 夜间策略（autonomy.night）

`night: {start: "00:30", end: "08:00", gate_policy: "queue"}`：

- 夜间作业进 `gate_wait` 时**不推送**，记入 `state/night-queue.json`；工单模板也告诉 worker
  夜间直接手写 handoff 记录（不调 notify.py，不打扰）。
- 夜间结束那一轮：把攒下的门槛 + 当前待处理作业汇总成一条 Bark 推给用户，清空队列。
- 派发照常跑（夜间名单场景下提交档位照样生效；user 档作业会停在 review_wait 排队）。
- 白天派出的单夜里撞上门槛：worker 按工单里的规则自己判断（读 autonomy.json 的 night 段）。

## 7. 通知

dispatcher 只发这三类（worker 的 handoff 由 worker 自己调 `host/notify.py`）：
`blocked`（作业卡死/pane 消失/空转超限/待材料）、`batch-done`（单家投递完成、
今日队列清空、夜间汇总）。`notify.yaml` 没配好时调用失败只记 `state/dispatcher.log`，不炸流程。

## 8. 故障恢复

- **崩溃重启**：所有状态在 `state/` 下，重启后轮到自己接上。幂等保证：作业已有 `task_id`
  就不再派；commands 按 id 去重；registry/tracker 在每次派单前重查。
- **worker pane 消失**（exited/不在 panes 里）：作业 → failed + blocked 通知。人工按
  `docs/handoff.md` §2 核查门户记录，确认没投成后用 approve 重派（ skipped 的单 approve 可复活）。
- **smx 桥掉线**：当轮跳过 pane 巡检与 nudge，不派新单之外的状态推进照常（标记文件照读）。
- **worker 回执 done 但没写 outcome**：进 blocked"语义不明"，人工核对，不自动重派（防重投优先）。
- **registry 写坏**：`state/registry.jsonl` 是可追加日志，坏行读取时自动跳过，可手工修复。

## 9. 从手动主持切过来

1. 回填已投登记：`cp tracker.md /tmp/ && uv run host/registry.py import-tracker <实例>/tracker.md`
   （先在副本上 `--dry-run` 看导入数对不对）。
2. 确认 `intent.yaml` 的 `permissions.submit` 与终审名单已按现在口径写好；练手档 `practice: auto`
   表示 worker 自检后直接提交，不想要就改成 `user`。
3. `uv run host/dispatcher.py --dry-run --once` 看一轮动作是否符合预期。
4. 默认 `auto` 广撒网（冲档仍 supervised 要批准）。想收窄：`cmd mode --target supervised`，
   或改 `state/autonomy.json` 的 `tier_modes` 单档覆盖。
5. 常驻：`install-plist --dest ~/Library/LaunchAgents` 后 `launchctl load`；或先在前台 pane 里跑。
6. 老批次文件 `log/B-xx.md` 不再维护——进度看 `dispatcher.py status`、web `/handoffs`、
   `host/report.py` 每日战报。
