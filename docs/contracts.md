# 数据契约（框架各模块共享，改动须同步所有读写方）

## 0. 路径约定

- 仓库根：`$CAMPUS_REPO`（默认脚本所在仓库）
- 实例目录：`$CAMPUS_INSTANCE`（每人本地，不进 git）。建议放在 `~/campus-apply-instance`（与仓库同级、仓库之外）；正在用于真实投递的实例在开发期间只读，测试一律用副本
- 宿主机脚本一律 Python，`uv run --script` 运行，依赖用 PEP 723 内联声明（如 pyyaml），不要求用户 pip install
- 容器内同样 Python；实例目录挂载到容器 `/instance`

## 1. 实例目录

```
$CAMPUS_INSTANCE/
├─ profile.md               我是谁（自由文本 + 固定小节，见 templates/profile.template.md）
├─ intent.yaml              我要什么、怎么判、权限（§2）
├─ question-bank.yaml       问题库真源（§3）
├─ question-bank.md         由 yaml 生成的只读视图（兼容旧 worker），头部注明"生成文件勿改"
├─ accounts.md              门户账号（含密码，仅本地）
├─ materials/               证件照、生活照、简历等（见 docs/materials.md）
├─ tracker.md               投递台账
├─ log/                     battle-map.md、B-xx.md、email-queue.md、ops/、shots/
└─ state/                   host-health.json 等运行态
```

## 2. intent.yaml

```yaml
version: 1
class_types: [8]                 # 数据源届数枚举：7=26届 8=27届 9=28届
published_since: 2026-07-01      # 只看此日期后发布的公告
targets: [腾讯, 小红书]           # 心仪公司：命中且非不合适 → 冲档
recall:
  keywords: [开发, 技术, 研发, 软件, 后端, 前端, 全栈, Java, Python, Go, 管培]
                                 # 宽召回：岗位片段命中即入队（含 exclude 词的片段仍作废），
                                 # 投不投由 judge.py 读 JD 分档；为空则退回 core/ok 旧口径
roles:
  core: [Agent应用开发, 大模型应用开发]     # 方向分 3
  ok: [后端, 软件工程师, 全栈]              # 方向分 2
  exclude: [算法, AI Infra, 硬件, 销售]     # 命中直接不合适
tiers: {大厂: 3, 名企: 2, 国企: 2, 中小: 1}
urgency: {le_7d: 2, le_14d: 1.5}
cities:
  prefer: [上海, 杭州, 成都]      # 顺序即优先级
  avoid: [北京]                   # 公告城市全落这里 → 练手档照投，不排除不压分
policies:
  soe: apply_all                 # apply_all | report_only | skip
  foreign_cv: en                 # en | cn
  attachments_required: hold     # hold（标待材料）| skip
  intern: true
permissions:
  submit:
    default: worker              # worker（自检后直接提交）| user（一律停在提交前）
    user_confirm_tiers: [大厂]
    user_confirm_companies: [腾讯, 字节跳动]
    practice: auto               # 练手档默认不审核直接提交 | user
  register_accounts: user        # user | worker_email_only（worker 只可用邮箱+邮件验证码注册）
  captcha: user                  # 图形验证码、扫码、人脸一律 user，不可改
  email:
    review: all                  # all（每封审）| sample（抽查）| none
    daily_limit: 90
judgment_notes:
  - 名字带 Agent 但实际是算法岗的不投
```

## 3. question-bank.yaml

```yaml
version: 1
items:
  - id: ethnicity                # 稳定 slug
    question: 民族
    aliases: [民族信息]
    category: 身份               # 身份 | 联系 | 教育 | 家庭 | 声明 | 偏好 | 经历 | 账号
    kind: value                  # value | rule | secret
    required: true
    hint: 填写与身份证一致的民族
    answer: <答案>
    updated: <YYYY-MM-DD>
    source: 用户
rules:                           # 通用默认规则（原 md 底部"通用默认规则"段）
  - id: declaration-wording
    text: 声明类问题按具体问法判断……
```

- `secret`：网页只显示已填/未填，只写不读；API 不返回其 answer
- 写入一律经 `host/qb.py` 或 web API：文件锁 + 版本校验（sha256）+ 临时文件原子替换；写后重新生成 question-bank.md

## 4. data/paperball/announcements.jsonl

