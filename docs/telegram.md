# Telegram 双向 bot

> 扫码、图形验证码、终审提交这类必须人来的环节，worker 会把截图推到 Telegram，
> 你在聊天里点按钮或回一句话，worker 就接着干。也能随时切档位、暂停、接管——
> **想介入就介入，想自动化就自动化**。
>
> 实现：`host/tgbot.py`（long polling，不要公网回调）+ `host/channels/telegram.py`（出站）。
> 与 dispatcher 只经 `state/` 文件协作（契约 docs/contracts.md §8），互不直接调用。

## 0. 两种模式（notify.yaml 的 `telegram.mode`）

| mode | 谁持有 token |  inbound | 适合 |
|---|---|---|---|
| `relay` | Cloudflare Worker `tg.example.com` | webhook → Worker → 本机签名 `/v1/pull` | 共享 bot：同学不用建 bot，配对即用（契约 §10，见 `docs/relay.md`） |
| `direct`（默认） | 本机钥匙串 | 本机 `getUpdates` 长轮询 | 单人使用：自己找 @BotFather 建 bot |

relay 模式的安装只有三步：`telegram.mode: relay` → `tgbot.py pair --nickname 名字` 拿配对码 →
Telegram 里发 `/pair <码>` → 等部署 Worker 的那个人批准（一个人用就是你自己，直通）。
批准后 chat_id 自动写回 `allowed_chat_ids`，下面 §1–2 的建 bot 步骤全跳过。
`unpair` 本机吊销；`/rank` 排行榜由 Worker 直接回复（不进 commands.jsonl）。
配对成功后 bot 还会发一条「📌 秋招面板」置顶消息（六个面板入口按钮 + 左下角「看板」菜单按钮），
链接前缀取 notify.yaml 顶层 `web_base`；前缀变了跑 `tgbot.py pair --update-web-base` 原地更新。
本文档其余小节描述的行为（指令、按钮、handoff 时效）两种模式完全一致。

## 1. 建 bot（仅 direct 模式）

1. Telegram 里找 **@BotFather** → `/newbot` → 按提示起名字 → 拿到 token（形如 `123456:ABC…`）。
2. token 存进钥匙串（不写明文文件）：

   ```bash
   security add-generic-password -s campus-apply-telegram -a bot -w '<token>'
   # 改 token：security add-generic-password -s campus-apply-telegram -a bot -w '<新token>' 会报已存在，
   # 先 security delete-generic-password -s campus-apply-telegram 再 add
   ```

3. `$CAMPUS_INSTANCE/notify.yaml` 里加（完整示例见 `templates/notify.example.yaml`）：

   ```yaml
   channels: [telegram, bark]
   telegram:
     bot_token_cmd: security find-generic-password -s campus-apply-telegram -w
     allowed_chat_ids: []              # 下一步填
     proxy: http://127.0.0.1:6152      # 你的本机代理端口；launchd 环境没有代理变量，必须显式配
   ```

4. 拿自己的 chat_id：先在 Telegram 里给 bot 发任意一句话，然后 30 秒内跑

   ```bash
   uv run $CAMPUS_REPO/host/tgbot.py whoami
   # chat_id: 123456789  (@you)
   ```

   把数字填进 `allowed_chat_ids`。可以填多个（比如小号/备用机），每条通知都会发给所有白名单 chat。

5. 自测：`uv run $CAMPUS_REPO/host/notify.py blocked --company 测试 --body "hello" --dry-run`
   能看到 telegram 与 bark 两路请求（token 打码）；去掉 `--dry-run` 真发一条。

## 2. 常驻（launchd）

```bash
uv run $CAMPUS_REPO/host/tgbot.py install-plist --dry-run     # 先看 plist 内容
uv run $CAMPUS_REPO/host/tgbot.py install-plist --dest ~/Library/LaunchAgents
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/dev.campus-apply.tgbot.plist
```

- `KeepAlive` 挂了自动拉起，`ThrottleInterval 30` 防崩溃风暴；日志在 `$CAMPUS_INSTANCE/state/tgbot.launchd.log`。
- 前台调试：`uv run $CAMPUS_REPO/host/tgbot.py run`（Ctrl-C 停）。
- 卸载：`launchctl bootout gui/$UID dev.campus-apply.tgbot`，再删 plist。

## 3. 指令

| 指令 | 作用 |
|---|---|
| `/today` | 今日待圈选（state/fit 里未建作业的 冲→常规→练手），每条 [投]/[跳过] 按钮，分页 |
| `/status` | 档位、暂停状态、各状态作业数、在跑作业（公司/状态/等多久）、待你处理的项 |
| `/mode manual\|supervised\|auto` | 切总档位；`/mode 练手 auto` 这种单档覆盖 |
| `/pause` `/resume` | 暂停/恢复派新单；带参数 `/pause 腾讯` = hold 单个作业 |
| `/hold <id\|公司>` | 暂停某作业；`/resume <id>` 恢复 |
| `/takeover <id>` | 你接管浏览器，worker 停手 |
| `/skip <id>` | 这家不投 |
| `/log <id>` | 作业 history 最近 10 条 + ops 留痕最后一段 |
| `/report` | 战报（host/report.py；没有时按 jobs 汇总） |
| `/rank` | 投递排行榜（仅 relay 模式，Worker 直接回复，不经本机） |
| `/pair <码>` | 申请绑定本 chat（仅 relay 模式，码由 `tgbot.py pair` 生成；维护者批准后生效） |

