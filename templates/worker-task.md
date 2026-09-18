# 投递工单 · {{COMPANY}}（{{ANNOUNCEMENT_ID}}）

> 本工单由 host/dispatcher.py 渲染派发。worker 契约、验收命令、范围由 smx-team 自动附在工单头尾。
> 开工前必读（按顺序）：
> 1. {{REPO}}/AGENTS.md、{{REPO}}/docs/sop.md、{{REPO}}/docs/redlines.md、{{REPO}}/docs/spec.md §5–§8
> 2. 实例文件：{{INSTANCE}}/profile.md、question-bank.md、accounts.md、tracker.md
> 3. 门户 playbook：{{REPO}}/portals/README.md 及对应门户页（识别不出门户时也要看）
> 4. ego-browser 用法以工具自带文档为准

## 公司信息

- 公司：{{COMPANY}}（层级 {{COMPANY_TIER}}，分档 {{TIER}}）
- announcement_id：{{ANNOUNCEMENT_ID}}（看板定位以此为准）
- 公告标题：{{TITLE}}
- 届数原文：{{CLASS_TIME}}
- 公告链接：{{LINK}}
- 投递链接：{{APPLY_LINK}}
- 城市：{{CITIES}}　截止（看板录）：{{EXPIRED_AT}}
- 建议岗位（judge 读 JD 后给出，仍需进门户核实真实岗位再选）：{{FIT_ROLES}}
- 风险提示：{{FIT_RISKS}}
{{EXTRA}}

## 权限（dispatcher 按 intent.yaml 生成，本单有效）

- 提交档位：**{{SUBMIT_LEVEL}}**（依据：{{SUBMIT_BASIS}}）
  - worker 档：填完自检后直接点最终提交 → 门户回读核验 → 看板标已投递(2) → 留痕 → 回执
  - user 档：填到最终提交按钮前，不点；写字段对照表 `{{INSTANCE}}/log/fields/{{ANNOUNCEMENT_ID}}.md`
    （三列：字段 | 填入值 | 来源：profile/question-bank 条目 id/简历）→ 截图 →
    `uv run {{REPO}}/host/notify.py handoff --company {{COMPANY}} --id {{ANNOUNCEMENT_ID}} --kind submit_confirm --shot <截图> --action "已填到提交前，请终审"` →
    交还浏览器 → 停手等 dispatcher 的 team-msg（**不要回执**）
  - 分不清某个按钮是不是最终提交时，按 user 档处理；有的门户"投递简历"点下去就是最终提交
- 注册账号：**{{REGISTER_POLICY}}**
  - user：预填全部可填字段后 handoff（kind=register），不点注册
  - worker_email_only：只可用邮箱 + 邮件验证码注册（himalaya 自取）；手机号、扫码、图形码仍 handoff
- 图形验证码、扫码、人脸、短信码：一律 handoff，不尝试（不可配置）
- 邮件：不发送任何邮件；邮件类公司只收集要素写入 {{INSTANCE}}/log/email-queue.md
- 附件政策：强制成绩单/在读证明/学生证等材料时 **{{ATTACH_POLICY}}**
  （hold：看板标待投递，备注"待材料：需XX，门户截止YYYY-MM-DD"，outcome 写 `blocked --reason "待材料：需XX"` 后回执 blocked；
  skip：标不合适并写原因）；外企一律 blocked
- 外企简历：**{{FOREIGN_CV}}**（en=英文版 PDF，cn=中文版）
- 不重投：先查 tracker、你自己的看板（若有）、门户申请记录，查到雇主主体层；已有记录 → blocked
- 不编造：字段只来自 profile / question-bank / 简历 PDF；无数据源 → blocked 并列出字段名
- 需要用户动手时必须调用 host/notify.py handoff（夜间例外见下），然后停手
- 只在 {{INSTANCE}}/log/、{{INSTANCE}}/state/outcomes/ 和本工单目录写文件；不碰 git；不改规则文件

## 登录与验证通道（项目约定，顺序固定，能用上游就不用下游）

撞登录/注册墙时：
1. 预登录：{{INSTANCE}}/accounts.md 有该平台记录 → 登录态活着直接进；掉了按记录方式恢复
2. 邮箱码：门户有邮箱注册/邮箱码登录入口 → 用邮箱，验证码 himalaya 只读取
3. 手机短信码：门户有手机码入口 → 填手机号发码 →
   `uv run {{REPO}}/host/sms-code.py --since 5m --sender-hint <公司或平台名> --wait 90`
   退出码非 0 → handoff kind=sms_code
