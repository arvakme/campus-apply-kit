# campus-apply

校招半自动投递框架，自托管在你自己的 Mac 上。它做三件事：

1. **筛**：把校招公告（你自己接的数据源）按你的 `intent.yaml`（方向、城市、排除项）打分排出候选
2. **投**：主持人 agent 拆批派单，worker agent 用浏览器查岗、填表，按你给的权限提交或停在提交前；也能起草并限速发送邮件投递
3. **叫你**：扫码、验证码、登录、名企终审这些必须本人做的事，排好队推到手机（Telegram / Bark），你处理完它接着干

个人信息全部放在仓库外的实例目录（`$CAMPUS_INSTANCE`），`git pull` 不会碰到它，也永远不该进任何 git 仓库。

接手或开发请先读 [AGENTS.md](AGENTS.md)。想先看个大概：[docs/flow.md](docs/flow.md)。

## 先说清楚的几件事

- **不带公告数据，也不带取数脚本。** 作者自己用的公告来自一个无权再分发的来源，所以这里既没有那份数据，也没有针对它的取数脚本。仓库里只有 `data/sample/` 的 12 条虚构示例，够你把"筛选 → 候选清单 → 看板"跑通；真实公告请把你能合法拿到的来源（自己整理的表格、学校就业网导出、你有权限用的接口）按 `adapters/sources/README.md` 的字段契约转成 jsonl，数据只留在你本机
- **不代过验证码。** 图形验证码、滑块、扫码、人脸、短信码永远交给你本人，框架只负责把它们排队推给你。不接任何反检测浏览器
- **不编造。** 表单字段只来自你的 profile → 问题库 → 简历，都没有就停下来问你
- **不代做测评/笔试。**
- **Telegram bot 自己建。** 默认 `mode: direct`：找 @BotFather 建一个 bot，token 存钥匙串（`docs/telegram.md`）。几个朋友想共用一个 bot、顺便比投递数排行榜，才需要自己部署 `relay/`（`docs/relay.md`）
- 文档里的「维护者」= 负责同步数据、部署 relay 的人，单人使用时就是你自己；「同学」= 和你共用同一份仓库的朋友
- 只在 macOS 上跑过。依赖 ego lite（浏览器）、Seedmux（多 agent 终端）、一个不按 token 计费的填表 agent（作者用 Devin CLI），安装来源见 `docs/setup-checklist.md`
- **已知限制**：投递池 `host/slot-pool.py` 用 Seedmux 的 `smx-team spawn --agent devin` 派 worker，较新的 Seedmux 版本去掉了这个参数。遇到时用环境变量 `SMX_BIN` 指向你自己的派单脚本，或改 `slot-pool.py` 的 `spawn()` 适配你的 agent
- `skills/fill-moka`（Moka 申请表批量填写）要一个 Vercel AI Gateway 的 key，存钥匙串 `campus-apply-vercel-gateway`；没有 key 它会自动降级成只列字段，不影响投递

## 第一次使用：复制给你的 agent

装好一个能读写文件、跑命令的 agent（Codex / Claude Code 等都行）并登录，把本仓库 clone 到 `~/campus-apply`，在仓库根目录启动它，把下面整段粘贴进去。

> **分工提醒**：这个 agent 只当「引导助手」带你装环境、生成实例文件。**浏览器填表（worker）请用不按 token 计费的 agent**：用按 token 计费的模型驱动浏览器填表，一家公司要反复读页面截图，额度很快烧完。没有合适的订阅：先用邮件投递 + 自己在浏览器里手动投。尖括号里的内容先改成你自己的。

环境分两步搭：**先 L1**（约 30 分钟，当天拿到榜单手工投），**后 L2**（再上自动投递）。两段提示词分开贴。

### L1 提示词（先跑这个）

````
你是我的秋招投递环境搭建助手。仓库在当前目录（campus-apply），先读 AGENTS.md。
我的个人实例目录是 ~/campus-apply-instance（不存在就创建，必须在仓库外）。
我的方向：<例如：27 届产品经理，偏好城市 上海 > 杭州，也看产品运营>