每行一条公告，按 announcement_id 升序，字段白名单（不含任何用户侧投递状态）：

```json
{"announcement_id": 10001, "title": "…", "company": "…", "company_tags": ["国企"],
 "class_time": "2027届", "class_types": [8], "published_at": "2026-09-01",
 "expired_at": "2026-09-30", "degrees": ["本科","硕士"], "original_jobs": "…",
 "link": "https://…", "from_url": "https://…", "written_test": null, "cities": ["深圳"]}
```

- `company_tags`：看板行业/性质标签 + 规模标签拼接，如 `["银行/金融","国企","一线大厂"]`
- `from_url`：邮件类公告是"邮箱投递：xx@yy（主题格式…）"文本，不是 mailto；邮箱从文本抽取
- `expired_at` 约九成为空，紧迫度加分只对有截止日的生效

`data/paperball/meta.json`：`{"synced_at": ISO时间, "count": N, "filter": {...}}`
`data/tiers.yaml`：公司 → 层级（大厂/名企/国企/外企），从公开名单整理，可人工补

## 5. 候选输出（scan.py → 实例）

`$CAMPUS_INSTANCE/log/battle-map.md`，保持现有表头，看板解析器按表头名取列：
`| score | 发布日期 | 公司 | 标题 | id | 类别 | 命中关键词 | expired_at | 邮件类 | 看板状态 | 备注 |`
（邮件类列：`否` 或 `是:<收件邮箱>`；类别列即层级：大厂/名企/国企/外企/中小；
备注列前缀档标 `[冲]/[常规]/[练手]`（来自 §7 state/fit），排序 冲→常规→未判→练手；
判"不合适"的不进主表，单列文末"已判不合适"段）

## 6. 通知与 handoff

`$CAMPUS_INSTANCE/notify.yaml`（不进 git）：

```yaml
bark_server: https://api.day.app      # 默认公共服务器；维护者可改成自建 bark-server
device_key: <Bark App 里复制>
web_base: https://<机器名>.<tailnet>.ts.net   # 通知点击跳转的前缀
quiet_hours: ["00:30", "08:00"]       # 期间只发 handoff，其余攒到结束后
events:
  handoff:    {enabled: true, level: timeSensitive, sound: alarm}
  blocked:    {enabled: true, level: active}
  batch-done: {enabled: true, level: passive}
  deadline:   {enabled: true, level: active}
  assessment: {enabled: true, level: timeSensitive}
```

handoff 记录 `$CAMPUS_INSTANCE/state/handoffs/<id>.json`：

```json
{"id": "20260915-2130-10001-qr", "event": "handoff", "kind": "wechat_qr",
 "company": "…", "announcement_id": 10001, "worker": "W3",
 "title": "北森登录需要微信扫码", "action": "长按图片识别二维码，扫完在 seedmux 回复 好了",
 "shot": "log/shots/10001-qr.png", "created_at": "ISO", "resolved_at": null}
```

调用：`uv run host/notify.py handoff --company 示例公司 --id 10001 --kind wechat_qr --shot log/shots/10001-qr.png --action "…"`
notify.py 负责：写 handoff 记录 → 10 分钟去重 → 发 Bark（url 指向 `web_base/handoff/<id>`）→ 失败重试 3 次并写 `state/notify.log`。
web 提供 `/handoff/<id>`（截图 + 说明 + "已处理"按钮写 resolved_at）和 `/handoffs`（未处理列表）。

## 7. data/jd（共享）与 state/fit（个人）

`$CAMPUS_REPO/data/jd/<announcement_id>.md`（fetch-jd.py 产出，JD 是公开信息；共用一个私有仓库时由一个人抓、大家 pull，单人使用就是本机产物）：

```markdown
---
announcement_id: 10001
company: 示例公司
source_url: https://mp.weixin.qq.com/s/…
fetched_at: 2026-09-15T23:40:00+08:00
status: ok                 # ok | ok_ocr | image_only | login_required | gone | missing
content_sha256: …          # 正文 sha256
chars: 4321
via: http                  # http | browser（ego-browser 兜底）| http+ocr | browser+ocr
ocr_images: 12             # 仅 ok_ocr：识别的图片张数
ocr_chars: 4321            # 仅 ok_ocr：OCR 出文字数（图片不入仓库）
---

（清洗后的正文，≤2 万字符）
```

