# 投递流程速览

一页看懂这个仓库怎么把"公告"变成"已投递"，以及哪些事必须你本人做。细节在 `AGENTS.md`、`docs/spec.md`、`docs/sop.md`，这里只讲主干和最容易踩的坑。

## 1. 谁干什么（最重要，先看这个）

| 角色 | 用什么 | 干什么 | 不干什么 |
|---|---|---|---|
| **引导助手** | Codex / Claude Code | 带你装环境、生成实例文件（profile、intent、问题库）、执行"同步更新" | **不投递、不填表** |
| **主持人** | 任一能跑长会话的 CLI | 拆批派单、回收结果、维护台账与问题库、整合错题本 | 不亲自进门户填表 |
| **worker** | **Devin CLI（`swe-2-max`）** | 一人一家公司：查岗 → 填表 → 提交或停在提交前 → 回读核验 → 写结果 | 不跨公司、不改规则文件、不碰 git |
| **你本人** | 手机 + ego lite | 扫码、验证码、登录、名企终审、测评笔试 | — |

> **别用 Codex 当 worker**。浏览器填表每一步都要读页面、截图，按 token 计费的模型一家公司就可能烧掉大量额度（实测：卡在"控制权失败"重试 15 分钟后 token 耗尽）。没有 Devin 订阅就先用邮件投递 + 手动投。

## 2. 主干流程

```
公告数据（你自己的数据源适配器写到本机 data/paperball/）
      ↓  host/scan.py 按你的 intent.yaml 筛
候选队列 log/battle-map.md
      ↓  主持人/投递池派单，一个 agent 一家公司
worker：查岗 → 选最匹配岗位 → 登录 → 填表
      ↓  需要你动手时（扫码/验证码/登录/终审）
关卡 handoff → Telegram 推给你 + ego 空间交给你
      ↓  你处理完点 Return to agent
worker：提交（或名企停在提交前）→ 门户"我的投递"回读确认
      ↓
写结果：state/outcomes/ + registry（防重投）+ jd/ 存档 + log/ops 留痕
      ↓
看板（tailnet 网页）、Telegram /rank 日榜
```

**邮件投递是另一条线**：worker 只起草（`mail/<批次>/<id>/message.json`），主持人统一用 `host/mail-send.py` 发，受 `intent.permissions.email.daily_limit` 限制（默认每天 90 封）。**worker 永不发邮件**。

## 3. 一天怎么跑

1. **拉更新**（装了自动更新就不用管）：`git -C ~/campus-apply pull`
2. **出候选**：`uv run host/scan.py`，看 `log/battle-map.md`
3. **派单投递**：主持人按候选派 worker；维护者这边用固定槽位投递池 `host/slot-pool.py`（N 个槽位、一个 agent 一家、投完自动换下一家）
4. **你处理关卡**：Telegram 收到就点开处理，ego 里找显示 **Return to agent** 的那个空间
5. **看结果**：看板首页（已投 / 在跑 / 近期待办）、`jd/INDEX.md`（每家投的岗位 + JD 存档）

## 4. 必须你本人做的事

- **扫码、图形验证码、滑块、短信码**：agent 一律不自动绕过（也不许用反检测浏览器），这是红线
- **名企终审**：一线/冷门大厂、腰部名企填好后停在"确认提交"前，你核对无误自己点提交
- **测评、笔试**：agent 不代做

## 5. 最常踩的六个坑

1. **控制权失败一直转圈**：你在 ego 里自己点过页面后，空间归属就变成你的。处理完必须点 **Return to agent**；agent 那边要用 `takeOverTaskSpace(空间 id)` 重新接管，不能继续用 `taskSpace("名字")`
2. **没装自动更新**：维护者修的 bug 送不到你机器上。`uv run host/install-schedule.py install --role member`，再 `status --role member` 确认
3. **提交阶段的滑块拖过就等于提交**（Moka）：处理完必须回读"我的投递"确认，不能当作没投又重填
4. **门户投递次数限制**：有的租户每人只能投 1 个职位，有的集团门户一个账号同时只能挂 1 个申请（具体见各门户 playbook）。同门户的公司不要并发投
5. **投上了不记账**：worker 必须写 `outcome` + `registry`，否则统计少算、以后还会重复投。续投前先查门户"我的投递"
6. **并发太高**：Devin 会触发模型限流一起卡住；ego 空间开太多会拖垮机器。维护者实测 5 个槽位比较稳，10 个要配自动降速

## 6. 红线（`docs/redlines.md` 全文）

- 提交权限只看 `intent.yaml` 的 `permissions.submit`，拿不准就停在提交前
- 不编造：字段只来自 profile → 问题库 → 简历；没有就停下来问本人
- 不重投：先查 registry 和门户申请记录
- 个人信息只写实例目录，不进仓库；提交前跑 `python3 scripts/check_secrets.py --instance $CAMPUS_INSTANCE`
- 不绕过人机验证（反检测浏览器、自动答验证码一律不做）
