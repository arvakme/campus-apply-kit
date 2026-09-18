#!/usr/bin/env python3
"""test_relay.py · 共享 bot 中转（contracts §10）离线验收。

python3 test_relay.py            # 在 relay/test/ 下跑；自建 scratch、起假 TG + wrangler dev

覆盖：
  worker 级——webhook 无/错 secret 拒；未绑定用户静默；配对码过期/重用失败；
  签名错 / ts 超 60s / nonce 重放拒；A 密钥拉不到 B 队列；ack 即删、TTL 过期；
  /v1/send 限绑定 chat；cron 播报每位参与者各一条且无公司名；admin revoke。
  端到端——本地 Worker + 假 Telegram API + 两个 scratch 实例：
  pair → handoff 推送(sendPhoto 经 /v1/send) → 回调按钮 → commands.jsonl 进对实例 →
  rank-report → /rank → cron 播报。
"""
import hashlib
import hmac
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
RELAY = os.path.dirname(HERE)
REPO = os.path.dirname(RELAY)
SCRATCH = os.path.join(HERE, "scratch")
REQS = os.path.join(SCRATCH, "requests.jsonl")
DEVVARS = os.path.join(RELAY, ".dev.vars")

WEBHOOK_SECRET = "test-webhook-secret"
ADMIN_KEY = "test-admin-key"
BOT_TOKEN = "fake-token-for-test"
MAINTAINER = 900      # 维护者 user_id（wrangler --var MAINTAINER_USER_ID）
PAIR_TTL_S = 8          # 测试旋钮：配对码有效期（契约默认 600）
QUEUE_TTL_S = 8         # 测试旋钮：队列 TTL（契约默认 86400）
CST = ZoneInfo("Asia/Shanghai")

PASS, FAILS = [], []
W = T = None            # worker / fake-tg 端口


def ok(name, cond, extra=""):
    (PASS if cond else FAILS).append(name)
    print(("PASS " if cond else "FAIL ") + name
          + ("  " + str(extra)[:400] if extra and not cond else ""))


def wj(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)


def rj(path, default=None):
    try:
        return json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return default


def reqs(method=None):
    out = []
    try:
        for ln in open(REQS, encoding="utf-8"):
            if ln.strip():
                r = json.loads(ln)
                if method is None or r["method"] == method:
                    out.append(r)
    except OSError:
        pass
    return out


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# --------------------------------------------------------------------------
# HTTP 助手
# --------------------------------------------------------------------------