只做 docs/setup-checklist.md 的 L1 部分，规则：
1. 每一项先跑检测命令，以输出为准；已通过的跳过，不重复安装，不改我已有的配置。
2. 这一阶段先不装：Apple container、tailscale、Bark、himalaya、Seedmux、ego lite、Devin——都属 L2，等我看完榜单再说。
3. 需要我本人操作的，一次只给我一步，等我回复“好了”再检测。
4. 密码、身份证号、API key 只写进实例目录或钥匙串，绝不写进仓库，绝不在对话里回显。
5. 生成实例文件前先问我方向（Agent/后端开发，还是产品经理，或其他），据此从
   templates/intent.dev.yaml 或 templates/intent.pm.yaml 复制并逐项和我确认；
   用 templates/profile.template.md 生成 profile.md。
   模板里的占位值（<填写…>、<改成你自己的密码>）必须让我改成自己的，不许沿用占位值。
6. 公告数据：仓库不带真实数据。先执行 cp -r data/sample data/paperball 用虚构示例把流程跑通；
   真实公告以后按 adapters/sources/README.md 接我自己的数据源（data/paperball/ 已被 .gitignore 忽略，数据只留本机）。
7. L1 完成后给我两样东西：log/battle-map.md 候选清单，以及本机看板页
   （uv run --with pyyaml python3 web/app/server.py 后 open http://127.0.0.1:8787/board），
   确认榜单打开、公司方向对得上。
8. 全部结束后运行 python3 scripts/check_secrets.py --instance ~/campus-apply-instance，确认 0 命中。

9. L1 最后装上自动更新（docs/setup-checklist.md L1.14：install-schedule.py install --role member），跑 status 确认已注册；
   上游的修复靠它每小时 git pull 送到我机器上。我的仓库只 pull 不 commit，要改东西先 fork。

L1 阶段不做任何投递、不建问题库；等我说“上 L2”再继续。
````

### L2 提示词（榜单看过了，准备自动投递时再跑）

````
继续我的 campus-apply 环境搭建。L1 已完成（intent/profile 已填、榜单能看）。
现在只做 docs/setup-checklist.md 的 L2 部分，规则：
1. 每一项先跑检测命令，以输出为准；已通过的跳过，不重复安装，不改我已有的配置。
2. Seedmux、ego lite、Devin CLI 等工具的下载地址见 docs/setup-checklist.md「L2.1 工具 · 安装来源」，
   你直接下载安装，不要让我去找维护者要安装包。
3. 需要我本人操作的，一次只给我一步，等我回复“好了”再检测。这些包括：
   - Devin 订阅与登录（docs/agents-and-models.md §2：devin auth login，确认 devin models list 里有 swe-2-max，
     再用 devin --model swe-2-max --permission-mode dangerous 做一次 -p 调用，确认不弹权限确认）
   - Gmail 两步验证与应用专用密码，存进钥匙串（templates/himalaya/gmail.toml 顶部注释）
   - 通用平台账号预注册：智联、北森、Moka、飞书招聘、牛客、hotjob，在 ego lite 浏览器里登录并保持登录态
   - iPhone 安装 Bark App 并复制 device key；Bark 用公共服务器 https://api.day.app，不需要自建
   - Tailscale：Mac 登录；手机用 Tailscale App 或 sing-box 的 tailscale endpoint 接入同一 tailnet
     （提醒我 iOS 同一时间只能开一个 VPN，已经在用 sing-box 的走 sing-box 方式）
   - Telegram：@BotFather 建一个自己的 bot，token 存钥匙串（docs/telegram.md，mode: direct）
4. 密码、身份证号、API key 只写进实例目录或钥匙串，绝不写进仓库，绝不在对话里回显。
5. 用 templates/ 生成 question-bank.yaml、accounts.md、notify.yaml。
6. 【硬门槛】问题库必须全部填完才能开始投递，包括个人信息类条目。按这个顺序做：
   a. 先让我把简历（PDF 或文字）发给你，从简历里抽出能填的条目写进 question-bank.yaml 和 profile.md，列表给我核对；
   b. 简历里没有的条目，一次只问我一个，直到 question-bank.yaml 里没有空答案；
   c. 模板里的占位值（例如注册密码 "<改成你自己的密码>"）必须让我改成自己的，不许沿用占位值；
   d. 获奖证书、推荐信这类不常用文件不强制，问一次我说没有就跳过；
   e. 问题库之外，按 docs/materials.md §1.1 必备表把 materials/ 备齐（证件照及压缩档、生活照、身份证正反面、
      学生身份证明、各学历成绩单、外语成绩证明；海外学历另加留服认证书或"待认证"口径）——国企、银行门户缺件会直接拦提交；
   f. 毕业证编号、学位证编号去学信网查好填进问题库，别等门户问到再找。
   没填完之前不许派单、不许启动投递，/setup 页的"问题库"项必须是绿的。
