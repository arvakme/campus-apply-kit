# 前置条件清单（分两档）

> 引导助手按本文逐项检查。每项先跑“检测命令”，输出满足“通过标准”就跳过，不重复安装。
> 需要用户本人操作的项，一次只给一步，等用户回复“好了”再检测。
> 约定：`export CAMPUS_REPO=~/campus-apply CAMPUS_INSTANCE=~/campus-apply-instance`（路径按实际调整）。

本清单分两档，按顺序做，别跳：

| 档 | 目标 | 要装什么 | 做完得到什么 |
|---|---|---|---|
| **L1 看得见** | 当天出榜单 | git/gh、uv、codex、python3 | 本机榜单页：“今天该投哪些”一目了然，照单**手工**投递 |
| **L2 自动投递** | worker 自动投 | L1 全部 + Devin、Seedmux、ego lite、himalaya、Bark、Apple container、tailscale | agent 查岗填表、按 intent 权限提交或停在提交前、扫码/验证码经 Bark 推到手机处理 |

L2 任一项卡住都不影响 L1 的榜单和手工投递；两档的检测项不重复，L1 装过的东西 L2 直接复用。

## L1 · 看得见（目标约 30 分钟）

- **做完你能得到什么**：`log/battle-map.md` 候选队列，加上浏览器里的 `/board` 每日榜单——按公司合并、分档（冲/常规/练手）、截止倒计时、投递入口。当天就能照单手工投递。
- **大约多久**：顺利约 30 分钟。其中 `judge.py --budget 10` 试判要 10 分钟上下，可以挂着先回头填 profile。
- **卡住了怎么退回**：L1 只新增几个普通命令行工具，没有系统级改动；删除实例目录和仓库 clone 即完全还原。任何时候都可以直接续做 L2。

### L1.1 工具

| # | 项 | 检测命令 | 通过标准 | 备注 |
|---|---|---|---|---|
| L1.1 | gh | `gh auth status` | 显示已登录 | 只用 `git clone` 的话可以不装 |
| L1.2 | 仓库 | `git -C $CAMPUS_REPO remote -v` | 能 `git -C $CAMPUS_REPO fetch` | `git clone <本仓库地址> ~/campus-apply` |
| L1.3 | uv | `uv --version` | 有版本号 | 宿主机脚本都用 `uv run`，依赖自动装 |
| L1.4 | codex | `codex --version` | 有版本号并已登录 | 引导助手 + judge.py 默认引擎 |
| L1.5 | Python | `python3 --version` | ≥ 3.10 | `check_secrets.py` 用 |

### L1.2 实例文件（最小集）

实例目录在仓库外（建议 `~/campus-apply-instance`），只放个人信息，永不进 git。

| # | 项 | 检测命令 | 通过标准 |
|---|---|---|---|
| L1.6 | 实例目录在仓库外 | `case "$CAMPUS_INSTANCE" in "$CAMPUS_REPO"*) echo BAD;; *) echo ok;; esac` | `ok` |
| L1.7 | 目录骨架 | `ls $CAMPUS_INSTANCE` | 有 `profile.md intent.yaml tracker.md materials log state`；`question-bank.yaml`、`accounts.md`、`notify.yaml` 到 L2 再建 |
| L1.8 | profile | `grep -c '<填写' $CAMPUS_INSTANCE/profile.md` | 0（从 `templates/profile.template.md` 复制；没有的项写“无”或删行） |
| L1.9 | intent | `uv run --with pyyaml python3 -c "import yaml,os;d=yaml.safe_load(open(os.path.expandvars('\$CAMPUS_INSTANCE/intent.yaml')));print(d['version'],d['roles']['core'],d['permissions']['submit']['default'])"` | 能解析，打印出方向和提交档位 |

生成实例文件时，先问用户方向（例如“27 届 Agent/后端”或“27 届产品经理”），再从 `templates/intent.dev.yaml` 或 `templates/intent.pm.yaml` 复制修改。`cities.prefer` 必须填真实城市，占位符不许留。