def raw_req(method, url, body=b"", headers=None, timeout=10):
    r = urllib.request.Request(url, data=body or None, headers=headers or {},
                               method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def sign(key_hex, iid, method, path, body=b""):
    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex[:24]
    msg = "%s|%s|%s|%s|%s" % (method, path, ts, nonce,
                              hashlib.sha256(body).hexdigest())
    sig = hmac.new(bytes.fromhex(key_hex), msg.encode(),
                   hashlib.sha256).hexdigest()
    return {"X-Instance": iid, "X-Ts": ts, "X-Nonce": nonce, "X-Sig": sig}


def api(method, path, key, iid, payload=None, headers=None):
    body = json.dumps(payload).encode() if payload is not None else b""
    h = sign(key, iid, method, path, body)
    if payload is not None:
        h["Content-Type"] = "application/json"
    if headers:
        h.update(headers)
    st, raw = raw_req(method, W + path, body, h)
    try:
        return st, json.loads(raw)
    except ValueError:
        return st, {"_raw": raw[:200]}


def webhook(update, secret=WEBHOOK_SECRET):
    body = json.dumps(update, ensure_ascii=False).encode()
    h = {"Content-Type": "application/json"}
    if secret is not None:
        h["X-Telegram-Bot-Api-Secret-Token"] = secret
    return raw_req("POST", W + "/tg/webhook", body, h)


def msg_update(uid, text, mid=1):
    return {"update_id": int(time.time() * 1000) % 10 ** 9 + uid,
            "message": {"message_id": mid, "from": {"id": uid},
                        "chat": {"id": uid, "type": "private"}, "text": text}}


def cb_update(uid, data, mid=900, text=""):
    return {"update_id": int(time.time() * 1000) % 10 ** 9 + uid + 5,
            "callback_query": {"id": "cb%s" % uid, "from": {"id": uid},
                               "data": data,
                               "message": {"message_id": mid,
                                           "chat": {"id": uid, "type": "private"},
                                           "text": text}}}


_IP_SEQ = [0]


def pair_init(iid, key, code, nickname="", rank=True, web_base=None, ip=None):
    ch = hashlib.sha256(code.encode()).hexdigest()
    p = {"instance_id": iid, "key": key, "code_hash": ch,
         "nickname": nickname, "rank": rank}
    if web_base:
        p["web_base"] = web_base
    if ip is None:
        # 每次调用换不同来源 IP，避免共享 PAIR_IP_RATE 桶影响主流程
        _IP_SEQ[0] += 1
        ip = "10.7.0.%d" % _IP_SEQ[0]
    return api("POST", "/v1/pair/init", key, iid, p,
               headers={"CF-Connecting-IP": ip})


def approve(uid, act="ok", by=MAINTAINER, text=""):
    """维护者点审批按钮：ap:ok:<uid> / ap:no:<uid>。"""
    return webhook(cb_update(by, "ap:%s:%d" % (act, uid),
                             text=text or "🔔 配对请求\n昵称：x\n实例：i-x"))


def pend_msg(uid):
    return [x for x in reqs("sendMessage")
            if str(x["fields"].get("chat_id")) == str(uid)
            and "等待维护者" in str(x["fields"].get("text", ""))]


def cron_trigger():
    # 走内部 /cron admin 路由（同步等 runCron 完成再返回），不用
    # /cdn-cgi/mf/scheduled——那条经 scheduled() 的 waitUntil 异步执行，
    # 固定 sleep 仍有竞态，偶发读不到播报消息。
    st, _ = raw_req("POST", W + "/cron", b"",
                    {"X-Admin-Key": ADMIN_KEY})
    return st


def bound_msg(uid):
    return [x for x in reqs("sendMessage")
            if str(x["fields"].get("chat_id")) == str(uid)
            and "配对成功" in str(x["fields"].get("text", ""))]


# --------------------------------------------------------------------------
# 进程：fake TG + wrangler dev
# --------------------------------------------------------------------------

def wrangler_bin():
    local = os.path.join(RELAY, "node_modules", ".bin", "wrangler")
    if os.path.isfile(local):
        return [local]
    return ["npx", "--no-install", "wrangler"]


def start_services():
    global W, T
    shutil.rmtree(SCRATCH, ignore_errors=True)
    os.makedirs(SCRATCH, exist_ok=True)
    tport, wport = free_port(), free_port()
    T, W = "http://127.0.0.1:%d" % tport, "http://127.0.0.1:%d" % wport
    tg = subprocess.Popen([sys.executable, os.path.join(HERE, "fake_tg.py"),
                           "--port", str(tport), "--dir", SCRATCH])
    with open(DEVVARS, "w", encoding="utf-8") as fh:
        fh.write("BOT_TOKEN=%s\nWEBHOOK_SECRET=%s\nADMIN_KEY=%s\n"
                 % (BOT_TOKEN, WEBHOOK_SECRET, ADMIN_KEY))
    wr = subprocess.Popen(
        wrangler_bin() + [
            "dev", "--local", "--port", str(wport),
            "--persist-to", os.path.join(SCRATCH, "do-state"),
            "--var", "TG_API_BASE:%s" % T,
            "--var", "PAIR_TTL_S:%d" % PAIR_TTL_S,
            "--var", "QUEUE_TTL_S:%d" % QUEUE_TTL_S,
            "--var", "MAINTAINER_USER_ID:%d" % MAINTAINER],
        cwd=RELAY, stdout=open(os.path.join(SCRATCH, "wrangler.log"), "w"),
        stderr=subprocess.STDOUT)
    deadline = time.time() + 40
    while time.time() < deadline:
        try:
            raw_req("GET", W + "/", timeout=2)
            break
        except Exception:
            time.sleep(0.4)
    else:
        print(open(os.path.join(SCRATCH, "wrangler.log")).read()[-2000:])
        raise SystemExit("wrangler dev 没起来")
    return tg, wr


# --------------------------------------------------------------------------
# 端到端 scratch 实例
# --------------------------------------------------------------------------

def run_repo(script, inst, *argv, env_key=None, timeout=90):
    env = dict(os.environ)
    if env_key:
        env["CAMPUS_RELAY_KEY"] = env_key
    return subprocess.run(
        ["uv", "run", "--script", os.path.join(REPO, "host", script)]
        + list(argv) + ["--instance", inst],
        capture_output=True, text=True, cwd=REPO, timeout=timeout, env=env)


def make_instance(name):
    inst = os.path.join(SCRATCH, name)
    os.makedirs(os.path.join(inst, "state"), exist_ok=True)
    os.makedirs(os.path.join(inst, "log", "shots"), exist_ok=True)
    with open(os.path.join(inst, "notify.yaml"), "w", encoding="utf-8") as fh:
        fh.write("channels: [telegram]\ntelegram:\n  mode: relay\n"
                 "  bot_token_cmd: \"true\"\n  relay:\n    base: %s\n"
                 "web_base: https://mac.example.ts.net\n" % W)
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d49444154789c626001000000ffff03000006000557bfabd4"
        "0000000049454e44ae426082")
    with open(os.path.join(inst, "log", "shots", "qr.png"), "wb") as fh:
        fh.write(png)
    return inst


def commands(inst):
    try:
        return [json.loads(l) for l in
                open(os.path.join(inst, "state", "commands.jsonl"),
                     encoding="utf-8") if l.strip()]
    except OSError:
        return []


# --------------------------------------------------------------------------
# worker 级验收
# --------------------------------------------------------------------------

