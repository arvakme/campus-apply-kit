"""live.py · 「在跑批次」区块：数据 + 渲染片段（契约 docs/contracts.md §9.3 看板「在跑批次」）。

数据源（全部只读，容器内 /instance 可达，不依赖 host/）：
  state/jobs/<id>.json        作业状态机（dispatcher 写）
  state/events.jsonl          worker 每步写的事件（host/events.py 写）
  state/outcomes/<id>.json    终局结果（卡在什么上/最近一步用）
  state/handoffs/*.json       未 resolved 的关卡（「等我处理」计数与内容）

用法（接线见 docs/automation.md §5）：
  import live
  data = live.build(INSTANCE)            # dict：running / events / waiting
  frag = live.render_fragment(data)      # 嵌进看板页的 HTML 片段
  page = live.page(INSTANCE)             # 独立整页（可选 /live 路由）
"""
import datetime as dt
import html
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))

ACTIVE_STATUSES = {"queued", "dispatched", "filling", "gate_wait", "review_wait",
                   "submitting", "held"}
WORKING = {"dispatched", "filling", "submitting"}
STATUS_LABEL = {"queued": "排队", "dispatched": "已派单", "filling": "填表中",
                "gate_wait": "等用户", "review_wait": "待终审", "submitting": "提交中",
                "held": "已暂停", "submitted": "已投递", "verified": "已投递",
                "blocked": "已阻塞", "skipped": "已跳过", "failed": "已失败"}
STEP_LABEL = {"claim": "接单", "portal_detected": "认出门户", "account_ready": "登录完成",
              "filling": "填表中", "field_blocked": "缺字段", "gate": "遇到关卡",
              "submitted": "提交成功", "verified": "回读核验", "failed": "失败/放弃",
              "note": "备注"}
GATE_LABEL = {"wechat_qr": "微信扫码", "qr": "扫码", "captcha": "图形验证码", "face": "人脸",
              "sms_code": "短信码", "register": "注册", "login": "登录", "attachments": "材料",
              "submit_confirm": "提交终审"}
EVENT_WINDOW_H = 24
EVENT_LIMIT = 100


def e(v):
    return html.escape("" if v is None else str(v), quote=True)


def parse_time(s):
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(str(s))
        return t if t.tzinfo else t.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)
    except ValueError:
        return None


def read_jsonl(path):
    out = []
    if not os.path.isfile(path):
        return out
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def read_json(path):
    try:
        return json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_jobs(instance):
    d = os.path.join(instance, "state", "jobs")
    out = {}
    if not os.path.isdir(d):
        return out
    for fn in os.listdir(d):
        if not fn.endswith(".json") or fn.startswith((".", "_")):
            continue
        rec = read_json(os.path.join(d, fn))
        if isinstance(rec, dict) and rec.get("announcement_id") is not None:
            out[str(rec["announcement_id"])] = rec
    return out


def load_outcomes(instance):
    d = os.path.join(instance, "state", "outcomes")
    out = {}
    if not os.path.isdir(d):
        return out
    for fn in os.listdir(d):
        if not fn.endswith(".json") or fn.startswith((".", "_")):
            continue
        rec = read_json(os.path.join(d, fn))
        if isinstance(rec, dict) and rec.get("announcement_id") is not None:
            out[str(rec["announcement_id"])] = rec
    return out


def load_events(instance):
    return read_jsonl(os.path.join(instance, "state", "events.jsonl"))


def open_handoffs(instance):
    d = os.path.join(instance, "state", "handoffs")
    out = []
    if not os.path.isdir(d):
        return out
    for fn in os.listdir(d):
        if not fn.endswith(".json") or fn.startswith("."):
            continue
        rec = read_json(os.path.join(d, fn))
        if isinstance(rec, dict) and rec.get("id") and not rec.get("resolved_at"):
            out.append(rec)
    out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return out


def pane_short(pane):
    return (pane or "")[:8]


