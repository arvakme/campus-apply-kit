"""Telegram 通道：Bot API 客户端 + 出站推送 + 回复映射（tg-map）。

notify.py（多通道推送）和 host/tgbot.py（双向 bot）共用这一份实现：

- TGClient：getUpdates / sendMessage / sendPhoto / sendDocument / answerCallbackQuery /
  editMessageText / editMessageReplyMarkup。支持 HTTP 代理、超时、429 按 retry_after
  退避、网络错误重试。token 只经 bot_token_cmd 取，不打印、日志里打码。
- push_*：各类事件的 Telegram 呈现——handoff 发图片+按钮、review-wait 发字段表+按钮、
  其余纯文本。按钮与回复的 inbound 处理在 tgbot.py，本模块只负责发。
- tg-map：state/tg-map.json 记录 bot 发出的 message_id → job/handoff（带 mkdir 锁，
  同 web/app/bank.py 的做法），tgbot.py 用它把"回复这条消息"映射回作业。

配置（$CAMPUS_INSTANCE/notify.yaml，契约 docs/contracts.md §8.5 与 §10）：

    channels: [telegram, bark]
    telegram:
      mode: direct                        # direct（默认旧行为）| relay（共享 bot 中转 §10）
      bot_token_cmd: security find-generic-password -s campus-apply-telegram -w
      allowed_chat_ids: [123456789]
      proxy: http://127.0.0.1:6152          # 可选；launchd 环境没有代理变量，必须显式配
      api_base: https://api.telegram.org    # 可选；测试用假 API 时覆盖
      # relay 段由 `host/tgbot.py pair` 写入；密钥在钥匙串 campus-apply-relay：
      # relay: {base: https://tg.example.com, instance_id: i-…, key_cmd: …,
      #         nickname: 阿伟, rank: true, chat_id: 123456789}
"""

import datetime as dt
import hashlib
import hmac
import json
import urllib.parse
import os
import shlex
import subprocess
import tempfile
import time
import uuid

API_BASE = "https://api.telegram.org"
RELAY_BASE_DEFAULT = os.environ.get("CAMPUS_RELAY_BASE", "")   # 公开版没有默认的共享 Worker：自己部署后用 --relay-base 或这个环境变量
MSG_LIMIT = 3800          # Telegram sendMessage 上限 4096，留余量
CAPTION_LIMIT = 900       # 图片/文件 caption 上限 1024
MAP_KEEP_DAYS = 45
MAP_KEEP_MAX = 2000


class TGError(Exception):
    """fatal=True 表示重试无意义（token 错、参数错），调用方别再重试。"""

    def __init__(self, msg, fatal=False):
        super().__init__(msg)
        self.fatal = fatal


def now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def mask(s):
    s = str(s or "")
    return s if len(s) <= 8 else "%s…%s" % (s[:4], s[-4:])


def _iso_ts(s):
    try:
        return dt.datetime.fromisoformat(str(s)).timestamp()
    except (ValueError, TypeError):
        return 0.0


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

def tg_cfg(cfg):
    """notify.yaml 的 telegram 段；没配返回 {}。"""
    return cfg.get("telegram") or {}


def get_token(cfg):
    """经 bot_token_cmd（如 security find-generic-password）取 token，不打印。"""
    cmd = str(tg_cfg(cfg).get("bot_token_cmd") or "").strip()
    if not cmd:
        raise TGError("notify.yaml 的 telegram.bot_token_cmd 没配", fatal=True)
    try:
        out = subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TGError("bot_token_cmd 执行失败：%s" % exc)
    token = (out.stdout or "").strip()
    if out.returncode != 0 or not token:
        raise TGError("bot_token_cmd 没取到 token：%s"
                      % ((out.stderr or "").strip() or cmd))
    return token


def chat_ids(cfg):
    out = []
    for x in tg_cfg(cfg).get("allowed_chat_ids") or []:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            pass
    return out


