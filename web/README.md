# web · 看板 / 问题库 / 前置条件 / 待我处理

一个 Python 标准库写的小网站（`http.server`，只依赖 pyyaml），跑在 Apple 原生 `container` 里，实例目录挂到容器 `/instance`。页面请求时渲染，源文件没变就走缓存。

安全边界是 tailnet：服务本身**没有登录**，端口只绑 `127.0.0.1`，不要直接暴露到公网。

## 本机直接跑（不装容器）

server 本身只是个 Python 脚本，不想装 `container` 也能用——L1 档（见 `docs/setup-checklist.md`）就用这种方式：

```bash
export CAMPUS_REPO=~/campus-apply CAMPUS_INSTANCE=~/campus-apply-instance
uv run --with pyyaml python3 $CAMPUS_REPO/web/app/server.py   # 前台运行，Ctrl-C 停止
open http://127.0.0.1:8787/board
```

- 只绑 `127.0.0.1:8787`；端口被占用时加 `--port 8797`（或 `CAMPUS_WEB_PORT=8797`）。
- 本机直跑时 `/board` 直接读仓库 `data/` **实时构建**，比容器里的每日快照还新；`/`、`/setup`、`/bank`、`/handoffs` 同样可用。
- 系统 python3 已装 pyyaml 的话也可以直接 `python3 web/app/server.py`。
- 手机访问仍需容器 + tailscale serve（见下两节）；本机直跑只够自己 Mac 看。

## 启动（容器）

前提：macOS 26 + Apple 芯片，已装 `container` CLI 1.0（`container --version`）。

```bash
export CAMPUS_INSTANCE=~/campus-apply-instance
web/run.sh              # system start（如未运行）→ build → 替换旧容器 → run → 等 /healthz
open http://127.0.0.1:8787/
```

`run.sh` 实际执行的是：

```bash
container system start --enable-kernel-install         # 仅在没运行时
container build -t campus-apply-web -f web/Containerfile .
container stop campus-web; container delete campus-web  # 仅在已存在时
container run -d --name campus-web -p 127.0.0.1:8787:8787 -v "$CAMPUS_INSTANCE":/instance campus-apply-web
```

其他用法：

| 命令 | 作用 |
|---|---|
| `web/run.sh --no-build` | 不重新构建，只重启容器 |
| `web/run.sh stop` | 停容器，镜像保留 |
| `container logs campus-web` | 看访问日志 |
| `CAMPUS_WEB_PORT=8797 web/run.sh` | 8787 被占用时换宿主机端口 |

## 手机访问（Tailscale）

容器起来后，在宿主机执行一次（`--bg` 会常驻，重启机器后仍生效）：

```bash
tailscale serve --bg 8787      # 把 http://127.0.0.1:8787 以 https://<机器名>.<tailnet>.ts.net 暴露给自己的 tailnet
tailscale serve status         # 查看；撤销用 tailscale serve --https=443 off
```

手机装 Tailscale App（或 sing-box 的 tailscale endpoint）登录同一账号，打开 `https://<机器名>.<tailnet>.ts.net/`。这个地址填进实例 `notify.yaml` 的 `web_base`，Bark 通知点开就会跳到对应的 `/handoff/<id>`。

## 添加到主屏幕（PWA）

看板是 PWA：manifest 在 `/manifest.webmanifest`，service worker 在 `/sw.js`，图标和离线页在 `web/app/static/`。

**iPhone / iPad**

1. 确认 tailnet 已连上，用 **Safari** 打开 `https://<机器名>.<tailnet>.ts.net/`（添加到主屏幕只有 Safari 的分享菜单里有）；
2. 分享 → **添加到主屏幕** → 添加。主屏上的「秋招」图标点开就是独立窗口，没有地址栏；
3. 页面内导航（榜单 / 日程 / 待我处理）都留在 app 里；深色模式跟随系统，状态栏不挡内容（已处理刘海安全区）。

**Telegram 菜单按钮**

bot 菜单按钮（`web_app` 类型）填同一个 `https://<机器名>.<tailnet>.ts.net/` 地址即可。Telegram 内置浏览器不支持 service worker 也能正常用——SW 只是增强，页面本身不依赖它。

**离线行为**

service worker 是**网络优先**：每次打开都拿实时数据，只有断网时才显示「连不上面板：检查 tailnet 是否连着」的提示页。它不缓存 `/api/*`、`/raw/*` 和任何 HTML 页面，个人数据不会留在缓存里。

**更新后让 SW 生效**

框架更新（`git pull` + `web/run.sh` 重建容器）后，下次打开页面时浏览器会自动更新 SW；如果改了 `web/app/static/` 里的文件，先把 `sw.js` 里的 `VERSION` 加一再部署，旧缓存会自动清掉。要立即验证：Safari 里长按刷新 / 关掉标签页重开，或在「设置 → Safari → 高级 → Website Data」删掉该站点后重新添加到主屏幕。

## 更新

```bash
git -C $CAMPUS_REPO pull
web/run.sh                     # 重新构建镜像并替换容器；实例数据在宿主机目录里，不受影响
```

只改了实例里的 md/yaml 不需要重启，刷新页面即可。

## 路由

