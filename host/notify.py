#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6", "httpx>=0.27"]
# ///
"""notify.py · 所有 agent 发手机通知的唯一入口，契约见 docs/contracts.md §6 与 §8.5。

多通道：notify.yaml 的 `channels` 按顺序都发（telegram 双向 / bark 只推送），
任一成功即算送达，全失败写 state/notify.log。channels 缺省 ["bark"]，旧配置行为不变。

    uv run --script host/notify.py handoff --company 示例公司 --id 10001 --kind wechat_qr \\
        --shot log/shots/10001-qr.png --action "长按识别二维码，扫完在 seedmux 回复 好了" [--worker W3] [--title ..]
    uv run --script host/notify.py blocked --company X --id 1 --body "卡在哪一步"
    uv run --script host/notify.py batch-done --title B-01 --body "成功 5 · 待用户 2"
    uv run --script host/notify.py deadline --company X --id 1 --date 2026-09-20
    uv run --script host/notify.py assessment --company X --date "09-18 19:00" --body "北森测评"
    uv run --script host/notify.py review-wait --id 10001 --company 示例公司   # 发 log/fields/<id>.md 字段对照表待审
    uv run --script host/notify.py handoff-update --id <hid|公告id> --status scanned|expired|resolved \\
        [--shot 新截图] [--expires-at ISO] [--note 新说明] [--chain-step kind]   # §8.6 原地更新
    uv run --script host/notify.py flush          # 免打扰结束后补发攒下的通知（任何非免打扰调用也会顺带补发）

公共参数：--instance DIR（默认 $CAMPUS_INSTANCE）、--dry-run（写 handoff 记录、打印请求，不发送）、--show-key

流程：handoff 先写 state/handoffs/<id>.json → 10 分钟去重 → 免打扰判断（handoff 不受限）→
      GET <bark_server>/<device_key>/<title>/<body>?level&sound&group&url → 失败重试 3 次 → 写 state/notify.log
bark_server 可以是公共 https://api.day.app，也可以是自建 bark-server（web/run-bark.sh），请求格式相同。
"""

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "web", "app"))
sys.path.insert(0, os.path.join(REPO, "host"))

import handoffs  # noqa: E402
import egospace  # noqa: E402
try:
    from channels import telegram as tgchan  # noqa: E402
except Exception:                            # channels/ 缺失时 bark 仍可用
    tgchan = None

EVENTS = ["handoff", "blocked", "batch-done", "deadline", "assessment", "review-wait"]
DEFAULT_EVENTS = {
    "handoff": {"enabled": True, "level": "timeSensitive", "sound": "alarm"},
    "blocked": {"enabled": True, "level": "active"},
    "batch-done": {"enabled": True, "level": "passive"},
    "deadline": {"enabled": True, "level": "active"},
    "assessment": {"enabled": True, "level": "timeSensitive"},
    "review-wait": {"enabled": True, "level": "timeSensitive", "sound": "alarm"},
}
KNOWN_CHANNELS = ("telegram", "bark")
DEDUP_SECONDS = 600
RETRIES = 3
GROUP = "campus-apply"
# §8.6 时效协议：二维码类关卡的默认有效期（页面没显示时按这个填 expires_at）
KIND_TTL = {"wechat_qr": 120, "qr": 120, "captcha": 300, "sms_code": 600,
            "login": 600, "face": 600}
HANDOFF_STATUS = ("queued", "ready", "waiting", "scanned", "expired", "resolved", "cancelled")
ACTIVE_STATUS = ("ready", "waiting", "scanned")
READY_ACK_SECONDS = 3 * 60             # 轮到后 worker 须在 3 分钟内确认页面就绪（handoff-update --status waiting --shot 新图）
QUEUE_STALE_SECONDS = 40 * 60          # 排队超过 40 分钟的关卡不再推送


class NotifyError(Exception):
    pass


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