def describe(cfg):
    """dry-run 展示用：尝试取 token 并打码，取不到只回显配置，不算错误。"""
    t = tg_cfg(cfg)
    if tg_mode(cfg) == "relay":
        rc = relay_cfg(cfg)
        try:
            key = mask(relay_key(cfg))
        except TGError:
            key = "<密钥取不到，先 pair>"
        return "mode=relay base=%s instance=%s chat_ids=%s key=%s" % (
            rc.get("base") or RELAY_BASE_DEFAULT, rc.get("instance_id") or "<未配对>",
            chat_ids(cfg) or "[]（配对后自动写入）", key)
    try:
        token = mask(get_token(cfg))
    except TGError:
        token = "<bot_token_cmd 取不到>"
    return "api_base=%s chat_ids=%s proxy=%s token=%s" % (
        t.get("api_base") or API_BASE, chat_ids(cfg) or "[]（未配，消息发不出去）",
        t.get("proxy") or "无", token)


# --------------------------------------------------------------------------
# mkdir 锁与 state 文件（tg-map / commands.jsonl 共用）
# --------------------------------------------------------------------------

class DirLock:
    """mkdir 在 virtiofs 上原子而 fcntl.flock 穿不过 Apple container 挂载——同 bank.py。"""

    STALE = 30
    WAIT = 10

    def __init__(self, path):
        self.dir = path + ".lockdir"

    def __enter__(self):
        deadline = time.time() + self.WAIT
        while True:
            try:
                os.mkdir(self.dir)
                return self
            except FileExistsError:
                try:
                    if time.time() - os.stat(self.dir).st_mtime > self.STALE:
                        os.rmdir(self.dir)
                        continue
                except OSError:
                    continue
                if time.time() > deadline:
                    raise TGError("状态文件被占用超过 %d 秒：%s" % (self.WAIT, self.dir))
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            os.rmdir(self.dir)
        except OSError:
            pass


def atomic_write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".%s." % os.path.basename(path), suffix=".tmp",
                               dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _map_path(instance):
    return os.path.join(instance, "state", "tg-map.json")


def _map_read(instance):
    try:
        m = json.load(open(_map_path(instance), encoding="utf-8"))
        return m if isinstance(m, dict) else {}
    except (OSError, ValueError):
        return {}


def map_put(instance, chat_id, message_id, info):
    """记录 bot 发出的消息 → 作业/handoff。用户回复该消息时 tgbot.py 查回作业 id。"""
    path = _map_path(instance)
    with DirLock(path):
        m = _map_read(instance)
        m["%s:%s" % (chat_id, message_id)] = dict(info, created_at=now_iso())
        cutoff = time.time() - MAP_KEEP_DAYS * 86400
        items = [(k, v) for k, v in m.items() if _iso_ts(v.get("created_at")) > cutoff]
        items.sort(key=lambda kv: kv[1].get("created_at") or "")
        atomic_write_json(path, dict(items[-MAP_KEEP_MAX:]))


def map_get(instance, chat_id, message_id):
    if message_id is None:
        return None
    return _map_read(instance).get("%s:%s" % (chat_id, message_id))


def map_find(instance, **match):
    """按字段找映射，返回 [(chat_id, message_id, info), ...]（handoff-update 定位要改的消息）。"""
    out = []
    for k, v in _map_read(instance).items():
        if all(v.get(key) == val for key, val in match.items()):
            cid, _, mid = k.partition(":")
            try:
                out.append((int(cid), int(mid), v))
            except (TypeError, ValueError):
                continue
    return out


# --------------------------------------------------------------------------
# Bot API 客户端
# --------------------------------------------------------------------------

