# 红线与投递权限

> 本文约束所有 agent。权限的具体取值在实例 `intent.yaml` 的 `permissions` 段，本文规定怎么解释和执行。
> 主持人派单时必须把 §8 的权限段落注入工单；worker 以工单里的权限段落为准，工单没写的按最严格处理。

## 1. 提交权限

```yaml
permissions:
  submit:
    default: worker              # worker | user
    user_confirm_tiers: [大厂]
    user_confirm_companies: [腾讯, 字节跳动]
```

主持人为每家公司算出档位，写进工单：

1. 公司名（或其雇主主体、所属集团本部）在 `user_confirm_companies` 中 → **user 档**
2. 公司层级（`data/tiers.yaml` 或 battle-map 的类别列）在 `user_confirm_tiers` 中 → **user 档**
3. 否则取 `default`

| 档位 | worker 行为 |
|---|---|
| worker | 填完 → 自检 → 直接点最终提交 → 门户回读核验 → 看板已投递 → 回执报备 |
| user | 填到最终提交按钮前 → 截图 → `notify.py handoff --kind submit_confirm` → 交还浏览器 → 停手；用户提交后做阶段 3 核验 |

规则：

- 子公司是否算名单内：名单写的是集团本部就只管本部；想覆盖子公司，用户要把子公司名也写进去。拿不准 → 问主持人，主持人拿不准 → 问用户，期间按 user 档。
- 用户在对话里临时说“这几家直接提交”，主持人要把公司名写进批次文件的说明，作为本批的档位依据。
- 自检清单：必填项无红标；附件刷新回读正确；志愿顺序和意向城市符合 intent；声明类题目读过题干。
- **注意即时提交按钮**：有的门户“投递简历”“一键投递”点下去就是最终提交，没有确认弹窗。user 档的单，任何可能是提交的按钮都不点，先读 `portals/` 或问主持人。

## 2. 永远交给用户的环节

以下不受 `intent` 控制，任何配置都不能改：

| 环节 | 说明 |
|---|---|
| 图形验证码 | 滑块拼图、点选、九宫格、字符图、GeeTest 等。先交验证码特种兵（`docs/auth-channels.md` §1 图形码支线），连败 3 次再 handoff；不绕过 |
| 扫码 | 微信、抖音、App 扫码登录或绑定 |
| 人脸、实名核验 | — |
| 电子签、付费 | — |

识别技巧：点“获取验证码”后看起来没反应，通常是验证码遮罩盖住了按钮。先截图确认，再 handoff。

**验证码自取通道（项目约定，优先级见 `docs/auth-channels.md`）：**

- 邮箱验证码：worker 用 himalaya 只读取码，前提是发码前没有图形验证码（有则先走图形码支线）。
- 短信验证码：iPhone 短信转发到 Mac 后，worker 用 `host/sms-code.py` 从本机 chat.db 只读取码，**不算 handoff**；自取失败（超时、chat.db 不可读、转发未开）再 handoff（kind=sms_code），由用户手输或在关卡会话里回码。
- 短信码自取可用于登录已有账号；**新注册账号仍按 §3 `register_accounts` 执行**（`worker_email_only` 只允许邮箱注册）。

## 3. 注册账号

```yaml
permissions:
  register_accounts: user        # user | worker_email_only
```

- `user`：worker 打开注册页，预填所有可填字段，handoff，用户完成注册。
- `worker_email_only`：worker 只能用邮箱 + 邮件验证码完成注册，密码按问题库 `account` 类答案。需要手机号、扫码或图形码的注册，仍然交用户。
- 登录通道优先级（项目约定）：平台预登录 → 邮箱码自取 → 手机短信码自取 → 扫码 handoff。判定流程与各平台默认通道见 `docs/auth-channels.md`。
- 通用平台建议用户提前注册，预注册清单见 `docs/platform-accounts.md`，检查项见 `docs/setup-checklist.md` §3。
- 新账号写进实例 `accounts.md`，仓库里不写账号。

## 4. 邮件

```yaml
permissions:
  email:
    review: all                  # all | sample | none
    daily_limit: 90
```

- worker 永不发邮件，只收集要素入队。
- 起草者按 `templates/mail/cover-letter.skeleton.md` 写，主题和附件名严格照公告要求。
- `review: all`：每封经用户在 web 审稿页批准后才发。`sample`：主持人抽查，首批仍全审。`none`：只在用户明确授权后使用。
- 当天发送数达到 `daily_limit` 立即停，剩余顺延。发信通道有自己的额度（例如某些 SMTP 服务免费额度每天 100 封），`daily_limit` 应低于通道额度。
- 同一收件邮箱同一岗位只发一次，发出后记入 tracker。

