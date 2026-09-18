# AGENTS.md · campus-apply 接手入口

> 任何 agent（Claude / Codex / Kimi / Devin / 其他）进入本仓库，先读完本文件再动手。
> 本文件是唯一入口，`CLAUDE.md` 只引用它。

## 1. 这是什么

校招半自动投递框架，每人在自己的 Mac 上自托管一套（可以一个人用，也可以几个朋友 fork 后共用一份公告数据）：

- 公告数据放在本机 `data/paperball/`（公开仓库不带数据也不带取数脚本，只有 `data/sample/` 示例；接自己的数据源见 `adapters/sources/README.md`）
- 按个人意向 `intent.yaml` 筛选打分，出候选队列
- 主持人拆批派单，worker 用浏览器查岗、填表、按权限提交或停在提交前
- 需要本人的环节（扫码、图形验证码、终审）经 Bark 推到手机，点开 tailnet 上的 web 页处理

## 2. 三层结构

| 层 | 位置 | 内容 | 谁改 |
|---|---|---|---|
| 仓库 | 本目录（`$CAMPUS_REPO`） | 文档、门户 playbook、模板、宿主机脚本、web（不含公告数据） | 上游维护；使用者只 `git pull`，要改先 fork |
| 实例 | `$CAMPUS_INSTANCE`（仓库外，建议 `~/campus-apply-instance`） | profile、intent、问题库、账号、材料、台账、日志、运行态 | 每人自己，永不进 git |
| 宿主机工具 | 本机 App 与 CLI | Seedmux.app、ego lite.app、himalaya、Apple `container`、uv、tailscale、`event`、gh、devin/codex | 每人自己装，见 `docs/setup-checklist.md` |

数据契约（实例目录结构、intent/问题库 schema、公告字段、通知与 handoff 格式）全部在 `docs/contracts.md`，改字段必须同步所有读写方。

## 3. 开工前必读顺序

1. 本文件
2. `docs/contracts.md`：文件和字段长什么样
3. `docs/redlines.md`：不能做什么，提交权限怎么分层
4. `docs/spec.md`：角色、状态机、批次流程
5. `docs/sop.md`：worker 单家公司的操作步骤（worker 必读）
6. 实例里的 `profile.md`、`intent.yaml`、`question-bank.md`（由 yaml 生成）、`accounts.md`
7. 要进门户时读 `portals/README.md` 和对应门户的 playbook
8. 中途接手读 `docs/handoff.md`

只读 1–4 不够开工；worker 不读 5–7 不许进门户。

## 4. 状态真源

| 问题 | 看哪里 |
|---|---|
| 某家公司投没投、到哪一步 | 实例 `tracker.md`（单一台账），再用门户“我的投递”核实 |
| 当前批次派了谁、谁卡住 | 实例 `log/B-xx.md`（编号最大的就是最新批） |
| 某家公司具体操作到哪一步、停在哪个页面 | 实例 `log/ops/B-xx-<announcement_id>.md` |
| 查岗结论、投递执行记录 | `log/B-xx-<id>.md`、`log/B-xx-<id>-apply.md` |
| worker 回执 | `~/.seedmux/team/tasks/T-*/reply.md` 与 `meta.json` 的 `status` |
| 等用户处理的事 | 实例 `state/handoffs/*.json` 中 `resolved_at` 为 null 的记录；web `/handoffs` |
| 候选队列 | 实例 `log/battle-map.md`（`host/scan.py` 生成） |
| 测评/笔试排期 | 实例 `assessments.md` |
| 看板进度 | 实例 `tracker.md` 与 `state/outcomes/`；你另有自己的看板时自行双写 |

冲突时的优先级：门户回读 > tracker > 看板 > 批次文件 > 回执。门户回读是唯一能证明“已投递”的证据。

## 5. 角色

| 角色 | 做什么 | 不做什么 |
|---|---|---|
| 用户 | 审批批次名单；扫码、图形验证码、短信码、人脸；按 intent 权限终审提交；答问题库缺口 | — |
| 主持人 | 读候选、拆批、派单（注入权限段落）、回收审阅、维护 tracker 和问题库、汇报、nudge 长任务 | 不亲自进门户填表 |
| worker | 一人一公司、一个浏览器 TaskSpace：查岗 → 填表 → 按权限提交或停手 → 核验 → 回写 → 留痕 → 回执 | 不跨公司，不改规则文件，不碰 git |
| 引导助手（Codex） | 按 README 提示词带用户装环境、生成实例文件、执行“同步更新” | 不参与投递 |

角色与 CLI、模型的对应见 `docs/agents-and-models.md`。

## 6. 红线摘要（全文见 `docs/redlines.md`）