class TGClient:
    """代理、超时、429 按 retry_after 退避、网络错误重试。fatal 错误直接抛。"""

    def __init__(self, token, api_base=API_BASE, proxy=None, retries=4,
                 sleep=time.sleep, on_log=None):
        import httpx
        self.token = token
        self.api_base = (api_base or API_BASE).rstrip("/")
        self.retries = retries
        self.sleep = sleep
        self.on_log = on_log or (lambda *a, **k: None)
        kwargs = {"timeout": httpx.Timeout(45.0, connect=10.0)}
        if proxy:
            kwargs["transport"] = httpx.HTTPTransport(proxy=proxy)
        self._http = httpx.Client(**kwargs)

    def _clean(self, s):
        return str(s).replace(self.token, "***")

    def api(self, method, data=None, files=None, timeout=None):
        url = "%s/bot%s/%s" % (self.api_base, self.token, method)
        last = "unknown error"
        for attempt in range(1, self.retries + 2):
            try:
                resp = self._http.post(url, data=data or {}, files=files,
                                       timeout=timeout or 45.0)
            except Exception as exc:                    # httpx.HTTPError 及底层网络错
                last = "network %s" % self._clean(exc)
            else:
                if resp.status_code == 200:
                    try:
                        payload = resp.json()
                    except ValueError:
                        last = "非 JSON 响应：%s" % resp.text[:200]
                    else:
                        if payload.get("ok"):
                            return payload.get("result")
                        raise TGError("%s → %s" % (
                            method, self._clean(payload.get("description") or payload)),
                            fatal=True)
                elif resp.status_code == 429:
                    ra = 5
                    try:
                        ra = int(resp.json().get("parameters", {}).get("retry_after") or 5)
                    except (ValueError, AttributeError):
                        pass
                    self.on_log("429", method=method, retry_after=ra)
                    self.sleep(min(ra, 60))
                    continue
                elif 400 <= resp.status_code < 500:
                    raise TGError("%s → HTTP %s %s" % (
                        method, resp.status_code, self._clean(resp.text[:200])), fatal=True)
                else:
                    last = "HTTP %s %s" % (resp.status_code, self._clean(resp.text[:120]))
            if attempt <= self.retries:
                self.sleep(min(2 ** (attempt - 1), 30))
        raise TGError("%s 失败（%d 次尝试）：%s" % (method, self.retries + 1, last))

    # ---- 具体 API ----

    def get_updates(self, offset=None, timeout=25, limit=100):
        data = {"timeout": timeout, "limit": limit,
                "allowed_updates": json.dumps(["message", "callback_query"])}
        if offset is not None:
            data["offset"] = offset
        return self.api("getUpdates", data=data, timeout=timeout + 20) or []

    @staticmethod
    def markup(rows):
        """rows: [[(按钮文本, callback_data), ...], ...]"""
        return json.dumps({"inline_keyboard": [
            [{"text": t, "callback_data": d} for t, d in row] for row in rows]},
            ensure_ascii=False)

    def send_message(self, chat_id, text, buttons=None, reply_to=None):
        data = {"chat_id": chat_id,
                "text": str(text or "（空消息）")[:MSG_LIMIT]}
        if reply_to:
            data["reply_parameters"] = json.dumps({"message_id": reply_to, "allow_sending_without_reply": True})
        if buttons is not None:
            data["reply_markup"] = self.markup(buttons)
        return self.api("sendMessage", data=data)

    def send_photo(self, chat_id, path, caption="", buttons=None):
        data = {"chat_id": chat_id, "caption": caption[:CAPTION_LIMIT]}
        if buttons is not None:
            data["reply_markup"] = self.markup(buttons)
        mime = "image/jpeg" if path.lower().endswith((".jpg", ".jpeg")) else "image/png"
        with open(path, "rb") as fh:
            files = {"photo": (os.path.basename(path), fh.read(), mime)}
            return self.api("sendPhoto", data=data, files=files)

    def send_document(self, chat_id, filename, content, caption="", buttons=None):
        data = {"chat_id": chat_id, "caption": caption[:CAPTION_LIMIT]}
        if buttons is not None:
            data["reply_markup"] = self.markup(buttons)
        if isinstance(content, str):
            content = content.encode("utf-8")
        return self.api("sendDocument", data=data,
                        files={"document": (filename, content)})

    def answer_callback(self, callback_id, text=""):
        data = {"callback_query_id": callback_id}
        if text:
            data["text"] = text[:200]
        return self.api("answerCallbackQuery", data=data)

    def edit_text(self, chat_id, message_id, text, buttons=None):
        data = {"chat_id": chat_id, "message_id": message_id,
                "text": str(text or "")[:MSG_LIMIT]}
        if buttons is not None:
            data["reply_markup"] = self.markup(buttons)
        return self.api("editMessageText", data=data)

    def edit_markup(self, chat_id, message_id, buttons=None):
        """buttons=None/[] 时清空按钮（用户已点过，防止重复点）。"""
        return self.api("editMessageReplyMarkup", data={
            "chat_id": chat_id, "message_id": message_id,
            "reply_markup": self.markup(buttons or [])})

    def edit_caption(self, chat_id, message_id, caption, buttons=None):
        """改媒体消息的说明（sendPhoto/sendDocument 发的）；buttons=[] 顺手清按钮。"""
        data = {"chat_id": chat_id, "message_id": message_id,
                "caption": str(caption or "")[:CAPTION_LIMIT]}
        if buttons is not None:
            data["reply_markup"] = self.markup(buttons)
        return self.api("editMessageCaption", data=data)

    def edit_media(self, chat_id, message_id, path, caption="", buttons=None):
        """editMessageMedia 原地换图（§8.6：二维码过期刷新不换消息）。"""
        media = {"type": "photo", "media": "attach://photo"}
        if caption:
            media["caption"] = str(caption)[:CAPTION_LIMIT]
        data = {"chat_id": chat_id, "message_id": message_id,
                "media": json.dumps(media, ensure_ascii=False)}
        if buttons is not None:
            data["reply_markup"] = self.markup(buttons)
        mime = "image/jpeg" if path.lower().endswith((".jpg", ".jpeg")) else "image/png"
        with open(path, "rb") as fh:
            files = {"photo": (os.path.basename(path), fh.read(), mime)}
            return self.api("editMessageMedia", data=data, files=files)


