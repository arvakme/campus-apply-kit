"""campus-apply web：看板 / 问题库 / 前置条件 / handoff。

标准库 ThreadingHTTPServer，除 pyyaml 外零依赖。实例目录取 $CAMPUS_INSTANCE，容器里默认 /instance。

    python3 web/app/server.py [--host 0.0.0.0] [--port 8787] [--instance DIR]

只监听容器内端口，对外暴露交给宿主机（container -p 127.0.0.1:8787 + tailscale serve）。
本服务没有登录，安全边界是 tailnet，千万不要直接绑到公网。
"""

import argparse
import datetime as dt
import json
import mimetypes
import os
import re
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import agenda  # noqa: E402
import bank  # noqa: E402
import board  # noqa: E402
import dashboard  # noqa: E402
import handoffs  # noqa: E402
import pages  # noqa: E402
import setup_checks  # noqa: E402

TEMPLATE = os.path.join(HERE, "templates", "dashboard.html")
STATIC_DIR = os.path.join(HERE, "static")
STATIC_EXT = {
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".html": "text/html; charset=utf-8",
}
RAW_PREFIXES = ("log/", "shots/")
RAW_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".md", ".txt"}
MAX_BODY = 1024 * 1024

INSTANCE = os.environ.get("CAMPUS_INSTANCE") or "/instance"


class DashboardCache:
    """按源文件 (mtime, size) + 日期 + 模板 mtime + 未处理 handoff 数缓存整页 HTML。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.key = None
        self.html = None
        self.stats = None

    def _key(self, root):
        sig = []
        for p in dashboard.source_paths(root) + [TEMPLATE]:
            try:
                st = os.stat(p)
                sig.append((p, st.st_mtime_ns, st.st_size))
            except OSError:
                sig.append((p, None, None))
        return (tuple(sig), dt.date.today().isoformat(), handoffs.open_count(root))

    def get(self, root):
        with self.lock:
            key = self._key(root)
            if key != self.key:
                data, stats = dashboard.build(root)
                data["openHandoffs"] = key[2]
                tpl = open(TEMPLATE, encoding="utf-8").read()
                self.html = dashboard.render(data, tpl).encode("utf-8")
                self.stats = stats
                self.key = key
            return self.html, self.stats


CACHE = DashboardCache()


class BoardCache:
    """榜单：实时构建按源文件 mtime 缓存；指定日快照不可变，按 (mtime,size) 缓存。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.key = None
        self.payload = None        # (data, info)
        self.snaps = {}            # date -> (sig, payload)

    def _key(self, root):
        sig = []
        for p in board.source_paths(root):
            try:
                st = os.stat(p)
                sig.append((p, st.st_mtime_ns, st.st_size))
            except OSError:
                sig.append((p, None, None))
        return (tuple(sig), dt.date.today().isoformat(), handoffs.open_count(root))

    def get(self, root, date=None):
        """返回 (payload 或 None, info)。date 非空时只读该日快照。"""
        if date:
            path = board.snapshot_path(root, date)
            try:
                st = os.stat(path)
                sig = (st.st_mtime_ns, st.st_size)
            except OSError:
                return None, {"mode": "missing", "missing": date, "dates": board.list_snapshots(root)}
            with self.lock:
                ent = self.snaps.get(date)
                if not ent or ent[0] != sig:
                    ent = (sig, board.load_snapshot(root, date))
                    self.snaps[date] = ent
                return ent[1], {"mode": "snapshot", "date": date, "dates": board.list_snapshots(root)}
        with self.lock:
            key = self._key(root)
            if key != self.key:
                self.payload = board.current(root)
                self.key = key
            return self.payload

    def pair_for_diff(self, root, date=None, base=None):
        """diff 用的两天 payload：(date 或最新) 与 (base 或次新)。"""
        dates = board.list_snapshots(root)
        cur_d = date if date else (dates[-1] if dates else None)
        base_d = base or None
        if cur_d and not base_d and cur_d in dates and dates.index(cur_d) > 0:
            base_d = dates[dates.index(cur_d) - 1]
        cur = self.get(root, cur_d)[0] if cur_d else None
        prev = self.get(root, base_d)[0] if base_d else None
        return cur, prev, {"dates": dates, "date": cur_d, "base": base_d}


