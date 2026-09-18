#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6", "requests>=2.31", "beautifulsoup4>=4.12",
#                 "pyobjc-framework-Vision>=11", "Pillow>=10"]
# ///
"""fetch-jd.py · 抓公告原文（JD），写 data/jd/<announcement_id>.md

    uv run --script host/fetch-jd.py [--data PATH] [--out-dir DIR] [--ids 1,2,3]
        [--limit N] [--since YYYY-MM-DD] [--incremental] [--dry-run]
        [--browser auto|always|never] [--concurrency N] [--interval S]

输入：data/paperball/announcements.jsonl（用每条的 link 字段）
输出：
  data/jd/<id>.md       YAML front matter + 清洗后的正文（≤2 万字符）
  data/jd/index.json    {id: {status, sha, chars, fetched_at, via}}，原子替换

status 取值（见 adapters/sources/jd-fetch.md）：
  ok             抓到正文
  ok_ocr         正文是长图，已用 Apple Vision 本地 OCR 出文字
  image_only     正文是长图且 OCR 没出可用文字（--no-ocr 或 OCR 失败）
  login_required 需要登录/验证码/环境异常，浏览器兜底也没拿到
  gone           内容被删除或链接失效
  missing        抓不到（网络/解析失败）

抓取策略：先普通 HTTP（公众号文章、官网静态页基本都能拿到）；失败的条目攒起来
走一次 ego-browser（adapters/sources/jd-fetch.mjs，自建 TaskSpace 用完 finish）。
image_only 的条目按顺序下载正文图片（公众号 data-src，单篇 <=30 张），用 Apple
Vision 本地 OCR（zh-Hans+en；本机 accurate 档不支持中文时自动落 fast+rev3），
超长图按高度切块识别再拼接；图片只进 /tmp 临时目录，用完即删，不入仓库。
限速：同域名请求间隔 >=1s；并发 <=3。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import requests
import yaml
from bs4 import BeautifulSoup

REPO = os.environ.get("CAMPUS_REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CST = ZoneInfo("Asia/Shanghai")
MAX_CHARS = 20000
MIN_TEXT = 300            # 低于这个字数且图多 → image_only
RETRY_MISSING_DAYS = 3    # --incremental 下 missing 超过 N 天重试一次

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

GONE_MARKS = ("该内容已被发布者删除", "此内容已被发布者删除", "该内容已被删除",
              "链接已过期", "参数错误", "文章已被删除")
AUTH_MARKS = ("环境异常", "完成验证", "操作频繁", "访问过于频繁", "登录后查看",
              "请先登录", "验证中心")
# 正文容器候选，按优先级
CONTENT_CSS = ["#js_content", "article", ".article-content", ".article_content",
               "#content", ".content", ".detail-content", "main"]


def now_iso() -> str:
    return dt.datetime.now(CST).isoformat(timespec="seconds")


def load_announcements(path: str) -> dict[int, dict]:
    out = {}
    for line in open(path, encoding="utf-8"):
        if line.strip():
            r = json.loads(line)
            out[r["announcement_id"]] = r
    return out


def load_index(path: str) -> dict:
    if os.path.isfile(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {}


def atomic_write(path: str, text: str):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def clean_text(text: str) -> str:
    lines = [re.sub(r"[ \t\u00a0]+", " ", ln).strip() for ln in text.splitlines()]
    out, blank = [], False
    for ln in lines:
        if not ln:
            if not blank:
                out.append("")
            blank = True
        else:
            out.append(ln)
            blank = False
    return "\n".join(out).strip()


def extract_from_html(html: str, base_url: str = "") -> tuple[str, int, list[str]]:
    """返回 (正文, 图片数, 正文内图片URL列表)。优先已知容器，否则取文本量最大的块。"""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
        tag.decompose()
    node = None
    for css in CONTENT_CSS:
        node = soup.select_one(css)
        if node and len(node.get_text(strip=True)) > 200:
            break
        node = None
    if node is None:
        best, best_len = soup.body or soup, 0
        for el in soup.find_all(["div", "section", "article"]):
            ln = len(el.get_text(strip=True))
            if ln > best_len:
                best, best_len = el, ln
        node = best
    imgs, urls = [], []
    if node:
        for im in node.find_all("img"):
            u = im.get("data-src") or im.get("src") or ""
            if u.startswith("//"):
                u = "https:" + u
            if u.startswith("http") and u not in urls:
                urls.append(u)
            imgs.append(im)
    return clean_text(node.get_text("\n") if node else ""), len(imgs), urls[:30]


# ---------------------------------------------------------------------------
# Apple Vision OCR（本地，不联网）。本机实测：accurate 档不支持 zh-Hans，
# 自动探测支持语言后回落到 fast + revision 3。
# ---------------------------------------------------------------------------

_OCR = {"ready": None, "level": None}


def _ocr_setup() -> bool:
    if _OCR["ready"] is not None:
        return _OCR["ready"]
    try:
        import Vision  # noqa: F401
        langs = []
        for rev in (3, 2):
            try:
                r, _ = Vision.VNRecognizeTextRequest.supportedRecognitionLanguagesForTextRecognitionLevel_revision_error_(
                    1, rev, None)
                langs = [str(x) for x in (r or [])]
            except Exception:
                langs = []
            if "zh-Hans" in langs:
                break
        _OCR["level"] = 1 if "zh-Hans" in langs else 0   # accurate 不含中文 → fast
        _OCR["ready"] = True
    except Exception:
        _OCR["ready"] = False
    return _OCR["ready"]


def ocr_file(path: str) -> list[str]:
    """对单张图跑 Vision OCR，按阅读顺序（上→下，同排左→右）返回文字行。"""
    import Vision
    import Foundation
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(_OCR["level"])
    req.setRecognitionLanguages_(["zh-Hans", "en-US"])
    req.setUsesLanguageCorrection_(True)
    try:
        req.setRevision_(3)
    except Exception:
        pass
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(
        Foundation.NSURL.fileURLWithPath_(path), None)
    ok, _err = handler.performRequests_error_([req], None)
    if not ok:
        return []
    obs = sorted(req.results() or [],
                 key=lambda r: (-round(r.boundingBox().origin.y, 1), r.boundingBox().origin.x))
    return [o.topCandidates_(1)[0].string() for o in obs]


def ocr_image(path: str) -> list[str]:
    """超长图（>3200px）按 2800px 切块、重叠 120px，逐块识别后拼接去重。"""
    from PIL import Image
    im = Image.open(path)
    if im.height <= 3200:
        return ocr_file(path)
    lines, top = [], 0
    while top < im.height:
        box = im.crop((0, top, im.width, min(im.height, top + 2800)))
        cp = path + ".part%d.png" % top
        box.save(cp)
        part = ocr_file(cp)
        os.unlink(cp)
        for ln in part:
            if not lines or ln != lines[-1]:     # 重叠区相邻去重
                lines.append(ln)
        top += 2800 - 120
    return lines


def clean_ocr(lines: list[str]) -> str:
    out = []
    for ln in lines:
        ln = re.sub(r"\s+", " ", ln).strip()
        if ln and (not out or ln != out[-1]):
            out.append(ln)
    return "\n".join(out)


def download_and_ocr(img_urls: list[str], referer: str, limiter: RateLimiter) -> str:
    """按顺序下载正文图片到 /tmp 临时目录并 OCR，返回拼接正文；用完即删。"""
    if not img_urls or not _ocr_setup():
        return ""
    tmpdir = tempfile.mkdtemp(prefix="jdocr-")
    lines: list[str] = []
    try:
        for i, u in enumerate(img_urls[:30]):
            limiter.wait(domain_of(u))
            try:
                r = requests.get(u, headers={"User-Agent": UA, "Referer": referer}, timeout=30)
                if r.status_code != 200 or len(r.content) < 3000:
                    continue
                p = os.path.join(tmpdir, "%02d.img" % i)
                open(p, "wb").write(r.content)
                lines += ocr_image(p)
            except (requests.RequestException, OSError):
                continue
    finally:
        import shutil as _sh
        _sh.rmtree(tmpdir, ignore_errors=True)
    return clean_ocr(lines)


def classify(text: str, imgs: int, http_status: int | None, final_url: str) -> str:
    joined = text[:2000]
    if any(m in joined for m in GONE_MARKS):
        return "gone"
    if http_status in (401, 403) or any(m in joined for m in AUTH_MARKS):
        return "login_required"
    if len(text) < MIN_TEXT:
        return "image_only" if imgs >= 3 else "missing"
    return "ok"


class RateLimiter:
    """同域名请求间隔 >= interval 秒。"""

    def __init__(self, interval: float):
        self.interval = max(1.0, interval)
        self.last: dict[str, float] = {}
        self.lock = threading.Lock()

    def wait(self, domain: str):
        with self.lock:
            delta = self.last.get(domain, 0) + self.interval - time.time()
            if delta > 0:
                time.sleep(delta)
            self.last[domain] = time.time()


def domain_of(url: str) -> str:
    m = re.match(r"https?://([^/:]+)", url or "")
    return m.group(1).lower() if m else ""


def fetch_http(rec: dict, limiter: RateLimiter, timeout: int = 20) -> dict:
    """普通 HTTP 抓一篇，返回 {status, text, via, http}。"""
    url = rec.get("link") or ""
    if not url.startswith("http"):
        return {"status": "missing", "text": "", "via": "http", "http": None}
    limiter.wait(domain_of(url))
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
                         timeout=timeout, allow_redirects=True)
    except requests.RequestException as e:
        return {"status": "missing", "text": "", "via": "http", "http": None, "err": str(e)[:120]}
    html = r.text if r.encoding != "ISO-8859-1" else r.content.decode(r.apparent_encoding or "utf-8", "replace")
    text, imgs, img_urls = extract_from_html(html, str(r.url))
    return {"status": classify(text, imgs, r.status_code, str(r.url)),
            "text": text, "via": "http", "http": r.status_code, "imgs": imgs,
            "img_urls": img_urls}


def fetch_via_browser(jobs: list[dict], out_dir: str, interval: float) -> dict[int, dict]:
    """对 HTTP 失败的条目走 ego-browser。返回 {id: {status, text}}。"""
    script = os.path.join(REPO, "adapters", "sources", "jd-fetch.mjs")
    if not (os.path.isfile(script) and shutil_which("ego-browser")):
        return {}
    tmpdir = tempfile.mkdtemp(prefix="jdfetch-")
    cfg = {"jobs": [{"id": r["announcement_id"], "url": r.get("link") or ""} for r in jobs],
           "out_dir": tmpdir, "interval_ms": int(max(1.0, interval) * 1000)}
    src = "globalThis.__JDFETCH_CFG__=%s;\n%s" % (json.dumps(cfg, ensure_ascii=False),
                                                open(script, encoding="utf-8").read())
    try:
        subprocess.run(["ego-browser", "nodejs"], input=src, text=True,
                       capture_output=True, timeout=max(600, 45 * len(jobs)))
    except (subprocess.TimeoutExpired, OSError) as e:
        print("WARN: ego-browser 兜底失败：%s" % e, file=sys.stderr)
        return {}
    res_path = os.path.join(tmpdir, "result.json")
    if not os.path.isfile(res_path):
        return {}
    try:
        results = {str(x["id"]): x for x in json.load(open(res_path, encoding="utf-8")).get("results", [])}
    except (OSError, ValueError):
        return {}
    out = {}
    for r in jobs:
        aid = str(r["announcement_id"])
        ent = results.get(aid)
        if not ent:
            continue
        txt_path = os.path.join(tmpdir, "%s.txt" % aid)
        text = ""
        if os.path.isfile(txt_path):
            text = clean_text(open(txt_path, encoding="utf-8").read())
        status = ent.get("status") or classify(text, ent.get("imgs") or 0, ent.get("http"), "")
        if status == "ok" and len(text) < MIN_TEXT:
            status = "missing"
        out[r["announcement_id"]] = {"status": status, "text": text, "via": "browser",
                                     "img_urls": ent.get("img_urls") or []}
    return out


def shutil_which(name: str) -> str | None:
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def write_jd(out_dir: str, rec: dict, status: str, text: str, via: str, ocr_images: int = 0) -> dict:
    text = text[:MAX_CHARS]
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    fm = {"announcement_id": rec["announcement_id"], "company": rec.get("company") or "",
          "source_url": rec.get("link") or "", "fetched_at": now_iso(),
          "status": status, "content_sha256": sha, "chars": len(text), "via": via}
    if status == "ok_ocr":
        fm["ocr_images"] = ocr_images
        fm["ocr_chars"] = len(text)
    body = "---\n%s---\n\n%s\n" % (yaml.safe_dump(fm, allow_unicode=True, sort_keys=False), text)
    atomic_write(os.path.join(out_dir, "%s.md" % rec["announcement_id"]), body)
    return {"status": status, "sha": sha, "chars": len(text), "fetched_at": fm["fetched_at"], "via": via}


def main() -> int:
    ap = argparse.ArgumentParser(description="抓公告 JD → data/jd/<id>.md")
    ap.add_argument("--data", default=os.path.join(REPO, "data", "paperball", "announcements.jsonl"))
    ap.add_argument("--out-dir", default=os.path.join(REPO, "data", "jd"))
    ap.add_argument("--ids", default=None, help="只抓这些 announcement_id（逗号分隔）")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--since", default=None, help="只抓 published_at >= 该日期的公告")
    ap.add_argument("--incremental", action="store_true",
                    help="只抓没有文件的；status=missing 且超过 %d 天的重试一次" % RETRY_MISSING_DAYS)
    ap.add_argument("--dry-run", action="store_true", help="只列要抓的，不请求不写盘")
    ap.add_argument("--browser", choices=["auto", "always", "never"], default="auto",
                    help="HTTP 失败后是否走 ego-browser 兜底（默认 auto）")
    ap.add_argument("--concurrency", type=int, default=3, help="HTTP 并发，上限 3")
    ap.add_argument("--interval", type=float, default=1.0, help="同域名最小间隔秒数，下限 1")
    ap.add_argument("--ocr", dest="ocr", action="store_true", default=True,
                    help="image_only 走 Apple Vision 本地 OCR（默认开）")
    ap.add_argument("--no-ocr", dest="ocr", action="store_false")
    args = ap.parse_args()

    anns = load_announcements(args.data)
    index = load_index(os.path.join(args.out_dir, "index.json"))

    if args.ids:
        wanted = {int(x) for x in re.split(r"[,，\s]+", args.ids) if x.strip()}
        recs = [anns[i] for i in sorted(wanted) if i in anns]
    else:
        recs = list(anns.values())
        if args.since:
            recs = [r for r in recs if (r.get("published_at") or "") >= args.since]
        recs.sort(key=lambda r: r.get("published_at") or "", reverse=True)
    if args.incremental:
        today = dt.datetime.now(CST).date()
        keep = []
        for r in recs:
            aid = str(r["announcement_id"])
            ent = index.get(aid)
            has_file = os.path.isfile(os.path.join(args.out_dir, "%s.md" % aid))
            if not has_file:
                keep.append(r)
            elif ent and ent.get("status") == "missing":
                try:
                    last = dt.date.fromisoformat(str(ent.get("fetched_at", ""))[:10])
                except ValueError:
                    last = dt.date.min
                if (today - last).days >= RETRY_MISSING_DAYS:
                    keep.append(r)
            elif ent and ent.get("status") == "image_only" and args.ocr and not ent.get("ocr"):
                keep.append(r)          # 长图没跑过 OCR 的补跑一次，跑过（无论成败）不再自动重试
        recs = keep
    if args.limit:
        recs = recs[: args.limit]

    print("待抓 %d 条（%s）" % (len(recs), "incremental" if args.incremental else "全量/指定"), file=sys.stderr)
    if args.dry_run:
        for r in recs:
            print("%s\t%s\t%s" % (r["announcement_id"], r.get("company"), (r.get("link") or "")[:80]))
        return 0
    if not recs:
        return 0

    os.makedirs(args.out_dir, exist_ok=True)
    limiter = RateLimiter(args.interval)
    workers = max(1, min(3, args.concurrency))
    t0 = time.time()
    results: dict[int, dict] = {}
    done_n = 0

    def one(rec):
        return rec, fetch_http(rec, limiter)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for rec, res in ex.map(one, recs):
            results[rec["announcement_id"]] = res
            done_n += 1
            if done_n % 20 == 0:
                print("…%d/%d" % (done_n, len(recs)), file=sys.stderr)

    # 浏览器兜底：HTTP 没拿到正文的（missing/login_required，以及 HTML 里没图 URL 的 image_only
    # ——公众号"小说阅读器"式文章图片靠 JS 渲染，浏览器里能拿到 data-src）
    retry = [r for r in recs
             if (lambda x: x.get("status") in ("missing", "login_required")
                 or (x.get("status") == "image_only" and not x.get("img_urls")))
             (results.get(r["announcement_id"], {}))]
    if retry and args.browser in ("auto", "always"):
        print("HTTP 未拿到正文 %d 条，走 ego-browser 兜底…" % len(retry), file=sys.stderr)
        got = fetch_via_browser(retry, args.out_dir, args.interval)
        for aid, res in got.items():
            if res.get("status") == "ok" or results[aid]["status"] != "ok":
                results[aid] = res if res.get("status") == "ok" else {**results[aid], **res}

    # image_only → 下载正文图片走 Apple Vision OCR
    if args.ocr:
        ocred = 0
        for r in recs:
            res = results.get(r["announcement_id"])
            if not res or res.get("status") != "image_only" or not res.get("img_urls"):
                continue
            txt = download_and_ocr(res["img_urls"], r.get("link") or "", limiter)
            res["ocr"] = True
            if len(txt) >= 150:
                res.update(status="ok_ocr", text=txt, ocr_images=len(res["img_urls"]),
                           via=res.get("via", "http") + "+ocr")
                ocred += 1
        if ocred:
            print("OCR 救回 %d 条长图公告" % ocred, file=sys.stderr)

    counts: dict[str, int] = {}
    for r in recs:
        res = results.get(r["announcement_id"], {"status": "missing", "text": "", "via": "http"})
        ent = write_jd(args.out_dir, r, res["status"], res.get("text") or "", res.get("via", "http"),
                       res.get("ocr_images") or 0)
        if res.get("ocr"):
            ent["ocr"] = res["status"]       # ok_ocr | image_only（OCR 失败也记，避免重复跑）
        index[str(r["announcement_id"])] = ent
        counts[res["status"]] = counts.get(res["status"], 0) + 1

    atomic_write(os.path.join(args.out_dir, "index.json"),
                 json.dumps(index, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    chars = [index[str(r["announcement_id"])]["chars"] for r in recs
             if index[str(r["announcement_id"])]["status"] in ("ok", "ok_ocr")]
    print("完成 %d 条 · %s · 平均 %d 字 · 耗时 %.1fs" % (
        len(recs), " · ".join("%s %d" % kv for kv in sorted(counts.items())),
        sum(chars) / max(1, len(chars)), time.time() - t0), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
