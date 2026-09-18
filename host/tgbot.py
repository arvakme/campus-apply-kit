#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx>=0.27", "pyyaml>=6"]
# ///
"""tgbot.py · Telegram 双向 bot（direct 长轮询 / relay 共享中转，契约 §8、§10）。

    uv run --script host/tgbot.py run --instance DIR [--api-base URL] [--once] [--poll-timeout 25]
    uv run --script host/tgbot.py whoami [--wait 30]        # direct：打印发消息者的 chat_id
    uv run --script host/tgbot.py pair [--nickname X] [--no-rank] [--wait 120]  # relay 配对
    uv run --script host/tgbot.py unpair                    # relay 解绑（本机吊销）
    uv run --script host/tgbot.py install-plist --dry-run   # 只打印 launchd plist
    uv run --script host/tgbot.py install-plist --dest DIR  # 只写 plist 文件，不做 launchctl 注册

模式：notify.yaml 的 telegram.mode（relay|direct）。direct = 本机 token 长轮询 getUpdates；
relay = 共享 bot 由 Cloudflare Worker 持 token，本机签名轮询 /v1/pull 并 ack（§10）。
relay 配对流程：pair 生成 instance_id+密钥(钥匙串 campus-apply-relay)+6 位配对码并预注册，
用户在 Telegram 发 /pair <码> 完成绑定，chat_id 自动写回 notify.yaml。

读：state/jobs/*.json、state/autonomy.json、state/handoffs/、state/fit/、log/fields/、log/ops/
写：state/commands.jsonl（dispatcher 消费，契约 docs/contracts.md §8.3）、
    state/tg-map.json、state/tg-offset.json、state/tg-relay-seen.json、state/tgbot.log

安全：只响应 notify.yaml 的 telegram.allowed_chat_ids，其他 chat 一律忽略并记 tgbot.log；
token 经 bot_token_cmd（钥匙串）取，不打印。进程常驻由 launchd KeepAlive 管。
"""

import argparse
import glob
import hashlib
import json
import os
import plistlib
import re
import secrets
import shutil
import subprocess
import sys
import time
import uuid
from collections import Counter

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "host"))
sys.path.insert(0, os.path.join(REPO, "web", "app"))

from channels import telegram as tg  # noqa: E402
import handoffs                      # noqa: E402
import notify                        # noqa: E402  (bark 降级/二次提醒复用同一套发送)

LABEL = "dev.campus-apply.tgbot"
PATH_ENV = "/opt/homebrew/bin:/usr/local/bin:{home}/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
PAGE = 8
POLL_TIMEOUT = 25
RUNNING = ("queued", "dispatched", "filling", "gate_wait", "review_wait", "submitting")
TIER_ORDER = {"冲": 0, "常规": 1, "练手": 2}
MODES = ("manual", "supervised", "auto")

HELP = """指令：
/today — 今日待圈选，逐条 [投]/[跳过]
/status — 档位、暂停、各状态作业数、在跑作业、待你处理
/mode manual|supervised|auto — 切总档位；/mode 练手 auto 这种单档覆盖
/pause — 暂停派新单；/pause <id|公司> — 暂停指定作业
/resume — 恢复派单；/resume <id|公司> — 恢复被 hold 的作业
/hold <id|公司> · /takeover <id|公司> · /skip <id|公司>
/log <id|公司> — 作业 history 最近 10 条 + ops 留痕最后一段
/report — 战报（host/report.py，不存在时按 jobs 汇总）
/rank — 投递排行榜（共享 bot relay 模式由 Worker 直接回复）
回复 bot 的推送消息写文字 = 转达给对应作业的 worker"""


# --------------------------------------------------------------------------
# 基础
# --------------------------------------------------------------------------

def _state(instance, name):
    return os.path.join(instance, "state", name)


