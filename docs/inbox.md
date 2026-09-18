# 收信分拣与日程同步（mail.py + schedule.py）

> 解决秋招最容易丢的机会：测评/笔试链接埋在邮箱里过期。
> `mail.py poll` 增量扫收件箱 → 规则/LLM 分类抽取 → 写实例 `state/`；
> `schedule.py sync` 把待办同步进 macOS 日历和提醒事项。

## 1. 流程

```
himalaya（只读 IMAP）
  └─ mail.py poll            每 15 分钟（launchd，见 §5）
       ├─ state/inbox/seen.json          已处理 message-id（幂等去重）
       ├─ state/inbox/<date>/<hash>.json 每封一档：分类 + 抽取 + ≤500 字片段
       ├─ state/next-steps.jsonl         一行一个待办（schedule.py 消费）
       └─ notify.py assessment           测评/笔试/面试/Offer 推手机
  └─ schedule.py sync
       ├─ 日历：测评/笔试按截止前 2h 建 1h 块（有开始时间按开始时间）；面试按时间
       └─ 提醒事项：截止/开始前 1 天 + 前 2 小时各一条
```

- **mail.py 不改 tracker.md**——台账由 `host/render-tracker.py` 从事件和结果生成。announcement_id 匹配结果只写进待办和封档。
- `done <id>` 标完成：待办转 `status:done`，对应提醒勾掉；日历事件保留当记录。

## 2. 分类

| kind | 含义 | 典型特征 | 后续动作 |
|---|---|---|---|
| assessment | 测评/人才测评 | 测评、评测、人才测评、assessment | 建待办 + 通知 + 日历/提醒 |
| written_test | 笔试/在线考试 | 笔试、机试、考试通知、written test | 同上 |
| interview | 面试邀约 | 面试、约面、interview | 同上 |
| offer | 录用通知 | 录用通知、offer letter、拟录用 | 同上 |
| rejection | 拒信 | 很遗憾、未通过、人才库、感谢信 | 只进汇总，不打扰 |
| application_ack | 投递确认 | 已收到、投递成功、申请已提交 | 只进汇总 |
| verification_code | 验证码 | 验证码、校验码、登录/邮箱验证 | 只识别，**不落正文** |
| other | 其他 | 营销、newsletter、注册激活等 | 只进汇总 |

判定顺序：规则先行——
1. 验证码与其他类同时命中时，看正文有没有"独立成行 4-8 位数字"的码样式，有才算验证码；
2. 多个类别命中时，主题命中的那一类算主要目的（"测评通知"正文顺带提笔试 → 测评）；
3. 拒信必含"感谢投递"措辞 → rejection 压过 application_ack；
4. 仍拿不准（多类无主题倾向、零命中但像招聘域）→ 交 LLM（`--engine codex|devin|claude`，默认 codex，非交互、严格 JSON schema）；LLM 挂了按"正文里最先出现的类别"兜底；
5. `--no-llm` 完全不调 LLM（验收/离线用）。

## 3. 抽取与匹配

每封抽取：公司、岗位、截止/开始时间（Asia/Shanghai）、入口链接、要做的动作、announcement_id。

- 时间口径：只有日期没有时刻的截止 → 当天 23:59；"72 小时内"类相对时间 → 以邮件处理时刻为基准换算绝对时间；
- announcement_id 匹配：实例 `tracker.md` 表 → `state/registry.jsonl` → 仓库 `data/paperball/announcements.jsonl`（+ `data/employer-aliases.yaml` 若有），正文里的平台词（腾讯会议/牛客/智联等）不匹配；同公司多候选时留空并记 `announcement_id_candidates`；
- 同公司同类新邮件进来，旧 open 待办自动转 `superseded`。

## 4. 只读保证与隐私

- 只调用 `himalaya envelope list` 和 `himalaya message read --preview`（`--preview` 官方承诺不打 Seen 标）。不删除、不移动、不打标签、不发信。
- 线上投递 worker 也在用 himalaya 读验证码——本工具全程只读，不抢 Seen。
- 落盘正文只保留抽取所需片段（≤500 字）；验证码邮件只记元信息不写正文。
- 含 token 的测评链接照常写进实例目录（实例不进 git）；提交仓库前照跑 `scripts/check_secrets.py`。
- `--dry-run` 不写任何状态、不发通知（也不调 notify.py，避免创建锁文件）——对真实邮箱验收用。