def load_config(instance):
    path = os.path.join(instance, "notify.yaml")
    if not os.path.isfile(path):
        raise NotifyError(
            "找不到 %s。\n先建这个文件（不进 git）：\n"
            "  bark_server: https://api.day.app     # 或自建 bark-server 地址\n"
            "  device_key: <Bark App 首页示例 URL 里 api.day.app/ 后面那一段>\n"
            "  web_base: https://<机器名>.<tailnet>.ts.net\n"
            "字段说明见 docs/contracts.md §6" % path)
    cfg = yaml.safe_load(open(path, encoding="utf-8")) or {}
    channels = [str(c).strip() for c in (cfg.get("channels") or ["bark"]) if str(c).strip()]
    unknown = [c for c in channels if c not in KNOWN_CHANNELS]
    if unknown:
        raise NotifyError("%s 的 channels 有不认识的通道：%s（可选：%s）"
                          % (path, ", ".join(unknown), "/".join(KNOWN_CHANNELS)))
    if not channels:
        raise NotifyError("%s 的 channels 是空的，至少要有一个（bark / telegram）" % path)
    cfg["channels"] = channels
    if "bark" in channels:
        key = str(cfg.get("device_key") or "").strip()
        if not key or key.startswith("<") or "Bark App" in key:
            raise NotifyError(
                "%s 里 device_key 没填。\n打开手机 Bark App → 首页会显示形如 https://api.day.app/XXXXXXXX/推送内容 的示例，"
                "把 XXXXXXXX 填进 device_key；自建服务器则在 App 里切换服务器后复制新的 key。\n"
                "不想用 Bark 就把 channels 里的 bark 去掉。" % path)
        cfg["device_key"] = key
        cfg["bark_server"] = str(cfg.get("bark_server") or "https://api.day.app").rstrip("/")
    if "telegram" in channels:
        t = cfg.get("telegram") or {}
        if not str(t.get("bot_token_cmd") or "").strip():
            raise NotifyError(
                "%s 的 telegram.bot_token_cmd 没配。\n"
                "先把 token 存进钥匙串：security add-generic-password -s campus-apply-telegram -a bot -w <token>\n"
                "再配 bot_token_cmd: security find-generic-password -s campus-apply-telegram -w" % path)
        if not t.get("allowed_chat_ids"):
            raise NotifyError(
                "%s 的 telegram.allowed_chat_ids 是空的。\n"
                "先配好 bot_token_cmd，再跑 `uv run --script host/tgbot.py whoami` 拿 chat_id。" % path)
    events = dict(DEFAULT_EVENTS)
    for k, v in (cfg.get("events") or {}).items():
        events[k] = {**DEFAULT_EVENTS.get(k, {}), **(v or {})}
    cfg["events"] = events
    return cfg


def mask(key):
    return key if len(key) <= 8 else "%s…%s" % (key[:4], key[-4:])


def in_quiet_hours(cfg, now=None):
    qh = cfg.get("quiet_hours")
    if not qh or len(qh) != 2:
        return False
    now = (now or dt.datetime.now()).strftime("%H:%M")
    start, end = str(qh[0]), str(qh[1])
    if start <= end:
        return start <= now < end
    return now >= start or now < end          # 跨午夜，如 23:00 → 07:30


def dnd_until(instance):
    """勿扰：state/dnd.json {"until": ISO, "reason": ...}。生效期间所有通知（含 handoff）只入队不推送。"""
    p = os.path.join(instance, "state", "dnd.json")
    try:
        d = json.load(open(p, encoding="utf-8"))
        until = dt.datetime.fromisoformat(d.get("until") or "")
    except (OSError, ValueError):
        return None
    now = dt.datetime.now(until.tzinfo) if until.tzinfo else dt.datetime.now()
    return until if until > now else None


# --------------------------------------------------------------------------
# 状态文件（去重、免打扰队列、日志）
# --------------------------------------------------------------------------

class StateLock:
    def __init__(self, instance):
        os.makedirs(os.path.join(instance, "state"), exist_ok=True)
        self.path = os.path.join(instance, "state", ".notify.lock")

    def __enter__(self):
        self.fh = open(self.path, "a+")
        fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        self.fh.close()


def _state(instance, name):
    return os.path.join(instance, "state", name)