## 5. 不重投

- 查岗第一步查 tracker、看板申请记录、门户申请记录，**查到雇主主体层**：集团公告和子公司公告、集团门户的子公司岗和子公司自有门户岗，视为同一雇主。
- 同雇主主体 + 同岗位（或同集团同岗）即重复 → 换备选岗或标不合适，报主持人。
- 申请已成功但回写看板失败 → 不得重投，blocked 说明。
- 超时、异常先查记录，禁止盲重试提交。
- 判断方法见 `docs/handoff.md` §4。

## 6. 不编造

- 字段只来自 `profile.md`、问题库、简历 PDF。
- 数字类（GPA、排名、分数）以简历和问题库为准，没有就留空或 blocked。
- 必填但无数据源 → blocked，列出字段名交主持人；不用“无”“0”冒充（填 0 分等于声称没考）。
- 用户主动选择的占位值（例如不愿提供家人真实电话）必须由用户写进问题库，worker 只照抄，不自行生成。
- 表单要求上传的材料没有 → 按 `intent.policies.attachments_required`，不找替代文件。

## 7. handoff 必须调用 notify.py

需要用户动手的任何时刻：

```bash
uv run $CAMPUS_REPO/host/notify.py handoff \
  --company <公司> --id <announcement_id> --kind <类型> \
  --shot log/shots/<id>-<说明>.png \
  --action "<用户要做的一句话>"
```

- `kind` 取值：`wechat_qr`（扫码）、`captcha`（图形验证码）、`sms_code`（短信码）、`face`（人脸）、`register`（注册）、`submit_confirm`（终审提交）、`attachments`（缺材料）。新增类型先和 web 同步。
- 先截图再调用，截图里要能看到二维码或验证码区域。
- 不直接拼 Bark URL；notify.py 负责写 `state/handoffs/<id>.json`、去重和重试。
- 调用后 worker 停手，给主持人发 team-msg，不回执 done。
- 用户处理完，在 web `/handoff/<id>` 点“已处理”或在 seedmux 回复，主持人再放行。

## 8. 工单权限段落模板

主持人派单时，把下面这段按实例 `intent.yaml` 填好，原样放在工单“任务”段之后。花括号是占位符。

```markdown
## 权限（主持人按 intent.yaml 生成，本单有效）

- 提交档位：**{worker|user}**（依据：{default=… / 公司在 user_confirm_companies / 层级 {层级} 在 user_confirm_tiers / 用户 {日期} 口头指定}）
  - worker 档：填完自检后直接点最终提交 → 门户回读核验 → 看板标已投递(2) → 留痕 → 回执
  - user 档：填到最终提交按钮前，不点；截图 → `uv run $CAMPUS_REPO/host/notify.py handoff --kind submit_confirm ...` → 交还浏览器 → 停手等主持人通知
  - 分不清某个按钮是不是最终提交时，按 user 档处理
- 注册账号：**{user|worker_email_only}**
  - user：预填全部可填字段后 handoff（kind=register），不点注册
  - worker_email_only：只可用邮箱 + 邮件验证码注册；手机号、扫码、图形码仍 handoff
- 图形验证码（先验证码特种兵，连败 3 次）、扫码、人脸、电子签：一律 handoff（不可配置）；短信码先 `host/sms-code.py` 自取，取不到再 handoff
- 邮件：不发送任何邮件；邮件类公司只收集要素写入 log/email-queue.md
- 附件政策：强制成绩单/在读证明/学生证等材料时 **{hold：看板标待投递，备注“待材料：需XX，门户截止YYYY-MM-DD”，报主持人 | skip：标不合适并写原因}**；外企一律 blocked
- 外企简历：**{en|cn}**
- 不重投：先查 tracker、你自己的看板（若有）、门户申请记录，查到雇主主体层；已有记录 → blocked
- 不编造：字段只来自 profile / question-bank / 简历 PDF；无数据源 → blocked 并列出字段名
- 需要用户动手时必须调用 host/notify.py handoff，然后停手
- 只在 $CAMPUS_INSTANCE/log/ 和本工单目录写文件；不碰 git；不改规则文件
```

## 9. 其他

- 个人信息只存实例目录。仓库、工单、回执、team-msg 里不写密码和证件号明文；留痕里账号写“邮箱登录”“手机码登录”即可。
- 不碰 git 破坏性操作。worker 只在实例 `log/` 和自己的工单目录写文件。
- 不在网页上展示密钥明文，不在网页上提交投递。