`data/jd/index.json`：`{"<id>": {status, sha, chars, fetched_at, via}}`，原子替换。
各 status 含义与重试策略见 `adapters/sources/jd-fetch.md` §3。

`$CAMPUS_INSTANCE/state/fit/<announcement_id>.json`（judge.py 产出，判断是个人的，不进仓库）：

```json
{"announcement_id": 10001, "company": "…", "tier": "冲|常规|练手|不合适",
 "fit": 82, "roles": ["建议投的具体岗位"], "reasons": ["≤3条"],
 "risks": ["届数存疑/需附件/仅限某城市"], "basis": "jd|jobs_only",
 "model": "codex", "intent_sha": "…", "jd_sha": "…", "judged_at": "ISO"}
```

缓存键 = (announcement_id, jd_sha, intent_sha)，未变不重判；失败记 `state/fit/_errors.jsonl`。
分档后校验（judge.py 强制，不看 LLM 措辞、只看结构字段）。策略=广撒网：
只要有任何目标方向岗位就投，judge 主要职责是选岗（roles）+ 分档 + 记 risks。
- `不合适` 只认三个硬依据：①公告岗位片段**全部**命中 roles.exclude
  （"非嵌入式"等否定写法不计命中）；②公告 class_types 与 intent.class_types
  不相交（明确不含目标届数）；③岗位片段无一命中 recall.keywords
  （完全没有目标方向岗位，如 dev 公告没有任何开发类岗位）。
  其余理由（毕业时间窗口/届数存疑、城市、学历"优先"、专业不对口、证据弱）
  一律降为练手并把依据写进 risks。
- targets 命中且非不合适 → 冲；公告城市全在 cities.avoid → 练手；
  冲需 fit≥85、常规需 fit≥60，否则降档。
成本：`--order score` 按 scan 粗排从高到低判；`--budget N` 限制本次 LLM 批次数
（--incremental 默认 60），每批 5–10 条（--batch-size，默认 8）。

## 8. 控制面：自动化档位、作业状态、审批与防重投

**原则：想介入就介入，想自动化就自动化。** 所有进程（dispatcher、Telegram bot、web、CLI）只通过实例 `state/` 下的文件协作，不直接互相调用；追加写一律加锁（mkdir 锁，见 web/app/bank.py 的做法）。

### 8.1 自动化档位 `state/autonomy.json`

```json
{"mode": "supervised", "paused": false,
 "tier_modes": {"冲": "supervised", "常规": "supervised", "练手": "auto"},
 "max_parallel": 4, "daily_cap": 40,
 "night": {"start": "00:30", "end": "08:00", "gate_policy": "queue"},
 "updated_by": "telegram", "updated_at": "ISO"}
```

| mode | 选公司 | 派单 | 提交 |
|---|---|---|---|
| `manual` | 只出 /today 清单 | 不自动派，只有用户点"投"才派 | 一律停在提交前 |
| `supervised` | 用户在 /today 或 Telegram 批准 | 批准的自动派 | 按 intent.permissions（练手 auto，心仪/确认名单停提交前） |
| `auto` | 按分档规则自动入队（不合适除外） | 自动派 | 同上 |

- `tier_modes` 覆盖单档；`paused: true` 时 dispatcher 不派新单，已在跑的做完当前步骤后停在安全点
- **硬门槛永远要人**，任何档位都不能关：微信扫码、人脸、短信验证码（接入短信前）、付费、电子签。图形验证码先交验证码特种兵，连续失败 3 次再要人
- 夜间（`night`）遇硬门槛：不打扰，作业进 `gate_wait` 排队，早上 08:00 汇总推送

### 8.2 作业 `state/jobs/<announcement_id>.json`

```json
{"announcement_id": 10001, "company": "…", "tier": "练手", "source": "auto|approved|manual",
 "status": "queued", "task_id": "T-xxxxxx", "pane": "UUID", "portal": "beisen",
 "gate": {"kind": "wechat_qr", "handoff_id": "…", "since": "ISO"},
 "field_table": "log/fields/10001.md",
 "history": [{"at": "ISO", "from": "queued", "to": "dispatched", "by": "dispatcher", "note": ""}]}
```