7. 必须明确告诉我投递权限的默认规则：intent.yaml 里 permissions.submit.practice: auto，
   即"练手档"（不想去的城市、边缘岗位，当练手投）不经过我审核，worker 自检后直接提交；
   心仪公司和 user_confirm_tiers 里的公司会停在提交前等我点。问我要不要改。
8. 材料按 docs/materials.md 检查：resume-cn.pdf、id-photo.jpg 必需，帮我用 sips 生成 ≤500KB 的证件照副本；
   可选材料列出缺哪些，问我要不要准备。
9. 最后运行 host/host-check.py（若已存在）、启动 web 容器并用 tailscale serve 暴露，
   发一条测试通知到我手机，让我点开确认 /setup 页全绿；把每一节的检测结果列成表给我看。
10. 全部结束后运行 python3 scripts/check_secrets.py --instance ~/campus-apply-instance，确认 0 命中。

如果我是中途让你（或别的 agent）接手，也先读 AGENTS.md，并同样遵守第 5、6 条：问题库没填完不投递，练手档默认免审直接提交要先告诉我。

之后我说“同步更新”时，按 docs/sync.md §3 执行：
确认工作区干净 → 记下当前 HEAD → git pull --ff-only → 读 CHANGELOG 新增部分并列出我要做的事 →
对比 templates/ 变化，检查 intent 和问题库有没有新增必填项并问我 → 需要时重建 web 容器 → 自检 → 汇报。
````

## 手机上怎么看、怎么被叫住（推荐组合）

三件事配齐以后，投递基本不用守着电脑：

| 件 | 作用 | 装在哪一档 |
|---|---|---|
| **tailscale serve** | 看板挂到自己的 tailnet：`tailscale serve --bg 8787`，拿到 `https://<机器名>.<tailnet>.ts.net`。**只有自己的设备能开**，自带 HTTPS，不暴露公网、不用备案、不用 Cloudflare | L2.5 |
| **Bark** | 手机推送。走 APNs，**不依赖 VPN 在线**，负责把你叫醒（测评截止、扫码、批次完成） | L2.5 |
| **Telegram bot** | 双向：扫码截图直接发到聊天里，点按钮或回一句话 worker 就继续；还能 `/today` 圈选、`/mode` 切档位、`/pause`、`/takeover`。见 `docs/telegram.md` | L2.5 |

iOS 同一时间只能开一个 VPN：已经在用 sing-box 的，在 sing-box 里配 tailscale endpoint，别再开 Tailscale App。
Telegram 在国内要代理才连得上，所以 **Bark 负责叫醒、Telegram 负责交互**，两个一起配最稳。

## 日常三条命令

```bash
export CAMPUS_REPO=~/campus-apply CAMPUS_INSTANCE=~/campus-apply-instance

git -C $CAMPUS_REPO pull --ff-only && head -30 $CAMPUS_REPO/CHANGELOG.md   # 1. 拿框架更新（公告由每日同步任务拉到本机）
uv run $CAMPUS_REPO/host/scan.py                                         # 2. 按 intent 刷新候选（参数见 --help）
open "$(awk '/^web_base:/{print $2}' $CAMPUS_INSTANCE/notify.yaml)"      # 3. 打开看板，处理候选和 handoff
```

只做完 L1 还没有 notify.yaml / tailscale：第 3 条换成本机看板
`uv run --with pyyaml python3 $CAMPUS_REPO/web/app/server.py` 然后 `open http://127.0.0.1:8787/board`（见 `web/README.md` 的「本机直接跑」）。

## 目录

| 路径 | 内容 |
|---|---|
| `AGENTS.md` | agent 入口 |
| `docs/` | 契约、规范、SOP、红线、接手、模型、材料、前置条件、同步 |
| `portals/` | 各招聘门户的操作经验 |
| `templates/` | 实例文件模板 |
| `host/` | 宿主机脚本（`uv run` 运行） |
| `web/` | 看板 web（Apple container） |
| `data/` | `sample/` 虚构示例；`tiers.yaml` 公司层级、`employer-aliases.yaml` 雇主别名；真实公告同步到本机 `data/paperball/`（不进 git） |
| `skills/` | 批量填表等 skill（`fill-moka`） |
| `relay/` | 可选：几个人共用一个 Telegram bot 的 Cloudflare Worker 中转 |
| `scripts/` | 脱敏检查等 |

## 许可

MIT。招聘门户各有各的使用条款，自动化到什么程度、投多少家，请自己负责。
