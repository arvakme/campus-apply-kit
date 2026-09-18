# JD 抓取：host/fetch-jd.py + adapters/sources/jd-fetch.mjs

> 实测日期 2026-09-15。把公告原文（JD）抓成 `data/jd/<announcement_id>.md`，本机产物；
> 判断结果是个人的，在各自实例 `state/fit/`（contracts §7）。

## 1. 运行

```bash
uv run --script host/fetch-jd.py --incremental        # 日常：只抓没有文件的（维护者 01:00 同步后跑）
uv run --script host/fetch-jd.py --ids 10001,10002    # 指定公告
uv run --script host/fetch-jd.py --since 2026-09-01 --limit 50 --dry-run
```

非 0 退出码 = 失败。其他参数：`--browser auto|always|never`（HTTP 失败是否走 ego-browser 兜底，
默认 auto）、`--ocr / --no-ocr`（image_only 是否走本地 OCR，默认开）、`--concurrency`（≤3）、
`--interval`（同域名间隔，下限 1s）、`--out-dir`。

`--incremental` 语义：已有 `.md` 文件的跳过；`status: missing` 且距上次抓取 ≥3 天的重试一次；
`image_only` 且 index 里没有 OCR 记录的补跑一次 OCR（OCR 过还失败的不再重复跑）；
`login_required / gone` 不自动重试。

## 2. 抓取策略

1. **普通 HTTP**（requests + BeautifulSoup）：公告 `link` 里约 99% 是 `mp.weixin.qq.com`
   公众号文章，正文在 `#js_content`，直抓即可；官网静态页用"常见正文容器 → 文本量最大块"兜底。
   清洗：去 script/style/iframe/svg/导航，保留岗位名、职责、要求、地点、投递方式、截止时间，
   单篇上限 2 万字符。实测（2026-09-16）公众号校招公告约 80% 是长图海报，正文走 §3 的 OCR。
2. **ego-browser 兜底**（`jd-fetch.mjs`）：HTTP 拿不到正文（missing / login_required /
   image_only 但没有 img_urls）的条目攒成一批，一次 TaskSpace 顺序 `goto` 再页面内提取
   （正文 + 图片 URL），间隔 ≥1s，用完 `finish({keep:[]})`。需要登录或纯 JS 门户仍拿不到的
   标 `login_required`，**不强求**——judge.py 会退回 `basis: jobs_only` 用岗位列表判断。
3. **长图 OCR**（`--ocr`，默认开）：image_only 条目按 `data-src`/浏览器回传顺序下载正文图片
   （单篇 ≤30 张，同限速），调 macOS Vision 本地 OCR（pyobjc，zh-Hans+en，accurate），
   超高图按 ≤2500px 分块识别再拼接，去重页眉页脚、合并断行。图片缓存在系统临时目录，
   用完即删，**不入仓库**。成功 → `status: ok_ocr` + front matter `ocr_images / ocr_chars`；
   失败保持 `image_only` 并在 index 记 `ocr: image_only`，incremental 不再重试。

## 3. status 取值（写进 front matter 和 index.json）

| status | 含义 | 后续 |
|---|---|---|
| `ok` | 抓到正文 | judge 用 `basis: jd` |
| `ok_ocr` | 长图经本地 Vision OCR 出正文 | judge 用 `basis: jd`（质量略低于 ok，fit 置信度靠 risks 提示） |
| `image_only` | 正文是长图且 OCR 失败/未跑 | judge 退回 jobs_only |
| `login_required` | 登录墙 / 环境异常 / 验证码 | 同上；公众号偶发"环境异常"限流页 |
| `gone` | 内容被发布者删除 / 链接失效 | 不重试 |
| `missing` | 其他抓取失败 | incremental ≥3 天重试一次 |

## 4. 输出格式

`data/jd/<id>.md`：

```markdown
---
announcement_id: 10001
company: 示例公司
source_url: https://mp.weixin.qq.com/s/…
fetched_at: 2026-09-15T23:40:00+08:00
status: ok
content_sha256: …
chars: 4321
via: http            # http | browser
---

（清洗后的正文）
```

`data/jd/index.json`：`{ "<id>": {status, sha, chars, fetched_at, via} }`，原子替换。

## 5. 限速与风控

- 同域名请求间隔 ≥1s，HTTP 并发 ≤3；公众号抓取相对温和，但不排除触发"环境异常"页
- 出现大面积 login_required / 401 / 403 时停手，隔几小时再跑；不要把间隔调到 1s 以下
- data/jd 是公开招聘信息，可能含 HR 手机号——check_secrets.py 对它只跑实例字面量检查，
  与 data/paperball 同口径