def test_worker_level():
    keyA, keyB, keyC, keyX = "aa" * 32, "bb" * 32, "cc" * 32, "dd" * 32

    # --- webhook secret ---
    st, _ = webhook(msg_update(111, "/status"), secret=None)
    ok("webhook 无 secret → 401", st == 401, st)
    st, _ = webhook(msg_update(111, "/status"), secret="wrong")
    ok("webhook 错 secret → 401", st == 401, st)

    # --- 未绑定用户静默 ---
    n0 = len(reqs("sendMessage"))
    st, _ = webhook(msg_update(999, "/status"))
    ok("未绑定用户 webhook → 200 静默", st == 200, st)
    st, _ = webhook(msg_update(999, "hello"))
    ok("未绑定用户不发 sendMessage", len(reqs("sendMessage")) == n0)

    # --- A 配对 + 绑定 ---
    st, r = pair_init("i-aaa11111", keyA, "111111", "阿伟",
                      web_base="https://a.example.ts.net")
    ok("pair/init A → ok", st == 200 and r.get("ok"), (st, r))
    st, _ = webhook(msg_update(111, "/pair 000000"))
    ok("错配对码 → 静默无回复", st == 200 and not bound_msg(111))
    st, _ = webhook(msg_update(111, "/pair 111111"))
    ok("/pair 正确 → pending 回「等待维护者确认」（未直接绑定）",
       st == 200 and bool(pend_msg(111)) and not bound_msg(111),
       reqs("sendMessage")[-3:])
    reqs_to_maint = [x for x in reqs("sendMessage")
                     if str(x["fields"].get("chat_id")) == str(MAINTAINER)
                     and "配对请求" in str(x["fields"].get("text", ""))]
    ok("Worker 私聊维护者审批（含昵称/user_id + 批准拒绝按钮）",
       bool(reqs_to_maint)
       and "user_id 111" in reqs_to_maint[-1]["fields"].get("text", "")
       and "ap:ok:111" in json.dumps(
           reqs_to_maint[-1]["fields"].get("reply_markup"), ensure_ascii=False)
       and "ap:no:111" in json.dumps(
           reqs_to_maint[-1]["fields"].get("reply_markup"), ensure_ascii=False),
       reqs_to_maint[-1]["fields"] if reqs_to_maint else "无")

    # pending 期间：实例 API 门全关、用户消息全静默
    st, r = api("GET", "/v1/pull?after=0", keyA, "i-aaa11111")
    ok("pending 实例 pull → bound=false 无更新",
       st == 200 and r.get("bound") is False and r.get("updates") == [], (st, r))
    st, r = api("POST", "/v1/send", keyA, "i-aaa11111",
                {"method": "sendMessage", "params": {"chat_id": 111, "text": "x"}})
    ok("pending 实例 /v1/send → 403 not_bound", st == 403, (st, r))
    n0 = len(reqs("sendMessage"))
    webhook(msg_update(111, "/status"))
    webhook(msg_update(111, "/rank"))
    st, r = api("GET", "/v1/pull?after=0", keyA, "i-aaa11111")
    ok("pending 用户消息与 /rank 全静默不入队",
       r.get("updates") == []
       and all("日榜" not in str(x["fields"].get("text", ""))
               for x in reqs("sendMessage")[n0:]), (r, reqs("sendMessage")[n0:]))
    st, r = pair_init("i-aaa22222", "ab" * 32, "121212", "阿伟二号")
    webhook(msg_update(111, "/pair 121212"))
    ok("pending 用户再试新码 → 提示已在等待（不重复占码）",
       any("已在等" in str(x["fields"].get("text", ""))
           and str(x["fields"].get("chat_id")) == "111"
           for x in reqs("sendMessage")), reqs("sendMessage")[-3:])

    # 非维护者点审批按钮 → 无效且仍 pending
    st, _ = webhook(cb_update(111, "ap:ok:111"))
    ok("非维护者点审批按钮 → 无效",
       st == 200 and any("只有维护者" in str(x["fields"].get("text", ""))
                         for x in reqs("answerCallbackQuery")),
       reqs("answerCallbackQuery"))
    st, r = api("GET", "/v1/pull?after=0", keyA, "i-aaa11111")
    ok("乱点后仍 pending（bound=false）", r.get("bound") is False, r)

    st, _ = approve(111)
    ok("维护者批准 → 绑定并回「配对成功」", st == 200 and bool(bound_msg(111)),
       reqs("sendMessage"))
    ok("审批消息原地标记 ✅ 已批准",
       any("已批准" in str(x["fields"].get("text", ""))
           for x in reqs("editMessageText")), reqs("editMessageText"))

    # 📌 秋招面板：绑定后自动发 + 置顶 + 菜单按钮（A 上报了 web_base）
    panel = [x for x in reqs("sendMessage")
             if str(x["fields"].get("chat_id")) == "111"
             and "秋招面板" in str(x["fields"].get("text", ""))]
    ok("绑定后自动发置顶面板（6 个 URL 按钮）",
       bool(panel) and "https://a.example.ts.net/bank" in json.dumps(
           panel[-1]["fields"].get("reply_markup"), ensure_ascii=False)
       and "https://a.example.ts.net/setup" in json.dumps(
           panel[-1]["fields"].get("reply_markup"), ensure_ascii=False),
       panel[-1]["fields"] if panel else "无")
    pins = [x for x in reqs("pinChatMessage")
            if str(x["fields"].get("chat_id")) == "111"]
    ok("面板 pinChatMessage 静默置顶（disable_notification）",
       bool(panel) and bool(pins)
       and pins[-1]["fields"].get("disable_notification") is True
       and pins[-1]["fields"].get("message_id") == panel[-1]["mid"], pins)
    menus = [x for x in reqs("setChatMenuButton")
             if str(x["fields"].get("chat_id")) == "111"]
    ok("setChatMenuButton web_app 看板 → web_base/",
       bool(menus) and ((menus[-1]["fields"].get("menu_button") or {})
                        .get("web_app") or {}).get("url")
       == "https://a.example.ts.net/", menus)

    # web_base 变更：编辑原置顶消息而不是新发
    st, r = api("POST", "/v1/pair/web_base", keyA, "i-aaa11111",
                {"web_base": "https://new.example.ts.net"})
    ok("pair/web_base → panel=edited", st == 200
       and r.get("panel") == "edited", (st, r))
    edits = [x for x in reqs("editMessageText")
             if str(x["fields"].get("chat_id")) == "111"]
    ok("web_base 变更 → 原地编辑同一面板消息（mid 不变）",
       bool(panel) and bool(edits)
       and edits[-1]["fields"].get("message_id") == panel[-1]["mid"]
       and "https://new.example.ts.net/bank" in json.dumps(
           edits[-1]["fields"].get("reply_markup"), ensure_ascii=False),
       edits[-1:] or "无")
    st, r = api("POST", "/v1/pair/web_base", keyA, "i-aaa11111",
                {"web_base": "notaurl"})
    ok("坏 web_base → 400 bad_web_base",
       st == 400 and r.get("error") == "bad_web_base", (st, r))
    st, r = api("POST", "/v1/pair/web_base", keyB, "i-aaa11111",
                {"web_base": "https://evil.example.ts.net"})
    ok("B 密钥改 A 的 web_base → 401（实例隔离）", st == 401, (st, r))

    # /v1/profile：bot 全局资料代发（tgbot.py profile 走这里）
    st, r = api("POST", "/v1/profile", keyA, "i-aaa11111",
                {"method": "setMyName", "params": {"name": "秋招助手"}})
    ok("/v1/profile setMyName → 透传 TG", st == 200 and r.get("ok"), (st, r))
    ok("fake tg 收到 setMyName 秋招助手",
       any(x["fields"].get("name") == "秋招助手" for x in reqs("setMyName")),
       reqs("setMyName"))
    st, r = api("POST", "/v1/profile", keyA, "i-aaa11111",
                {"method": "deleteWebhook", "params": {}})
    ok("/v1/profile 非白名单 → 400", st == 400
       and r.get("error") == "method_not_allowed", (st, r))
    st, r = api("POST", "/v1/profile", keyB, "i-aaa11111",
                {"method": "setMyName", "params": {"name": "越权"}})
    ok("B 密钥调 A 的 profile → 401", st == 401, (st, r))

    st, r = api("GET", "/v1/pull?after=0", keyA, "i-aaa11111")
    ok("pull A → 有 bound 系统事件", st == 200 and any(
        (i.get("sys") or {}).get("type") == "bound"
        and i["sys"].get("chat_id") == 111 for i in r.get("updates", [])),
        (st, r))

    # 配对码一次性：另一个 user 用同一个码 → 静默，未绑定
    st, _ = webhook(msg_update(222, "/pair 111111"))
    ok("配对码重用 → 静默且对方未绑定", st == 200 and not bound_msg(222))
    st, r = api("GET", "/v1/pull?after=0", keyB, "i-bbb22222")
    ok("未注册实例 → 401 unknown_instance", st == 401, (st, r))

    # --- 配对码过期（PAIR_TTL_S=8 测试旋钮） ---
    st, r = pair_init("i-eee99999", keyX, "999999", "过期侠")
    ok("pair/init X → ok", st == 200 and r.get("ok"))
    time.sleep(PAIR_TTL_S + 1.5)
    st, _ = webhook(msg_update(222, "/pair 999999"))
    ok("过期配对码 → 静默无回复", st == 200 and not bound_msg(222))

    # B 现在才注册并绑定自己的码
    st, r = pair_init("i-bbb22222", keyB, "222222", "小明")
    ok("pair/init B → ok", st == 200 and r.get("ok"))
    st, _ = webhook(msg_update(222, "/pair 222222"))
    ok("B /pair → pending", bool(pend_msg(222)))
    approve(222)
    ok("B 经批准 → 绑定成功", bool(bound_msg(222)))
    ok("无 web_base 实例不发面板/不置顶",
       not any(str(x["fields"].get("chat_id")) == "222"
               for x in reqs("pinChatMessage"))
       and not any(str(x["fields"].get("chat_id")) == "222"
                   for x in reqs("setChatMenuButton")))

    # C：不参与排行榜
    st, r = pair_init("i-ccc33333", keyC, "333333", "隐身", rank=False)
    ok("pair/init C（rank=false）→ ok", st == 200 and r.get("ok"))
    webhook(msg_update(333, "/pair 333333"))
    approve(333)

    # --- 拒绝：实例被删 ---
    st, r = pair_init("i-ddd44444", "dd" * 32, "444444", "路人甲")
    ok("pair/init D（陌生人）→ ok", st == 200 and r.get("ok"))
    webhook(msg_update(777, "/pair 444444"))
    ok("陌生人 /pair → pending 不回配对成功",
       bool(pend_msg(777)) and not bound_msg(777))
    st, _ = approve(777, act="no")
    ok("维护者拒绝 → ok", st == 200, st)
    st, r = api("GET", "/v1/pull?after=0", "dd" * 32, "i-ddd44444")
    ok("被拒实例 → 401 unknown_instance（已删除）", st == 401, (st, r))
    ok("被拒用户收到「未获通过」",
       any("未获通过" in str(x["fields"].get("text", ""))
           and str(x["fields"].get("chat_id")) == "777"
           for x in reqs("sendMessage")), reqs("sendMessage")[-5:])

    # --- 维护者自己配对：直通不审批 ---
    n_req = len([x for x in reqs("sendMessage")
                 if "配对请求" in str(x["fields"].get("text", ""))])
    st, r = pair_init("i-fff55555", "ff" * 32, "555555", "维护者")
    ok("pair/init 维护者实例 → ok", st == 200 and r.get("ok"))
    st, _ = webhook(msg_update(MAINTAINER, "/pair 555555"))
    ok("维护者本人 /pair → 直接绑定（不产生审批请求）",
       st == 200 and bool(bound_msg(MAINTAINER))
       and len([x for x in reqs("sendMessage")
                if "配对请求" in str(x["fields"].get("text", ""))]) == n_req)

    # --- pair/init 来源 IP 全局限流（每 IP 每小时 5 次） ---
    for n in range(5):
        pair_init("i-0bad%04d" % (n + 1), "0e" * 32, "88%04d" % n,
                  ip="9.9.9.9")
    st, r = pair_init("i-0bad0006", "0e" * 32, "880006", ip="9.9.9.9")
    ok("同 IP 第 6 次 pair/init → 429 rate_limited",
       st == 429 and r.get("error") == "rate_limited", (st, r))

    # --- 签名安全 ---
    st, r = api("GET", "/v1/pull?after=0", "ee" * 32, "i-aaa11111")
    ok("签名错 → 401 bad_sig", st == 401 and r.get("error") == "bad_sig", (st, r))
    st, r = api("GET", "/v1/pull?after=0", keyA, "i-bbb22222")
    ok("A 密钥当 B 用 → 401（队列隔离）", st == 401, (st, r))
    # 过期时间戳
    body = b""
    ts = str(int(time.time()) - 120)
    nonce = uuid.uuid4().hex[:24]
    msg = "%s|%s|%s|%s|%s" % ("GET", "/v1/pull?after=0", ts, nonce,
                              hashlib.sha256(body).hexdigest())
    sig = hmac.new(bytes.fromhex(keyA), msg.encode(), hashlib.sha256).hexdigest()
    st, _ = raw_req("GET", W + "/v1/pull?after=0", headers={
        "X-Instance": "i-aaa11111", "X-Ts": ts, "X-Nonce": nonce, "X-Sig": sig})
    ok("ts 超 60s → 401 bad_ts", st == 401, st)
    # nonce 重放
    ts = str(int(time.time()))
    nonce = "replay-nonce-1"
    msg = "%s|%s|%s|%s|%s" % ("GET", "/v1/pull?after=0", ts, nonce,
                              hashlib.sha256(body).hexdigest())
    sig = hmac.new(bytes.fromhex(keyA), msg.encode(), hashlib.sha256).hexdigest()
    h = {"X-Instance": "i-aaa11111", "X-Ts": ts, "X-Nonce": nonce, "X-Sig": sig}
    st1, _ = raw_req("GET", W + "/v1/pull?after=0", headers=h)
    st2, r2 = raw_req("GET", W + "/v1/pull?after=0", headers=h)
    ok("nonce 首用过、重放拒", st1 == 200 and st2 == 401
       and b"nonce_replay" in (r2 or b""), (st1, st2, r2))

    # --- 队列：隔离 + ack 即删 + TTL 过期 ---
    webhook(msg_update(111, "/status"))
    webhook(msg_update(222, "/status"))
    st, rA = api("GET", "/v1/pull?after=0", keyA, "i-aaa11111")
    stB, rB = api("GET", "/v1/pull?after=0", keyB, "i-bbb22222")
    ok("A/B 队列各自只看到自己的更新",
       all((u.get("upd") or {}).get("message", {}).get("from", {}).get("id") == 111
           or u.get("sys") for u in rA.get("updates", []))
       and all((u.get("upd") or {}).get("message", {}).get("from", {}).get("id") == 222
               or u.get("sys") for u in rB.get("updates", []))
       and any((u.get("upd") or {}).get("message", {}).get("text") == "/status"
               for u in rA.get("updates", []))
       and any((u.get("upd") or {}).get("message", {}).get("text") == "/status"
               for u in rB.get("updates", [])),
       (rA, rB))
    st, r = api("POST", "/v1/ack", keyA, "i-aaa11111",
                {"qids": [u["qid"] for u in rA.get("updates", [])]})
    st, rA2 = api("GET", "/v1/pull?after=0", keyA, "i-aaa11111")
    ok("ack 后删除", st == 200 and rA2.get("updates") == [], rA2)
    st, rB2 = api("GET", "/v1/pull?after=0", keyB, "i-bbb22222")
    ok("ack A 不影响 B 队列",
       any((u.get("upd") or {}).get("message", {}).get("text") == "/status"
           for u in rB2.get("updates", [])), rB2)
    time.sleep(QUEUE_TTL_S + 1.5)
    st, rB3 = api("GET", "/v1/pull?after=0", keyB, "i-bbb22222")
    ok("队列 TTL 过期（契约 24h，测试旋钮 8s）", rB3.get("updates") == [], rB3)

    # --- /v1/send：绑定 chat 限制 ---
    st, r = api("POST", "/v1/send", keyA, "i-aaa11111",
                {"method": "sendMessage", "params": {"chat_id": 111, "text": "hi A"}})
    ok("/v1/send sendMessage → 透传 TG", st == 200 and r.get("ok"), (st, r))
    ok("sendMessage 到绑定 chat",
       any(str(x["fields"].get("chat_id")) == "111"
           and "hi A" in str(x["fields"].get("text", ""))
           for x in reqs("sendMessage")))
    st, r = api("POST", "/v1/send", keyA, "i-aaa11111",
                {"method": "sendMessage", "params": {"chat_id": 222, "text": "越权"}})
    ok("发给别的 chat → 403 chat_forbidden", st == 403, (st, r))
    st, r = api("POST", "/v1/send", keyA, "i-aaa11111",
                {"method": "getMe", "params": {}})
    ok("非白名单方法 → 400", st == 400, (st, r))

    # --- rank/report + /rank + cron ---
    api("POST", "/v1/rank/report", keyA, "i-aaa11111",
        {"date": "2026-09-16", "applied_today": 3, "applied_total": 12})
    api("POST", "/v1/rank/report", keyB, "i-bbb22222",
        {"date": "2026-09-16", "applied_today": 1, "applied_total": 8})
    st, r = api("POST", "/v1/rank/report", keyA, "i-aaa11111",
                {"date": "2026-09-16", "applied_today": -1, "applied_total": 0})
    ok("rank/report 负数 → 400", st == 400, (st, r))
    # pending 用户（uid 888）此刻仍在等审批——/rank 与 cron 都不该有他
    st, r = pair_init("i-88888888", "88" * 32, "888888", "待审")
    webhook(msg_update(888, "/pair 888888"))
    n0 = len(reqs("sendMessage"))
    webhook(msg_update(222, "/rank"))
    rank_msgs = [x for x in reqs("sendMessage")[n0:]
                 if str(x["fields"].get("chat_id")) == "222"]
    ok("/rank → 榜单含昵称与数字、无公司名",
       bool(rank_msgs) and "阿伟" in rank_msgs[-1]["fields"].get("text", "")
       and "小明" in rank_msgs[-1]["fields"].get("text", "")
       and "12" in rank_msgs[-1]["fields"].get("text", "")
       and "腾讯" not in rank_msgs[-1]["fields"].get("text", ""),
       rank_msgs[-1]["fields"] if rank_msgs else "无")
    ok("/rank 不含退出者 C（隐身）与 pending 用户（待审）",
       bool(rank_msgs)
       and "隐身" not in rank_msgs[-1]["fields"].get("text", "")
       and "待审" not in rank_msgs[-1]["fields"].get("text", ""))
    n0 = len(reqs("sendMessage"))
    st = cron_trigger()
    bmsg = [x for x in reqs("sendMessage")[n0:]
            if "日榜" in str(x["fields"].get("text", ""))]
    chats = {str(x["fields"].get("chat_id")) for x in bmsg}
    ok("cron 播报：参与者 A(111) B(222) 各一条",
       st == 200 and "111" in chats and "222" in chats,
       (st, sorted(chats), [x["fields"].get("text") for x in bmsg]))
    ok("cron 播报不含退出者 C(333) 与 pending 用户(888)、无公司名",
       "333" not in chats and "888" not in chats and all(
           "腾讯" not in str(x["fields"].get("text", ""))
           and "字节" not in str(x["fields"].get("text", "")) for x in bmsg))

    # --- 上榜开关跟着上报走：配对时选了不上榜，事后改本地配置即可上榜，反之亦然 ---
    def rank_text_for(chat):
        n = len(reqs("sendMessage"))
        webhook(msg_update(chat, "/rank"))
        m = [x for x in reqs("sendMessage")[n:] if str(x["fields"].get("chat_id")) == str(chat)]
        return m[-1]["fields"].get("text", "") if m else ""
    st, r = api("POST", "/v1/rank/report", keyC, "i-ccc33333",
                {"date": "2026-09-16", "applied_today": 5, "applied_total": 20, "rank": True})
    ok("rank/report 带 rank=true → ok 且回显", st == 200 and r.get("rank") is True, (st, r))
    ok("C 改成上榜后 /rank 出现「隐身」", "隐身" in rank_text_for(222))
    st, r = api("POST", "/v1/rank/report", keyC, "i-ccc33333",
                {"date": "2026-09-16", "applied_today": 5, "applied_total": 20})
    ok("rank/report 不带 rank → 保持上一次的选择", "隐身" in rank_text_for(222), (st, r))
    st, r = api("POST", "/v1/rank/report", keyC, "i-ccc33333",
                {"date": "2026-09-16", "applied_today": 5, "applied_total": 20, "rank": False})
    ok("C 改回不上榜后 /rank 不再出现", "隐身" not in rank_text_for(222), (st, r))

    # --- unpair / admin ---
    st, r = api("POST", "/v1/unpair", keyX, "i-eee99999", {})
    ok("本机 unpair → ok", st == 200 and r.get("ok"), (st, r))
    st, r = api("GET", "/v1/pull?after=0", keyX, "i-eee99999")
    ok("unpair 后 → 401 unknown_instance", st == 401, (st, r))
    st, r = raw_req("POST", W + "/v1/admin/revoke",
                    json.dumps({"instance_id": "i-ccc33333"}).encode(),
                    {"X-Admin-Key": "wrong", "Content-Type": "application/json"})
    ok("admin 错密钥 → 401", st == 401, st)
    st, r = raw_req("POST", W + "/v1/admin/revoke",
                    json.dumps({"instance_id": "i-ccc33333"}).encode(),
                    {"X-Admin-Key": ADMIN_KEY, "Content-Type": "application/json"})
    ok("admin/revoke C → ok", st == 200, (st, r))
    st, r = api("GET", "/v1/pull?after=0", keyC, "i-ccc33333")
    ok("被吊销实例 → 401", st == 401, (st, r))