| 路由 | 说明 |
|---|---|
| `/` | 看板。解析 `tracker.md`、`log/battle-map.md`、最新 `log/B-NN.md`、`log/email-queue.md`，数字口径与旧 `build-dashboard.py` 一致。顶部「我的日程」区块合并 `state/next-steps.jsonl` + `assessments.md` + `tracker.md`（合并规则见 `docs/agenda.md`），≤7 天高亮、过期灰化。窄屏（≤640px）候选表改为卡片 |
| `/agenda` | 我的日程全表：类型/状态/搜索筛选、来源列、完成/撤销按钮（追加写 `state/agenda-done.jsonl`，不改源文件）。同步进日历用 `host/schedule.py sync --from-agenda` |
| `/api/agenda/done` | `POST {id, done}` 标记/撤销完成，写 `state/agenda-done.jsonl` |
| `/board` | 每日榜单。`data/paperball/announcements.jsonl` 全量公告按雇主主体合并成一行一家：层级、档（state/fit）、建议岗位、截止 D-n、**日程**（该公司最近一条未闭环日程）、入口、JD 状态、**我的状态**（`state/jobs` > `registry.jsonl` > `tracker.md`/battle-map > 未处理）。顶部有全量/今日新增/已投/进行中/等我处理/今日截止统计，支持按状态、档、层级、入口、城市筛选与搜索；窄屏改卡片 |
| `/board?date=YYYY-MM-DD` | 看历史快照（`state/board/<date>.json`，由 `host/board-snapshot.py` 产出） |
| `/board/diff` | 最新快照 vs 前一天：新增、下线、状态变化；`?date=` 与 `&base=` 可指定两天 |
| `/api/board` | 同一份榜单数据的 JSON（支持 `?date=`） |
| `/board.csv` | 当前榜单 CSV 导出（支持 `?date=`，带 BOM） |
| `/bank` | 问题库编辑。按 category 分组，组内未填在前；显示必填完成度、填写提示；secret 条目只显示已填/未填，输入框留空表示不改 |
| `/setup` | 前置条件。宿主机部分读 `state/host-health.json`（由 `host/host-check.py` 写）；实例部分在容器内检查 profile、intent、问题库必填、accounts、notify.yaml（Bark 只查公共服务器用的 device_key）、materials/ |
| `/handoffs` | 待我处理列表（`state/handoffs/*.json` 中 `resolved_at` 为空的在上面） |
| `/handoff/<id>` | 单条 handoff：说明、截图（二维码可长按识别）、"已处理"按钮（写 `resolved_at`） |
| `/api/bank` | `GET` 返回问题库（secret 不含 answer）和 `version`（文件 sha256）；`PUT {version, updates:[{id, answer}]}`，版本不符返回 409 |
| `/api/handoff/<id>/resolve` | `POST`，写 `resolved_at` |
| `/api/dashboard/stats` | 看板统计（对账用） |
| `/raw/log/…`、`/raw/shots/…` | 只读访问截图和批次 md；实例里其他文件（accounts.md 等）一律 404 |
| `/manifest.webmanifest`、`/sw.js`、`/apple-touch-icon.png`、`/favicon.ico`、`/static/…` | PWA 资源，只读暴露 `web/app/static/`（文件名白名单），见「添加到主屏幕」一节 |
| `/healthz` | 健康检查 |

## 每日快照

容器只挂 `/instance`，读不到仓库 `data/`：`/board` 在容器内默认展示**最新快照**；宿主机直跑 `server.py` 或挂了仓库时才实时构建。

```bash
uv run --script "$CAMPUS_REPO/host/board-snapshot.py" --instance "$CAMPUS_INSTANCE"
# 建议接在 host/daily-sync.sh / pull-update.sh 之后，每天一条 state/board/<date>.json
```

## 问题库写入规则

网页和 `host/qb.py` 共用 `web/app/bank.py`：

1. `fcntl.flock` 管同一侧的并发；
2. `question-bank.yaml.lockdir`（mkdir 锁）管宿主机和容器之间的并发。实测 flock 穿不过 Apple container 的目录挂载，mkdir 能；
3. 锁内比对 sha256 版本号，不一致返回冲突（网页 409，qb.py 退出码 3）；
4. 写临时文件后 `os.replace` 原子替换，并重新生成只读的 `question-bank.md`。

网页保存遇到 409 时，如果别人改的不是你正在改的条目，会基于新版本自动重试一次；改的是同一条就提示你刷新，输入框里的内容不会丢。

## 可选：自建 bark-server（维护者用）

默认用公共服务器 `https://api.day.app`，只填 `device_key` 就行，同学不需要这一节，`/setup` 也不检查它。

想让推送不经过第三方中转时，可以自己跑 bark-server（官方镜像是 `finab/bark-server`，不是 `finb`）：

```bash
web/run-bark.sh          # 拉 arm64 镜像（首次）→ 起 campus-bark，127.0.0.1:8788 → 容器 8080，数据在 $CAMPUS_INSTANCE/state/bark
curl http://127.0.0.1:8788/ping
web/run-bark.sh stop     # 停容器，镜像和数据保留
```

默认宿主机端口是 8788，因为 8090 常被别的服务占用，可用 `CAMPUS_BARK_PORT` 改。

暴露给手机（同样由你决定是否执行）：

```bash
tailscale serve --bg --https=8443 8788     # → https://<机器名>.<tailnet>.ts.net:8443
```

然后：

1. 手机 Bark App → 服务器 → 添加 `https://<机器名>.<tailnet>.ts.net:8443`，App 会向它注册并给出新的 device_key；
2. 实例 `notify.yaml` 改成 `bark_server: http://127.0.0.1:8788`（notify.py 在宿主机上跑，直接走本机端口）和新的 `device_key`；
3. `uv run --script host/notify.py batch-done --title 测试 --dry-run` 看请求，去掉 `--dry-run` 实发一条。

自建服务器依然要经过苹果 APNs，只是省掉了 api.day.app 这层中转；手机必须连着 tailnet 才能完成注册。