状态机：`queued → dispatched → filling → (gate_wait ↔ filling) → review_wait → submitting → submitted → verified`；任意状态可到 `blocked / skipped / failed / held`。
- `review_wait`：停在提交前等人，附字段对照表 `log/fields/<id>.md`（字段 | 填入值 | 来源：profile/question-bank 条目 id/简历）
- `held`：用户 `/hold` 暂停的单，`/resume` 回到 hold 前状态
- worker 只写回执文件和 `log/`；**作业状态只由 dispatcher 改**，dispatcher 以回执文件落盘为准，不解析 pane 输出

### 8.3 用户指令 `state/commands.jsonl`（追加写）

```json
{"id": "cmd-uuid", "at": "ISO", "by": "telegram|web|cli", "action": "approve|skip|hold|resume|refresh|takeover|submit|mode|pause|unpause|reply",
 "target": 10001, "args": {"mode": "auto", "text": "好了"}}
```

dispatcher 消费后写 `state/commands.done.jsonl`（同 id + 结果），保证每条只执行一次。`reply` 把文本转发给该作业的 worker（`smx-team send`），`takeover` 让 worker `handOff()` 浏览器并停止。

### 8.4 已投登记 `state/registry.jsonl`（防重投）

一行一次投递：`{"employer": "示例集团", "employer_key": "示例集团", "brand": "示例子品牌", "portal": "moka", "account": "邮箱或手机号的 sha256 前 12 位", "announcement_ids": [10001], "job": "…", "status": "submitted", "at": "ISO"}`
- `employer_key`：去掉"股份有限公司/集团/校招"等后缀、括号内容后的主体名，别名表 `data/employer-aliases.yaml`
- dispatcher 派单前必须查：同 employer_key + 同 portal 已有 submitted/verified → 不派，作业标 `skipped` 备注"已投过"

### 8.5 通知通道 `notify.yaml` 增补

```yaml
channels: [telegram, bark]          # 按顺序都发；telegram 是双向通道，bark 只推送
telegram:
  bot_token_cmd: security find-generic-password -s campus-apply-telegram -w
  allowed_chat_ids: [123456789]     # 只响应这些 chat，其他一律忽略并记日志
  proxy: http://127.0.0.1:6152      # launchd 没有代理环境变量，国内必须显式配置
```

### 8.6 人工关卡（扫码等）的时效协议

目标：worker 发现关卡到你手机上看到图 **≤ 5 秒**；二维码过期自动刷新**原地替换同一条消息**；扫完后的下一道认证在**同一个会话里接力**。

handoff 记录增补字段（`state/handoffs/<id>.json`）：

```json
{"status": "queued|waiting|scanned|expired|resolved|cancelled",   // queued：关卡队列已满（默认 3 个），轮到才推送
 "expires_at": "ISO 或 null（页面显示了有效期就填，否则按 kind 默认：wechat_qr 120s）",
 "refresh_count": 0, "max_refresh": 3,
 "qr_payload": "二维码解码出的内容（可选，只存实例）",
 "chain": ["wechat_qr", "wechat_confirm", "sms_code"],
 "channels": {"telegram": {"chat_id": 0, "message_id": 0}, "bark": {"sent_at": "ISO"}}}
```

worker 侧（写进 templates/worker-task.md）：
1. 发现二维码 → **只截二维码元素**（清晰、留白边，PNG）→ 立刻同步调用 `host/notify.py handoff`（不等 dispatcher 轮询）→ 然后每 2 秒检查页面状态
2. 页面变化时调用 `host/notify.py handoff-update --id <id> --status scanned|expired|resolved [--shot 新图] [--expires-at …]`
3. 过期：自动点刷新、截新图、`handoff-update --shot`（同一条 Telegram 消息换图，计数 +1），超过 `max_refresh` 停止，状态 expired，等用户点 [重新获取]
4. 扫码后出现下一道认证（手机上点确认、短信码、绑定手机号、人脸）：同一个 handoff `chain` 追加一步，`handoff-update` 发新说明；需要输入的（短信码）等 `commands.jsonl` 里该 handoff 的 `reply` 文本，填入后继续
5. 人脸、付费、电子签：直接 blocked，不接力