def make_client(cfg, api_base=None, inst_dir=None, **kw):
    """出站客户端：direct → TGClient 直连 api.telegram.org；relay → RelayClient 经 /v1/send。"""
    if tg_mode(cfg) == "relay":
        return RelayClient(make_relay_http(cfg, api_base=api_base, inst_dir=inst_dir, **kw))
    t = tg_cfg(cfg)
    return TGClient(get_token(cfg),
                    api_base=api_base or t.get("api_base") or API_BASE,
                    proxy=t.get("proxy"), **kw)


# --------------------------------------------------------------------------
# relay 模式（契约 docs/contracts.md §10）：Mac ↔ Cloudflare Worker
# --------------------------------------------------------------------------

def tg_mode(cfg):
    """telegram.mode: relay|direct。未显式配置时：有 relay 段 → relay，否则 direct（旧配置不回归）。"""
    t = tg_cfg(cfg)
    m = str(t.get("mode") or "").strip().lower()
    if m in ("relay", "direct"):
        return m
    if t.get("relay"):
        return "relay"
    return "direct"


def relay_cfg(cfg):
    return dict(tg_cfg(cfg).get("relay") or {})


def relay_key(cfg, inst_dir=None):
    """实例密钥（32 字节 hex）解析顺序：CAMPUS_RELAY_KEY 环境变量 → relay.key_cmd
    （pair 写入，默认钥匙串 campus-apply-relay）→ relay.key_file / state/relay.key。"""
    k = os.environ.get("CAMPUS_RELAY_KEY")
    if k and k.strip():
        return k.strip()
    rc = relay_cfg(cfg)
    iid = str(rc.get("instance_id") or "")
    cmd = str(rc.get("key_cmd") or "").strip()
    if not cmd and iid:
        cmd = "security find-generic-password -s campus-apply-relay -a %s -w" % iid
    if cmd:
        try:
            out = subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=15)
            tok = (out.stdout or "").strip()
            if out.returncode == 0 and tok:
                return tok
        except (OSError, subprocess.TimeoutExpired):
            pass
    for p in (str(rc.get("key_file") or ""),
              os.path.join(inst_dir or os.environ.get("CAMPUS_INSTANCE") or "",
                           "state", "relay.key")):
        if p and os.path.isfile(p):
            return open(p, encoding="utf-8").read().strip()
    raise TGError("relay 密钥取不到：跑 `tgbot.py pair` 生成，或配 telegram.relay.key_cmd / "
                  "环境变量 CAMPUS_RELAY_KEY", fatal=True)