def read_json(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return default


def log(instance, level, **kw):
    os.makedirs(_state(instance, ""), exist_ok=True)
    entry = {"at": tg.now_iso(), "level": level, **kw}
    with open(_state(instance, "tgbot.log"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_notify_cfg(instance):
    path = os.path.join(instance, "notify.yaml")
    if not os.path.isfile(path):
        raise SystemExit("ERROR: 找不到 %s（格式见 docs/contracts.md §8.5，"
                         "模板 templates/notify.example.yaml）" % path)
    cfg = yaml.safe_load(open(path, encoding="utf-8")) or {}
    return cfg


class Ctx:
    def __init__(self, instance, cfg, client):
        self.instance = instance
        self.cfg = cfg
        self.client = client
        self.allowed = set(tg.chat_ids(cfg))
        if tg.tg_mode(cfg) == "relay" and not self.allowed:
            bc = tg.relay_cfg(cfg).get("chat_id")   # 绑定后没写 allowed_chat_ids 的兜底
            if bc:
                try:
                    self.allowed = {int(bc)}
                except (TypeError, ValueError):
                    pass


def build_ctx(args, need_allowed=True):
    instance = getattr(args, "instance", None) or os.environ.get("CAMPUS_INSTANCE")
    if not instance or not os.path.isdir(instance):
        raise SystemExit("ERROR: 实例目录不存在，传 --instance 或设置 CAMPUS_INSTANCE")
    instance = os.path.abspath(instance)
    cfg = load_notify_cfg(instance)
    try:
        client = tg.make_client(cfg, api_base=getattr(args, "api_base", None),
                                inst_dir=instance,
                                on_log=lambda *a, **k: log(instance, "warn", **k))
    except tg.TGError as exc:
        raise SystemExit("ERROR: %s" % exc)
    ctx = Ctx(instance, cfg, client)
    if need_allowed and not ctx.allowed and tg.tg_mode(cfg) != "relay":
        raise SystemExit("ERROR: notify.yaml 的 telegram.allowed_chat_ids 为空。"
                         "先跑 `uv run --script host/tgbot.py whoami` 拿 chat_id")
    return ctx


def save_notify_cfg(instance, cfg):
    """回写 notify.yaml（pair/unpair/绑定事件用）。只动 telegram 子树，其余键原样保留。"""
    path = os.path.join(instance, "notify.yaml")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("# notify.yaml · telegram.relay 段由 tgbot.py pair/unpair 维护，"
                 "格式见 docs/contracts.md §8.5 §10\n")
        yaml.safe_dump(cfg, fh, allow_unicode=True, sort_keys=False)
    os.replace(tmp, path)


def _apply_bound(instance, cfg, chat_id):
    """Worker 通知绑定成功：chat_id 写回 relay.chat_id 与 allowed_chat_ids。"""
    t = cfg.setdefault("telegram", {})
    rc = t.setdefault("relay", {})
    rc["chat_id"] = chat_id
    ids = t.get("allowed_chat_ids")
    if not ids:
        t["allowed_chat_ids"] = [chat_id]
    else:
        have = set()
        for x in ids:
            try:
                have.add(int(x))
            except (TypeError, ValueError):
                pass
        if chat_id not in have:
            ids.append(chat_id)
    save_notify_cfg(instance, cfg)


def handle_sys(ctx, ev):
    """relay 队列里的系统事件（非 Telegram update）。"""
    if (ev or {}).get("type") == "bound":
        cid = int(ev.get("chat_id") or 0)
        cfg = load_notify_cfg(ctx.instance)
        _apply_bound(ctx.instance, cfg, cid)
        ctx.cfg = load_notify_cfg(ctx.instance)
        ctx.allowed = set(tg.chat_ids(ctx.cfg))
        log(ctx.instance, "info", detail="relay 绑定完成 chat_id=%s" % cid)


# --------------------------------------------------------------------------
# state 读取（只读：jobs/autonomy/fit/handoffs 是 dispatcher 和各 worker 写的）
# --------------------------------------------------------------------------

def jobs(instance):
    d = _state(instance, "jobs")
    out = {}
    if not os.path.isdir(d):
        return out
    for fn in os.listdir(d):
        if not fn.endswith(".json") or fn.startswith("."):
            continue
        j = read_json(os.path.join(d, fn), None)
        if isinstance(j, dict) and j.get("announcement_id") is not None:
            try:
                out[int(j["announcement_id"])] = j
            except (TypeError, ValueError):
                continue
    return out


def fits(instance):
    d = _state(instance, "fit")
    out = {}
    if not os.path.isdir(d):
        return out
    for fn in os.listdir(d):
        if not fn.endswith(".json") or fn.startswith(".") or fn.startswith("_"):
            continue
        f = read_json(os.path.join(d, fn), None)
        if isinstance(f, dict) and f.get("announcement_id") is not None:
            try:
                out[int(f["announcement_id"])] = f
            except (TypeError, ValueError):
                continue
    return out


def pending_targets(instance):
    """commands.jsonl 里已写但还没进 commands.done.jsonl 的 approve/skip 目标。"""
    done = set()
    for ln in _read_lines(_state(instance, "commands.done.jsonl")):
        if ln.get("id"):
            done.add(ln["id"])
    out = set()
    for ln in _read_lines(_state(instance, "commands.jsonl")):
        if ln.get("action") in ("approve", "skip") and ln.get("id") not in done \
                and ln.get("target") is not None:
            try:
                out.add(int(ln["target"]))
            except (TypeError, ValueError):
                pass
    return out


def _read_lines(path):
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
    except OSError:
        pass
    return out


def write_command(instance, action, target=None, args=None):
    """契约 §8.3：{"id","at","by","action","target","args"}，追加写带 mkdir 锁。"""
    rec = {"id": "cmd-" + uuid.uuid4().hex[:12], "at": tg.now_iso(), "by": "telegram",
           "action": action}
    if target is not None:
        rec["target"] = target
    if args:
        rec["args"] = args
    path = _state(instance, "commands.jsonl")
    with tg.DirLock(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    log(instance, "info", command=rec)
    return rec


def _last_at(job):
    hist = job.get("history") or []
    return (hist[-1].get("at") if hist else None) or job.get("created_at") or ""


def _ago(iso):
    ts = tg._iso_ts(iso)
    if not ts:
        return "时间未知"
    m = int((time.time() - ts) // 60)
    if m < 60:
        return "等 %d 分钟" % m
    if m < 1440:
        return "等 %.1f 小时" % (m / 60)
    return "等 %d 天" % (m // 1440)


def _fmt_cmd(rec):
    return "已提交：%s%s（%s），dispatcher 生效后通知" % (
        rec["action"],
        " #%s" % rec["target"] if rec.get("target") is not None else "",
        rec["id"])


def resolve_target(ctx, s):
    """<公司或id>：数字=announcement_id；公司名先查 jobs（精确→包含），再查 fit。"""
    s = (s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    jb = jobs(ctx.instance)
    fuzzy = None
    for aid, j in jb.items():
        c = str(j.get("company") or "")
        if c == s:
            return aid
        if s in c or (c and c in s):
            fuzzy = fuzzy if fuzzy is not None else aid
    if fuzzy is not None:
        return fuzzy
    for aid, f in fits(ctx.instance).items():
        c = str(f.get("company") or "")
        if c == s or s in c:
            return aid
    return None


# --------------------------------------------------------------------------
# 指令
# --------------------------------------------------------------------------

def today_page(ctx, page):
    jb = jobs(ctx.instance)
    pend = pending_targets(ctx.instance)
    cands = []
    for aid, f in fits(ctx.instance).items():
        if f.get("tier") not in TIER_ORDER or aid in jb:
            continue
        cands.append((aid, f, aid in pend))
    cands.sort(key=lambda x: (TIER_ORDER[x[1]["tier"]],
                              -(x[1].get("fit") or 0), x[0]))
    if not cands:
        return "今日待圈选为空（state/fit 里没有未建作业的 冲/常规/练手）。", None
    pages = (len(cands) + PAGE - 1) // PAGE
    page = max(0, min(page, pages - 1))
    lines = ["今日待圈选 · 第 %d/%d 页 · 共 %d 家" % (page + 1, pages, len(cands))]
    kb = []
    for aid, f, is_pend in cands[page * PAGE:(page + 1) * PAGE]:
        role = "、".join(str(r) for r in (f.get("roles") or [])[:2])[:24]
        lines.append("【%s】%s · fit %s%s · #%d" % (
            f["tier"], f.get("company") or "?", f.get("fit") or "-",
            " · " + role if role else "", aid))
        if is_pend:
            kb.append([("…指令待生效 #%d" % aid, "a:noop")])
        else:
            kb.append([("投", "a:ok:%d:%d" % (aid, page)),
                       ("跳过", "a:no:%d:%d" % (aid, page))])
    nav = []
    if page > 0:
        nav.append(("‹ 上一页", "t:%d" % (page - 1)))
    if page < pages - 1:
        nav.append(("下一页 ›", "t:%d" % (page + 1)))
    if nav:
        kb.append(nav)
    lines.append("\n「投」=批准派单；「跳过」=这家不投。控制全局用 /mode /pause。")
    return "\n".join(lines), kb


def status_text(ctx):
    auto = read_json(_state(ctx.instance, "autonomy.json"), {})
    mode = auto.get("mode") or "未配置"
    tm = auto.get("tier_modes") or {}
    tier_s = " ".join("%s=%s" % (k, tm[k]) for k in ("冲", "常规", "练手") if tm.get(k))
    lines = ["档位：%s%s · %s" % (mode, "（%s）" % tier_s if tier_s else "",
                                 "已暂停" if auto.get("paused") else "运行中")]
    jb = jobs(ctx.instance)
    cnt = Counter(str(j.get("status") or "?") for j in jb.values())
    lines.append("作业：" + (" · ".join("%s %d" % kv for kv in sorted(cnt.items()))
                            if cnt else "无"))
    running = [j for j in jb.values()
               if j.get("status") in RUNNING or j.get("status") == "held"]
    if running:
        lines.append("在跑：")
        for j in sorted(running, key=_last_at)[:10]:
            lines.append("- %s #%s · %s · %s%s" % (
                j.get("company") or "?", j.get("announcement_id"), j.get("status"),
                _ago(_last_at(j)), "（已 hold）" if j.get("status") == "held" else ""))
    waits = [h for h in handoffs.list_all(ctx.instance) if not h.get("resolved_at")]
    review = [j for j in jb.values() if j.get("status") == "review_wait"]
    if waits or review:
        lines.append("待你处理：")
        for h in waits:
            lines.append("- %s：%s（%s）" % (
                h.get("id"), h.get("title") or handoffs.KIND_LABEL.get(h.get("kind"), "处理"),
                h.get("company") or ""))
        for j in review:
            lines.append("- 审字段：%s #%s" % (j.get("company") or "?",
                                              j.get("announcement_id")))
    return "\n".join(lines)


def cmd_mode(ctx, args):
    if not args:
        auto = read_json(_state(ctx.instance, "autonomy.json"), {})
        return "当前 mode=%s paused=%s tier_modes=%s\n用法：/mode manual|supervised|auto 或 /mode 冲|常规|练手 <档位>" % (
            auto.get("mode") or "未配置", auto.get("paused"), auto.get("tier_modes") or {})
    if len(args) == 1 and args[0] in MODES:
        return _fmt_cmd(write_command(ctx.instance, "mode", args={"mode": args[0]}))
    if len(args) == 2 and args[0] in TIER_ORDER and args[1] in MODES:
        return _fmt_cmd(write_command(ctx.instance, "mode",
                                      args={"tier": args[0], "mode": args[1]}))
    return "用法：/mode manual|supervised|auto 或 /mode 冲|常规|练手 <档位>"


def _targeted(ctx, args, action, need=True):
    if not args:
        return ("要指定作业：/%s <公司或公告id>" % action) if need else None
    aid = resolve_target(ctx, args[0])
    if aid is None:
        return "没找到「%s」：给公告 id，或公司名（会查 jobs 和 fit）" % args[0]
    return _fmt_cmd(write_command(ctx.instance, action, target=aid))


def _ops_tail(instance, aid):
    pats = [os.path.join(instance, "log", "ops", "*-%d.md" % aid),
            os.path.join(instance, "log", "ops", "*-%d-*.md" % aid),
            os.path.join(instance, "log", "*-%d.md" % aid),
            os.path.join(instance, "log", "*-%d-*.md" % aid)]
    files = [p for pat in pats for p in glob.glob(pat)]
    if not files:
        return None
    path = max(files, key=os.path.getmtime)
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return None
    parts = re.split(r"(?m)^(?=##? )", text)
    tail = parts[-1].strip() if len(parts) > 1 else text.strip()
    return "留痕 %s（最后一段）：\n%s" % (os.path.basename(path), tail[-2600:])


def cmd_log(ctx, args):
    if not args:
        return "用法：/log <公司或公告id>"
    aid = resolve_target(ctx, args[0])
    if aid is None:
        return "没找到「%s」：给公告 id，或公司名" % args[0]
    out = ["#%s" % aid]
    j = jobs(ctx.instance).get(aid)
    if j:
        out.append("%s · %s · 状态 %s" % (j.get("company") or "?", j.get("tier") or "",
                                          j.get("status") or "?"))
        hist = (j.get("history") or [])[-10:]
        if hist:
            out.append("history：")
            for h in hist:
                out.append("- %s %s→%s（%s）%s" % (
                    h.get("at", "")[5:16], h.get("from") or "·", h.get("to") or "·",
                    h.get("by") or "", h.get("note") or ""))
    else:
        out.append("state/jobs 里没有这个作业")
    tail = _ops_tail(ctx.instance, aid)
    if tail:
        out.append(tail)
    return "\n".join(out)[:tg.MSG_LIMIT]


def cmd_report(ctx):
    rp = os.path.join(REPO, "host", "report.py")
    if os.path.isfile(rp):
        try:
            out = subprocess.run(["uv", "run", "--script", rp, "--instance", ctx.instance],
                                 capture_output=True, text=True, timeout=90, cwd=REPO)
            txt = (out.stdout or out.stderr or "").strip()
            if txt:
                return txt[:3500]
        except (OSError, subprocess.TimeoutExpired) as exc:
            log(ctx.instance, "warn", detail="report.py 调用失败：%s" % exc)
    jb = jobs(ctx.instance)
    cnt = Counter(str(j.get("status") or "?") for j in jb.values())
    lines = ["战报（report.py 不存在时的 jobs 汇总）",
             "作业：" + (" · ".join("%s %d" % kv for kv in sorted(cnt.items()))
                         if cnt else "无")]
    for j in sorted(jb.values(), key=_last_at, reverse=True)[:15]:
        lines.append("- %s #%s · %s · %s" % (j.get("company") or "?",
                     j.get("announcement_id"), j.get("status"), _ago(_last_at(j))))
    return "\n".join(lines)


def dispatch(ctx, name, args):
    if name in ("start", "help"):
        return HELP, None
    if name == "today":
        return today_page(ctx, 0)
    if name == "status":
        return status_text(ctx), None
    if name == "mode":
        return cmd_mode(ctx, args), None
    if name == "pause":
        if args:
            return _targeted(ctx, args, "hold"), None
        return _fmt_cmd(write_command(ctx.instance, "pause")), None
    if name == "resume":
        if args:
            return _targeted(ctx, args, "resume"), None
        return _fmt_cmd(write_command(ctx.instance, "unpause")), None
    if name in ("hold", "takeover", "skip"):
        return _targeted(ctx, args, name), None
    if name == "log":
        return cmd_log(ctx, args), None
    if name == "report":
        return cmd_report(ctx), None
    if name == "rank":
        return ("排行榜是共享 bot（relay 模式）的功能，由 Worker 直接回复；"
                "本机是 direct 模式，没有榜单。"), None
    return "未知指令 /%s。/help 看全部。" % name, None


# --------------------------------------------------------------------------
# update 处理
# --------------------------------------------------------------------------

def _save_incoming_file(ctx, cid, msg):
    """用户在私聊里发来的文件/图片 → 实例 materials/inbox/（材料上传入口）。"""
    doc = msg.get("document") or {}
    photos = msg.get("photo") or []
    fid = doc.get("file_id") or (photos[-1].get("file_id") if photos else "")
    http = getattr(ctx.client, "http", None)
    if not fid or http is None or not hasattr(http, "download"):
        _send_quiet(ctx, cid, "收到文件，但当前不是中转模式，没法自动保存；请直接放进 materials/ 文件夹。", None)
        return
    try:
        data, fpath = http.download(fid)
    except Exception as exc:
        log(ctx.instance, "error", detail="下载用户文件失败：%s" % exc)
        _send_quiet(ctx, cid, "文件没存下来：%s，可以重发一次。" % exc, None)
        return
    name = doc.get("file_name") or ("photo-%s%s" % (tg.now_iso()[:19].replace(":", ""), os.path.splitext(fpath)[1] or ".jpg"))
    name = re.sub(r"[\\/:*?\"<>|]", "_", name)
    inbox = os.path.join(ctx.instance, "materials", "inbox")
    os.makedirs(inbox, exist_ok=True)
    dest = os.path.join(inbox, name)
    stem, ext = os.path.splitext(dest)
    n = 1
    while os.path.exists(dest):
        dest = "%s-%d%s" % (stem, n, ext)
        n += 1
    with open(dest, "wb") as fh:
        fh.write(data)
    note = (msg.get("caption") or "").strip()
    log(ctx.instance, "info", detail="收到材料 %s（%d KB）%s" % (os.path.basename(dest), len(data) // 1024,
                                                             ("备注：" + note) if note else ""))
    _send_quiet(ctx, cid, "📥 已收到「%s」（%d KB），存进 materials/inbox/，主持人会核对后改名归档。"
                % (os.path.basename(dest), len(data) // 1024), None)


def handle_message(ctx, msg):
    cid = (msg.get("chat") or {}).get("id")
    if cid not in ctx.allowed:
        log(ctx.instance, "ignored", chat_id=cid, kind="message",
            text=(msg.get("text") or "")[:80])
        return
    if msg.get("document") or msg.get("photo"):
        _save_incoming_file(ctx, cid, msg)
        return
    text = (msg.get("text") or "").strip()
    if text.startswith("/"):
        parts = text.split()
        name = parts[0][1:].split("@")[0].lower()
        try:
            reply, buttons = dispatch(ctx, name, parts[1:])
        except Exception as exc:
            log(ctx.instance, "error", detail="指令 /%s 失败：%s" % (name, exc))
            reply, buttons = "这条指令处理出错：%s" % exc, None
        if reply:
            _send_quiet(ctx, cid, reply, buttons)
        return
    rtm = msg.get("reply_to_message") or {}
    info = tg.map_get(ctx.instance, cid, rtm.get("message_id"))
    if info and text:
        aid = info.get("announcement_id")
        if info.get("handoff_id"):          # §8.7：回复过 = 有响应，不再二次提醒
            handoffs.update(ctx.instance, info["handoff_id"],
                            interacted_at=handoffs.now_iso())
        c = write_command(ctx.instance, "reply", target=aid,
                          args={"text": text, "handoff_id": info.get("handoff_id")})
        _send_quiet(ctx, cid, "已转达 worker%s：%s" % (
            " #%s" % aid if aid is not None else "", c["id"]))
        return
    _send_quiet(ctx, cid, "没看懂。/help 看指令；要传话给某个作业，直接回复它那条带按钮的消息。")


def _send_quiet(ctx, cid, text, buttons=None):
    try:
        ctx.client.send_message(cid, text, buttons=buttons)
    except tg.TGError as exc:
        log(ctx.instance, "error", detail="sendMessage 失败：%s" % exc)


def _answer(ctx, cb, text):
    try:
        ctx.client.answer_callback(cb["id"], text)
    except tg.TGError as exc:
        log(ctx.instance, "warn", detail="answerCallbackQuery 失败：%s" % exc)


def _clear_buttons(ctx, cid, mid):
    try:
        ctx.client.edit_markup(cid, mid, [])
    except tg.TGError:
        pass


def _resolve_handoff(ctx, hid):
    try:
        handoffs.resolve(ctx.instance, hid)
    except Exception as exc:
        log(ctx.instance, "warn", detail="handoff %s 标记已处理失败：%s" % (hid, exc))


def handle_callback(ctx, cb):
    msg = cb.get("message") or {}
    cid = (msg.get("chat") or {}).get("id")
    mid = msg.get("message_id")
    data = cb.get("data") or ""
    if cid not in ctx.allowed:
        log(ctx.instance, "ignored", chat_id=cid, kind="callback", data=data[:80])
        _answer(ctx, cb, "未授权")
        return
    parts = data.split(":")
    kind = parts[0]

    if data == "a:noop":
        _answer(ctx, cb, "这条已经提交过指令，等 dispatcher 生效")
        return

    if kind == "t":                                        # /today 翻页
        page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        text, kb = today_page(ctx, page)
        try:
            ctx.client.edit_text(cid, mid, text, buttons=kb)
        except tg.TGError as exc:
            log(ctx.instance, "warn", detail="today 翻页失败：%s" % exc)
        _answer(ctx, cb)
        return

    if kind == "a" and len(parts) >= 3:                    # 今日圈选 [投]/[跳过]
        act, aid_s = parts[1], parts[2]
        page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        if not aid_s.isdigit():
            _answer(ctx, cb, "按钮数据坏了")
            return
        aid = int(aid_s)
        action = "approve" if act == "ok" else "skip"
        rec = write_command(ctx.instance, action, target=aid)
        text, kb = today_page(ctx, page)
        try:
            ctx.client.edit_text(cid, mid, text, buttons=kb)
        except tg.TGError:
            pass
        _answer(ctx, cb, _fmt_cmd(rec))
        return

    if kind == "h" and len(parts) >= 3:                    # handoff 按钮（§8.6）
        act, hid = parts[1], ":".join(parts[2:])
        info = tg.map_get(ctx.instance, cid, mid) or {}
        aid = info.get("announcement_id")
        if aid is None:
            aid = (handoffs.load(ctx.instance, hid) or {}).get("announcement_id")

        def _upd(**fields):
            return handoffs.update(ctx.instance, hid, **fields)

        def _refresh_msg():
            rec_now = handoffs.load(ctx.instance, hid) or {}
            tg.update_handoff(ctx.client, ctx.cfg, rec_now, ctx.instance)

        _upd(interacted_at=handoffs.now_iso())  # §8.7：点过按钮 = 有响应，不再二次提醒

        if act in ("ok", "scan"):            # 「好了」(旧) / 「已扫码」
            text = "好了" if act == "ok" else "已扫码"
            rec = write_command(ctx.instance, "reply", target=aid,
                                args={"text": text, "handoff_id": hid})
            if act == "scan":
                _upd(status="scanned")       # 不标 resolved：由 worker 回读页面后 handoff-update
                _refresh_msg()               # 按钮换成 [重新获取][跳过这家][我来接管]
            else:
                _upd(status="resolved",
                     resolved_at=handoffs.now_iso())
                _refresh_msg()
            _answer(ctx, cb, "已转达 worker：%s" % text)
        elif act == "ref":                   # 「重新获取」：refresh 指令，dispatcher 重派/刷新
            rec = write_command(ctx.instance, "refresh", target=aid,
                                args={"handoff_id": hid, "text": "重新获取"})
            _answer(ctx, cb, "已提交：重新获取")
        elif act == "no":                    # 「跳过」
            rec = write_command(ctx.instance, "skip", target=aid)
            _upd(status="cancelled", resolved_at=handoffs.now_iso())
            _refresh_msg()
            _answer(ctx, cb, "已提交：跳过这家")
        elif act == "me":                    # 「我来接管」
            rec = write_command(ctx.instance, "takeover", target=aid)
            h = handoffs.load(ctx.instance, hid) or {}
            _answer(ctx, cb, "正在把 ego 空间交给你…")      # 先应答按钮，ego 调用要几秒
            msg = cb.get("message") or {}
            cid, mid = (msg.get("chat") or {}).get("id"), msg.get("message_id")
            sid = h.get("ego_space")
            if str(sid or "").isdigit():
                import egospace
                ok, info = egospace.prepare_and_hand_off(sid, h.get("announcement_id"), h.get("ego_page"),
                                                         company=h.get("company"))
                if ok:
                    name = info.get("name") or ""
                    note = "🖐 已交给你：ego lite 工作区 #%s「%s」\n处理完在 ego 里点 Return to agent" % (sid, name)
                    _upd(status="scanned", action="已交给你：ego lite 工作区 #%s「%s」，处理完点 Return to agent"
                         % (sid, name))
                else:
                    note = "⚠️ 没交出：%s\n可以点「跳过这家」" % info
                    _upd(status="waiting")
            else:
                note = "⚠️ 这个关卡没记录 ego 工作区编号，找显示 Return to agent 的工作区；找不到就点「跳过这家」"
                _upd(status="scanned")
            _refresh_msg()
            if cid and mid:
                try:
                    ctx.client.send_message(cid, note, reply_to=mid)   # 新消息会响铃，工作区名一眼可见
                except (tg.TGError, TypeError) as exc:
                    log(ctx.instance, "warn", detail="接管回执发送失败：%s" % exc)
        else:
            _answer(ctx, cb, "按钮数据坏了")
            return
        log(ctx.instance, "info", command=rec)
        return

    if kind == "r" and len(parts) >= 3:                    # review_wait 按钮
        act = parts[1]
        aid = int(parts[2]) if parts[2].isdigit() else None
        if act == "ok" and aid is not None:
            rec = write_command(ctx.instance, "submit", target=aid)
            _answer(ctx, cb, "已提交：确认提交")
        elif act == "no" and aid is not None:
            rec = write_command(ctx.instance, "skip", target=aid)
            _answer(ctx, cb, "已提交：跳过")
        else:
            _answer(ctx, cb, "按钮数据坏了")
            return
        _clear_buttons(ctx, cid, mid)
        return

    _answer(ctx, cb, "不认识这个按钮")


def process_update(ctx, upd):
    if "message" in upd:
        handle_message(ctx, upd["message"])
    elif "callback_query" in upd:
        handle_callback(ctx, upd["callback_query"])


# --------------------------------------------------------------------------
# offset（state/tg-offset.json 记"已处理到的 update_id"，重启不重放）
# --------------------------------------------------------------------------

def _heartbeat(instance):
    """§8.7 兜底：守护/监控看 state/tgbot.heartbeat 的 mtime 判 bot 是否活着。"""
    try:
        path = _state(instance, "tgbot.heartbeat")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(tg.now_iso() + "\n")
    except OSError:
        pass


def _gate_watchdog(ctx):
    """§8.7：handoff 发出 5 分钟仍 waiting（没按钮没回复）→ Bark 二次提醒一次。"""
    try:
        ncfg = notify.load_config(ctx.instance)
    except Exception:
        return
    if "bark" not in ncfg["channels"]:
        return
    now = time.time()
    for h in handoffs.list_all(ctx.instance):
        if h.get("resolved_at") or (h.get("status") or "waiting") != "waiting":
            continue
        if h.get("reminded_at") or h.get("interacted_at"):
            continue                             # 已提醒过 / 用户已点过按钮或回复过
        base = tg._iso_ts(h.get("notified_at") or h.get("created_at"))
        if not base or now - base < 300:
            continue
        hid = h["id"]
        mins = int((now - base) // 60)
        title = "还没处理 · 打开 Telegram · %s" % (h.get("company") or "关卡")
        body = "%s · 已等 %d 分钟；Telegram 没收到就点链接在浏览器处理" % (
            handoffs.KIND_LABEL.get(h.get("kind") or "", "处理"), mins)
        base_url = str(ncfg.get("web_base") or "").rstrip("/")
        link = "%s/handoff/%s" % (base_url, hid) if base_url else None
        try:
            ok = notify.send_bark(ctx.instance, ncfg, "handoff",
                                  notify.build_url(ncfg, "handoff", title, body, link))
            log(ctx.instance, "info" if ok else "warn",
                detail="handoff %s 二次提醒 bark=%s" % (hid, ok))
        except Exception as exc:
            log(ctx.instance, "warn", detail="handoff %s 二次提醒失败：%s" % (hid, exc))
            continue
        handoffs.update(ctx.instance, hid, reminded_at=tg.now_iso())


def read_offset(instance):
    v = read_json(_state(instance, "tg-offset.json"), {})
    try:
        return int(v.get("offset"))
    except (TypeError, ValueError):
        return None


def write_offset(instance, update_id):
    tg.atomic_write_json(_state(instance, "tg-offset.json"), {"offset": update_id})


def cmd_run_relay(args, ctx):
    """relay 模式（§10）：签名轮询 /v1/pull → 处理 → ack；不写 getUpdates offset。"""
    try:
        http = tg.make_relay_http(ctx.cfg, api_base=getattr(args, "api_base", None),
                                  inst_dir=ctx.instance)
    except tg.TGError as exc:
        raise SystemExit("ERROR: %s" % exc)
    box = {"after": read_offset(ctx.instance) or 0}
    seen_p = _state(ctx.instance, "tg-relay-seen.json")
    seen = set()
    for x in read_json(seen_p, {}).get("qids") or []:
        try:
            seen.add(int(x))
        except (TypeError, ValueError):
            pass
    rc = tg.relay_cfg(ctx.cfg)
    poll_s = float(getattr(args, "poll_interval", None)
                   or rc.get("poll_seconds") or 2)

    def flush_seen():
        tg.atomic_write_json(seen_p, {"qids": sorted(seen)[-300:]})

    def once():
        resp = http.pull(after=box["after"])
        n = 0
        for item in resp.get("updates") or []:
            qid = int(item.get("qid") or 0)
            if qid in seen:
                try:
                    http.ack([qid])
                except tg.TGError:
                    pass
                continue
            try:
                if item.get("sys"):
                    handle_sys(ctx, item["sys"])
                elif item.get("upd") is not None:
                    process_update(ctx, item["upd"])
            except Exception as exc:
                log(ctx.instance, "error",
                    detail="relay update %s 处理失败：%s" % (qid, exc))
            try:
                http.ack([qid])
            except tg.TGError as exc:
                log(ctx.instance, "warn", detail="ack %s 失败：%s" % (qid, exc))
            seen.add(qid)
            if qid > box["after"]:
                box["after"] = qid
                write_offset(ctx.instance, qid)
            n += 1
        flush_seen()
        _gate_watchdog(ctx)
        _heartbeat(ctx.instance)
        return n

    if args.once:
        n = once()
        print("处理 %d 条 relay update" % n)
        return 0

    print("tgbot relay 启动：instance=%s iid=%s base=%s chat_ids=%s poll=%.1fs" % (
        ctx.instance, http.iid, http.base, sorted(ctx.allowed), poll_s))
    log(ctx.instance, "info", detail="bot relay 启动 iid=%s chat_ids=%s"
        % (http.iid, sorted(ctx.allowed)))
    backoff = 2
    while True:
        _heartbeat(ctx.instance)
        try:
            once()
            backoff = 2
        except tg.TGError as exc:
            if exc.fatal:
                log(ctx.instance, "error", detail="致命错误退出：%s" % exc)
                print("ERROR: %s" % exc, file=sys.stderr)
                return 2
            log(ctx.instance, "warn", detail="relay 轮询失败：%s" % exc)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        time.sleep(poll_s)


def cmd_run(args):
    ctx = build_ctx(args)
    if tg.tg_mode(ctx.cfg) == "relay":
        return cmd_run_relay(args, ctx)
    offset = read_offset(ctx.instance)
    if offset is None:
        ups = ctx.client.get_updates(timeout=0)
        if ups:
            write_offset(ctx.instance, max(int(u["update_id"]) for u in ups))
            log(ctx.instance, "info", detail="首次启动：跳过 %d 条历史消息" % len(ups))
        offset = read_offset(ctx.instance) or 0

    def once():
        ups = ctx.client.get_updates(offset=offset + 1, timeout=0)
        n = 0
        for u in ups:
            uid = int(u["update_id"])
            try:
                process_update(ctx, u)
            except Exception as exc:
                log(ctx.instance, "error", detail="update %s 处理失败：%s" % (uid, exc))
            write_offset(ctx.instance, uid)
            n += 1
        _gate_watchdog(ctx)
        _heartbeat(ctx.instance)
        return n

    if args.once:
        n = once()
        print("处理 %d 条 update" % n)
        return 0

    print("tgbot 启动：instance=%s chat_ids=%s api=%s" % (
        ctx.instance, sorted(ctx.allowed), ctx.client.api_base))
    log(ctx.instance, "info", detail="bot 启动 chat_ids=%s" % sorted(ctx.allowed))
    backoff = 2
    while True:
        _heartbeat(ctx.instance)
        try:
            ups = ctx.client.get_updates(offset=offset + 1,
                                         timeout=args.poll_timeout or POLL_TIMEOUT)
            backoff = 2
        except tg.TGError as exc:
            if exc.fatal:
                log(ctx.instance, "error", detail="致命错误退出：%s" % exc)
                print("ERROR: %s" % exc, file=sys.stderr)
                return 2
            log(ctx.instance, "warn", detail="轮询失败：%s" % exc)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        for u in ups:
            uid = int(u["update_id"])
            try:
                process_update(ctx, u)
            except Exception as exc:
                log(ctx.instance, "error", detail="update %s 处理失败：%s" % (uid, exc))
            write_offset(ctx.instance, uid)
            offset = uid
        _gate_watchdog(ctx)


def _pair_ctx(args):
    """pair/unpair 用的轻量上下文：实例目录 + notify.yaml（可为空）。"""
    instance = getattr(args, "instance", None) or os.environ.get("CAMPUS_INSTANCE")
    if not instance or not os.path.isdir(instance):
        raise SystemExit("ERROR: 实例目录不存在，传 --instance 或设置 CAMPUS_INSTANCE")
    instance = os.path.abspath(instance)
    path = os.path.join(instance, "notify.yaml")
    cfg = {}
    if os.path.isfile(path):
        cfg = yaml.safe_load(open(path, encoding="utf-8")) or {}
    if not isinstance(cfg, dict):
        raise SystemExit("ERROR: %s 不是 YAML 字典" % path)
    return instance, cfg


def _relay_base(args, rc):
    return (getattr(args, "relay_base", None) or getattr(args, "api_base", None)
            or rc.get("base") or os.environ.get("CAMPUS_RELAY_BASE")
            or tg.RELAY_BASE_DEFAULT).rstrip("/")


def cmd_update_web_base(args, instance, cfg):
    """--update-web-base：上报最新 web_base，Worker 原地编辑已置顶的面板消息。"""
    t = cfg.get("telegram") or {}
    rc = t.get("relay") or {}
    iid = str(rc.get("instance_id") or "")
    if not iid:
        raise SystemExit("ERROR: 本机还没配对，先 `tgbot.py pair`")
    web_base = str(getattr(args, "web_base", None)
                   or cfg.get("web_base") or "").rstrip("/")
    if not web_base:
        raise SystemExit("ERROR: notify.yaml 顶层没有 web_base；"
                         "先填上，或用 --web-base 显式传")
    base = _relay_base(args, rc)
    try:
        http = tg.RelayHTTP(base, iid, tg.relay_key(cfg, instance),
                            proxy=rc.get("proxy"))
        resp = http.update_web_base(web_base)
    except tg.TGError as exc:
        raise SystemExit("ERROR: 上报 web_base 失败：%s" % exc)
    tail = {"edited": "置顶面板消息已原地更新",
            "sent": "原置顶消息不可编辑，已新发一条并置顶",
            "skipped": "已存；还没绑定 chat，/pair 成功后面板会自动发",
            "failed": "已存，但面板消息发送失败（Worker 侧 TG 调用未回 ok）",
            }.get(resp.get("panel"), "")
    print("web_base=%s 已上报 Worker。%s" % (web_base, tail))
    log(instance, "info", detail="relay web_base=%s panel=%s"
        % (web_base, resp.get("panel")))
    return 0


def cmd_pair(args):
    """§10.1 配对：本机生成 instance_id + 32B 密钥 + 6 位码，向 Worker 预注册（只发哈希）。"""
    instance, cfg = _pair_ctx(args)
    t = cfg.setdefault("telegram", {})
    rc = t.get("relay") or {}
    if getattr(args, "update_web_base", False):
        return cmd_update_web_base(args, instance, cfg)
    if rc.get("instance_id") and not args.force:
        raise SystemExit("ERROR: 已配对 instance_id=%s；要换绑先 `tgbot.py unpair`，"
                         "或加 --force 强制重配" % rc["instance_id"])
    base = _relay_base(args, rc)
    iid = "i-" + secrets.token_hex(8)
    key = os.environ.get("CAMPUS_RELAY_KEY") or secrets.token_hex(32)
    code = "%06d" % (secrets.randbelow(900000) + 100000)
    code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
    web_base = str(cfg.get("web_base") or "").rstrip("/")
    payload = {"instance_id": iid, "key": key, "code_hash": code_hash,
               "nickname": args.nickname or "", "rank": not args.no_rank}
    if web_base:
        payload["web_base"] = web_base
    http = tg.RelayHTTP(base, iid, key, proxy=rc.get("proxy"))
    try:
        resp = http.call("POST", "/v1/pair/init", payload)
    except tg.TGError as exc:
        raise SystemExit("ERROR: 预注册失败：%s" % exc)

    key_cmd = None
    if not os.environ.get("CAMPUS_RELAY_KEY"):
        if shutil.which("security"):
            r = subprocess.run(
                ["security", "add-generic-password", "-s", "campus-apply-relay",
                 "-a", iid, "-w", key, "-U"],
                capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                key_cmd = ("security find-generic-password -s campus-apply-relay "
                           "-a %s -w" % iid)
            else:
                log(instance, "warn", detail="钥匙串写入失败：%s"
                    % (r.stderr or "").strip()[:200])
        if not key_cmd:
            kf = os.path.join(instance, "state", "relay.key")
            os.makedirs(os.path.dirname(kf), exist_ok=True)
            fd = os.open(kf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(key + "\n")
            key_cmd = "cat %s" % kf

    t["mode"] = "relay"
    t["relay"] = {"base": base, "instance_id": iid,
                  "nickname": args.nickname or iid, "rank": not args.no_rank}
    if key_cmd:
        t["relay"]["key_cmd"] = key_cmd
    if rc.get("proxy"):
        t["relay"]["proxy"] = rc["proxy"]
    if not t.get("allowed_chat_ids"):
        t["allowed_chat_ids"] = []          # 绑定成功后自动回填
    if not str(t.get("bot_token_cmd") or "").strip():
        # notify.py 的通道校验要求 bot_token_cmd 非空；relay 模式不会调用它，占位即可
        t["bot_token_cmd"] = "true"
    save_notify_cfg(instance, cfg)

    print("已预注册实例 %s → %s" % (iid, base))
    print("配对码：%s（%s 前有效，一次性）" % (code, resp.get("expires_at") or "10 分钟"))
    print("下一步：在 Telegram 里给共享 bot 发  /pair %s" % code)
    if not web_base:
        print("提示：notify.yaml 顶层没有 web_base，绑定后不会自动发置顶面板消息；"
              "填上后跑 `tgbot.py pair --update-web-base` 可补")
    log(instance, "info", detail="relay pair init iid=%s base=%s" % (iid, base))

    if not args.wait:
        return 0
    deadline = time.time() + args.wait
    print("等用户发 /pair ……（Ctrl-C 退出不影响，run 启动后也会自动完成绑定）")
    seen = set()
    while time.time() < deadline:
        try:
            pull = http.pull(after=0)
        except tg.TGError as exc:
            print("WARN: pull 失败：%s" % exc, file=sys.stderr)
            break
        for item in pull.get("updates") or []:
            qid = int(item.get("qid") or 0)
            if qid in seen:
                continue
            ev = item.get("sys") or {}
            if ev.get("type") == "bound":
                seen.add(qid)
                cfg2 = load_notify_cfg(instance)
                _apply_bound(instance, cfg2, int(ev.get("chat_id") or 0))
                try:
                    http.ack([qid])
                except tg.TGError:
                    pass
                print("✅ 绑定成功，chat_id=%s 已写回 notify.yaml；"
                      "`tgbot.py run` 即可收消息" % ev.get("chat_id"))
                return 0
        time.sleep(2)
    print("还没等到绑定；码过期就重新 pair。")
    return 0


# 共享 bot @your_campus_bot 的资料（幂等，`tgbot.py profile` 全量重放）
BOT_PROFILE = [
    ("setMyName", {"name": "秋招助手"}),
    ("setMyShortDescription",
     {"short_description": "替你投简历，扫码叫你，每晚九点排行。"}),
    ("setMyDescription", {"description":
        "我的秋招投递助手。先在自己的 Mac 上运行配对命令拿到 6 位码，"
        "再发 /pair 加码给我。未配对的账号我不会回复。"}),
    ("setMyCommands", {"commands": [
        {"command": "today", "description": "今日待圈选（带 [投]/[跳过] 按钮）"},
        {"command": "status", "description": "档位、暂停、在跑与待处理一览"},
        {"command": "rank", "description": "今日投递排行榜"},
        {"command": "mode", "description": "切档位 manual/supervised/auto"},
        {"command": "pause", "description": "暂停派新单"},
        {"command": "resume", "description": "恢复派单"},
        {"command": "pair", "description": "绑定本 chat：/pair <6位配对码>"},
    ]}),
]


def cmd_profile(args):
    """幂等设置共享 bot 资料：名字/简介/指令菜单，可反复跑。relay 经 Worker，direct 直连。"""
    instance, cfg = _pair_ctx(args)
    if tg.tg_mode(cfg) == "relay":
        rc = (cfg.get("telegram") or {}).get("relay") or {}
        iid = str(rc.get("instance_id") or "")
        if not iid:
            raise SystemExit("ERROR: relay 模式还没配对，先 `tgbot.py pair`")
        http = tg.RelayHTTP(_relay_base(args, rc), iid,
                            tg.relay_key(cfg, instance), proxy=rc.get("proxy"))

        def call(method, params):
            return http.profile(method, params)
    else:
        try:
            client = tg.make_client(cfg, api_base=getattr(args, "api_base", None))
        except tg.TGError as exc:
            raise SystemExit("ERROR: %s" % exc)

        def call(method, params):
            # TGClient.api 走 form 表单：非标量字段要 JSON 序列化
            return client.api(method, {
                k: json.dumps(v, ensure_ascii=False)
                if isinstance(v, (list, dict)) else v
                for k, v in params.items()})
    failed = []
    for method, params in BOT_PROFILE:
        try:
            call(method, params)
            print("ok   %s" % method)
        except tg.TGError as exc:
            failed.append(method)
            print("FAIL %s：%s" % (method, exc))
    log(instance, "warn" if failed else "info",
        detail="bot profile 设置 failed=%s" % (failed or "无"))
    if failed:
        return 1
    print("bot 资料已设置（幂等，资料漂移了再跑一遍即可）")
    return 0


def cmd_unpair(args):
    """本机吊销：通知 Worker 删除实例与队列，再清本机 relay 配置与密钥。"""
    instance, cfg = _pair_ctx(args)
    t = cfg.get("telegram") or {}
    rc = t.get("relay") or {}
    iid = str(rc.get("instance_id") or "")
    if not iid:
        print("本机没有 relay 配对，无需操作")
        return 0
    base = _relay_base(args, rc)
    try:
        http = tg.RelayHTTP(base, iid, tg.relay_key(cfg, instance),
                            proxy=rc.get("proxy"))
        http.unpair()
        print("Worker 已吊销实例 %s" % iid)
    except tg.TGError as exc:
        print("WARN: 通知 Worker 吊销失败（继续清本机配置）：%s" % exc, file=sys.stderr)
    if shutil.which("security"):
        subprocess.run(["security", "delete-generic-password", "-s",
                        "campus-apply-relay", "-a", iid],
                       capture_output=True, text=True, timeout=15)
    kf = os.path.join(instance, "state", "relay.key")
    if os.path.isfile(kf):
        os.remove(kf)
    t.pop("relay", None)
    if t.get("mode") == "relay":
        t.pop("mode")
    save_notify_cfg(instance, cfg)
    log(instance, "info", detail="relay unpair iid=%s" % iid)
    print("本机 relay 配置与密钥已清除（allowed_chat_ids 保留）")
    return 0


def cmd_whoami(args):
    ctx = build_ctx(args, need_allowed=False)
    if tg.tg_mode(ctx.cfg) == "relay":
        print("relay 模式不用 whoami：`tgbot.py pair` 配对后 chat_id 自动写进 notify.yaml")
        return 0
    print("等 %d 秒：在 Telegram 里给 bot 发任意一句话……" % args.wait)
    deadline = time.time() + args.wait
    seen, maxid = {}, None
    while time.time() < deadline and not seen:
        ups = ctx.client.get_updates(timeout=5)
        for u in ups:
            maxid = max(maxid or 0, int(u["update_id"]))
            m = u.get("message") or {}
            c = (m.get("chat") or {}).get("id")
            if c is not None:
                fr = m.get("from") or {}
                seen[c] = fr.get("username") or fr.get("first_name") or ""
    if maxid is not None:
        ctx.client.get_updates(offset=maxid + 1, timeout=0)   # 确认已读，bot 启动不重放
    if not seen:
        print("没等到消息。确认 bot_token_cmd 能取到 token、代理通，再跑一次。")
        return 1
    for cid, who in seen.items():
        print("chat_id: %s%s" % (cid, "  (@%s)" % who if who else ""))
    print("\n把 chat_id 填进 %s/notify.yaml 的 telegram.allowed_chat_ids" % ctx.instance)
    return 0


def cmd_install_plist(args):
    instance = getattr(args, "instance", None) or os.environ.get("CAMPUS_INSTANCE")
    if not instance:
        raise SystemExit("ERROR: 需要 --instance 或 CAMPUS_INSTANCE（要写死进 plist）")
    instance = os.path.abspath(os.path.expanduser(instance))
    uv = shutil.which("uv") or "/opt/homebrew/bin/uv"
    script = os.path.join(REPO, "host", "tgbot.py")
    plist = {
        "Label": LABEL,
        "ProgramArguments": [uv, "run", "--script", script,
                             "--instance", instance, "run"],
        "EnvironmentVariables": {
            "CAMPUS_REPO": REPO,
            "CAMPUS_INSTANCE": instance,
            "PATH": PATH_ENV.format(home=os.path.expanduser("~")),
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "ProcessType": "Background",
        "StandardOutPath": os.path.join(instance, "state", "tgbot.launchd.log"),
        "StandardErrorPath": os.path.join(instance, "state", "tgbot.launchd.log"),
    }
    if args.dry_run:
        sys.stdout.write(plistlib.dumps(plist).decode("utf-8"))
        return 0
    if not args.dest:
        print("ERROR: install-plist 只支持 --dry-run 或 --dest DIR（不代做 launchctl 注册）",
              file=sys.stderr)
        return 2
    dest = os.path.abspath(os.path.expanduser(args.dest))
    os.makedirs(dest, exist_ok=True)
    path = os.path.join(dest, LABEL + ".plist")
    with open(path, "wb") as fh:
        plistlib.dump(plist, fh)
    print("已写入 %s（未注册）" % path)
    print("要启用：cp 到 ~/Library/LaunchAgents 后 "
          "launchctl bootstrap gui/%d ~/Library/LaunchAgents/%s.plist" % (os.getuid(), LABEL))
    return 0


def main():
    common = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    common.add_argument("--instance", help="实例目录，默认 $CAMPUS_INSTANCE")
    common.add_argument("--api-base", help="覆盖 telegram.api_base（测试用假 API）")

    ap = argparse.ArgumentParser(description="Telegram 双向 bot（long polling）",
                                 parents=[common])
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("run", parents=[common], help="跑 long polling（默认命令）")
    p.add_argument("--once", action="store_true", help="只处理一轮就退出（测试用）")
    p.add_argument("--poll-timeout", type=int, default=POLL_TIMEOUT)
    p = sub.add_parser("whoami", parents=[common], help="打印发消息者的 chat_id（direct 模式）")
    p.add_argument("--wait", type=int, default=30)
    p = sub.add_parser("pair", parents=[common],
                       help="relay 配对：生成密钥+6 位配对码并向 Worker 预注册")
    p.add_argument("--nickname", default="", help="排行榜昵称（默认可后改）")
    p.add_argument("--no-rank", action="store_true", help="不参与每日排行榜")
    p.add_argument("--relay-base", help="你自己部署的 Worker 地址（必填，或设环境变量 CAMPUS_RELAY_BASE）")
    p.add_argument("--wait", type=int, default=0,
                   help="等用户在 Telegram 发 /pair 码的秒数（0=不等）")
    p.add_argument("--force", action="store_true", help="已有配对时强制重新预注册")
    p.add_argument("--update-web-base", action="store_true",
                   help="不重新配对：上报最新 web_base 并原地改置顶面板消息")
    p.add_argument("--web-base",
                   help="面板链接前缀（默认读 notify.yaml 顶层 web_base）")
    p = sub.add_parser("unpair", parents=[common],
                       help="relay 解绑：吊销 Worker 上的实例并清本机密钥")
    p.add_argument("--relay-base", help="Worker 地址（测试用）")
    p = sub.add_parser("profile", parents=[common],
                       help="幂等设置共享 bot 资料：名字/简介/指令菜单")
    p.add_argument("--relay-base", help="Worker 地址（测试用）")
    p = sub.add_parser("install-plist", parents=[common], help="生成 launchd plist")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--dest", help="只写 plist 文件到 DIR，不注册 launchctl")
    args = ap.parse_args()

    cmd = args.cmd or "run"
    if cmd == "run":
        return cmd_run(args)
    if cmd == "whoami":
        return cmd_whoami(args)
    if cmd == "pair":
        return cmd_pair(args)
    if cmd == "unpair":
        return cmd_unpair(args)
    if cmd == "profile":
        return cmd_profile(args)
    return cmd_install_plist(args)


if __name__ == "__main__":
    sys.exit(main())
