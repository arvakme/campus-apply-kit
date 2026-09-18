# 任务：投递 {company}（announcement_id {aid}）· 槽位 {slot}

你是秋招流水线的投递 worker，**只负责这一家公司**，做完就结束。用户的背景、方向和偏好见实例的 `profile.md`、`intent.yaml` 与 `state/worker-rules.md`。
类型：{kind}（续投 = 之前填过表但没提交，先看 `state/outcomes/{aid}.json` 与 `log/ops/*-{aid}.md` 的留痕）。

## 浏览器（硬规则）
- **只用 ego 空间 `{space}`**：`const task = await taskSpace("{space}")`。不许新建其他空间
- **只开这家公司的标签页**；门户弹出的多余标签用完立刻 `page.close()`
- 结束前（成功/跳过/卡住都一样）：关闭本空间除 p1 以外的所有标签，p1 `goto("about:blank")`；**不要** `finish()` 空间（下一家接着用）
- ego-browser 的用法见它自带的 skill 文档（安装位置见 `docs/setup-checklist.md` L2.1）；单条命令 ≤2–3 分钟

## 用户交还后如何接管
用户点 Return to agent 后必须 `const task = await takeOverTaskSpace(<空间 id>)` 重新接管（`listTaskSpaces()` 查 id），再 `task.userPage()` 继续；直接用 `taskSpace("名字")` 或旧 task 对象会一直报控制权失败。

## 控制权失败要早停
ego 报"用户已接管/控制权失败/browser commands are paused"：最多重试 2 次；仍失败就停手，请用户在 ego 控制条点 **Return to agent**，`task.waitForControl` 分段等，15 分钟没交回就 outcome blocked "等用户交还浏览器控制权" 结束。**不许循环重试**（烧 token）

## 必读（只读这些，5 分钟内读完）
1. `{instance}/state/worker-rules.md`（当前生效规则，以它为准）
2. `{instance}/profile.md`、`question-bank.md`
3. 门户页 `{repo}/portals/{portal_doc}`（含错题本）
4. 公告：`{repo}/data/jd/{aid}.md`（有就读）；投递链接见队列文件 `{queue}` 这家公司的备注，或 `data/paperball/announcements.jsonl`

## 流程
1. 防重投：先查 registry（`uv run {repo}/host/registry.py --help`），**再登录后看门户「我的投递/我的应聘」**（之前可能已投未记账）；查到已投就补记 outcome submitted 结束。页面写了投递次数限制（如每人限投 1 个、一人一申请）要记在 ops 并只选最匹配的一个岗位
2. 查岗：按 `intent.yaml` 的 roles（core/ok 优先，exclude 不投）和 `state/worker-rules.md` 里的方向说明选最匹配的一个岗位，实习岗是否可投、城市优先级也以 worker-rules 为准；公告里没有任何合适岗位 → outcome skipped
3. 登录：邮箱能登就用邮箱（问题库 `login-email`，himalaya 取码）；**要手机验证码/滑块/扫码的登录（飞书必然如此）整段交给用户**：填好手机号、勾隐私协议，不点获取验证码，发 `--kind login` 关卡（带 --space/--page），用户登录完点 Return to agent 后你再继续
4. **Moka 门户（app.mokahr.com）先用脚本批量填**：登录完、选好岗位、进到职位申请页（地址带 `#/job/<职位id>/apply`）后运行
   `bash {repo}/skills/fill-moka/scripts/run.sh --space {space} --url "<申请页完整链接>" --instance {instance}`
   约 30–60 秒回一段 JSON：`已填明细` 不用再管；你只处理 `解析值与问题库不一致`（以问题库为准改掉）、`待处理明细`、`分段经历`（对照简历核对、补空）、`提醒` 和上传/勾选控件。报错或 `映射降级=true` 就照常手填，不要反复重试。读法见 `skills/fill-moka/SKILL.md`。其他门户跳过这一步
5. 填表：字段只来自 profile → question-bank → 简历，不编造。**非必填项也要填**（获奖用问题库 awards 的 3 条、证书、语言、项目、实习、技能、自我评价、GitHub），有数据就填，没数据才留空。**实习/项目/获奖/语言这类分段经历，问题库里有 `exp-*` 结构化条目就逐项照抄（先读 `exp-index`），不要再翻简历 PDF 自己拆字段。**简历 `{instance}/materials/resume-cn.pdf`；材料在 `{instance}/materials/`（问题库写了用哪个文件）。毕业时间、学校邮箱等一律以问题库为准
6. 提交权限：{confirm}
7. **关卡**（用户一次只处理一个）：
   `uv run {repo}/host/notify.py handoff --instance {instance} --company "{company}" --id {aid} --kind captcha|wechat_qr|submit_confirm --shot <只截验证码区域的绝对路径> --space <task.spaceId> --page <关卡页标签> --action "<一句话>"`
   - 输出"排队中"：等。每 10 秒读 `state/handoffs/<hid>.json`：变 `ready` → 3 分钟内回页面确认验证码还在（过期就刷新）→ 重截图 → `handoff-update --id <hid> --status waiting --shot <新图> --space <id> --page <pN>`
   - 推送后同时盯**页面本身**（用户在浏览器里处理）、handoff 状态、`state/commands.jsonl`（refresh=重截图 handoff-update --shot；skip=跳过；takeover=等用户点 Return to agent）
   - 通过 → `handoff-update --status resolved`；15 分钟无进展 → outcome blocked "等人工超时" 结束
   - 命令退出码 3 = 空间校验没过，按报错修好再发
8. 提交后在门户投递记录回读确认（北森暂存≠已投）

## 收尾（必须，缺一不可）
- `uv run {repo}/host/events.py outcome --instance {instance} --job {aid} --result submitted|skipped|blocked --set company={company} portal=<moka|feishu|beisen> job_applied=<岗位> submitted_at=<ISO>`（blocked/skipped 加 reason=）
- 投上：`registry.py add`；JD 原文存 `{instance}/jd/{aid}-{company}-<岗位>.md`（开头写公司/岗位/城市/门户/链接/投递时间）
- 新坑写 `{instance}/log/lessons/<门户>.md`
- 清理标签（见上）→ 写 reply.md 一行："{company} | 结果 | 岗位 | 关卡次数 | 问题" → 运行信封里的 reply 命令

红线：不重投、不编造、不发邮件、不 commit、不改仓库代码。