通知侧：
- Telegram `sendPhoto` 带说明（公司、有效期倒计时到几点几分、第几次刷新）+ 按钮 [已扫码] [重新获取] [跳过] [我来接管]；`handoff-update` 用 `editMessageMedia` / `editMessageCaption` 原地更新，resolved 后改成 ✅ 并移除按钮
- **Bark 同时发一条 timeSensitive 提醒**："打开 Telegram 扫码 · 公司 · 有效至 hh:mm"。原因：国内 iPhone 上 Telegram 推送依赖 VPN 在线，Bark 走 APNs 不依赖 VPN，负责把人叫醒
- 用户直接**回复**关卡消息的文字（例如短信码）→ `reply` 指令 → dispatcher 立刻转给 worker（收到 reply 类指令时 dispatcher 立即处理，不等 30 秒轮询）
- 延迟埋点：handoff 记录写 `detected_at`、`notified_at`、`delivered_at`（Telegram API 返回时间），战报统计 p50/p95

### 8.7 关卡兜底矩阵

标准做法：**Telegram 装在平板上，手机微信扫平板屏幕**。所有兜底都要自动化，不能让一单卡死整条流水线。

| 情况 | 判定 | 自动处理 | 人要做的 |
|---|---|---|---|
| 二维码过期没扫 | 页面提示过期/倒计时结束 | 自动刷新换图，最多 3 次 | 无 |
| 3 次都没扫 | refresh_count 用尽 | 作业转 `gate_wait:parked`，**释放 worker 和浏览器去做下一家**；消息留 [重新获取] | 方便时点 [重新获取]，dispatcher 重新派单（浏览器登录态若还在就接着填） |
| 扫了但页面没反应 | scanned 后 60 秒页面未跳转 | 截图 + 刷新页面检测登录态，已登录就继续；未登录回到"过期"分支 | 无 |
| 扫码后要求点确认/短信码/绑手机 | 页面出现新提示 | chain 接力，同一条会话更新说明 | 点确认或回复验证码 |
| 验证码回复错了/过期 | 页面报错 | 截图回传，提示重新获取，最多 2 次 | 再回复一次 |
| 平板 Telegram 没收到（VPN 断/平板离线） | 发出 5 分钟无任何按钮/回复（tgbot 巡检，写 reminded_at） | Bark 再提醒一次；web `/handoff/<id>` 同步显示同一张图（任意设备浏览器打开） | 换设备打开 |
| bot 进程挂了 | `state/tgbot.heartbeat` 的 mtime 超过 2 分钟 | launchd KeepAlive 重启；notify.py 发送失败降级 Bark + web | 无 |
| 人脸/付费/电子签 | 页面识别 | blocked，不接力 | 自己决定 |
| 同一门户反复卡关 | 同 portal 24 小时内 parked ≥3 | 该门户后续作业暂停并在战报标出 | 看战报决定 |
| parked 超过 48 小时 | since 超时 | 作业转 `skipped:gate_timeout`，截止前 2 天会再提醒一次 | 无 |

### 8.8 Telegram 按钮 → 指令映射（实现现状）

| 按钮 | 写入 commands | handoff 状态 |
|---|---|---|
| [已扫码] | `reply` text=已扫码, args.handoff_id | scanned（最终 resolved 仍由 worker `handoff-update` 写） |
| [重新获取] | `refresh` target=公告id, args.handoff_id（在跑→转 worker 刷新二维码；parked→重派；dispatcher 同时兼容旧的带 handoff_id 的 resume） | 不变 |
| [跳过] | `skip` | cancelled |
| [我来接管] | `takeover` | resolved |
| 回复关卡消息的文字 | `reply` text=原文, args.handoff_id | 不变 |

handoff 记录由 bot 增写 `interacted_at`（用户点过按钮或回复过）、`reminded_at`（已二次提醒）。
bot 自有状态文件：`state/tg-map.json`（message_id→作业/handoff）、`state/tg-offset.json`、`state/tgbot.log`、`state/tgbot.heartbeat`；5 分钟无响应的 Bark 二次提醒由 tgbot 负责（dispatcher 不重复发）；可选配置 `telegram.api_base`（测试用）。

## 9. 数据回流：worker 写结构化事件，台账与看板自动生成

**原则：人不手写台账。** worker 每完成一步就写结构化文件，dispatcher 更新作业状态，tracker/assessments/看板全部由脚本生成。手写文件只保留为"生成结果"，任何 agent 都不再直接编辑它们。

### 9.1 事件流 `state/events.jsonl`（追加，mkdir 锁）