1. 提交权限只看 `intent.yaml` 的 `permissions.submit`，拿不准算 user，停在提交前。
2. 图形验证码、扫码、人脸、短信码永远交给用户，不尝试自动化。
3. 注册账号按 `permissions.register_accounts` 执行，默认 user。
4. 邮件按 `permissions.email` 审稿，受每日上限约束；worker 永不发邮件。
5. 不重投：先查 tracker 和门户申请记录，查到雇主主体层。已成功但回写失败的，blocked，禁止重试提交。
6. 不编造：字段只来自 profile → question-bank → 简历 PDF，都没有就 blocked 问主持人。
7. 需要用户动手时必须调用 `host/notify.py handoff`，不直接拼 Bark URL。
8. 个人信息只写实例目录，不写仓库，不回显密码和证件号。提交前跑 `python3 scripts/check_secrets.py --instance $CAMPUS_INSTANCE`，0 命中才能 commit。
9. **投递前硬门槛**：`question-bank.yaml` 全部条目有答案（学位证、成绩单等不常用文件除外）、没有沿用模板占位值，才能派单。补全顺序：先要用户简历抽取 → 简历没有的逐个问用户。
10. **练手档默认免审**：`permissions.submit.practice: auto` 时，练手档公司 worker 自检后直接提交。接手的 agent 必须先把这条告诉用户，用户要改再改。

### 浏览器控制权交接（会碰 ego 浏览器的 agent 必读）

ego 的空间同一时间只归一方控制。用户一旦接管（或你 `handOff()` 交出），你的浏览器命令会被**硬停**，并且 ego 不允许你自己把控制权抢回来。所以：

1. 需要用户动手（扫码、验证码、登录、终审）：`await task.handOff()`，然后**不要结束这一轮**，用 `await task.waitForControl({ timeout: 120000 })` 分段循环等
2. 用户点 **Return to agent** 后：`const task = await takeOverTaskSpace(<空间 id>)` 重新接管，再 `task.userPage()` 拿回用户那一页继续。**不要**再用 `taskSpace("名字")` 或旧的 task 对象——会一直报"控制权失败"
3. 用户在你没交出时就抢了控制权：你的命令会报 "The user has taken control"。最多重试 2 次，然后按第 1 条原地等；15 分钟没交回就写 outcome blocked 结束。**不许循环重试**（每次都在烧 token）
4. 主持人派 worker 时，把这一节原样写进 worker 的 prompt——worker 不一定会读 templates/

## 7. 常用命令

```bash
export CAMPUS_REPO=~/campus-apply CAMPUS_INSTANCE=~/campus-apply-instance

# 同步框架与公告数据
git -C $CAMPUS_REPO pull && head -40 $CAMPUS_REPO/CHANGELOG.md

# 按 intent 生成候选（写 $CAMPUS_INSTANCE/log/battle-map.md；参数见 --help）
uv run $CAMPUS_REPO/host/scan.py --help

# 问题库读写（带锁和版本校验，写后重新生成 question-bank.md）
uv run $CAMPUS_REPO/host/qb.py --help

# 需要用户处理时发通知
uv run $CAMPUS_REPO/host/notify.py handoff --company <公司> --id <announcement_id> \
  --kind wechat_qr --shot log/shots/<id>-qr.png --action "扫码后在 seedmux 回复 好了"

# worker 派单、回执、看现场
~/.seedmux/bin/smx-team panes
~/.seedmux/bin/smx-team show T-xxxxxx

# 邮箱验证码
himalaya envelope list -s 15
himalaya message read <ID>

# 脱敏检查（commit 前必须 0 命中）
python3 scripts/check_secrets.py --instance $CAMPUS_INSTANCE
```

`host/host-check.py` 在路线图中，尚未落地；以 `CHANGELOG.md` 为准。

## 8. 接手第一件事

按顺序做，做完再决定下一步：

1. 确认环境变量：`echo $CAMPUS_REPO $CAMPUS_INSTANCE`，两个目录都存在；不确定时问用户，不要猜。
2. 读 §3 的 1–4。
3. 打开实例 `tracker.md`，数一下各状态家数，找出“填表中”“待提交”“待投递·待材料”的行。
4. 打开编号最大的 `log/B-xx.md`，列出状态不是“已投递/不合适/已停止”的行。
5. 对第 4 步的每一行，查 `~/.seedmux/team/tasks/<task>/meta.json` 与 `reply.md`，再读对应 `log/ops/` 留痕的最后一节。
6. 查 `state/handoffs/` 里未处理的记录。
7. 按 `docs/handoff.md` §3 把每单归为：在跑 / 等用户 / 卡住待决 / 需要重派 / 已闭环未回写。
8. 把这张表发给用户确认后再派任何单。确认前不点任何提交，不开新浏览器空间。

## 9. 错题本 → playbook（主持人职责）

- worker 每单把新踩的坑写进实例 `log/lessons/<门户>.md` 与 `log/lessons/question-bank.md`
- 流程/工具/看板/协作层面的问题整合进 `docs/lessons.md`（现象 → 根因 → 改在哪 → 状态）
- 主持人每批结束（或每天）整理一次：门户类的并进 `portals/<门户>.md` 正文与其「错题本」表并标 ✅；问题库类的用 `host/qb.py add` 补条目或规则，并把可泛化的并进 `templates/question-bank.example.yaml`；流程类的改 `templates/worker-task.md`
- 整理完提交仓库，`CHANGELOG.md` 记一条，同学 pull 后就拿到

## 10. 本仓库的写入规则

- 仓库里只放框架：不写任何人的姓名、证件号、手机号、住址、学号、账号密码、简历路径。
- 门户经验写进 `portals/`，写法见 `portals/README.md`；个人答案写进各自实例的问题库。
- 改了流程或模板，同时在 `CHANGELOG.md` 记一条，写清同学要做什么。