BOARD = BoardCache()


class Handler(BaseHTTPRequestHandler):
    server_version = "campus-web/1"
    protocol_version = "HTTP/1.1"

    # ---- 输出 ----
    def _send(self, status, body, ctype="text/html; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _read_json(self):
        if "json" not in (self.headers.get("Content-Type") or ""):
            raise ValueError("需要 Content-Type: application/json")
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("请求体过大")
        raw = self.rfile.read(n) if n else b""
        return json.loads(raw.decode("utf-8") or "{}")

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (dt.datetime.now().strftime("%H:%M:%S"), fmt % args))

    # ---- 路由 ----
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        try:
            if path == "/healthz":
                return self._json(200, {"ok": True, "instance": INSTANCE, "instance_exists": os.path.isdir(INSTANCE)})
            if path == "/":
                html, _ = CACHE.get(INSTANCE)
                return self._send(200, html)
            if path == "/api/dashboard/stats":
                _, stats = CACHE.get(INSTANCE)
                return self._json(200, stats)
            if path == "/board":
                q = parse_qs(urlsplit(self.path).query)
                date = (q.get("date") or [""])[0]
                payload, info = BOARD.get(INSTANCE, date or None)
                if payload is None:
                    return self._send(200, board.missing_page(INSTANCE, info, date))
                return self._send(200, board.page(payload, INSTANCE))
            if path == "/api/board":
                q = parse_qs(urlsplit(self.path).query)
                date = (q.get("date") or [""])[0]
                payload, info = BOARD.get(INSTANCE, date or None)
                if payload is None:
                    return self._json(404, {"error": "没有榜单数据", "dates": info.get("dates") or []})
                return self._json(200, payload)
            if path == "/board.csv":
                q = parse_qs(urlsplit(self.path).query)
                date = (q.get("date") or [""])[0]
                payload, _ = BOARD.get(INSTANCE, date or None)
                if payload is None:
                    return self._send(404, "not found", "text/plain; charset=utf-8")
                return self._send(200, board.to_csv(payload), "text/csv; charset=utf-8",
                                  {"Content-Disposition": 'attachment; filename="board-%s.csv"'
                                   % (payload.get("asOf") or "export")})
            if path == "/board/diff":
                q = parse_qs(urlsplit(self.path).query)
                date = (q.get("date") or [""])[0]
                base = (q.get("base") or [""])[0]
                cur, prev, info = BOARD.pair_for_diff(INSTANCE, date or None, base or None)
                return self._send(200, board.diff_page(INSTANCE, cur, prev, info))
            if path == "/agenda":
                return self._send(200, agenda.page(INSTANCE))
            if path == "/bank":
                doc, version = bank.load(self._bank_path())
                return self._send(200, pages.bank_page(INSTANCE, doc, version))
            if path == "/api/bank":
                doc, version = bank.load(self._bank_path())
                view = bank.public_view(doc)
                view["version"] = version
                return self._json(200, view)
            if path == "/setup":
                host, note = setup_checks.host_health(INSTANCE)
                mats, mats_ok = setup_checks.materials_checks(INSTANCE)
                return self._send(200, pages.setup_page(INSTANCE, host, note,
                                                        setup_checks.instance_checks(INSTANCE), mats, mats_ok))
            if path == "/handoffs":
                return self._send(200, pages.handoffs_page(INSTANCE, handoffs.list_all(INSTANCE)))
            m = re.fullmatch(r"/handoff/([^/]+)", path)
            if m:
                rec = handoffs.load(INSTANCE, m.group(1)) if handoffs.ID_RE.match(m.group(1)) else None
                if rec is None:
                    return self._send(404, pages.shell("没找到", "/handoffs",
                                                       '<h1>没找到这条 handoff</h1><p class="sub"><a href="/handoffs">返回列表</a></p>',
                                                       INSTANCE))
                return self._send(200, pages.handoff_page(INSTANCE, rec))
            if path.startswith("/raw/"):
                return self._raw(path[len("/raw/"):])
            if path == "/sw.js":
                return self._static("sw.js")
            if path == "/manifest.webmanifest":
                return self._static("manifest.webmanifest")
            if path == "/apple-touch-icon.png":
                return self._static("apple-touch-icon.png")
            if path == "/favicon.ico":
                return self._static("icon.svg")       # PNG 图标被 .gitignore 挡着进不了仓库，统一用 SVG
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            return self._send(404, "not found", "text/plain; charset=utf-8")
        except (bank.BankError, ValueError) as exc:
            return self._json(500, {"error": str(exc)})

    def do_PUT(self):
        path = urlsplit(self.path).path
        if path != "/api/bank":
            return self._json(405, {"error": "method not allowed"})
        try:
            body = self._read_json()
            version = body.get("version")
            updates = body.get("updates")
            if not isinstance(version, str) or not isinstance(updates, list):
                return self._json(400, {"error": "需要 {version: str, updates: [...]}"})
            new = bank.mutate(self._bank_path(), lambda d: bank.apply_updates(d, updates, source="网页"),
                              expected_version=version)
            return self._json(200, {"ok": True, "version": new})
        except bank.Conflict as exc:
            return self._json(409, {"error": "问题库已被修改，请基于最新版本重试", "version": exc.current})
        except (bank.BankError, ValueError) as exc:
            return self._json(400, {"error": str(exc)})

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == "/api/agenda/done":
            try:
                body = self._read_json()
                rec = agenda.mark_done(INSTANCE, str(body.get("id") or ""), bool(body.get("done", True)))
            except (ValueError, OSError, TimeoutError) as exc:
                return self._json(400, {"error": str(exc)})
            return self._json(200, {"ok": True, "id": rec["id"], "status": rec["status"]})
        m = re.fullmatch(r"/api/handoff/([^/]+)/resolve", path)
        if not m:
            return self._json(405, {"error": "method not allowed"})
        try:
            self._read_json()
            rec = handoffs.resolve(INSTANCE, m.group(1))
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        if rec is None:
            return self._json(404, {"error": "没有这条 handoff"})
        return self._json(200, {"ok": True, "resolved_at": rec["resolved_at"]})

    # ---- 工具 ----
    def _bank_path(self):
        return os.path.join(INSTANCE, bank.YAML_NAME)

    def _static(self, name):
        """只读暴露 web/app/static/ 下的文件（manifest、sw.js、图标、离线页）。文件名白名单防遍历。"""
        ext = os.path.splitext(name)[1].lower()
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name) or ext not in STATIC_EXT:
            return self._send(404, "not found", "text/plain; charset=utf-8")
        full = os.path.join(STATIC_DIR, name)
        if not os.path.isfile(full):
            return self._send(404, "not found", "text/plain; charset=utf-8")
        with open(full, "rb") as fh:
            return self._send(200, fh.read(), STATIC_EXT[ext])

    def _raw(self, rel):
        """只读暴露 log/ 与 shots/ 下的截图和 md，其他实例文件（accounts.md 等）一律 404。"""
        rel = rel.lstrip("/")
        ext = os.path.splitext(rel)[1].lower()
        root = os.path.realpath(INSTANCE)
        full = os.path.realpath(os.path.join(root, rel))
        inside = full.startswith(root + os.sep) and os.path.relpath(full, root).replace(os.sep, "/").startswith(RAW_PREFIXES)
        if not inside or ext not in RAW_EXT or not os.path.isfile(full):
            return self._send(404, "not found", "text/plain; charset=utf-8")
        ctype = "text/plain; charset=utf-8" if ext in (".md", ".txt") else (mimetypes.guess_type(full)[0] or "application/octet-stream")
        with open(full, "rb") as fh:
            data = fh.read()
        extra = {"Cache-Control": "private, max-age=60"} if ctype.startswith("image/") else None
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", (extra or {}).get("Cache-Control", "no-store"))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)


def main():
    global INSTANCE
    ap = argparse.ArgumentParser(description="campus-apply web")
    ap.add_argument("--host", default=os.environ.get("CAMPUS_WEB_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("CAMPUS_WEB_PORT", "8787")))
    ap.add_argument("--instance", default=INSTANCE)
    args = ap.parse_args()
    INSTANCE = os.path.abspath(args.instance)
    if not os.path.isdir(INSTANCE):
        print("WARN: 实例目录不存在：%s（页面会显示缺失占位）" % INSTANCE, file=sys.stderr)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print("campus-web 监听 http://%s:%d · 实例 %s" % (args.host, args.port, INSTANCE), file=sys.stderr, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
