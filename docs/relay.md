# 共享 Telegram bot 中转（relay · tg.example.com）

> 一个 bot，几位同学各自私聊。token 只在 Cloudflare Worker 里，同学不用建 bot、不用 Bark。
> 契约：`docs/contracts.md` §10。实现：`relay/`（Worker + Durable Object）、
> `host/tgbot.py`（pair/unpair/run）、`host/channels/telegram.py`（/v1/send 出站）、
> `host/report.py rank-report`（每日数字上报）。

## 1. 这是什么

```
Telegram 用户 ──webhook──> tg.example.com（Cloudflare Worker + DO）
                              │ 按 user_id → instance_id 分队列
   Mac A（维护者）<──签名 /v1/pull──┤   /v1/send 代发回 Telegram
   Mac B（同学） <──签名 /v1/pull──┤   /v1/rank/report 每日数字
   Mac C（同学） <──签名 /v1/pull──┘   cron 21:00(北京) 排行榜播报
        （人数可再扩：一人一台 Mac、一个 instance_id、一把密钥）
```

每台 Mac 一个 `instance_id` + 一把 32 字节密钥（钥匙串 `campus-apply-relay`）。
Worker 不存消息内容：入队更新被 ack 即删，24h 未取自动过期，图片只流式转发。

## 2. 维护者部署（一次性）

> 单人使用不需要 relay（直接 `mode: direct`）。几个人共用一个 bot 时，部署的那个人就是「维护者」。
> **先把 `relay/wrangler.toml` 和下文命令里的 `tg.example.com` 全部换成你自己在 Cloudflare 上的子域**，否则部署会指向一个不属于你的域名。

共享 bot 是 **@your_campus_bot**（用户名公开，可进仓库；**token 永不进仓库**，只进
Worker secret 和钥匙串）。

部署前在 @BotFather 里把入群关掉：`/setjoingroups` → 选 bot → `Disable`
（bot 只私聊，被拉进群没有任何收益，关掉少一个面）。

```bash
cd $CAMPUS_REPO/relay
npm install                        # 只装 wrangler（devDependency）

# 三个 secret（只写进 Worker，不落仓库）
npx wrangler secret put BOT_TOKEN        # @BotFather 给的 token
npx wrangler secret put WEBHOOK_SECRET   # 自己生成的随机串，setWebhook 时还要用
npx wrangler secret put ADMIN_KEY        # 维护者管理密钥（吊销/查看实例用）

# wrangler.toml 里的 vars 要先把两个值填对：
#   MAINTAINER_USER_ID —— 维护者自己的 Telegram user_id（审批配对、本人直通；必填）
#   PAIR_IP_RATE       —— pair/init 每来源 IP 每小时上限（默认 5，防刷实例）

npx wrangler deploy                    # wrangler.toml 已配 route tg.example.com/*
                                       # 与 cron "0 13 * * *"（UTC）= 北京 21:00
```

部署后把 webhook 指给 Worker（**必须带 secret_token**，否则 webhook 401）：

```bash
curl -X POST "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" \
  -d "url=https://tg.example.com/tg/webhook" \
  -d "secret_token=<WEBHOOK_SECRET>" \
  -d 'allowed_updates=["message","callback_query"]'
# 验证：curl "https://api.telegram.org/bot<BOT_TOKEN>/getWebhookInfo"
```

bot 资料（名字「秋招助手」、简介、指令菜单）用幂等命令设置，资料漂移了重跑即可：

```bash
uv run $CAMPUS_REPO/host/tgbot.py profile          # relay 模式经 Worker /v1/profile
                                                   # direct 模式直连 api.telegram.org
```

管理操作（排障用，ADMIN_KEY 只在维护者手里）：

```bash
curl -H "X-Admin-Key: <ADMIN_KEY>" https://tg.example.com/v1/admin/list           # 看实例/队列
curl -X POST -H "X-Admin-Key: <ADMIN_KEY>" -H "content-type: application/json" \
  -d '{"instance_id":"i-xxxxxxxx"}' https://tg.example.com/v1/admin/revoke        # 吊销某台 Mac
```

## 3. 同学配对（3 步）

1. `git pull` 仓库；实例 `notify.yaml` 里 `telegram.mode` 设为 `relay`
   （新模板默认已是 relay；`channels` 里保留 `telegram`）。
