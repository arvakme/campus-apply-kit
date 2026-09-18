# 流水线规范

> 本文从维护者 v1 流水线（2026-09-14 定稿，此后多轮校准）提炼而来。个人偏好一律改为读实例文件：
> 意向、打分、政策、权限读 `intent.yaml`；字段答案和填表规则读 `question-bank.yaml`。

## 1. 目标

帮用户半自动投递校招公告数据源上的公司：数据全量同步，按个人意向本地筛选；机器能做的机器做，必须本人做的集中推给用户。

## 2. 角色

| 角色 | 职责 |
|---|---|
| 主持人 | 读候选队列、拆批派单、审阅回执、向用户汇报、维护 tracker 与问题库、监控长任务 |
| worker | 每人一个浏览器 TaskSpace，一次只处理一家公司；同一 announcement_id 绝不派给两人 |
| 用户 | 审批名单；注册/登录中需要本人的环节；按权限终审提交；回答问题库缺口 |
| 邮件通道 | 邮件类投递由主持人或专门 agent 起草，经 himalaya 发送，受 `intent.permissions.email` 约束 |

并发上限由用户机器决定。维护者实测 4–5 个 worker 并发是上限，浏览器空间闭环后要及时关闭。

## 3. 数据流

```
host/sync-source.py（你自己的数据源适配器）→ 本机 data/paperball/announcements.jsonl（不进 git）
  → host/scan.py 读 intent.yaml → $CAMPUS_INSTANCE/log/battle-map.md（候选 + score）
  → 主持人拆批 → log/B-xx.md + seedmux 工单
  → worker 执行 → log/ops/ 留痕 + 看板回写 + reply.md
  → 主持人审阅 → tracker.md
```

## 4. 合适性判断

全部由 `intent.yaml` 决定：

| 判断 | 字段 | 规则 |
|---|---|---|
| 届数 | `class_types` | 公告 `class_types` 与之有交集才看；worker 仍需读公告原文核实 |
| 时间 | `published_since` | 早于此日期的公告不进候选。维护者经验：往年带新届标签的记录大多是蹭标签，正式校招集中在当年 7 月底之后 |
| 方向 | `roles.core / ok / exclude` | 命中 exclude 直接不合适；核心 3 分、可投 2 分 |
| 公司层级 | `tiers` + `data/tiers.yaml` | 层级分 |
| 紧迫 | `urgency` | 截止 ≤7 天、≤14 天加权 |
| 城市 | `cities.prefer / avoid` | 多选时按 prefer 顺序；只能选 avoid 城市时怎么办写在 `judgment_notes` |
| 其他判断 | `judgment_notes` | 自由文本，worker 必须读 |

打分：`score = 方向分 × 层级分 × 紧迫系数`，由 `host/scan.py` 计算。

名字和实际不符的岗位很常见（名字带 Agent 但实际是算法岗、名字叫后端但实为芯片后端、名字叫产品但实为销售）。worker 查岗必须打开 JD 看职责，不按标题判断。拿不准时对照简历看经历是否搭，仍模糊就 blocked。

不合适必须写具体原因，例如“仅招 26 届”“全部为电气岗，无软件岗”“已截止”。

## 5. 特殊公司政策

由 `intent.policies` 决定，主持人派单时写进工单：

| 字段 | 取值 | 含义 |
|---|---|---|
| `soe` | `apply_all` / `report_only` / `skip` | 国企央企：有合适岗位就投 / 只汇报由用户决定 / 跳过 |
| `foreign_cv` | `en` / `cn` | 外企（含外资在华分部）用哪版简历 |
| `attachments_required` | `hold` / `skip` | 表单强制成绩单、在读证明、学生证等附件时：标“待投递·待材料”报主持人 / 标不合适 |
| `intern` | `true` / `false` | 是否投实习岗 |

补充约定：

- 外企遇到强制附加材料，一律 blocked 问用户，不自动 skip。
- GPA、均分等数字字段以简历 PDF 为准，简历没有就查问题库，都没有留空，不编。
- 邮件投递类公司：worker 只收集要素入队（见 `docs/sop.md` §3.1），不发送。
- 同一集团多个主体：按雇主主体防重，见 `docs/handoff.md` §4。

## 6. 状态


### 6.1 tracker 状态机

```
候选 → 查岗 ─┬→ 不合适（写原因）
             └→ 填表 → 待提交 → 已投递 → 待笔试/已笔试 → 一面/二面/三面 → Offer
                  │        │
                  │        └→ 待核验（提交后门户回读不到）
                  └→ 待投递·待材料（缺附件，等用户备料）
任意状态 → 已停止（用户暂缓，写重启路径）
```

| tracker 状态 |
|---|
| 候选、查岗 |
| 不合适 |
| 填表、待提交、待投递·待材料 |
| 已投递 |
| 已停止 |
| 笔试面试各阶段 |

## 7. 批次流程

```
主持人按 battle-map 取 score 前 N 家（N 看并发和截止日）
  → 写 log/B-xx.md，用户圈选批准
  → 派 worker 查岗
      ├ 不合适：看板标 10 + 原因，回执，不打扰用户
      └ 合适：看板标 1，判断投递方式
  → 注册墙：按 permissions.register_accounts 与验证码红线处理
      需要用户时：预填所有可填字段 → notify.py handoff → worker 停手等待
  → 用户处理完 → 主持人放行 → worker 接管继续填表
  → 自检 + 留痕
  → 提交分层（docs/redlines.md §1）
      worker 档：直接提交
      user 档：停在提交前截图 → notify.py handoff → 用户本人提交
  → 门户回读核验 → 看板 2 → tracker 已投递 → 回执
  → 主持人审阅，批次文件更新；当天结束汇总投了哪些公司
```

批次命名 `B-01`、`B-02` 递增。临近截止的公司可以单独开“急救批”。候选池里没有明确截止日（“招满即止”）的公司按 score 排队。

## 8. 表单字段来源

顺序：`profile.md` → `question-bank.yaml`（经 `question-bank.md` 视图或 `host/qb.py` 读） → 简历 PDF → 都没有则 blocked 问主持人。

用户回答后，主持人经 `host/qb.py` 或 web `/bank` 写回问题库，下一单直接复用。新题型第一次出现时，同时补 `aliases` 和 `hint`，方便后续匹配不同问法。

## 9. 投递后

- 门户成功页常提示测评（24–72 小时内完成），worker 必须在回执和 tracker 里写明期限，主持人登记到 `assessments.md` 并发 `assessment` 通知。
- 笔试、面试通知多走邮箱，收件后更新 tracker 和看板。