def _encode_multipart(fields, files):
    """手工拼 multipart/form-data——签名要 sha256(原始 body)，得先拿到字节串。"""
    boundary = "----campusrelay" + uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                      % (boundary, k, v)).encode("utf-8"))
    for k, spec in (files or {}).items():
        fname, content = spec[0], spec[1]
        mime = spec[2] if len(spec) > 2 else "application/octet-stream"
        if hasattr(content, "read"):
            content = content.read()
        if isinstance(content, str):
            content = content.encode("utf-8")
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; "
                      "filename=\"%s\"\r\nContent-Type: %s\r\n\r\n"
                      % (boundary, k, fname, mime)).encode("utf-8"))
        parts.append(content)
        parts.append(b"\r\n")
    parts.append(("--%s--\r\n" % boundary).encode("ascii"))
    return b"".join(parts), "multipart/form-data; boundary=%s" % boundary


class RelayHTTP:
    """Mac ↔ Worker 签名请求核心（§10.1）：

    X-Instance + X-Ts(±60s) + X-Nonce(10min 不重) +
    X-Sig = HMAC-SHA256(key, "METHOD|path?query|ts|nonce|sha256(body)")。
    tgbot.py（pull/ack/pair）与 report.py（rank/report）共用。
    """

    def __init__(self, base, instance_id, key_hex, proxy=None, retries=3,
                 sleep=time.sleep, on_log=None):
        import httpx
        self.base = (base or RELAY_BASE_DEFAULT).rstrip("/")
        self.iid = instance_id
        kh = str(key_hex or "").strip()
        self.key = bytes.fromhex(kh) if len(kh) == 64 else kh.encode("utf-8")
        self.retries = retries
        self.sleep = sleep
        self.on_log = on_log or (lambda *a, **k: None)
        kwargs = {"timeout": httpx.Timeout(30.0, connect=10.0)}
        if proxy:
            kwargs["transport"] = httpx.HTTPTransport(proxy=proxy)
        self._http = httpx.Client(**kwargs)

    def _headers(self, method, path, body):
        ts = str(int(time.time()))
        nonce = uuid.uuid4().hex[:24]
        msg = "%s|%s|%s|%s|%s" % (method.upper(), path, ts, nonce,
                                  hashlib.sha256(body or b"").hexdigest())
        sig = hmac.new(self.key, msg.encode("utf-8"), hashlib.sha256).hexdigest()
        return {"X-Instance": self.iid, "X-Ts": ts, "X-Nonce": nonce, "X-Sig": sig}

    def request(self, method, path, body=b"", content_type=None, timeout=30.0):
        """签名发一次，网络错误重试。返回 {"status","json","text"}；网络耗尽抛 TGError。"""
        if isinstance(body, str):
            body = body.encode("utf-8")
        url = self.base + path
        headers = self._headers(method, path, body)
        if content_type:
            headers["Content-Type"] = content_type
        last = "unknown"
        for attempt in range(1, self.retries + 2):
            try:
                resp = self._http.request(method, url, content=body or None,
                                          headers=headers, timeout=timeout)
            except Exception as exc:
                last = "network %s" % exc
            else:
                try:
                    j = resp.json()
                except ValueError:
                    j = None
                return {"status": resp.status_code, "json": j, "text": resp.text}
            if attempt <= self.retries:
                self.sleep(min(2 ** (attempt - 1), 20))
        raise TGError("relay %s %s 网络失败（%d 次）：%s"
                      % (method, path, self.retries + 1, last))

    def call(self, method, path, payload=None, timeout=30.0):
        """JSON 调用：200 且 ok:true 返回响应体；其余抛 TGError（4xx fatal）。"""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") \
            if payload is not None else b""
        r = self.request(method, path, body=body,
                         content_type="application/json" if payload is not None else None,
                         timeout=timeout)
        j = r["json"] if isinstance(r["json"], dict) else {}
        if r["status"] == 200 and j.get("ok"):
            return j
        err = j.get("error") or r["text"][:200] or "HTTP %s" % r["status"]
        raise TGError("relay %s → %s" % (path.split("?")[0], err),
                      fatal=r["status"] in (400, 401, 403, 404, 409, 410))

    # ---- §10.2 端点 ----

    def pull(self, after=0, limit=100):
        return self.call("GET", "/v1/pull?after=%d&limit=%d" % (int(after), int(limit)))

    def download(self, file_id, timeout=120.0):
        """/v1/file：取回用户发来的文件字节。返回 (bytes, file_path)。"""
        path = "/v1/file?file_id=" + urllib.parse.quote(str(file_id), safe="")
        url = self.base + path
        resp = self._http.request("GET", url, headers=self._headers("GET", path, b""), timeout=timeout)
        if resp.status_code != 200:
            raise TGError("relay /v1/file → HTTP %s %s" % (resp.status_code, resp.text[:120]))
        return resp.content, resp.headers.get("x-file-path", "")

    def ack(self, qids):
        return self.call("POST", "/v1/ack", {"qids": [int(q) for q in qids]})

    def unpair(self):
        return self.call("POST", "/v1/unpair", {})

    def rank_report(self, payload):
        return self.call("POST", "/v1/rank/report", payload)

    def update_web_base(self, web_base):
        return self.call("POST", "/v1/pair/web_base", {"web_base": web_base})

    def profile(self, method, params):
        return self.call("POST", "/v1/profile",
                         {"method": method, "params": params})


