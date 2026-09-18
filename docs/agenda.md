# 我的日程（agenda.py + /agenda + schedule.py --from-agenda）

> 测评/笔试/面试/截止散在 `next-steps.jsonl`、`assessments.md`、`tracker.md` 三处，
> 容易漏。`web/app/agenda.py` 把三处合并成一张日程表，web 端展示 + 标记完成，
> `schedule.py sync --from-agenda` 把有时间的条目同步进 macOS 日历和提醒事项。

## 1. 数据源与优先级

| 优先级 | 来源 | 说明 |
|---|---|---|
| 1 | `state/next-steps.jsonl` | mail.py 从邮件抽的待办，`status=superseded` 跳过，`done` 算已完成 |
| 2 | `assessments.md` | 人工登记表（公司/类型/期限/状态 列；缺列整表跳过并告警，坏行跳过并告警） |
| 3 | `tracker.md` | 台账「关键日期/记录」列里的测评/笔试/面试片段，及未投递行的门户/材料截止 |

合并去重：**同公司主体**（归一：去括号备注、去「有限公司/集团/校招」等尾缀）+
**同类型** + **时间相近**（同日或相差 ≤2 天；任一侧无时间则视为可并）并为一条，
`sources` 记录全部出处。字段取值按优先级从成员里挑第一个非空的。

条目类型：测评 / 笔试 / 面试 / 材料截止 / 门户截止 / Offer。

## 2. 时间口径

- 「前/截止/勿晚/deadline」上下文 → 截止时间；材料/门户截止类无 hint 也按截止。
- 其余日期按开始时间；无时刻的开始按当天 09:00，无时刻的截止按当天 23:59。
- `MM-DD` 缺年补今年，早于今天 100 天以上进明年。
- 状态：`待做`（未做且未到）/ `已约`（assessments.md 标已约）/ `已过期`（过点未做）/ `已完成`（done 记录或源标记）。

## 3. 完成标记

- web `/agenda` 点「完成」→ `POST /api/agenda/done` → 追加写 `state/agenda-done.jsonl`
  （一行 `{"id","status":"done","at"}`，`status:"open"` 表示撤销完成）。
- **不改** `assessments.md`、`tracker.md`、`next-steps.jsonl`；刷新后状态保持。
- 日程 id：`ns-*`（next-steps 原 id）或 `ag-<sha1(ckey|kind|日期)[:10]>`（合并条）。
- 命令行等价物：`schedule.py done <id>`——next-step id 照旧标 done，
  日程 id 走 agenda-done.jsonl，两种 id 都会把对应提醒标完成。

## 4. 同步进日历 / 提醒事项

```bash
uv run --script host/schedule.py sync --from-agenda \
  --instance $CAMPUS_INSTANCE --calendar 秋招 --reminders 秋招 [--dry-run]
```

- 只同步「待做/已约且有时间」的条目；无时间的（待通知场次）只在 web 显示。
- 事件：有开始时间 → 开始时起 1 小时；只有截止 → 截止前 2h 的 1h 块。
- 提醒：截止/开始前 1 天 + 前 2 小时各一条（已过时刻不建）。
- 幂等：notes 里 `campus-apply:<id>`（提醒 `:d1`/`:h2` 后缀），已存在不重复，
  时间变了用 update；映射兜底在 `state/schedule-map.json`。

launchd（只生成不安装；launchd 无 TCC 图形会话，权限不行就手动跑/cron）：

```bash
uv run --script host/schedule.py plist --instance $CAMPUS_INSTANCE \
  > ~/Library/LaunchAgents/com.campus-apply.schedule.plist
# launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.campus-apply.schedule.plist
```

## 5. web 入口

- `/` 看板顶部「我的日程」：最近 8 条未完成项，≤7 天高亮、过期灰化、解析告警提示。
- `/agenda`：全部条目 + 类型/状态/搜索筛选 + 完成/撤销按钮 + 来源列。
- `/board`：行内「日程」列——按公告 id 优先、公司主体名其次，取该公司最近一条未闭环项。