```json
{"at": "ISO", "job": 10001, "by": "worker|dispatcher|mail|user", "pane": "UUID",
 "step": "claim|portal_detected|account_ready|filling|field_blocked|gate|submitted|verified|failed|note",
 "portal": "beisen", "msg": "一行人话", "data": {"任意补充字段": "值"}}
```
- worker 在这些时刻必须写：接单、认出门户、登录完成、开始填表、遇到缺字段、遇到关卡、提交成功、门户回读核验、失败或放弃
- `msg` 是给人看的一行；`data` 放结构化细节（如 candidateId、岗位名、截止日）

### 9.2 投递结果 `state/outcomes/<announcement_id>.json`（worker 最终写一次）

```json
{"announcement_id": 10001, "company": "…", "employer_key": "…", "portal": "beisen",
 "job_applied": "后端开发工程师（示例城市）", "result": "submitted|blocked|skipped|failed",
 "submitted_at": "ISO", "portal_status": "简历筛选-进行中", "candidate_id": "…",
 "deadline": "2026-10-06", "resume": "cn|en", "attachments": ["证件照"],
 "next_steps": [{"kind": "assessment", "due_at": "ISO", "link": "…", "note": "72h 内完成"}],
 "reason": "失败/跳过时必填", "evidence": ["", "log/shots/10001-done.png"]}
```
- **测评、笔试、面试信息不只靠邮件**：worker 在门户页面看到就写进 `next_steps`，与 `host/mail.py` 的 `state/next-steps.jsonl` 汇合去重

### 9.3 生成物（只读，头部注明"本文件由脚本生成，勿手改"）

| 文件 | 由什么生成 |
|---|---|
| `tracker.md` | `state/jobs/` + `state/outcomes/` + `state/registry.jsonl` |
| `assessments.md` | `state/next-steps.jsonl` + outcomes 里的 next_steps |
| 看板「我的日程」 | 同上（见 §L 的 agenda 合并规则） |
| 看板「在跑批次」 | `state/jobs/` + `state/events.jsonl` 最近事件 |

生成器：`host/render-tracker.py [--check]`。`--check` 只比对不写，用于 CI 与迁移期对账。
迁移期（G 未完成前）：手写 tracker 仍是真源，生成器只写 `tracker.generated.md` 供对照，确认一致后再切换。

## 10. 共享 Telegram bot 中转（relay）

一个 bot，每位使用者各自私聊；Cloudflare Worker `tg.example.com` 持有 token，按人分队列。同学不用建 bot、不用 Bark。

### 10.1 安全
- webhook 必须校验 `X-Telegram-Bot-Api-Secret-Token`，不符直接 401
- 身份 = Telegram 数字 `user_id`；只接受 `chat.type == private` 且 `chat.id == from.id`
- 未绑定的 user_id：**不回复任何内容**（防探测），只计数
- 配对：`host/tgbot.py pair` 在本机生成 `instance_id` + 32 字节密钥（存钥匙串 `campus-apply-relay`）+ 6 位配对码（10 分钟、一次性），先向 Worker 预注册（码只存哈希）；用户在 Telegram 发 `/pair <码>` 完成绑定。同一 user_id 只能绑一个 instance；`unpair` 本机与管理员均可吊销
- Mac ↔ Worker 请求头：`X-Instance`、`X-Ts`（±60s）、`X-Nonce`（10 分钟内不可重复）、`X-Sig = HMAC-SHA256(key, method|path|ts|nonce|sha256(body))`
- Worker **不记录消息内容**：入队的指令被 ack 即删，24 小时未取自动过期；图片不在 Worker 落盘（流式转发给 Telegram）
- 高风险指令（冲档 `submit`、`mode auto`、`takeover`）由 Mac 端 dispatcher 二次确认或拒绝（配置 `telegram.relay.confirm_high_risk: true` 默认开）

### 10.2 接口（Worker）
| 方法 | 路径 | 调用方 | 作用 |
|---|---|---|---|
| POST | `/tg/webhook` | Telegram | 收更新，按 user_id → instance 入队 |
| POST | `/v1/pair/init` | Mac（签名用新密钥） | 预注册配对码哈希 |
| POST | `/v1/send` | Mac（签名） | 代发 sendMessage / sendPhoto / editMessage*；只能发给本 instance 绑定的 chat |
| GET  | `/v1/pull?after=` | Mac（签名） | 拉本 instance 的待处理更新（按钮回调、回复文字、指令） |
| POST | `/v1/ack` | Mac（签名） | 确认已处理，删除 |
| POST | `/v1/rank/report` | Mac（签名） | 上报 `{date, applied_today, applied_total}`，只有数字 |
| POST | `/v1/admin/revoke` | 维护者（管理密钥） | 吊销某 instance |