def make_relay_http(cfg, api_base=None, inst_dir=None, **kw):
    rc = relay_cfg(cfg)
    iid = str(rc.get("instance_id") or "")
    if not iid:
        raise TGError("notify.yaml 的 telegram.relay.instance_id 没配，"
                      "先跑 `uv run host/tgbot.py pair`", fatal=True)
    return RelayHTTP(api_base or rc.get("base") or RELAY_BASE_DEFAULT, iid,
                     relay_key(cfg, inst_dir), proxy=rc.get("proxy"), **kw)


class RelayClient(TGClient):
    """relay 出站：与 TGClient 同接口，全部经 Worker /v1/send 代发（图片走 multipart）。"""

    def __init__(self, http):
        self.http = http
        self.token = ""                          # 本机没有 token，日志无需打码
        self.api_base = http.base
        self.retries = http.retries
        self.sleep = http.sleep
        self.on_log = http.on_log
        self._http = http._http

    def _clean(self, s):
        return str(s)

    def api(self, method, data=None, files=None, timeout=None):
        last = "unknown"
        for attempt in range(1, self.retries + 2):
            if files:
                body, ctype = _encode_multipart(
                    {"method": method, "params": json.dumps(data or {}, ensure_ascii=False)},
                    files)
            else:
                body, ctype = (json.dumps({"method": method, "params": data or {}},
                                          ensure_ascii=False).encode("utf-8"),
                               "application/json")
            try:
                r = self.http.request("POST", "/v1/send", body=body, content_type=ctype,
                                      timeout=timeout or 45.0)
            except TGError as exc:
                if exc.fatal:
                    raise
                last = str(exc)
                if attempt <= self.retries:
                    self.sleep(min(2 ** (attempt - 1), 30))
                continue
            st, j = r["status"], r["json"]
            if st == 200 and isinstance(j, dict) and j.get("ok"):
                return j.get("result")
            if st == 429:
                ra = 5
                try:
                    ra = int((j or {}).get("parameters", {}).get("retry_after") or 5)
                except (ValueError, AttributeError):
                    pass
                self.on_log("429", method=method, retry_after=ra)
                self.sleep(min(ra, 60))
                continue
            if isinstance(j, dict) and j.get("ok") is False:
                raise TGError("%s → %s" % (method, j.get("description")
                                           or j.get("error") or j), fatal=True)
            if 400 <= st < 500:
                raise TGError("%s → HTTP %s %s" % (method, st, r["text"][:200]), fatal=True)
            last = "HTTP %s %s" % (st, r["text"][:120])
            if attempt <= self.retries:
                self.sleep(min(2 ** (attempt - 1), 30))
        raise TGError("%s 失败（%d 次尝试）：%s" % (method, self.retries + 1, last))

    def get_updates(self, *a, **k):
        raise TGError("relay 模式不用 getUpdates，走 /v1/pull", fatal=True)