推送消息的用法：

- **handoff**（扫码/验证码/终审）：bot 发**图片**（二维码长按可直接识别）+ 说明 + 按钮
  [已扫码] [重新获取] [跳过这家] [我来接管]（§8.6 时效协议）。
  说明文案含公司、有效至 hh:mm:ss、第几次刷新（n/max）、当前步骤（chain）。
  - 二维码过期 worker 会自动刷新并**原地换图**（editMessageMedia，同一条消息）；超过
    max_refresh（默认 3）停下等你点 [重新获取]——它写 `refresh` 指令（带 handoff_id），
    dispatcher 据此重派 parked 单或让 worker 刷新。
  - 你点 [已扫码] → 写 `reply("已扫码")` 且 handoff 转 scanned；**不会直接标 resolved**——
    resolved 只由 worker 回读页面后 `handoff-update`（或 web「已处理」）写，防止"点了但没扫上"。
  - **回复这条消息的文字**（比如短信码）会写成 `reply` 指令（带 handoff_id）立刻转给 worker。
  - Bark 同时发一条 timeSensitive 叫醒短讯「打开 Telegram · 微信扫码 · 公司 · 有效至 hh:mm」
    （APNs 不依赖 VPN，不论 channels 顺序）；Telegram 没送达时 Bark 降级发完整内容并附
    `/handoff/<id>` 链接，点开在浏览器里处理。
  - **5 分钟无响应兜底**（§8.7）：handoff 发出 5 分钟仍 waiting 且没有任何按钮/回复
    （`interacted_at` 为空），bot 每轮轮询扫一遍并 Bark 再提醒一次（timeSensitive），
    记录 `reminded_at`，每个 handoff 最多提醒一次。
  - worker 侧页面变化走 `uv run host/notify.py handoff-update --id <hid|公告id>
    --status scanned|expired|resolved|cancelled [--shot 新图] [--expires-at ISO]
    [--note 新说明] [--chain-step kind]`。
- **review_wait**（字段对照表）：发 `log/fields/<id>.md` 内容（过长时作为 .md 文件）+ [提交] [跳过]。
  **直接回复这条消息的文字**（如「学历填硕士」）会写成 `reply` 指令，dispatcher 转给对应 worker。
- blocked / batch-done / deadline / assessment：普通文本。

时效埋点：handoff 记录 `detected_at`（发现时刻）→ `notified_at`（发起推送）→
`delivered_at`（Telegram API 返回），战报据此统计 p50/p95；`channels.telegram` 记
首条送达的 chat_id/message_id。bot 每轮轮询（约 30s 以内）写 `state/tgbot.heartbeat`
供守护判活——心跳超过 2 分钟没动说明 bot 挂了（VPN 断/进程死），该查 launchd 日志。

**推荐做法（§8.7 矩阵）**：Telegram 装在**平板**上常驻，扫码时用**手机微信扫平板屏幕**——
这样手机不离开微信，截图即扫即走；平板离线时 Bark 叫醒短讯兜底到手机。

所有按钮和指令都落成 `state/commands.jsonl` 里的一条记录（`by: telegram`），dispatcher 消费后生效；
bot 点完按钮会即时回显「已提交，dispatcher 生效后通知」。

## 4. 截图示例

```
┌─────────────────────────────────┐
│ [图片：北森登录二维码]            │
│ ⚠️ 示例公司 · 需要微信扫码          │
│ 长按识别二维码，扫完点「已扫码」    │
│ 有效至 21:34                     │
│ [已扫码][重新获取][跳过这家][我来接管]│
└─────────────────────────────────┘
        ↓ 你扫完 / 二维码过期自动刷新
┌─────────────────────────────────┐
│ [图片：新二维码（原地替换）]       │
│ ✅ 示例公司 · 需要微信扫码          │
│ 状态：已扫码，等页面跳转           │
│ [重新获取] [跳过这家] [我来接管]    │
└─────────────────────────────────┘
```

回复它：「用我微信小号扫的，继续」或短信码 → worker 收到这句原话（`reply` 指令带 handoff_id）。

## 5. 排错

| 现象 | 查哪 |
|---|---|
| bot 没反应 | `state/tgbot.log`（非白名单 chat 会记 ignored）；`state/tgbot.launchd.log`；确认 chat_id 在 `allowed_chat_ids` |
| 推送发不出 | `state/notify.log`；代理是否通的：`curl -x http://127.0.0.1:6152 https://api.telegram.org` |
| token 错 | `security find-generic-password -s campus-apply-telegram -w` 能不能取出；BotFather 可 `/revoke` 重发 |
| 409 conflict | 有另一个进程在 getUpdates（装了 launchd 又手动 run）；停掉一个 |
| 点了按钮没生效 | 看 `state/commands.jsonl` 有没有写入、`commands.done.jsonl` 有没有被 dispatcher 消费 |