4. 扫码（微信/抖音/App）：前三条都走不通才用 → 截二维码 + `notify.py handoff --kind wechat_qr`（按下面「人工关卡时效协议」接力）
- 新注册账号仍按上方"注册账号"权限执行；短信码自取只用于登录已有账号
- 图形验证码/滑块/点选：先验证码特种兵（截图发其 pane，连败 3 次停）→ handoff kind=captcha；纯字符码可自行 OCR
- 人脸/实名核验/付费/电子签：blocked，不接力
- 各平台默认通道与降级细则：{{REPO}}/docs/auth-channels.md；预注册清单：{{REPO}}/docs/platform-accounts.md

## 人工关卡时效协议（扫码/二维码类关卡，契约 §8.6）

从你看到二维码到用户手机收到图 ≤5 秒是硬指标；过期自动刷新是原地换图，不是新发通知。

1. 发现二维码 → **只截二维码元素**（清晰、留白边、PNG，不要整页截图）→ 立刻同步调
   `uv run {{REPO}}/host/notify.py handoff --company {{COMPANY}} --id {{ANNOUNCEMENT_ID}} --kind wechat_qr --shot <截图相对实例路径> --action "长按识别二维码，微信扫码"`
   （不等 dispatcher 轮询）→ 记下返回的 handoff id，之后每 2 秒检查一次页面状态。
2. 页面变化 → `uv run {{REPO}}/host/notify.py handoff-update --id <handoff_id> --status scanned|expired|resolved [--shot 新图] [--expires-at ISO]`。
   **关卡通过后必须立刻 `--status resolved`**：以页面确认登录/验证通过为准（用户点了 [已扫码] 只是 scanned）；连续撞多道关卡时每个 handoff 各自 resolve，否则看板「待我处理」会一直挂着。
3. 二维码过期：自己点页面上的刷新/换码 → 截新图 → `handoff-update --id <hid> --shot <新图>`
   （同一条通知原地换图，refresh_count 自动 +1）。刷新到 max_refresh（默认 3）次仍没人扫 →
   `handoff-update --id <hid> --status expired`，然后 handOff() 浏览器交还现场
   （**不关 TaskSpace**，登录态留给重派用），本回合结束、不回执——dispatcher 会把本单
   parked 并去派下一家；用户之后点「重新获取」，本单可能重新派回（优先回原 pane），按新工单继续。
4. 扫码后出现下一道认证（手机上点确认 / 短信码 / 绑定手机号）：同一条 handoff 接力——
   `handoff-update --id <hid> --chain-step <kind> --note "<新说明：要用户做什么>"`；
   需要用户输入的（如短信码）在 --note 里写清回什么，然后等 dispatcher 转发来的
   `[team-msg task=…] 用户说：…`，填入后继续。短信码先试上面第 3 条通道自取。
5. 人脸、付费、电子签：不接力，直接 outcome blocked。
6. scanned 后页面超 60 秒没跳转：dispatcher 会发消息让你检测——刷新页面看登录态，
   已登录 → `handoff-update --status resolved` 继续填报；未登录 → 回第 3 步走过期分支。
7. **等待期间以页面为准，回复只是其中一个信号**：用户可能直接在浏览器里亲手处理（扫码、拖滑块、点选、输验证码），
   不一定回 Telegram。所以任何关卡的等待循环都必须**每 2–3 秒同时检查两件事**：
   ① 页面状态（关卡元素是否消失、URL 是否跳转、是否出现下一步表单/登录态）；
   ② handoff 状态与 commands.jsonl 里带该 handoff_id 的回复。
   **任一条件满足就继续**：页面已通过 → 立刻 `handoff-update --status resolved` 并往下做，不再等回复；
   收到回复（验证码、格子编号）→ 填入后再确认页面通过。禁止只盯回复、不看页面的纯等待循环。
8. **不同类型的关卡各开各的 handoff**：扫码之后又撞图形验证码/点选/滑块，要重新截图并新开 `--kind captcha` 的 handoff，
   不要复用扫码的 handoff id（第 4 条的 chain-step 只用于"扫码后在手机上点确认、填短信码"这类无需新截图的接力）。