# --------------------------------------------------------------------------
# 出站推送（notify.py 调用；按钮回调由 tgbot.py 处理）
# push_* 返回 (deliveries, errors)：deliveries = [{"chat_id","message_id","at"}, ...]
# --------------------------------------------------------------------------

STATUS_LABEL = {"waiting": "等你处理", "scanned": "已扫码，等页面跳转",
                "expired": "已过期", "resolved": "已处理", "cancelled": "已取消"}


def handoff_buttons(hid, status="waiting"):
    """§8.6：[已扫码][重新获取][跳过这家][我来接管]；resolved/cancelled 不挂按钮。"""
    if status in ("resolved", "cancelled"):
        return []
    if status == "scanned":
        return [[("重新获取", "h:ref:" + hid), ("跳过这家", "h:no:" + hid),
                 ("我来接管", "h:me:" + hid)]]
    return [[("已扫码", "h:scan:" + hid), ("重新获取", "h:ref:" + hid),
             ("跳过这家", "h:no:" + hid), ("我来接管", "h:me:" + hid)]]


def _kind_label(kind):
    """chain 步骤的中文名；handoffs 模块在 sys.path 时用它的 KIND_LABEL。"""
    try:
        import handoffs
        return handoffs.KIND_LABEL.get(kind or "", kind or "处理")
    except Exception:
        return kind or "处理"


def handoff_caption(rec):
    """handoff 消息文案：状态图标 + 标题 + 要做什么 + 有效期/刷新计数/当前步骤。"""
    status = rec.get("status") or "waiting"
    head = {"resolved": "✅", "cancelled": "⏭", "scanned": "✅", "expired": "⌛"}.get(status, "⚠️")
    lines = ["%s %s" % (head, rec.get("title") or "需要你处理")]
    if rec.get("action"):
        lines.append(str(rec["action"]))
    meta = []
    if rec.get("expires_at") and status in ("waiting", "scanned"):
        try:
            exp = dt.datetime.fromisoformat(str(rec["expires_at"]))
            meta.append("有效至 %s" % exp.strftime("%H:%M:%S"))
        except ValueError:
            pass
    meta.append("刷新 %s/%s" % (int(rec.get("refresh_count") or 0),
                               int(rec.get("max_refresh") or 3)))
    chain = rec.get("chain") or []
    if chain:
        meta.append("当前步骤：%s" % _kind_label(chain[-1]))
    if meta:
        lines.append(" · ".join(meta))
    if status != "waiting":
        lines.append("状态：%s" % STATUS_LABEL.get(status, status))
    return "\n".join(lines)[:CAPTION_LIMIT]


def _deliver(client, ids, send_one, instance, map_info=None):
    """对全部白名单 chat 逐个发，成功的记 tg-map。返回 (deliveries, errors)。"""
    deliveries, errors = [], []
    for cid in ids:
        try:
            res = send_one(cid)
            mid = (res or {}).get("message_id")
            deliveries.append({"chat_id": cid, "message_id": mid, "at": now_iso()})
            if mid is not None and map_info:
                map_put(instance, cid, mid, map_info)
        except TGError as exc:
            errors.append("%s: %s" % (cid, exc))
    return deliveries, errors