# --------------------------------------------------------------------------
# 端到端：本地 Worker + 假 TG + 两个 scratch 实例
# --------------------------------------------------------------------------

def test_e2e():
    instA, instB = make_instance("instA"), make_instance("instB")
    keyA, keyB = "f1" * 32, "f2" * 32
    uidA, uidB = 501, 502

    codes = {}
    for inst, key, nick in ((instA, keyA, "阿伟"), (instB, keyB, "小明")):
        r = run_repo("tgbot.py", inst, "pair", "--api-base", W,
                     "--nickname", nick, env_key=key)
        ok("e2e pair %s" % nick, r.returncode == 0 and "配对码" in r.stdout,
           r.stdout + r.stderr[-300:])
        m = re.search(r"配对码：(\d{6})", r.stdout)
        codes[inst] = m.group(1) if m else None
    webhook(msg_update(uidA, "/pair %s" % codes[instA]))
    webhook(msg_update(uidB, "/pair %s" % codes[instB]))
    ok("e2e 双实例 /pair → pending", bool(pend_msg(uidA)) and bool(pend_msg(uidB)))
    approve(uidA)
    approve(uidB)
    r = run_repo("tgbot.py", instA, "run", "--once", "--api-base", W, env_key=keyA)
    ny_text = open(os.path.join(instA, "notify.yaml"), encoding="utf-8").read()
    ok("e2e A 绑定回写 chat_id+allowed_chat_ids",
       r.returncode == 0
       and re.search(r"chat_id:\s*%d" % uidA, ny_text)
       and re.search(r"allowed_chat_ids:\s*\n\s*-\s*%d" % uidA, ny_text),
       (r.returncode, r.stdout[-200:] + r.stderr[-300:], ny_text))
    r = run_repo("tgbot.py", instB, "run", "--once", "--api-base", W, env_key=keyB)
    ok("e2e B 绑定", r.returncode == 0, r.stdout[-200:] + r.stderr[-300:])

    # 📌 秋招面板：notify.yaml 顶层 web_base 随 pair 上报，绑定后自动发+置顶+菜单
    panel = [x for x in reqs("sendMessage")
             if str(x["fields"].get("chat_id")) == str(uidA)
             and "秋招面板" in str(x["fields"].get("text", ""))]
    ok("e2e 绑定后自动发置顶面板+静默 pin+菜单按钮",
       bool(panel)
       and "https://mac.example.ts.net/bank" in json.dumps(
           panel[-1]["fields"].get("reply_markup"), ensure_ascii=False)
       and any(str(x["fields"].get("chat_id")) == str(uidA)
               and x["fields"].get("disable_notification") is True
               and x["fields"].get("message_id") == panel[-1]["mid"]
               for x in reqs("pinChatMessage"))
       and any(str(x["fields"].get("chat_id")) == str(uidA)
               and ((x["fields"].get("menu_button") or {})
                    .get("web_app") or {}).get("url")
               == "https://mac.example.ts.net/"
               for x in reqs("setChatMenuButton")),
       reqs("pinChatMessage") + reqs("setChatMenuButton"))

    # web_base 变更 → pair --update-web-base → 原地编辑同一置顶消息
    ny_path = os.path.join(instA, "notify.yaml")
    ny = open(ny_path, encoding="utf-8").read().replace(
        "https://mac.example.ts.net", "https://new.example.ts.net")
    with open(ny_path, "w", encoding="utf-8") as fh:
        fh.write(ny)
    r = run_repo("tgbot.py", instA, "pair", "--update-web-base",
                 "--api-base", W, env_key=keyA)
    ok("e2e pair --update-web-base", r.returncode == 0 and "已上报" in r.stdout,
       r.stdout + r.stderr[-300:])
    edits = [x for x in reqs("editMessageText")
             if str(x["fields"].get("chat_id")) == str(uidA)]
    ok("e2e 面板原地编辑同一 mid、按钮换新前缀",
       bool(panel) and bool(edits)
       and edits[-1]["fields"].get("message_id") == panel[-1]["mid"]
       and "https://new.example.ts.net/bank" in json.dumps(
           edits[-1]["fields"].get("reply_markup"), ensure_ascii=False),
       edits[-1:] or "无")

    # bot 资料：relay 经 Worker /v1/profile，direct 直连 TG——都打到 fake tg
    r = run_repo("tgbot.py", instA, "profile", "--api-base", W, env_key=keyA)
    ok("e2e relay profile 幂等四连", r.returncode == 0
       and "bot 资料已设置" in r.stdout, r.stdout + r.stderr[-300:])
    ok("e2e setMyName/Short/Desc/Commands 全部到 fake tg",
       any(x["fields"].get("name") == "秋招助手" for x in reqs("setMyName"))
       and any("替你投简历" in str(x["fields"].get("short_description", ""))
               for x in reqs("setMyShortDescription"))
       and any("未配对" in str(x["fields"].get("description", ""))
               and "几个同学" in str(x["fields"].get("description", ""))
               for x in reqs("setMyDescription"))
       and any("/rank" in json.dumps(x["fields"].get("commands"), ensure_ascii=False)
               or "rank" in json.dumps(x["fields"].get("commands"), ensure_ascii=False)
               for x in reqs("setMyCommands")),
       [x["method"] for x in reqs()])
    instD = os.path.join(SCRATCH, "instD")
    os.makedirs(os.path.join(instD, "state"), exist_ok=True)
    with open(os.path.join(instD, "notify.yaml"), "w", encoding="utf-8") as fh:
        fh.write("channels: [telegram]\ntelegram:\n  mode: direct\n"
                 "  bot_token_cmd: echo faketoken\n  allowed_chat_ids: [999]\n")
    r = run_repo("tgbot.py", instD, "profile", "--api-base", T)
    ok("e2e direct profile 直连假 TG", r.returncode == 0
       and "bot 资料已设置" in r.stdout, r.stdout + r.stderr[-300:])
    ok("direct setMyCommands 表单字段 JSON 序列化",
       any("rank" in str(x["fields"].get("commands", ""))
           and "pair" in str(x["fields"].get("commands", ""))
           for x in reqs("setMyCommands")), reqs("setMyCommands"))

    # handoff 推送：notify.py → /v1/send → fake TG sendPhoto
    r = run_repo("notify.py", instA, "handoff", "--company", "示例公司",
                 "--id", "10001", "--kind", "sms_code",   # 不需要 ego 空间校验的关卡种类：这里只测 relay 的发图链路
                 "--shot", "log/shots/qr.png", "--action", "报短信码",
                 env_key=keyA)
    ok("e2e handoff 推送成功", r.returncode == 0, r.stdout[-200:] + r.stderr[-200:])
    photos = [x for x in reqs("sendPhoto")
              if str(x["fields"].get("chat_id")) == str(uidA)]
    ok("e2e sendPhoto 经 relay 到 A 的 chat",
       bool(photos) and photos[-1]["files"].get("photo", 0) > 0,
       reqs("sendPhoto"))
    hid = None
    hdir = os.path.join(instA, "state", "handoffs")
    hfiles = [f for f in os.listdir(hdir)] if os.path.isdir(hdir) else []
    if hfiles:
        hid = sorted(hfiles)[-1][:-5]
    ok("e2e handoff 记录已写", bool(hid))

    # 回调按钮（h:scan）→ A 的 commands.jsonl 有 reply；B 没有
    if hid and photos:
        webhook(cb_update(uidA, "h:scan:%s" % hid, mid=photos[-1]["mid"]))
        r = run_repo("tgbot.py", instA, "run", "--once", "--api-base", W,
                     env_key=keyA)
        cmdsA, cmdsB = commands(instA), commands(instB)
        ok("e2e 回调 → reply 指令进 A 的 commands.jsonl",
           any(c.get("action") == "reply" and c.get("target") == 10001
               and (c.get("args") or {}).get("handoff_id") == hid for c in cmdsA),
           cmdsA)
        ok("e2e 指令不串到 B 的实例", cmdsB == [])
        hrec = rj(os.path.join(hdir, hid + ".json"), {})
        ok("e2e handoff status=scanned", hrec.get("status") == "scanned", hrec)
        ok("e2e 原地改消息 editMessageCaption",
           any("已扫码" in str(x["fields"].get("caption", ""))
               for x in reqs("editMessageCaption")), reqs("editMessageCaption"))

    # rank-report ×2 → /rank → cron 各一条
    today = datetime.now(CST).strftime("%Y-%m-%d")
    with open(os.path.join(instA, "state", "registry.jsonl"), "w",
              encoding="utf-8") as fh:
        fh.write(json.dumps({"employer": "X", "employer_key": "X", "portal": "p",
                             "announcement_ids": [10001], "status": "submitted",
                             "at": today + "T10:00:00+08:00"},
                            ensure_ascii=False) + "\n")
    r = run_repo("report.py", instA, "rank-report", "--relay-base", W, env_key=keyA)
    ok("e2e A rank-report", r.returncode == 0 and "today=1" in r.stdout,
       r.stdout + r.stderr[-300:])
    r = run_repo("report.py", instB, "rank-report", "--relay-base", W, env_key=keyB)
    ok("e2e B rank-report（0 也上报）", r.returncode == 0,
       r.stdout + r.stderr[-300:])
    n0 = len(reqs("sendMessage"))
    webhook(msg_update(uidA, "/rank"))
    msgs = [x for x in reqs("sendMessage")[n0:]
            if str(x["fields"].get("chat_id")) == str(uidA)]
    ok("e2e /rank 含两昵称与数字、无公司名",
       bool(msgs) and "阿伟" in msgs[-1]["fields"].get("text", "")
       and "小明" in msgs[-1]["fields"].get("text", "")
       and "示例公司" not in msgs[-1]["fields"].get("text", ""),
       msgs[-1]["fields"] if msgs else "无")
    n0 = len(reqs("sendMessage"))
    cron_trigger()
    bchats = {str(x["fields"].get("chat_id")) for x in reqs("sendMessage")[n0:]
              if "日榜" in str(x["fields"].get("text", ""))}
    ok("e2e cron 播报 A/B 各一条", str(uidA) in bchats and str(uidB) in bchats,
       sorted(bchats))


def main():
    tg, wr = start_services()
    try:
        test_worker_level()
        test_e2e()
    finally:
        wr.send_signal(signal.SIGTERM)
        tg.send_signal(signal.SIGTERM)
        try:
            os.remove(DEVVARS)
        except OSError:
            pass
    print("\n%d pass, %d fail" % (len(PASS), len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