9. 等待上限 15 分钟：页面仍未通过且没有回复 → 该 handoff `--status expired`，outcome blocked「等人工·<关卡类型>」，
   handOff() 交还浏览器（不关 TaskSpace），去做下一家。
10b. **轮到你时要确认（两段式）**：排队的 handoff 轮到时 status 会从 `queued` 变成 `ready`（此时还没推给用户）。你必须在 **3 分钟内**：
   回到该公司页面 → 确认验证码/扫码还在（过期就刷新出新的）→ 重新截图 → 关掉无关 tab、`task.handOff()` →
   `notify.py handoff-update --id <hid> --status waiting --shot <新截图> --space <task.spaceId> --page <关卡页标签如 p1> --note "<说明>"`，这一步才真正推给用户。
   **宿主机硬校验**（不通过就不推送、命令退出码 3、handoff 置 expired）：空间名必须含该公司 announcement_id（`campus-<id>-<公司>`，通用空间一律拒绝）；推送前自动关掉该空间里 --page 以外的标签，再 handOff 给用户。新建 handoff（captcha/wechat_qr/submit_confirm/login）同样必须带 `--space` 和 `--page`；sms_code 不需要。
   3 分钟不确认会被置 expired（视为你已离开）；页面已经不在了就直接 `--status cancelled`。
10. **关卡队列**：同时推给用户的关卡最多 3 个（notify.yaml `gate_queue.max_active`）。handoff 命令输出"排队中"时，记录 status=`queued`，轮到才推送；照常等待，**15 分钟计时从 status 变成 waiting 才开始**，不要重发、不要自己刷新验证码（排队期间验证码过期就等轮到后按 refresh 重截）。
7. 收到 `[team-msg task=…] 用户点了「重新获取」`：立刻刷新二维码 + `handoff-update --shot` 换图。

## 每开始一家前重读 state/worker-rules.md

主持人发的消息在你跑长命令时会排队、可能很晚才看到。实例里的 `state/worker-rules.md` 是**当前生效规则的唯一真源**：每开始一家公司、每次发关卡前都重读一遍，与本模板冲突时以它为准。

### 用户交还控制权后，用 `takeOverTaskSpace(空间 id)` 重新接管

`handOff()` 之后空间归属变成 `agentDelegatedToUser`。用户点了 **Return to agent** 之后：

- **正确**：`const task = await takeOverTaskSpace(<空间 id>)`，再 `task.userPage()` 拿回用户当前那一页继续做
- **错误**：继续用 `taskSpace("名字")` 或旧的 task 对象——空间不是 agent 拥有时会报"控制权失败/用户已接管"，反复重试就变成长时间转圈
- 空间 id 用 `listTaskSpaces()` 查（名字可能重名）；`ownership` 仍是 `agentDelegatedToUser` 说明用户还没点 Return to agent，按下节早停

## 控制权失败要早停（不许一直重试）

ego 返回 "The user has taken control of this task space" / 控制权失败 / "browser commands are paused" 时：
1. **最多重试 2 次**（每次间隔 30 秒以上，先 `listTaskSpaces()` 看 ownership）
2. 仍是用户控制 → 立刻停止一切浏览器操作，发 handoff（或在对话里告诉用户）："请在 ego lite 底部控制条点 **Return to agent**，把空间交还给我"，然后用 `task.waitForControl({ timeout: 120000 })` 分段等待
3. 等满 15 分钟还没交回 → outcome blocked "等用户交还浏览器控制权" 并结束。**绝不循环重试**——每次重试都在读页面、烧 token

## 交给用户时把浏览器空间交出去（Return to agent）

用户要在浏览器里亲手处理（拖滑块、点选、扫码页、接管填表）时，否则用户在一堆标签页里找不到：
0. **一家公司一个 TaskSpace，一个空间只留一个 tab**：空间名 `campus-<announcement_id>-<公司>`；查下一家就新建空间，不在同一空间里开新 tab；
   门户弹出的新窗口/新 tab，用完立刻 `page.close()`，关卡页必须是空间里唯一（至少是最后激活）的那个 tab
1. 关卡**轮到你**（handoff status 从 queued 变成 waiting）时，先关掉该空间里与关卡无关的 tab，再 `await task.handOff()`：
   ego lite 里这个空间会显示「Return to agent」，用户一眼就能找到。**排队中（queued）不要 handOff**，保证同一时间只有一个空间在等用户
