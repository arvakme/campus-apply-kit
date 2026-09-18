# Agent、CLI 与模型

## 1. 角色 → CLI → 模型

| 角色 | 在哪跑 | CLI 与启动方式 | 模型 | 说明 |
|---|---|---|---|---|
| 主持人 | Seedmux pane | 任选一个能跑长会话的 CLI（维护者用过 Kimi、Claude Code、Codex） | 该 CLI 的强档模型 | 拆批、派单、审阅、维护台账；需要能调用 `~/.seedmux/bin/smx-team` |
| worker | Seedmux pane，每单一个 | `devin --model swe-2-max --permission-mode dangerous` | `swe-2-max` | 浏览器填表主力；必须免确认运行，见 §3 |
| 邮件起草 | 主持人会话内，或单独 pane | 同主持人 | 同主持人 | 按 `templates/mail/cover-letter.skeleton.md` 起草，发送走 himalaya，受 `permissions.email` 约束 |
| 引导助手 | 普通终端 | `codex`（在仓库根目录启动） | Codex 默认模型 | 执行 README 引导提示词和“同步更新”，不参与投递 |

历史说明：维护者早期 worker 用 `swe-2-high`，2026-09-15 起新开 pane 统一 `swe-2-max`。存量 pane 跑完当前任务再换。

## 2. Devin 订阅准备

### 2.1 需要什么

- worker 依赖 Devin CLI 的 `swe-2-max` 模型。Devin Pro 档下，`devin models list` 中 SWE-2 系列（`swe-2-high / swe-2-medium / swe-2-max`）标注为 Free，即不按 token 另计费。
- 免费档能否使用 `swe-2-max`、各档额度和价格，**以 Devin 官网为准**，本仓库不写数字。
- 同学如果不订阅：**不建议用 Codex 当 worker**——浏览器填表每一步都要读页面/截图，按 token 计费的模型一家公司就可能烧掉大量额度（实测：卡在"控制权失败"重试 15 分钟后 token 耗尽）。替代：邮件投递 + 手动投；或有独立额度的 agent（Cursor composer 等）小批量验证。门户 playbook 和 SOP 按 Devin worker 实测写成。

### 2.2 安装与登录

```bash
# 安装方式以官网为准；装好后：
devin version
devin auth login            # 浏览器授权；远程/SSH 用 devin auth login --force-manual-token-flow
devin auth status           # 通过标准：输出 Logged in
```

### 2.3 验证订阅与模型可用

```bash
devin models list | grep -A4 'SWE-2'
```

通过标准：列表里有 `swe-2-max`。

再做一次真实调用（在任意临时目录）：

```bash
mkdir -p /tmp/devin-check && cd /tmp/devin-check
devin --model swe-2-max --permission-mode dangerous --respect-workspace-trust false \
  -p "运行 pwd 并只输出结果"
```

通过标准：不弹权限确认，输出 `/tmp/devin-check`（或其 `/private` 前缀形式）。报模型不可用或额度不足 → 订阅未生效，去官网确认档位。

交互模式再确认一次：

```bash
devin --model swe-2-max --permission-mode dangerous
```

状态栏应显示已绕过权限确认（维护者看到的是 “bypass permissions on”）。没有显示时在会话里补发 `/yolo`。

### 2.4 权限模式

`--permission-mode` 取值（`devin --help`）：`auto` 只自动批准只读工具；`accept-edits` 加上工作区编辑；`smart` 让快速模型判断安全操作；`dangerous` 自动批准所有工具。worker 必须用 `dangerous`。

**教训**：维护者第一批 worker 没有真正开启免确认，权限弹窗卡住了回执和写盘，消息在队列里积压，主持人只能反复手动批准。spawn 后先看状态栏，确认生效再派单。

### 2.5 权限白名单

`dangerous` 已经自动批准所有工具。白名单是第二层保险：模式没生效或以后收紧模式时，常用命令仍不弹窗。

- 放置位置：worker 启动目录（`cwd`）下的 `.devin/config.local.json`。维护者的 worker 在实例目录的上一级启动，所以白名单放在那一级。
- 模板：`templates/devin/config.local.json`，复制后把 `<HOME>` 换成自己的家目录绝对路径。
- 这个文件是本机配置，不进 git。

## 3. seedmux 桥

### 3.1 为什么不用 smx-team spawn

`smx-team spawn` 和 `assign` 目前不支持 devin（支持 claude、codex、grok、opencode、kimi、agy）。devin worker 用桥 API 直接起 pane、投工单。

### 3.2 取端口和 token

```bash
CFG="${SEEDMUX_TEAM_BRIDGE_PATH:-$HOME/Library/Application Support/Seedmux/team-bridge.json}"
PORT=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["port"])' "$CFG")
TOKEN=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["token"])' "$CFG")
curl -sS -H "X-Token: $TOKEN" "http://127.0.0.1:$PORT/panes"
```

### 3.3 起 worker

```bash
curl -sS -H "X-Token: $TOKEN" -H 'Content-Type: application/json' \
  -X POST "http://127.0.0.1:$PORT/spawn" \
  -d '{"cwd":"<worker 启动目录绝对路径>","launch":"devin --model swe-2-max --permission-mode dangerous","direction":"right","focus":false}'
```

- 返回新 pane id。等 TUI 就绪（`/panes` 显示 idle 或 capture 看到输入框）再投工单。
- `--permission-mode dangerous` 在 spawn 时直接生效；capture 确认状态栏，没生效就 `POST /send` 发 `/yolo`。

### 3.4 投工单

1. 手工建任务目录 `~/.seedmux/team/tasks/T-xxxxxx/`：
   - `meta.json`：至少 `task`、`from_pane`（主持人 pane）、`to_pane`（worker pane）、`cwd`、`created_at`、`status: "dispatched"`
   - `prompt.md`：worker 契约段（回执写 `reply.md`、`smx-team reply` 用法、team-msg 用法）+ 任务 + `docs/redlines.md` §8 权限段落
   - 可参考 `smx-team` 为其他 CLI 生成的任务目录格式
2. 投信封，消息必须单行：

```bash
curl -sS -H "X-Token: $TOKEN" -H 'Content-Type: application/json' \
  -X POST "http://127.0.0.1:$PORT/send" \
  -d '{"to":"<pane id>","text":"[team-task T-xxxxxx] 请读 ~/.seedmux/team/tasks/T-xxxxxx/prompt.md 并执行，完成后按契约 reply","enter":true}'
```

3. 响应里 `ok=true` 只说明桥已注入，不说明 worker 已处理。
4. 看现场：`GET /capture?pane=<id>&lines=200` 或 `smx-team capture <pane> -n 200`。

### 3.5 长任务自停与 nudge

- devin 长任务跑到约 1 小时会自己停下（输出截断或进入空闲），不会主动回执。
- 主持人要定时巡检：`/panes` 看状态，idle 的 pane 若对应任务仍是 `dispatched`，capture 最后几十行判断停在哪。
- nudge 用单行消息：`[team-msg task=T-xxxxxx] 继续执行，先读 log/ops/<批次>-<id>.md 最后一节确认进度`。
- 不要在 worker 等用户扫码、等验证码时 nudge，会让它误以为可以继续操作。

## 4. Codex 引导

- 同学第一次使用：在仓库根目录启动 `codex`，粘贴 README 里的引导提示词。
- 之后说“同步更新”，Codex 执行 `docs/sync.md` 的流程。
- Codex 读 `AGENTS.md`，所以不需要额外说明仓库结构。
