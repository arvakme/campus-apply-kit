#!/usr/bin/env python3
"""fake_tg.py · 假 Telegram Bot API，relay 测试用。

用法：python3 fake_tg.py --port N --dir <scratch>
- POST /bot<token>/<method> → 记录到 <dir>/requests.jsonl（method/fields/files/mid），
  回 {"ok":true,"result":{"message_id":N}}。
- GET /<any> → 也记录（兼作假 bark / 探针）。
"""
import argparse
import json
import os
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIR = ""
LOCK = threading.Lock()
MID = [1000]


def _p(name):
    return os.path.join(DIR, name)


def _append(path, obj):
    with LOCK, open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _parse_multipart(raw, ctype):
    fields, files = {}, {}
    m = re.search(r"boundary=([^\s;]+)", ctype)
    if not m:
        return fields, files
    boundary = m.group(1).strip().strip('"').encode()
    for part in raw.split(b"--" + boundary):
        if b"Content-Disposition" not in part:
            continue
        head, _, body = part.partition(b"\r\n\r\n")
        nm = re.search(rb'name="([^"]+)"', head)
        if not nm:
            continue
        name = nm.group(1).decode()
        if b"filename=" in head:
            files[name] = {"size": len(body.rstrip(b"\r\n-")), "body": body.rstrip(b"\r\n-")[:32]}
        else:
            fields[name] = body.rstrip(b"\r\n").decode("utf-8", "replace")
    return fields, files


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        m = re.match(r"/bot[^/]+/(\w+)", self.path or "")
        method = m.group(1) if m else "?"
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        ctype = self.headers.get("Content-Type", "")
        if ctype.startswith("multipart/form-data"):
            fields, files = _parse_multipart(raw, ctype)
            files = {k: v["size"] for k, v in files.items()}
        elif "application/json" in ctype:
            try:
                fields = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                fields = {"_raw": raw[:200].decode("utf-8", "replace")}
            files = {}
        else:
            fields = {k: v[0] for k, v in
                      urllib.parse.parse_qs(raw.decode("utf-8", "replace")).items()}
            files = {}
        with LOCK:
            MID[0] += 1
            mid = MID[0]
        _append(_p("requests.jsonl"),
                {"method": method, "fields": fields, "files": files, "mid": mid})
        self._reply(200, {"ok": True, "result": {"message_id": mid}})

    def do_GET(self):
        _append(_p("requests.jsonl"),
                {"method": "GET", "fields": {"path": self.path}, "files": {}})
        self._reply(200, {"ok": True})


def main():
    global DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--dir", required=True)
    args = ap.parse_args()
    DIR = os.path.abspath(args.dir)
    ThreadingHTTPServer(("127.0.0.1", args.port), H).serve_forever()


if __name__ == "__main__":
    main()