def human_delta(t_from, now):
    if not t_from:
        return "—"
    sec = max(0, int((now - t_from).total_seconds()))
    if sec < 3600:
        return "%dm" % (sec // 60)
    if sec < 86400:
        return "%dh%02dm" % (sec // 3600, (sec % 3600) % 3600 // 60)
    return "%dd%dh" % (sec // 86400, (sec % 86400) // 3600)


# --------------------------------------------------------------------------
# 数据
# --------------------------------------------------------------------------

def build(instance, now=None):
    """→ {generated_at, running: [...], events: [...], waiting: n, waiting_items: [...]}"""
    now = now or dt.datetime.now().astimezone()
    jobs = load_jobs(instance)
    outcomes = load_outcomes(instance)
    events = load_events(instance)
    handoffs = open_handoffs(instance)

    last_ev, first_ev = {}, {}
    for rec in events:
        j = str(rec.get("job") or "")
        if not j:
            continue
        last_ev[j] = rec
        first_ev.setdefault(j, rec)
    handoff_by_aid = {}
    for h in handoffs:
        handoff_by_aid.setdefault(str(h.get("announcement_id") or ""), h)

    running = []
    for aid, job in jobs.items():
        st = job.get("status") or ""
        if st not in ACTIVE_STATUSES:
            continue
        lev = last_ev.get(aid)
        outcome = outcomes.get(aid)
        hist = job.get("history") or []
        started = parse_time((hist[0].get("at") if hist else "") or job.get("created_at")) \
            or parse_time((first_ev.get(aid) or {}).get("at"))

        gate = job.get("gate") or {}
        ho = handoff_by_aid.get(aid)
        stuck_on = ""
        if st == "gate_wait":
            kind = gate.get("kind") or (ho or {}).get("kind") or ""
            stuck_on = "等用户·%s" % GATE_LABEL.get(kind, kind or "处理")
            if ho and ho.get("title"):
                stuck_on += "（%s）" % ho["title"]
        elif st == "review_wait":
            stuck_on = "字段对照表待用户终审"
            if ho and ho.get("title"):
                stuck_on = ho["title"]
        elif st == "held":
            stuck_on = "用户暂停（/resume 恢复）"
        elif outcome and outcome.get("result") in ("blocked", "failed"):
            stuck_on = outcome.get("reason") or outcome.get("result")
        elif st in WORKING and lev:
            t = parse_time(lev.get("at"))
            if t and (now - t).total_seconds() > 1800:
                stuck_on = "最近事件 %s 前无新进展" % human_delta(t, now)
        elif st in WORKING and not lev:
            stuck_on = "还没有事件记录"

        running.append({
            "id": aid, "company": job.get("company") or (outcome or {}).get("company") or "?",
            "tier": job.get("tier") or "", "portal": job.get("portal")
            or (outcome or {}).get("portal") or "",
            "status": st, "status_label": STATUS_LABEL.get(st, st),
            "step": (lev or {}).get("step") or "",
            "step_label": STEP_LABEL.get((lev or {}).get("step") or "", "—"),
            "pane": pane_short(job.get("pane")),
            "task_id": job.get("task_id") or "",
            "elapsed": human_delta(started, now),
            "last_event": ({"at": (lev.get("at") or "")[5:16].replace("T", " "),
                            "msg": lev.get("msg") or STEP_LABEL.get(lev.get("step"), ""),
                            "step": lev.get("step") or ""} if lev else None),
            "stuck_on": stuck_on,
        })
    rank = {"gate_wait": 0, "review_wait": 1, "held": 2, "queued": 4}
    running.sort(key=lambda r: (rank.get(r["status"], 3), r["id"]))

    since = now - dt.timedelta(hours=EVENT_WINDOW_H)
    recent = [rec for rec in events
              if (parse_time(rec.get("at")) or now) >= since]
    recent.sort(key=lambda r: r.get("at") or "", reverse=True)   # 按事件时间倒序，不按文件追加序
    recent_items = [{
        "at": (rec.get("at") or "")[5:16].replace("T", " "),
        "job": rec.get("job"), "company": (jobs.get(str(rec.get("job"))) or {}).get("company") or "",
        "step": rec.get("step") or "", "step_label": STEP_LABEL.get(rec.get("step") or "", rec.get("step") or ""),
        "by": rec.get("by") or "", "pane": pane_short(rec.get("pane")),
        "msg": rec.get("msg") or "",
    } for rec in recent[:EVENT_LIMIT]]

    waiting_items = [{"id": h.get("id"), "company": h.get("company") or "",
                      "aid": h.get("announcement_id"), "kind": h.get("kind") or "",
                      "kind_label": GATE_LABEL.get(h.get("kind") or "", h.get("kind") or ""),
                      "title": h.get("title") or "", "since": h.get("created_at") or ""}
                     for h in handoffs]
    for r in running:
        if r["status"] == "review_wait" and r["id"] not in handoff_by_aid:
            waiting_items.append({"id": "", "company": r["company"], "aid": r["id"],
                                  "kind": "submit_confirm", "kind_label": "提交终审",
                                  "title": "字段对照表待终审（无 handoff 记录）",
                                  "since": ""})

    return {"generated_at": now.strftime("%Y-%m-%d %H:%M"), "running": running,
            "events": recent_items, "waiting": len(waiting_items),
            "waiting_items": waiting_items,
            "counts": {"running": len(running), "events_24h": len(recent_items),
                       "waiting": len(waiting_items)}}


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------

LIVE_CSS = """
  .live-jobs { border-top: var(--lw-grid) solid var(--grid); }
  .live-job { display: grid; grid-template-columns: 1fr auto; gap: 2px 12px;
              padding: 9px 0; border-bottom: var(--lw-grid) solid var(--grid); }
  .live-job .nm { font-weight: 700; }
  .live-job .st { font-family: var(--font-mono); font-size: 12px; color: var(--ink-soft);
                  white-space: nowrap; }
  .live-job .dt { grid-column: 1 / 3; color: var(--ink-soft); font-size: 13px; }
  .live-job .stuck { color: var(--seq3); }
  .live-ev { font-family: var(--font-mono); font-size: 12px; color: var(--ink-soft);
             padding: 3px 0; border-bottom: 1px dashed var(--grid); }
  .live-ev b { color: var(--ink); font-weight: 400; }
"""


def render_fragment(data):
    """嵌进看板/榜单页的「在跑批次」区块。返回 <section> HTML。"""
    c = data.get("counts") or {}
    parts = ['<style>%s</style>' % LIVE_CSS,
             '<section id="live"><h2>在跑批次 <span class="hint">'
             '<span class="num">%d</span> 个作业 · 等我处理 <span class="num">%d</span> 件 · '
             '24h 事件 <span class="num">%d</span> 条 · 更新 %s</span></h2>'
             % (c.get("running", 0), c.get("waiting", 0), c.get("events_24h", 0),
                e(data.get("generated_at") or ""))]
    if not data.get("running"):
        parts.append('<div class="missing">没有在跑的作业</div>')
    else:
        rows = []
        for r in data["running"]:
            meta = []
            if r.get("tier"):
                meta.append('<span class="tag">%s</span>' % e(r["tier"]))
            if r.get("portal"):
                meta.append('<span class="tag">%s</span>' % e(r["portal"]))
            meta.append('<span class="tag %s">%s</span>'
                        % ("hot" if r["status"] in ("gate_wait", "review_wait") else "blue",
                           e(r["status_label"])))
            det = []
            if r.get("step_label") and r["step_label"] != "—":
                det.append("当前步骤 %s" % e(r["step_label"]))
            if r.get("pane"):
                det.append("worker %s" % e(r["pane"]))
            if r.get("elapsed"):
                det.append("已耗时 %s" % e(r["elapsed"]))
            if r.get("last_event"):
                det.append("最近 %s %s" % (e(r["last_event"]["at"]), e(r["last_event"]["msg"])))
            if r.get("stuck_on"):
                det.append('<span class="stuck">卡在：%s</span>' % e(r["stuck_on"]))
            rows.append('<div class="live-job"><span class="nm">%s <span class="num">%s</span></span>'
                        '<span class="st">%s</span><div class="dt">%s</div></div>'
                        % (e(r["company"]), e(r["id"]), "".join(meta), " · ".join(det)))
        parts.append('<div class="live-jobs">%s</div>' % "".join(rows))
    if data.get("waiting_items"):
        items = "".join('<div class="live-ev">%s <b>%s</b> %s%s</div>' % (
            e((w.get("since") or "")[5:16].replace("T", " ")), e(w.get("company") or ""),
            e(w.get("title") or w.get("kind_label") or ""),
            ' <a href="/handoff/%s">处理</a>' % e(w["id"]) if w.get("id") else "")
            for w in data["waiting_items"])
        parts.append('<h2>等我处理 <span class="hint"><span class="num">%d</span> 件</span></h2>%s'
                     % (len(data["waiting_items"]), items))
    if data.get("events"):
        evs = "".join('<div class="live-ev">%s <b>%s%s</b> %s%s</div>' % (
            e(x["at"]), e(x.get("company") or ""), ("·%s" % x["job"]) if x.get("job") else "",
            e(x["step_label"]), (" — " + e(x["msg"])) if x.get("msg") else "")
            for x in data["events"])
        parts.append('<h2>最近 %d 小时事件 <span class="hint"><span class="num">%d</span> 条</span></h2>%s'
                     % (EVENT_WINDOW_H, len(data["events"]), evs))
    parts.append('</section>')
    return "".join(parts)


def render_text(data):
    """纯文本摘要（team-msg / 日志用）。"""
    c = data.get("counts") or {}
    lines = ["在跑 %d · 等我处理 %d · 24h 事件 %d（%s）"
             % (c.get("running", 0), c.get("waiting", 0), c.get("events_24h", 0),
                data.get("generated_at") or "")]
    for r in data.get("running") or []:
        line = "  %s %s [%s%s] %s·%s · %s" % (
            r["company"], r["id"], r.get("tier") or "-", "/" + r["portal"] if r.get("portal") else "",
            r["status_label"], r.get("step_label") or "—", r.get("elapsed") or "—")
        if r.get("last_event"):
            line += " · 最近 %s %s" % (r["last_event"]["at"], r["last_event"]["msg"])
        if r.get("stuck_on"):
            line += " · 卡在：%s" % r["stuck_on"]
        lines.append(line)
    for w in data.get("waiting_items") or []:
        lines.append("  等我：%s %s（%s）" % (w.get("company") or "", w.get("title") or w.get("kind_label"), w.get("id") or "—"))
    return "\n".join(lines)


def page(instance):
    """独立整页（可选 /live 路由用；不在 server.py 接线时不调用）。"""
    import pages  # noqa: 延迟导入，render_fragment 不需要 pages
    data = build(instance)
    body = ('<h1>在跑批次</h1><p class="sub">worker 结构化事件的实时视图（contracts §9）</p>'
            + render_fragment(data))
    return pages.shell("在跑批次", "/", body, instance)