2. handoff 的 `--action` 文案写明空间编号和名字，例如："ego lite 空间 #42 campus-10001-示例公司（显示 Return to agent），处理完点 Return to agent"
3. 等待用 `task.waitForControl({ timeout: 120000 })` 分段轮询（单条命令 ≤2–3 分钟），期间照常看 handoff 记录与 commands.jsonl；
   用户点了 Return to agent → 控制权回来，读页面确认关卡已过，继续提交与核验
4. 用户只在手机 Telegram 上处理（没动浏览器）的关卡不需要 handOff

## 保存 JD（每家提交成功后必须做）

用户面试前要翻看投过岗位的要求。提交并核验成功后，打开所投岗位的详情页，把岗位名称、城市、职责、要求等正文**原样**保存为
`$CAMPUS_INSTANCE/jd/<announcement_id>-<公司>-<岗位>.md`（文件名去掉 `/` 等非法字符）。开头写公司、岗位、城市、门户、详情链接、投递时间；
正文不改写、不总结。详情页打不开就保存投递页能看到的岗位描述并注明。没保存的，`host/jd-archive.py` 会用公告补录一份（信息较粗）。

## 夜间门槛（只在本单处于夜间时段时适用）

需要用户动手时，先读 `{{INSTANCE}}/state/autonomy.json` 的 `night` 段：当前时间落在
`[night.start, night.end)` 且 `gate_policy=queue` 时，**不要调用 notify.py**（不打扰用户），改为手写
handoff 记录 `{{INSTANCE}}/state/handoffs/{{ANNOUNCEMENT_ID}}-<kind>.json`，字段按
docs/contracts.md §6（id 用 `{{ANNOUNCEMENT_ID}}-<kind>`、event=handoff、resolved_at=null、
created_at 填当前 ISO），截图照存，然后给派发者发 team-msg 说明在等什么，停手。
白天或 gate_policy 不是 queue 时，正常 `notify.py handoff`。

## 流程（SOP 骨架，细节以 docs/sop.md 为准）

1. 一个 ego-browser TaskSpace 用到底（命名 campus-<id>），p1 固定留给门户；闭环后才 finish()。
2. 在本机公告数据（`data/paperball/announcements.jsonl`）里按公司名查、用 {{ANNOUNCEMENT_ID}} 对齐三要素（公司名+标题+届数）；
   再查 tracker / 你自己的看板（若有） / 门户申请记录防重（查到雇主主体层，已有 → blocked）。
3. 届数核对：class_types 与本人工位届数有交集才继续；不含 → 看板标不合适+原因，outcome 写 `skipped --reason "不合适：…"`，回执。
4. 开投递链接逐岗读 JD，按 intent 选最合适岗位（{{FIT_ROLES}} 只是建议）；核实门户真实截止日。
5. 判定投递方式：邮件 → 只收集要素入队；官网表单 / 需注册按权限段落走。
6. 填表 → 自检（必填无红标、附件刷新回读、志愿顺序符合 intent、声明题读题干）。
7. 按提交档位：worker 档直接提交；user 档写字段对照表 + handoff + 停手。
8. 提交后门户"我的投递"回读核验截图 → 写 outcome → 补留痕。
9. 写 outcome 文件（见下）→ 操作留痕 `{{INSTANCE}}/log/ops/{{BATCH}}-{{ANNOUNCEMENT_ID}}.md`（SOP §6 格式）→ 回执。

## 状态标记文件（dispatcher 只认这些，务必照写）

| 时机 | 文件 | 含义 |
|---|---|---|
| user 档停在提交前 | `{{INSTANCE}}/log/fields/{{ANNOUNCEMENT_ID}}.md` | 作业进入 review_wait，等用户终审 |
| 等用户动手（扫码/验证码/注册/材料…） | `notify.py handoff` 产出的 `state/handoffs/*.json` | 作业进入 gate_wait，不催你 |
| 任何终局 | `{{INSTANCE}}/state/outcomes/{{ANNOUNCEMENT_ID}}.json` | dispatcher 据此结案 |

## 结构化回写（contracts §9；tracker.md / assessments.md 由脚本生成，不要手改）