### L1.3 出榜单

| # | 项 | 命令 | 通过标准 |
|---|---|---|---|
| L1.10 | 拿公告数据 | `test -f $CAMPUS_REPO/data/paperball/announcements.jsonl \|\| cp -r $CAMPUS_REPO/data/sample $CAMPUS_REPO/data/paperball; cat $CAMPUS_REPO/data/paperball/meta.json` | 有 `count`；公开版先用 `data/sample/` 的虚构示例跑通，真实公告按 `adapters/sources/README.md` 接你自己的数据源（数据只留在本机，`data/paperball/` 已被 .gitignore 忽略） |
| L1.11 | 候选扫描 | `uv run $CAMPUS_REPO/host/scan.py` | `log/battle-map.md` 生成，前 20 行方向符合 intent |
| L1.12 | LLM 试判 | `uv run $CAMPUS_REPO/host/judge.py --budget 10` | `state/fit/` 下生成一批 json；完成后重跑一次 scan.py，battle-map 备注列出现 `[冲]/[常规]/[练手]` 档标 |
| L1.14 | **自动更新（热更新）** | `uv run $CAMPUS_REPO/host/install-schedule.py install --role member`，再跑 `uv run $CAMPUS_REPO/host/install-schedule.py status --role member` | `launchd:` 段有 `state =`；之后每小时自动拉取仓库，bot/看板有改动会自动重启重建，更新说明推到 Telegram（配对后）。**这一步不能省，维护者修的 bug 靠它送到你机器上** |
| L1.13 | 本机看板 | `uv run --with pyyaml python3 $CAMPUS_REPO/web/app/server.py`，另开终端 `open http://127.0.0.1:8787/board` | 榜单页能打开：按公司合并、有分档和“我的状态”列、入口可点 |

看板只绑 `127.0.0.1`，关掉终端即停；以后想再看就跑同一条命令。端口占用、快照等细节见 `web/README.md` 的「本机直接跑」一节。

### L1 明确不装的东西

macOS 26 / Apple 芯片、Apple `container`、tailscale、Bark、himalaya、Seedmux、ego lite、Devin、问题库、材料、accounts.md——全属于 L2。L1 不检查也不要求它们；已经装了也无妨。

## L2 · 自动投递（L1 完成后再做）

- **做完你能得到什么**：完整流水线——定时同步公告、主持人拆批派单、Devin worker 用浏览器查岗填表、按 `intent.permissions.submit` 提交或停在提交前、扫码/验证码/终审经 Bark 推到手机点开处理。
- **大约多久**：一两个晚上。最费时的是平台预注册（L2.3）和问题库（L2.4），都只需人工做一次。
- **卡住了怎么退回**：L2 每一项独立，卡住一项只影响对应能力（如没装 Bark 就收不到手机推送），L1 榜单和手工投递始终可用。修好再继续。

### L2.0 系统

| # | 项 | 检测命令 | 通过标准 |
|---|---|---|---|
| L2.0.1 | macOS 版本 | `sw_vers -productVersion` | ≥ 26（Apple `container` 需要） |
| L2.0.2 | Apple 芯片 | `uname -m` | `arm64` |

### L2.1 工具

> 实例目录如果用 git 做本地备份，先执行 `git -C $CAMPUS_INSTANCE config core.quotepath false`：否则中文文件名（如 jd 存档）会被转义，seedmux 验收会把 worker 的正常写入误判为越界。


**安装来源**（都不需要找维护者要安装包，agent 可以自己装；需要本人点的只有 App 首次启动授权与登录）：