def push_handoff(client, cfg, rec, instance):
    """handoff：发图片（二维码长按可识别）+ 说明 + §8.6 按钮组。"""
    hid = rec.get("id") or ""
    caption = handoff_caption(rec)
    buttons = handoff_buttons(hid, rec.get("status") or "waiting")
    shot = rec.get("shot") or ""
    shot_path = os.path.join(instance, shot) if shot else ""
    if shot_path and not os.path.isfile(shot_path):
        shot_path = ""
    info = {"kind": "handoff", "handoff_id": hid,
            "announcement_id": rec.get("announcement_id")}

    def send_one(cid):
        if shot_path:
            return client.send_photo(cid, shot_path, caption=caption, buttons=buttons)
        return client.send_message(cid, caption + "\n（没有截图附件）", buttons=buttons)

    return _deliver(client, chat_ids(cfg), send_one, instance, info)


def update_handoff(client, cfg, rec, instance, new_shot=None):
    """handoff-update：原地换图（editMessageMedia）或改说明（editMessageCaption），
    resolved/cancelled 时改 ✅ 文案并移除按钮。返回 (updated, errors)。"""
    hid = rec.get("id") or ""
    status = rec.get("status") or "waiting"
    caption = handoff_caption(rec)
    buttons = handoff_buttons(hid, status)
    targets = map_find(instance, kind="handoff", handoff_id=hid)
    updated, errors = [], []
    for cid, mid, _info in targets:
        try:
            if status in ("resolved", "cancelled"):
                _edit_any(client, cid, mid, caption, [])
            elif new_shot:
                try:
                    client.edit_media(cid, mid, new_shot, caption, buttons)
                except TGError:
                    _edit_any(client, cid, mid,
                              caption + "\n（新图换不上，见 web /handoffs）", buttons)
                # 原地编辑不会响铃：另发一条短消息回复原关卡，让手机弹通知
                try:
                    client.send_message(cid, "🔄 %s 二维码已刷新，扫上面这条" % (rec.get("company") or ""),
                                        reply_to=mid)
                except TGError as exc:
                    errors.append("%s:%s ping %s" % (cid, mid, exc))
            else:
                _edit_any(client, cid, mid, caption, buttons)
            updated.append({"chat_id": cid, "message_id": mid, "at": now_iso()})
        except TGError as exc:
            errors.append("%s:%s %s" % (cid, mid, exc))
    return updated, errors


def _edit_any(client, cid, mid, text, buttons):
    """媒体消息改 caption，纯文本消息改 text。"""
    try:
        client.edit_caption(cid, mid, text, buttons)
    except TGError:
        client.edit_text(cid, mid, text, buttons)


def push_review(client, cfg, announcement_id, company, fields_text, instance):
    """review_wait：字段对照表 + [提交][跳过]；用户直接回复本条文字 → reply 指令。

    fields_text 是 log/fields/<id>.md 的内容；过长时作为 .md 文件发送。
    """
    title = ("待你审字段 · %s #%s\n"
             "确认无误点「提交」；要改直接回复本条文字（如：学历填硕士）"
             % (company or "", announcement_id))
    buttons = [[("提交", "r:ok:%s" % announcement_id),
                ("跳过", "r:no:%s" % announcement_id)]]
    info = {"kind": "review", "announcement_id": announcement_id}

    def send_one(cid):
        if len(fields_text or "") <= MSG_LIMIT:
            return client.send_message(
                cid, "%s\n\n%s" % (title, fields_text or "（字段表文件缺失）"),
                buttons=buttons)
        return client.send_document(cid, "fields-%s.md" % announcement_id,
                                    fields_text, caption=title, buttons=buttons)

    return _deliver(client, chat_ids(cfg), send_one, instance, info)


def push_text(client, cfg, title, body, link=None, instance=None):
    """blocked / batch-done / deadline / assessment：普通文本（不记 tg-map，instance 可省）。"""
    text = str(title or "")
    if body:
        text += "\n%s" % body
    if link:
        text += "\n%s" % link
    return _deliver(client, chat_ids(cfg), lambda cid: client.send_message(cid, text),
                  instance)