命令入口：`uv run {{REPO}}/host/events.py`（参数错误会给中文报错；`--instance` 默认 `$CAMPUS_INSTANCE`）。

### 事件流 `state/events.jsonl`——关键节点必须写，一行一条

```bash
uv run {{REPO}}/host/events.py event --job {{ANNOUNCEMENT_ID}} --step <step> --msg "一行人话" [--portal <短名>] [--data k=v ...]
```

| step | 什么时候写 | 常带的字段 |
|---|---|---|
| `claim` | 接单开工 | `--msg "接单，开 TaskSpace N"` |
| `portal_detected` | 认出门户 | `--portal beisen/moka/…` |
| `account_ready` | 登录/注册完成 | `--msg 登录方式` |
| `filling` | 开始填表 | `--data page=字段页` |
| `field_blocked` | 遇到缺字段 | `--data field=字段名` |
| `gate` | 遇到关卡（先调 notify.py handoff，再写本事件） | `--data kind=wechat_qr --data handoff=<id>` |
| `submitted` | 门户提示提交成功 | `--data candidateId=…` |
| `verified` | 门户"我的投递"回读到 | `--msg 回读状态` |
| `failed` | 失败或放弃 | `--msg 原因`，随后写 outcome |
| `note` | 其他想说清的进展 | `--msg 自由文本` |

### 终局 outcome `state/outcomes/{{ANNOUNCEMENT_ID}}.json`——每单只写一次，写完再回执

```bash
uv run {{REPO}}/host/events.py outcome --job {{ANNOUNCEMENT_ID}} --result submitted \
  --company {{COMPANY}} --portal <短名> --job-applied "实际岗位名" \
  --portal-status "门户回读状态" --candidate-id "若有" --deadline <门户核实截止> \
  --evidence log/ops/{{BATCH}}-{{ANNOUNCEMENT_ID}}.md --evidence log/shots/{{ANNOUNCEMENT_ID}}-done.png
```

- `--result` 枚举（§9.2）：`submitted`（含门户已回读的）| `blocked` | `skipped` | `failed`；后三个**必须** `--reason`
- `submitted` 尽量带 `--portal-status` / `--candidate-id` / `--submitted-at`（缺省取当前时间）
- 不合适/已投过 → `skipped --reason "…"`；待材料 → `blocked --reason "待材料：需XX"`；卡住 → `blocked --reason "…"`
- 额外字段用 `--set k=v`（如 `--set note=第1志愿`）

### 测评/笔试/面试——门户页面看到就写 `--next-step`（可重复）

```bash
  --next-step "kind=assessment,due_at=2026-10-01T18:00:00+08:00,link=https://…,note=72h 内完成"
```

kind：assessment / written_test / interview / offer。写了会自动并进 `state/next-steps.jsonl`
（与邮件待办去重），进 assessments 表和日历；**不要**只靠留痕文本记期限。

### 与 dispatcher 信号的关系

事件/outcome 是给台账和看板的数据源，**不替代**现有信号文件：handoff 记录、
`log/fields/<id>.md`、回执照照发。worker 只写 `{{INSTANCE}}/log/` 与 `{{INSTANCE}}/state/` 下本单文件。

- 收到 `[team-msg task=…] 继续` 说明 handoff 已处理，恢复操作；
  收到 `提交` 说明用户已终审（或本人已点提交），直接做阶段 3 核验，不要重复点提交。


## 跳过也必须写结果

查岗后决定不投（岗位不对口、届数不符、只招硬件/芯片/算法、强制附件缺失）时，同样执行 `host/events.py outcome --result skipped --reason "<一句话原因>"`，否则看板查不到这家看过、会被重复派单。

## 错题本（每单必写，流程越跑越顺靠它）

回执前把本单踩到的坑追加到实例 `log/lessons/<门户短名>.md`（没有就新建），一行一条：

`| 日期 | 公司(announcement_id) | 现象 | 怎么解决的 / 没解决 | 建议写进哪里（portal playbook / 问题库 / 工单模板 / 工具） |`

- 只写**新的**坑：先读 `{{REPO}}/portals/<门户>.md` 的正文和错题本，已经写过的不重复
- 问题库缺字段、答案口径不清的，写进 `log/lessons/question-bank.md`，并注明表单里的原始问法
- 不写账号、手机号、candidateId 等个人信息