| 工具 | 怎么装 | 装完要做 |
|---|---|---|
| Seedmux.app | GitHub Releases 下载 dmg：`https://github.com/wishworldbetter/seedmux/releases/latest`（Apple 芯片用 `Seedmux-latest.dmg`，Intel 用 `Seedmux-latest-x86_64.dmg`）；命令行：`gh release download -R wishworldbetter/seedmux -p 'Seedmux-latest.dmg' -D ~/Downloads` | 拖进 /Applications 并打开一次；App 会装好 `~/.seedmux/bin/smx-team`；系统设置里允许辅助功能/自动化授权 |
| ego lite.app | 官网 `https://lite.ego.app/`；或直链 dmg：Apple 芯片 `https://cdn.ego.app/setup/macos/arm64/egolite.dmg`，Intel `https://cdn.ego.app/setup/macos/x64/egolite.dmg` | 打开 App 完成首次引导（本人操作）：引导会注册 `ego-browser` 命令到 `~/.local/bin`，并安装 agent 用的 ego-browser skill（`~/.local/share/ego/ego-skills`）；`~/.local/bin` 不在 PATH 就补上 |
| Devin CLI | `brew install --cask devin-cli`（文档 `https://cli.devin.ai/docs`） | `devin auth login`（本人登录，需要 Devin 订阅）；然后按下表 L2.1.9–11 检测 |
| Apple container | `https://github.com/apple/container/releases` 下载 pkg 安装 | `container system start` |
| tailscale | `brew install --cask tailscale` 或 App Store | 本人登录 |
| himalaya | `brew install himalaya` | 配置见 `templates/himalaya/` |


| # | 项 | 检测命令 | 通过标准 | 备注 |
|---|---|---|---|---|
| L2.1.1 | Seedmux.app | `ls -d /Applications/Seedmux.app && ~/.seedmux/bin/smx-team panes` | App 存在；`panes` 返回列表不报连接错误 | 需要先打开 App |
| L2.1.2 | ego lite.app | `ls -d "/Applications/ego lite.app" && command -v ego-browser` | App 与 `ego-browser` CLI 都在 | 首次运行 `ego-browser onboarding` |
| L2.1.4 | himalaya | `himalaya --version` | 有版本号，features 含 `+smtp +imap` | 配置见 L2.2 |
| L2.1.5 | Apple container | `container --version && container system status` | 有版本号；status 为 `running` | 未运行时 `container system start` |
| L2.1.6 | tailscale（Mac） | `tailscale version && tailscale status` | 有版本号；status 列出本机且在线 | 装 Tailscale.app 并登录 |
| L2.1.7 | tailscale（手机） | `tailscale status` | 列表里出现手机设备且在线 | 见 L2.5 |
| L2.1.8 | event | `event --version && event reminders lists --help >/dev/null` | 有版本号 | 首次访问会弹日历/提醒事项授权，用户点允许 |
| L2.1.9 | devin | `devin auth status && devin models list \| grep swe-2-max` | `Logged in`；列表含 `swe-2-max` | 订阅与验证见 `docs/agents-and-models.md` §2 |
| L2.1.10 | devin 免确认 | `docs/agents-and-models.md` §2.3 的 `-p` 调用 | 不弹权限确认，输出正确 | — |
| L2.1.11 | devin 白名单 | `test -f <worker 启动目录>/.devin/config.local.json` | 文件存在，JSON 合法 | 模板 `templates/devin/config.local.json` |

### L2.2 邮箱

| # | 项 | 检测命令 | 通过标准 |
|---|---|---|---|
| L2.2.1 | 配置文件 | `himalaya account list` | 列出投递邮箱账户，DEFAULT 为 yes，BACKENDS 含 IMAP、SMTP |
| L2.2.2 | 密码不落明文 | `grep -n 'auth.raw' ~/Library/Application\ Support/himalaya/config.toml` | 无输出；密码经 `auth.cmd` 从钥匙串读 |
| L2.2.3 | 钥匙串条目 | `security find-generic-password -s campus-apply-mail -a <邮箱> >/dev/null && echo ok` | 输出 `ok` |
| L2.2.4 | 能收 | `himalaya account doctor <账户名> && himalaya envelope list -s 5` | doctor 无报错；列出最近 5 封 |
| L2.2.5 | 能发 | 给自己发一封测试信（见下） | 2 分钟内 `himalaya envelope list -s 3` 能看到 |