2. `uv run $CAMPUS_REPO/host/tgbot.py pair --nickname 你的名字 --wait 120`
   → 打印一个 6 位配对码（10 分钟有效）。
3. 在 Telegram 给共享 bot 发 `/pair 123456` → 先回「已提交，等待维护者确认」，
   Worker 同时私聊维护者一条**审批消息**（昵称 + @username + user_id + [批准]/[拒绝]）。
   维护者点 [批准] 后才回「✅ 配对成功」，本机 `chat_id` 自动写回 `notify.yaml`，
   `uv run host/tgbot.py run`（或 launchd）开始收消息；[拒绝] 则实例直接删除。
   **维护者自己配对直通，不用审批**（`MAINTAINER_USER_ID` 命中）。

陌生人即使自己造 instance + 配对码来发 `/pair`，也只能停在 pending：
不进 rank、不收播报、消息全静默、`/v1/send` 403——没有维护者批准什么都不会发生。

**📌 秋招面板**：`notify.yaml` 顶层有 `web_base` 时，`pair` 会把它上报给 Worker；
`/pair` 绑定成功后 bot 自动发一条「📌 秋招面板」消息并**静默置顶**
（`pinChatMessage` `disable_notification`），带看板/榜单/日程/待我处理/问题库/前置条件
六个 URL 按钮（路径与 `web/app/board.py` 的 NAV 一致），同时把聊天菜单按钮设成
`web_app` 看板指向 `web_base/`。没配 `web_base` 则跳过面板，不影响配对。

`web_base` 之后变了（换机器名/tailnet）：改 `notify.yaml` 顶层 `web_base`，跑
`uv run host/tgbot.py pair --update-web-base`——Worker **原地编辑**那条置顶消息
（`editMessageText`，不重发不重新置顶）；原消息已被删才会新发一条再置顶。

不想上排行榜：pair 时加 `--no-rank`——既不上榜也不收 21:00 播报。

## 4. 安全模型逐条对应 §10.1

| 契约要求 | 实现 |
|---|---|
| webhook 校验 `X-Telegram-Bot-Api-Secret-Token`，不符 401 | `onWebhook` 常量时间比较 `WEBHOOK_SECRET` |
| 身份 = Telegram `user_id`；只收 `chat.type==private` 且 `chat.id==from.id` | `extract()` + webhook 前置过滤（群聊/转发/回调来源不符直接丢） |
| 未绑定 user_id 不回复、只计数 | `unboundWebhook` 非 `/pair` 一律静默写 `probe:<uid>`；配对码错/过期同样静默 |
| 配对：本机生成 instance_id + 32B 密钥（钥匙串 `campus-apply-relay`）+ 6 位码（10 分钟一次性，只存哈希）；`/pair <码>` 完成绑定；同一 user_id 只能绑一个 instance | `tgbot.py pair` → `/v1/pair/init` 预注册 `{code_hash}`（**实例自举签名**：body 声明的 key 验本次请求，非 admin key）；webhook `/pair` 核销 `code:` 索引并删除；`uid:<uid>` 唯一映射；码错不计回复且每 uid 10 分钟限 5 次；`pair/init` 另按 `CF-Connecting-IP` 每 IP 每小时限 `PAIR_IP_RATE`（默认 5）次防刷实例 |
| 陌生人不能自助加入 | `/pair` 有效码 → `status:pending` + `pend:<uid>`：不入 `members`、不写 `uid:` 映射、消息全静默、`/v1/send` 403、`pull.bound=false`；Worker 私聊 `MAINTAINER_USER_ID` 审批（inline 按钮 `ap:ok/no:<uid>`，**回调校验 cb.from.id==维护者**，旁人点了无效）；批准 → `bindUser`（uid 映射+members+bound 事件+面板）；拒绝 → `removeInstance` 整个删；维护者本人 `/pair` 直通 |
| `unpair` 本机与管理员均可吊销 | 本机 `tgbot.py unpair` → 签名的 `/v1/unpair`；维护者 `X-Admin-Key` → `/v1/admin/revoke`；都清实例、uid 映射、队列、rank |
| `X-Instance`/`X-Ts`(±60s)/`X-Nonce`(10min 不重)/`X-Sig` HMAC | `RelayHTTP._headers` 签名；Worker `verifySig` 校验 ts 窗口 → 签名 → nonce 原子核销（DO transaction，签错不消耗 nonce）；签名串 `METHOD|path?query|ts|nonce|sha256(body)` |
| Worker 不记消息内容：ack 即删、24h 过期、图片不落盘 | 队列只有 pull/ack 两个操作；pull 时惰性清 24h 过期项；`/v1/send` multipart 重建 FormData 直接转发，不写存储 |
| 高风险指令二次确认（`telegram.relay.confirm_high_risk`，默认开） | **dispatcher 侧实现（另一工单范围）**；本工单只把配置项写进模板与文档。relay 通道本身与 direct 一样只写 `state/commands.jsonl`，不直达 worker |