```bash
# 手动跑一次（真实邮箱，只读预览）
uv run host/mail.py poll --since 3d --dry-run
# 实跑（写 state + 发通知）
uv run host/mail.py poll --since 2d
# 离线样例目录（.eml + expected.json 算准确率）
uv run host/mail.py poll --fixtures <dir> --instance <测试实例> --no-llm
```

## 5. 日程同步

```bash
uv run host/schedule.py sync [--calendar 秋招] [--reminders 秋招] [--dry-run]
uv run host/schedule.py sync --from-agenda   # 合并三来源的「我的日程」（见 docs/agenda.md）
uv run host/schedule.py done <id> [--dry-run] # next-step id 或日程 ag-xxxx id 均可
uv run host/schedule.py plist                # 生成 launchd plist（只打印不安装）
```

- `--from-agenda`：同步 `web/app/agenda.py` 合并后的日程（next-steps + assessments.md +
  tracker），待做/已约且有时间的条目才进日历/提醒；合并规则见 `docs/agenda.md`。
- 幂等：事件/提醒的 notes 里写 `campus-apply:<待办id>`（提醒加 `:d1`/`:h2` 后缀）做唯一标记；
  已存在不重建，时间变了用 `event update` 更新；映射同时存 `state/schedule-map.json` 兜底。
- 配置：命令行参数优先；否则读实例 `schedule.yaml`（`calendar:` / `reminders:` 字段，可缺省）；都没有用默认 `秋招`。
- **权限（TCC 按"负责进程"记授权，谁跑 `event` 授权给谁）**：
  - 交互跑：需要承载 `event` 的终端 App 的 Info.plist 里有
    NSCalendars/NSRemindersUsageDescription 才会弹授权框。**kitty.app 有（已实测）、
    Terminal.app 有；Seedmux.app 没有**——在 seedmux pane 里跑会直接报
    `Permission denied` 且无法弹框。
  - 授权方法：在 **kitty（或 Terminal.app）窗口**里跑一次 `event calendar list`，
    系统弹框点"允许"；授权条目出现在 系统设置 → 隐私与安全性 → 日历 / 提醒事项，
    名字是承载终端（如 kitty），不是 event 本身。也可在那里手动开关。
  - 复验：`event calendar list --json` 与 `event reminders lists list` 能输出
    JSON/列表而不是 Permission denied 即通过；然后跑
    `uv run host/schedule.py sync --dry-run` 看计划，再去掉 `--dry-run` 实同步。
  - 换过终端或授权被拒后想重弹框：`tccutil reset Calendars net.kovidgoyal.kitty`
    和 `tccutil reset Reminders net.kovidgoyal.kitty`（bundle id 换成对应终端）。
  - launchd 定时任务没有可弹框的 GUI 负责进程，EventKit 直接拒绝，见 §6 备注。

## 6. launchd 每 15 分钟跑一次（生成命令，不自动安装）

```bash
cat > ~/Library/LaunchAgents/me.campus-apply.mail.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>me.campus-apply.mail</string>
  <key>ProgramArguments</key><array>
    <string>/bin/bash</string><string>-lc</string>
    <string>cd $CAMPUS_REPO && uv run host/mail.py poll --since 2d >> $CAMPUS_INSTANCE/state/mail.log 2>&1 && uv run host/schedule.py sync >> $CAMPUS_INSTANCE/state/schedule.log 2>&1</string>
  </array>
  <key>StartInterval</key><integer>900</integer>
  <key>RunAtLoad</key><false/>
</dict></plist>
EOF
launchctl load ~/Library/LaunchAgents/me.campus-apply.mail.plist   # 确认无误再执行
```

注意：launchd 跑的 `schedule.py sync` 同样受 §5 权限约束——launchd 进程没有可弹框的
GUI 父进程，EventKit 会直接拒绝。如果 sync 因此失败，可选方案：
(a) 让 launchd 只跑 `mail.py poll`，日程同步由人工/登录时跑一次；
(b) 把 `schedule.py sync` 包进带 NSCalendars/NSRemindersUsageDescription 的 .app 壳里跑。

## 7. 与其他 worker 的边界

- 只写 `state/inbox/`、`state/next-steps.jsonl`、`state/schedule-map.json`；
- tracker.md、commands.jsonl、registry.jsonl 只读不写；
- 通知统一过 `host/notify.py`（按 `notify.yaml` 的 channels 顺序发，Telegram / Bark）。