Gmail 用户：先开两步验证，再生成应用专用密码（用户本人操作），存进钥匙串，模板见 `templates/himalaya/gmail.toml`。自有域名走 Resend SMTP 的，见 `templates/himalaya/resend.toml`。

测试发信：

```bash
printf 'From: <邮箱>\nTo: <邮箱>\nSubject: campus-apply smtp test\n\nok\n' | himalaya message send
```

投递邮箱建议单独开一个，只用于校招注册和收发，别混用个人主邮箱。

### L2.3 通用平台账号预注册

提前注册、登录态留在 ego lite 里，能省掉大量注册墙 handoff。账号写进实例 `accounts.md`，不写进仓库。
**各平台注册方式、账号是否跨企业通用、预注册收益排序见 `docs/platform-accounts.md`**；
通道优先级与降级见 `docs/auth-channels.md`。

| # | 平台 | 入口特征 | 注册方式（维护者实测） | 通过标准 |
|---|---|---|---|---|
| L2.3.1 | 智联招聘（校园） | `xiaoyuan.zhaopin.com`、`*.zhaopin.com` | 手机验证码或微信扫码；账号全站通用 | ego lite 打开 `xiaoyuan.zhaopin.com` 右上角显示已登录；账号手机号与 profile 一致；顺手填好 `i.zhaopin.com/resume` 账号级简历 |
| L2.3.2 | 北森 | `*.zhiye.com`、企业自有域名的北森 Phoenix 表单 | 按企业分站；微信扫码后绑定邮箱或手机 | 任一北森站登录一次，记录绑定方式（租户独立，预注册收益低，见 platform-accounts §2） |
| L2.3.3 | Moka | `app.mokahr.com` | 邮箱验证码，账号跨分站复用 | 打开任一 Moka 分站右上角有头像 |
| L2.3.4 | 飞书招聘 | `*.jobs.feishu.cn` | +86 手机验证码或抖音扫码，无邮箱入口 | 任一飞书分站登录一次 |
| L2.3.5 | 牛客 | `nowcoder.com` | 手机或邮箱 | 已登录 |
| L2.3.6 | hotjob（大易） | `wecruit.hotjob.cn` | 仅微信扫码 | 任一 hotjob 站扫码登录一次 |
| L2.3.7 | 前程无忧 51job / 国聘 / 猎聘校园 / BOSS / 实习僧 | — | 按平台（部分待实测，见 platform-accounts §1） | 可选，已登录 |
| L2.3.8 | 短信码自取链路 | iPhone 短信转发到本机 | iPhone 设置 → 信息 → 短信转发 → 勾选本机；运行方要有完全磁盘访问权限 | `uv run $CAMPUS_REPO/host/sms-code.py health` 能报告最近短信时间；发一条真实短信试 `--since 5m --sender-hint 测试` |

注意：智联账号绑定的手机号若是旧号，改绑需要旧号验证，尽早处理。各平台的门户坑见 `portals/`。

### L2.4 实例文件补齐

L1 已建 `profile.md`、`intent.yaml`、`tracker.md` 和目录骨架，本节补齐投递所需的其余文件。

| # | 项 | 检测命令 | 通过标准 |
|---|---|---|---|
| L2.4.1 | 问题库 | `uv run $CAMPUS_REPO/host/qb.py --help` 后按其校验子命令检查 | `required: true` 且 kind 为 value/secret 的条目 answer 非空；且对照 `templates/question-bank.example.yaml`逐条确认——证书编号、留学生字段组、银行国企扩展字段等缺项是投递中 blocked 的头号来源；本条是投递硬门槛，没填满不派单 |
| L2.4.2 | 问题库视图 | `head -3 $CAMPUS_INSTANCE/question-bank.md` | 头部注明“生成文件勿改” |
| L2.4.3 | 账号表 | `test -f $CAMPUS_INSTANCE/accounts.md` | 存在；L2.3 注册过的平台都有记录 |
| L2.4.4 | 台账与日志 | `ls $CAMPUS_INSTANCE/tracker.md $CAMPUS_INSTANCE/log` | 存在（可为空表） |
| L2.4.5 | 不进 git | `git -C $CAMPUS_REPO status --porcelain \| grep -E 'profile.md\|intent.yaml\|accounts.md'` | 无输出 |
| L2.4.6 | 脱敏 | `python3 $CAMPUS_REPO/scripts/check_secrets.py --instance $CAMPUS_INSTANCE` | `0 处命中` |