def read_json(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return default


def log(instance, entry):
    entry = {"at": handoffs.now_iso(), **entry}
    with open(_state(instance, "notify.log"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def dedup_key(event, args):
    raw = "|".join(str(x or "") for x in (event, args.company, args.id, args.kind, args.title, args.date))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# Bark
# --------------------------------------------------------------------------

def build_url(cfg, event, title, body, url=None):
    ev = cfg["events"].get(event, {})
    q = {"group": GROUP}
    for k in ("level", "sound", "icon"):
        if ev.get(k):
            q[k] = ev[k]
    if url:
        q["url"] = url
    path = "/".join(urllib.parse.quote(x, safe="") for x in (cfg["device_key"], title, body))
    return "%s/%s?%s" % (cfg["bark_server"], path, urllib.parse.urlencode(q, quote_via=urllib.parse.quote))


def send_bark(instance, cfg, event, url, sleep=time.sleep):
    shown = url.replace(cfg["device_key"], mask(cfg["device_key"]))
    last = ""
    for attempt in range(1, RETRIES + 2):          # 首次 + 重试 3 次
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "campus-apply-notify/1"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8", "replace")
            try:
                code = json.loads(body).get("code")
            except ValueError:
                code = None
            if code in (200, None):
                log(instance, {"event": event, "ok": True, "attempt": attempt, "url": shown})
                return True
            last = "bark code=%s %s" % (code, body[:200])
        except urllib.error.HTTPError as exc:
            last = "HTTP %s %s" % (exc.code, exc.read().decode("utf-8", "replace")[:200])
            if 400 <= exc.code < 500 and exc.code != 429:     # key 错、设备失效：重试没有意义
                log(instance, {"event": event, "ok": False, "attempt": attempt, "error": last, "url": shown})
                break
        except (urllib.error.URLError, OSError) as exc:
            last = "%s" % exc
        log(instance, {"event": event, "ok": False, "attempt": attempt, "error": last, "url": shown})
        if attempt <= RETRIES:
            sleep(2 ** (attempt - 1))
    print("ERROR: Bark 推送失败（第 %d 次尝试后放弃）：%s，详见 state/notify.log" % (attempt, last), file=sys.stderr)
    return False


# --------------------------------------------------------------------------
# Telegram（实现见 host/channels/telegram.py；按钮回调由 host/tgbot.py 处理）
# --------------------------------------------------------------------------

def _read_fields_md(instance, aid):
    path = os.path.join(instance, "log", "fields", "%s.md" % aid)
    try:
        return open(path, encoding="utf-8").read()
    except OSError:
        return ""


def send_telegram(instance, cfg, event, title, body, link, args, rec):
    """返回 (ok, deliveries)——deliveries 供 handoff 记录写 channels.telegram。"""
    if tgchan is None:
        raise NotifyError("host/channels/telegram.py 加载失败")
    client = tgchan.make_client(cfg)
    if event == "handoff" and rec:
        deliveries, errs = tgchan.push_handoff(client, cfg, rec, instance)
    elif event == "review-wait" and getattr(args, "id", None):
        aid = args.id
        deliveries, errs = tgchan.push_review(
            client, cfg, int(aid) if str(aid).isdigit() else aid,
            getattr(args, "company", None), _read_fields_md(instance, aid), instance)
    else:
        deliveries, errs = tgchan.push_text(client, cfg, title, body, link, instance)
    entry = {"event": event, "channel": "telegram", "ok": bool(deliveries),
             "sent": len(deliveries)}
    if errs:
        entry["errors"] = errs
    log(instance, entry)
    return bool(deliveries), deliveries


def tg_preview(event, title, body, link, args, rec):
    """--dry-run 展示用：telegram 这一路会发什么。"""
    if event == "handoff" and rec:
        shot = (rec.get("shot") or "无截图")
        return "sendPhoto(shot=%s) + 按钮[好了/跳过这家/我来接管]\ncaption=%s" % (
            shot, (rec.get("title") or "") + " " + (rec.get("action") or ""))
    if event == "review-wait" and getattr(args, "id", None):
        return "sendMessage/sendDocument(log/fields/%s.md) + 按钮[提交/跳过]" % args.id
    return "sendMessage %s" % "\n".join(x for x in (title, body, link) if x)


def previews(cfg, event, title, body, link, args, rec):
    """每个通道一行 dry-run 预览（token/key 打码）。"""
    out = []
    for ch in cfg["channels"]:
        if ch == "bark":
            url = build_url(cfg, event, title, body, link)
            shown = url if getattr(args, "show_key", False) else url.replace(
                cfg["device_key"], mask(cfg["device_key"]))
            out.append("bark GET %s" % shown)
        elif ch == "telegram":
            out.append("telegram %s" % (tgchan.describe(cfg) if tgchan else "<channels/telegram.py 缺失>"))
            out.append("  → %s" % tg_preview(event, title, body, link, args, rec))
    return out


def bark_handoff_reminder(rec):
    """§8.6：Telegram 已送达时 Bark 只负责把人叫醒（APNs 不依赖 VPN）。"""
    kind = handoffs.KIND_LABEL.get(rec.get("kind") or "", "处理")
    txt = "打开 Telegram · %s · %s" % (kind, rec.get("company") or "")
    if rec.get("expires_at"):
        try:
            exp = dt.datetime.fromisoformat(str(rec["expires_at"]))
            txt += " · 有效至 %s" % exp.strftime("%H:%M")
        except ValueError:
            pass
    return txt


def deliver(instance, cfg, event, title, body, link, args, rec=None):
    """按 channels 顺序都发，任一成功即算送达；全失败写 notify.log。

    返回 (ok, results, details)：details[ch] = {"deliveries": [...]} 或 {"sent_at": iso}。
    """
    results, details = {}, {}
    for ch in cfg["channels"]:
        try:
            if ch == "bark":
                btitle, bbody = title, body
                if event == "handoff" and rec and results.get("telegram"):
                    btitle, bbody = bark_handoff_reminder(rec), ""
                elif event == "handoff" and "telegram" in results \
                        and not results["telegram"]:
                    bbody = (body + "\n" if body else "") + \
                        "Telegram 未送达，点链接在浏览器处理"
                results[ch] = send_bark(instance, cfg, event,
                                        build_url(cfg, event, btitle, bbody, link))
                if results[ch]:
                    details[ch] = {"sent_at": handoffs.now_iso()}
            else:
                results[ch], details[ch] = send_telegram(
                    instance, cfg, event, title, body, link, args, rec)
                details[ch] = {"deliveries": details[ch]}
        except Exception as exc:
            log(instance, {"event": event, "channel": ch, "ok": False,
                           "error": str(exc)[:300]})
            results[ch] = False
    if not any(results.values()):
        log(instance, {"event": event, "ok": False,
                       "error": "所有通知通道都失败", "channels": results})
        print("ERROR: 所有通知通道都失败：%s，详见 state/notify.log" % results,
              file=sys.stderr)
        return False, results, details
    return True, results, details


# --------------------------------------------------------------------------
# 事件
# --------------------------------------------------------------------------

def compose(event, args):
    who = args.company or ""
    ident = " #%s" % args.id if args.id else ""
    if event == "handoff":
        label = handoffs.KIND_LABEL.get(args.kind or "", args.kind or "需要你处理")
        title = args.title or "需要你处理 · %s" % (who or label)
        body = args.body or "%s%s：%s" % (label, ident, args.action or "打开网页查看")
    elif event == "blocked":
        title = args.title or "卡住 · %s" % who
        body = args.body or "worker%s 停下来了%s" % (" " + args.worker if args.worker else "", ident)
    elif event == "batch-done":
        title = args.title or "批次完成"
        body = args.body or "打开看板查看结果"
    elif event == "deadline":
        title = args.title or "明天截止 · %s" % who
        body = args.body or "%s%s 截止，还没投" % (args.date or "", ident)
    elif event == "review-wait":
        title = args.title or "待你审字段 · %s" % who
        body = args.body or "%s填表完成，停在提交前等你确认" % ident
    else:
        title = args.title or "测评 / 笔试 · %s" % who
        body = args.body or "%s%s" % (args.date or "", ident)
    return title.strip(" ·"), body


def link_for(cfg, event, args, hid=None):
    if args.url:
        return args.url
    base = str(cfg.get("web_base") or "").rstrip("/")
    if not base:
        return None
    return "%s/handoff/%s" % (base, hid) if hid else base + ("/handoffs" if event == "blocked" else "/")


def flush_queue(instance, cfg, dry_run):
    qpath = _state(instance, "notify-queue.jsonl")
    if not os.path.isfile(qpath) or in_quiet_hours(cfg) or dnd_until(instance):
        return 0
    lines = [ln for ln in open(qpath, encoding="utf-8") if ln.strip()]
    if dry_run:
        for ln in lines:
            print("[dry-run] 待补发：%s" % json.loads(ln)["title"])
        return len(lines)
    keep = []
    for ln in lines:
        item = json.loads(ln)
        ok, _, _ = deliver(instance, cfg, item["event"], item["title"], item["body"],
                        item.get("url"), None)
        if not ok:
            keep.append(ln)
    with open(qpath, "w", encoding="utf-8") as fh:
        fh.writelines(keep)
    return len(lines) - len(keep)


def check_space(instance, rec):
    """需要用户在浏览器处理的关卡：推送前宿主机校验 ego 空间（一家一空间、只留关卡页）并 handOff。"""
    if (rec.get("kind") or "") not in egospace.NEED_SPACE_KINDS:
        return True, ""
    ok, info = egospace.prepare_and_hand_off(rec.get("ego_space"), rec.get("announcement_id"), rec.get("ego_page"),
                                             company=rec.get("company"))
    log(instance, {"event": "space-check", "handoff_id": rec.get("id"), "ok": ok,
                   "info": info if ok else None, "error": None if ok else info})
    if ok and info.get("name") and info["name"] not in str(rec.get("action") or ""):
        # 推送文案统一带上工作区名，用户一眼知道去 ego 哪个空间
        rec["action"] = "【ego 工作区 #%s %s】%s" % (rec.get("ego_space"), info["name"], rec.get("action") or "")
        handoffs.update(instance, rec["id"], action=rec["action"])
    return ok, ("" if ok else info)


def gate_max_active(cfg):
    """关卡队列长度：同时推到手机、等用户处理的关卡上限（notify.yaml gate_queue.max_active，默认 3）。"""
    try:
        return max(1, int((cfg.get("gate_queue") or {}).get("max_active") or 3))
    except (TypeError, ValueError):
        return 3


def active_gates(instance, exclude=None):
    return [h for h in handoffs.list_all(instance)
            if h.get("status") in ACTIVE_STATUS and not h.get("resolved_at") and h.get("id") != exclude]


def push_handoff_rec(instance, cfg, rec):
    """把一条 handoff 记录推出去（入队后晋升时用），回写 channels。"""
    ns = argparse.Namespace(company=rec.get("company"), kind=rec.get("kind"), action=rec.get("action"),
                            id=str(rec.get("announcement_id") or ""), title=None, body=None,
                            url=None, worker=rec.get("worker"), shot=rec.get("shot"), date=None)
    title, body = compose("handoff", ns)
    link = link_for(cfg, "handoff", ns, rec["id"])
    ok, _results, details = deliver(instance, cfg, "handoff", title, body, link, ns, rec)
    upd = {"channels": {}}
    tgd = (details.get("telegram") or {}).get("deliveries") or []
    if tgd:
        upd["channels"]["telegram"] = {"chat_id": tgd[0]["chat_id"], "message_id": tgd[0]["message_id"]}
        upd["delivered_at"] = tgd[0]["at"]
    handoffs.update(instance, rec["id"], **upd)
    return ok


def promote_gate_queue(instance, cfg, dry_run=False):
    """队列里有空位就按先来后到把 queued 关卡推出去。返回推出的条数。"""
    if dnd_until(instance):
        return 0
    queued = sorted((h for h in handoffs.list_all(instance)
                     if h.get("status") == "queued" and not h.get("resolved_at")),
                    key=lambda h: h.get("created_at") or "")
    n = 0
    for rec in queued:
        if len(active_gates(instance)) >= gate_max_active(cfg):
            break
        created = None
        try:
            created = dt.datetime.fromisoformat(rec.get("created_at") or "")
        except ValueError:
            pass
        if created and (dt.datetime.now(created.tzinfo) - created).total_seconds() > QUEUE_STALE_SECONDS:
            # 排队太久：截图里的验证码早过期、worker 也可能已经走了。不推给用户，交回 worker 重新发
            if not dry_run:
                handoffs.update(instance, rec["id"], status="expired",
                                action="排队超过 %d 分钟已过期：worker 回到该页面、验证码准备好后重新发 handoff"
                                % (QUEUE_STALE_SECONDS // 60))
            print("关卡队列：%s（%s）排队太久，置 expired 不推送" % (rec["id"], rec.get("company")))
            continue
        if dry_run:
            print("[dry-run] 轮到关卡 %s（%s），会推送" % (rec["id"], rec.get("company")))
            n += 1
            continue
        # 两段式：先置 ready 等 worker 确认页面还在、重截验证码、handOff 空间；worker 回 --status waiting 才推送
        rec = handoffs.update(instance, rec["id"], status="ready", ready_at=handoffs.now_iso())
        print("关卡队列：轮到 %s（%s），等 worker 确认页面就绪后推送" % (rec["id"], rec.get("company")))
        n += 1
    return n


def run_event(event, args, instance, cfg):
    ev = cfg["events"].get(event, {})
    if not ev.get("enabled", True):
        print("事件 %s 在 notify.yaml 里关闭了，跳过" % event)
        return 0

    with StateLock(instance):
        now = time.time()
        dedup_path = _state(instance, "notify-dedup.json")
        dedup = {k: v for k, v in read_json(dedup_path, {}).items() if now - v.get("at", 0) < DEDUP_SECONDS}
        key = dedup_key(event, args)
        prev = dedup.get(key)
        hid = None

        if prev and event == "handoff":
            old = handoffs.load(instance, prev.get("handoff_id") or "")
            if old and not old.get("resolved_at"):
                print("10 分钟内已发过同一 handoff（%s），不重复写记录也不重复推送" % old["id"])
                return 0
            prev = None
        if prev:
            print("10 分钟内已发过同一 %s 通知，跳过" % event)
            return 0

        rec = None
        if event == "handoff":
            if args.shot and not os.path.isfile(os.path.join(instance, args.shot)):
                print("WARN: 截图不存在：%s（记录照写，页面会显示破图）" % args.shot, file=sys.stderr)
            title_txt = args.title or "%s需要%s" % (args.company + " · " if args.company else "",
                                                    handoffs.KIND_LABEL.get(args.kind or "", "你处理"))
            hid = handoffs.unique_id(instance, handoffs.make_id(args.id, args.kind))
            expires_at = getattr(args, "expires_at", None)
            if not expires_at and (args.kind or "") in KIND_TTL:
                exp = dt.datetime.now().astimezone() + dt.timedelta(seconds=KIND_TTL[args.kind])
                expires_at = exp.isoformat(timespec="seconds")
            rec = {
                "id": hid, "event": "handoff", "kind": args.kind, "company": args.company,
                "announcement_id": int(args.id) if (args.id or "").isdigit() else args.id,
                "worker": args.worker, "title": title_txt, "action": args.action, "shot": args.shot,
                "created_at": handoffs.now_iso(), "resolved_at": None,
                "status": "waiting", "expires_at": expires_at,
                "refresh_count": 0, "max_refresh": 3, "chain": [args.kind] if args.kind else [],
                "channels": {},
                "detected_at": getattr(args, "detected_at", None) or handoffs.now_iso(),
                "notified_at": handoffs.now_iso(), "delivered_at": None,
                "ego_space": int(args.space) if str(getattr(args, "space", "") or "").isdigit() else None,
                "ego_page": getattr(args, "page", None),
            }
            n_active = len(active_gates(instance))
            if n_active >= gate_max_active(cfg):
                rec["status"] = "queued"
                rec["notified_at"] = None
            handoffs.atomic_write_json(handoffs.path_for(instance, hid), rec)
            print("handoff 记录：state/handoffs/%s.json" % hid)
            if rec["status"] == "queued":
                n_q = sum(1 for h in handoffs.list_all(instance) if h.get("status") == "queued")
                print("关卡队列已满（%d 个在等用户），本关卡排队中（队列第 %d 位），轮到时自动推送；"
                      "照常按 handoff 协议等待状态变化，不要重发" % (n_active, n_q))
                return 0

        if rec is not None and not args.dry_run and not dnd_until(instance):
            okspace, why = check_space(instance, rec)
            if not okspace:
                handoffs.update(instance, hid, status="expired", action="未推送：" + why)
                print("ERROR: 关卡没推给用户——%s。修好后用 handoff-update --id %s --status waiting --space <编号> [--page pN] 重发"
                      % (why, hid), file=sys.stderr)
                return 3
        title, body = compose(event, args)
        link = link_for(cfg, event, args, hid)
        if link is None:
            print("WARN: notify.yaml 没有 web_base，通知点开不会跳转", file=sys.stderr)

        dnd = dnd_until(instance)
        if dnd:
            if args.dry_run:
                print("[dry-run] 勿扰中（至 %s），会入队稍后补发" % dnd.strftime("%m-%d %H:%M"))
                return 0
            with open(_state(instance, "notify-queue.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"event": event, "title": title, "body": body, "url": link,
                                     "queued_at": handoffs.now_iso()}, ensure_ascii=False) + "\n")
            print("勿扰中（至 %s），已入队 state/notify-queue.jsonl，结束后补发" % dnd.strftime("%m-%d %H:%M"))
            return 0

        if event != "handoff" and in_quiet_hours(cfg):
            if args.dry_run:
                print("[dry-run] 免打扰时段 %s，会入队稍后补发" % "-".join(cfg["quiet_hours"]))
                for line in previews(cfg, event, title, body, link, args, rec):
                    print("[dry-run] %s" % line)
                return 0
            with open(_state(instance, "notify-queue.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"event": event, "title": title, "body": body, "url": link,
                                     "queued_at": handoffs.now_iso()}, ensure_ascii=False) + "\n")
            print("免打扰时段，已入队 state/notify-queue.jsonl，结束后补发")
            return 0

        if args.dry_run:
            for line in previews(cfg, event, title, body, link, args, rec):
                print("[dry-run] %s" % line)
            flush_queue(instance, cfg, True)
            return 0

        flush_queue(instance, cfg, False)
        ok, results, details = deliver(instance, cfg, event, title, body, link, args, rec)
        if ok:
            dedup[key] = {"at": now, "event": event, "handoff_id": hid}
            handoffs.atomic_write_json(dedup_path, dedup)
            via = "+".join(ch for ch, v in results.items() if v)
            print("已推送 %s（%s）%s" % (event, via, " → " + link if link else ""))
        if rec is not None:
            upd = {"channels": {}}
            tgd = (details.get("telegram") or {}).get("deliveries") or []
            if tgd:
                upd["channels"]["telegram"] = {"chat_id": tgd[0]["chat_id"],
                                               "message_id": tgd[0]["message_id"]}
                upd["delivered_at"] = tgd[0]["at"]
            if (details.get("bark") or {}).get("sent_at"):
                upd["channels"]["bark"] = {"sent_at": details["bark"]["sent_at"]}
            handoffs.update(instance, rec["id"], **upd)
        return 0 if ok else 1


def run_handoff_update(args, instance, cfg):
    """handoff-update：§8.6 原地更新——换图/改状态/接力下一道认证。"""
    rec = handoffs.load(instance, args.id) or handoffs.find_open(instance, args.id)
    if rec is None:
        print("ERROR: 找不到 handoff：%s（给 handoff id 或公告 id，后者取最新未处理的一条）"
              % args.id, file=sys.stderr)
        return 2
    hid = rec["id"]
    fields = {}
    if args.status:
        fields["status"] = args.status
        if args.status in ("resolved", "cancelled") and not rec.get("resolved_at"):
            fields["resolved_at"] = handoffs.now_iso()
        elif args.status not in ("resolved", "cancelled") and rec.get("resolved_at"):
            fields["resolved_at"] = None                     # 重新打开
    new_shot = None
    if args.shot:
        new_shot = os.path.join(instance, args.shot)
        if not os.path.isfile(new_shot):
            print("WARN: 新截图不存在：%s" % args.shot, file=sys.stderr)
            new_shot = None
        else:
            fields["shot"] = args.shot
            fields["refresh_count"] = int(rec.get("refresh_count") or 0) + 1
            if fields["refresh_count"] > int(rec.get("max_refresh") or 3):
                fields["status"] = "expired"
                print("已达 max_refresh=%s，状态置 expired，等用户点「重新获取」"
                      % rec.get("max_refresh"), file=sys.stderr)
    if args.expires_at:
        fields["expires_at"] = args.expires_at
    if str(getattr(args, "space", "") or "").isdigit():
        fields["ego_space"] = int(args.space)
    if getattr(args, "page", None):
        fields["ego_page"] = args.page
    if args.note or args.action:            # --note 是 --action 的别名（说明文案）
        fields["action"] = args.note or args.action
    if args.chain_step or args.chain:
        fields["chain"] = (rec.get("chain") or []) + [args.chain_step or args.chain]
    was = rec.get("status")
    if was in ("queued", "ready", "expired") and fields.get("status") == "waiting" and not rec.get("delivered_at"):
        fields["notified_at"] = handoffs.now_iso()
        if (rec.get("kind") or "") in KIND_TTL:
            exp = dt.datetime.now().astimezone() + dt.timedelta(seconds=KIND_TTL[rec["kind"]])
            fields["expires_at"] = exp.isoformat(timespec="seconds")
    rec = handoffs.update(instance, hid, **fields)
    print("handoff %s → status=%s refresh=%s chain=%s"
          % (hid, rec.get("status"), rec.get("refresh_count"), rec.get("chain")))
    if was in ("queued", "expired") and not rec.get("delivered_at") and rec.get("status") == "waiting" \
            and len(active_gates(instance, exclude=hid)) >= gate_max_active(cfg):
        handoffs.update(instance, hid, status="queued")
        print("关卡队列未轮到你（还有人在处理），保持排队；轮到时状态会变 ready")
        return 0
    if was in ("queued", "ready", "expired") and rec.get("status") == "waiting" and not rec.get("delivered_at") \
            and not args.dry_run:
        okspace, why = check_space(instance, rec)
        if not okspace:
            handoffs.update(instance, hid, status="expired", action="未推送：" + why)
            print("ERROR: 关卡没推给用户——%s。修好后用 handoff-update --status waiting --space <编号> [--page pN] 重发"
                  % why, file=sys.stderr)
            return 3
        push_handoff_rec(instance, cfg, rec)            # 首次推送（此前没有 Telegram 消息）
        print("已推送给用户（worker 已确认页面就绪）")
        return 0
    if rec.get("status") not in ACTIVE_STATUS and not args.dry_run:
        promote_gate_queue(instance, cfg)           # 腾出空位 → 推下一个
    if rec.get("status") in ("queued", "ready"):
        return 0                                    # 还没推送过，没有 Telegram 消息可改

    if "telegram" not in cfg["channels"]:
        return 0
    if tgchan is None:
        print("WARN: channels/telegram.py 缺失，Telegram 消息没更新", file=sys.stderr)
        return 0
    if args.dry_run:
        print("[dry-run] telegram %s" % tgchan.describe(cfg))
        print("[dry-run]   → 原地更新 handoff 消息：%s" %
              tgchan.handoff_caption(rec).splitlines()[0])
        return 0
    try:
        updated, errs = tgchan.update_handoff(
            tgchan.make_client(cfg), cfg, rec, instance, new_shot)
        log(instance, {"event": "handoff-update", "handoff_id": hid,
                       "ok": not errs, "updated": len(updated), "errors": errs or None})
        if errs:
            print("WARN: telegram 原地更新部分失败：%s" % errs, file=sys.stderr)
    except tgchan.TGError as exc:
        log(instance, {"event": "handoff-update", "handoff_id": hid,
                       "ok": False, "error": str(exc)[:300]})
        print("WARN: telegram 原地更新失败：%s" % exc, file=sys.stderr)
    return 0


def main():
    ap = argparse.ArgumentParser(description="手机通知（Telegram / Bark，多通道）")
    ap.add_argument("event", choices=EVENTS + ["flush", "handoff-update"])
    ap.add_argument("--instance", default=os.environ.get("CAMPUS_INSTANCE"))
    ap.add_argument("--company")
    ap.add_argument("--id", help="announcement_id；handoff-update 里也接受 handoff id")
    ap.add_argument("--kind", help="handoff 类型：wechat_qr / captcha / face / sms_code / submit_confirm / login")
    ap.add_argument("--shot", help="截图路径（相对实例目录，如 log/shots/10001-qr.png）")
    ap.add_argument("--action", help="要用户做什么")
    ap.add_argument("--worker")
    ap.add_argument("--page", help="关卡页在空间里的标签（如 p2）；缺省取当前激活标签。推送前其余标签会被关掉")
    ap.add_argument("--space", help="关卡所在 ego 空间编号（task.spaceId）；推送时由宿主机自动 handOff 给用户")
    ap.add_argument("--title")
    ap.add_argument("--body")
    ap.add_argument("--date")
    ap.add_argument("--url", help="覆盖点击跳转地址")
    ap.add_argument("--status", choices=HANDOFF_STATUS,
                    help="handoff-update：新状态")
    ap.add_argument("--expires-at", help="二维码有效期（ISO）；handoff 缺省按 kind 默认")
    ap.add_argument("--detected-at", help="worker 发现关卡的时间（ISO），缺省=现在")
    ap.add_argument("--note", help="handoff-update：新说明文案（--action 的别名）")
    ap.add_argument("--chain", help="handoff-update：接力下一道认证的 kind，追加进 chain")
    ap.add_argument("--chain-step", dest="chain_step",
                    help="同 --chain")
    ap.add_argument("--dry-run", action="store_true", help="只打印请求不发送（handoff 记录照写）")
    ap.add_argument("--show-key", action="store_true", help="dry-run 打印完整 device_key")
    args = ap.parse_args()

    if not args.instance or not os.path.isdir(args.instance):
        print("ERROR: 实例目录不存在，传 --instance 或设置 CAMPUS_INSTANCE", file=sys.stderr)
        return 2
    instance = os.path.abspath(args.instance)
    try:
        cfg = load_config(instance)
    except (NotifyError, yaml.YAMLError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2
    if args.event == "handoff" and not (args.company and args.kind and args.action):
        print("ERROR: handoff 需要 --company、--kind、--action（建议再给 --id 和 --shot）", file=sys.stderr)
        return 2
    if args.event == "review-wait" and not args.id:
        print("ERROR: review-wait 需要 --id（会读 log/fields/<id>.md 发字段对照表）", file=sys.stderr)
        return 2
    if args.event == "handoff-update":
        if not args.id:
            print("ERROR: handoff-update 需要 --id（handoff id 或公告 id）", file=sys.stderr)
            return 2
        if not (args.status or args.shot or args.action or args.note
                or args.chain or args.chain_step or args.expires_at or args.space or args.page):
            print("ERROR: handoff-update 至少要给一个更新项"
                  "（--status/--shot/--note/--chain-step/--expires-at）",
                  file=sys.stderr)
            return 2
        return run_handoff_update(args, instance, cfg)
    if args.event == "flush":
        n = flush_queue(instance, cfg, args.dry_run)
        m = promote_gate_queue(instance, cfg, args.dry_run)
        print("补发 %d 条 · 关卡队列推出 %d 个" % (n, m))
        return 0
    return run_event(args.event, args, instance, cfg)


if __name__ == "__main__":
    sys.exit(main())