### 10.3 排行榜 /rank
- 每台 Mac 每天由 `report.py` 上报一次数字；**不含公司名、岗位、任何个人信息**；配对时可选择不参与（默认参与）
- `/rank`：任一已绑定用户可查，显示昵称（配对时自取）+ 今日 / 累计
- 定时播报：Worker Cron 每天 21:00（北京时间，UTC 13:00）私聊推给每位参与者
- 排名文案可以有点"卷"，但不点评具体公司

## 11. bot 管全局：AI 值班员 + 人工关卡总控

### 11.1 AI 值班员
- **引擎：codex CLI**（更聪明，优先）；devin 作备选。跑在各自 Mac 上，Worker 只转发，看不到问答内容
- **常驻的是 tgbot 进程，不是 AI**：AI 只在用户发来的文字规则接不住时调用
- 分层：① 关键词规则 + 模板（今天投了几家 / 截止 / 卡在哪 / 排行）0 token；② 接不住才调 codex，上下文只给预计算的状态摘要（≤2k token），细节按工具查；③ 同问题 10 分钟内且 state 未变返回缓存
- 每人每天 AI 调用上限（默认 30），超出降级为模板回答
- 主动播报全部模板化，不经 AI：早 9 点今日计划、晚 9 点战报 + 排行、事件即时推送
- **AI 没有执行权**：只能输出白名单内的"建议动作"卡片，用户点 [确认] 才写 `commands.jsonl`；白名单 = §8.3 的指令 + `targets add/remove`；不能跑 shell、改文件、给别人发消息
- 上下文排除：问题库 `secret` 条目、accounts、证件号；JD/邮件正文按不可信内容处理（防注入），不能据此自动产生动作

### 11.2 人工关卡总控
- **全局关卡队列**：同一时间只推一个，按剩余有效期升序；消息尾注"还有 N 个在排队"；处理完自动推下一个
- **同平台合并**：同门户同账号，只让第一家登录，其余等其登录成功后再开浏览器（dispatcher 负责）
- **"清关卡" / "先别吵我"**：用户口令切换，挂起期间关卡只排队
- 关卡类型与处理：

| 关卡 | 处理 |
|---|---|
| 邮箱码 | 自动（himalaya），失败转人工 |
| 短信码 | 自动（sms-code.py），发送方匹配不唯一或超时 → 转人工 |
| 微信扫码 | 推截图到平板，手机扫；§8.6 时效协议 |
| 字符型图形验证码 | 特种兵先试；失败则**截图发 bot，用户看图回复文字** |
| 九宫格点选、拖拽滑块 | **用户自己处理**：bot 提醒 + 远程接管入口（tailnet 屏幕共享），worker 暂停等待 |
| 人脸、实名、电子签、付费 | blocked，只提醒 |
| 微信公众号 / 小程序表单 | worker 进不去：bot 发链接 + 待填字段一键复制清单，用户手机上完成后回复"好了" |

- **等待以页面为准**：worker 等关卡时每 2–3 秒同时检查页面状态与 handoff/回复，页面已通过就立即 resolve 并继续，不必等用户在 Telegram 回复（用户可能直接在浏览器里亲手处理）；不同类型关卡各开 handoff，不复用 id
- **验证码回复必须绑定关卡**：推送用 ForceReply，只认"回复该关卡消息"的文字；聊天框里散发的数字不路由
- **验证码用完即删**：转交 worker 后 bot 删除用户那条消息；不写日志、不进 AI 上下文、Worker 不存
- **风控**：同平台同时只一个登录会话；验证码重发遵守门户冷却（默认 60s）且每关卡最多重发 2 次
- **离线兜底**：关卡超时走 §8.7（parked → 48h gate_timeout → 截止前 2 天提醒）；Bark 对同学为可选项
- **可观测**：每个关卡记 detected_at / resolved_at / portal / kind；战报列"最费人的门户前三"并建议预登录