### L2.5 通知与内网访问

| # | 项 | 检测命令 | 通过标准 |
|---|---|---|---|
| L2.5.1 | Bark App | 用户在 iPhone 安装 Bark，打开后允许通知，首页复制 device key | 用户回复“好了” |
| L2.5.2 | notify.yaml | `grep -E '^(bark_server\|device_key\|web_base):' $CAMPUS_INSTANCE/notify.yaml` | `bark_server` 为 `https://api.day.app`（公共服务器，默认不改）；`device_key` 已填；`web_base` 为 tailnet 地址 |
| L2.5.3 | 推送可达 | `uv run $CAMPUS_REPO/host/notify.py --help` 后发一条测试事件 | 手机 30 秒内收到 |
| L2.5.4 | web 容器 | `container list` | web 容器在运行 |
| L2.5.5 | tailscale serve | `tailscale serve status` | 有一条 https 规则指向 web 容器的本机端口 |
| L2.5.6 | 手机访问 | 手机浏览器打开 `web_base` | 看板能打开，数字与 tracker 一致 |
| L2.5.7 | 点击跳转 | 发一条测试 handoff，点通知 | 打开 `/handoff/<id>` 页面，截图可见 |
| L2.5.8 | 定时同步已安装（L1.14 装过就跳过） | `uv run $CAMPUS_REPO/host/install-schedule.py status --role member`（维护者用 `--role maintainer`） | 退出码 0，`launchd:` 段有 `state =` 行；未安装则跑 `install --role <角色>`（见 docs/sync.md §3） |

手机接入 tailnet 两种方式，二选一：

- **Tailscale App**：最简单，登录同一账号即可。
- **sing-box 的 tailscale endpoint**：适合平时已经用 sing-box 的人，在配置里加 tailscale endpoint，登录同一 tailnet。

**iOS 同一时间只能有一个 VPN 生效。** 开着其他代理 App 时 Tailscale App 会被顶掉，推送点开后页面打不开。已经在用 sing-box 的，用第二种方式把 tailnet 并进同一个配置。

### L2.5.x 可选（不是前置条件，同学不用做）

- **自建 bark-server**：维护者可选，用 Apple `container` 跑 bark-server，把 `notify.yaml` 的 `bark_server` 改成自建地址。公共服务器 `https://api.day.app` 能用就不需要。
- **Cloudflare Tunnel + Access**：需要在 tailnet 外访问看板时才用。访问默认走 Tailscale。

### L2.6 材料

按 `docs/materials.md` §1.1 必备表和 §6 检查表逐项核对。最低要求（实测被门户拦过的全在这档）：`resume-cn.pdf`、`id-photo.jpg` + ≤500KB 压缩副本、`life-photo.jpg`、`id-card-front/back.jpg`、学生身份证明（学生证/在读证明/学籍验证报告任一）、各学历成绩单、英语成绩证明；海外学历再加 `liufu-cert.pdf` 和毕业证/学位证扫描件（编号核对用）。国企/银行门户的强制附件位见各门户 playbook。

### L2.7 总自检

| # | 项 | 检测命令 | 通过标准 |
|---|---|---|---|
| L2.7.1 | host-check | `uv run $CAMPUS_REPO/host/host-check.py` | 全绿（脚本落地前跳过，按本表人工检查） |
| L2.7.2 | web `/setup` 页 | 手机打开 `web_base/setup` | 全绿 |
| L2.7.3 | 候选生成 | `uv run $CAMPUS_REPO/host/scan.py`（参数见 `--help`） | `log/battle-map.md` 生成，前 20 行方向符合 intent |