另外：`/v1/send` 强制 `params.chat_id == 绑定 chat_id`（`chat_forbidden`）——A 的密钥
给 B 发消息、或给任意 chat 发消息都会被 403；队列按 `X-Instance` 隔离，A 拿不到 B 的更新。

## 5. 排行榜 /rank

- 每台 Mac：`uv run host/report.py rank-report`（建议挂每日调度，dispatcher/launchd 均可；
  `--dry-run` 先看 payload）。只发 `{date, applied_today, applied_total}`，无公司名。
- 已绑定用户发 `/rank`：Worker 直接回榜单（昵称 + 今日 + 累计），不经过 Mac。
- 播报：cron `0 13 * * *`（UTC）= 每天 21:00 北京时间，私聊推给每位 rank 参与者；

- 只显示昵称（pair 时 `--nickname` 取的）与数字，不点评公司。

## 6. 排障

| 现象 | 查哪 |
|---|---|
| `pair` 预注册失败 | `curl https://tg.example.com/` 通不通；`--relay-base` 指错没；代理 |
| 发了 `/pair` 没反应 | 码是否超 10 分钟/已用过（重新 pair）；user_id 是否已绑别的实例（让维护者 `admin/list` 看）；worker 日志（`wrangler tail`） |
| `run` 报 bad_sig/401 | `key_cmd` 取的密钥和 Worker 上注册的是否同一把（重配过忘了 unpair 会这样）；本机时间是否偏差 >60s |
| `run` 报 unknown_instance | 实例被吊销（维护者 `admin/revoke` 或过期清理）→ 重新 `pair` |
| 消息发不出去 chat_forbidden | 这台 Mac 绑定的是另一个 chat；检查 `allowed_chat_ids`/`relay.chat_id` 是否被手改 |
| 队列积压 | `admin/list` 看 `queued`；`run` 没起来或 ack 失败；24h 未取自动过期 |
| 置顶面板没发/按钮打不开 | `notify.yaml` 顶层有没有 `web_base`（pair 时上报）；改了它跑 `pair --update-web-base`；链接要 tailnet 在线才打得开 |
| 没收到 21:00 播报 | `rank: false` 退出了；当天 `rank-report` 没跑（不影响收播报，只影响数字）；`wrangler tail` 看 cron 日志 |
| 换电脑/换实例 | 老机器 `tgbot.py unpair`，新机器重新 `pair`；或维护者 `admin/revoke` |

## 7. 上线清单（维护者按顺序）

1. `cd relay && npm install && npx wrangler secret put BOT_TOKEN|WEBHOOK_SECRET|ADMIN_KEY`
2. `npx wrangler deploy`（route `tg.example.com/*` 生效，确认 DNS/zone 在 Cloudflare 账号下）
3. `setWebhook` 带 `secret_token`（见 §2），`getWebhookInfo` 确认 `url` 与 `pending_update_count` 正常
4. `tgbot.py profile` 设 bot 资料（秋招助手/简介/指令菜单）；BotFather `/setjoingroups` 已关
5. 维护者自己先 `pair` + `run --once` 验证收发，再让其他同学按 §3 配对
6. `python3 test/test_relay.py`（本地全绿不算上线验证，只做回归）

**首次播报前的检查清单**：

- [ ] 每位使用者都完成 `pair`（admin/list 里每个实例都 bound）
- [ ] 每人至少跑过一次 `report.py rank-report`（榜单有数字，播报才不空）
- [ ] `wrangler tail` 确认 cron 已注册（deploy 输出里 triggers.crons 有 `0 13 * * *`）
- [ ] 播报前一天提醒同学「明天 21:00 第一条榜单推送，名字按 --nickname 显示」
