#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# ///
"""handoff-sweep.py · 关卡兜底收尾：worker 忘了 resolve 时，由宿主机按证据自动收掉

    uv run --script host/handoff-sweep.py [--instance DIR] [--dry-run] [--stale-min 30]

判定（任一满足就 resolve，并原地把 Telegram 关卡消息改成 ✅）：
  1. 该作业在 handoff 创建之后出现了"已越过关卡"的事件：account_ready / filling / submitted / verified，
     或又开了一个新的 handoff（说明前一道已经过了）
  2. 该作业已有 outcome（submitted / skipped / blocked / failed）——作业结束，关卡没意义了（blocked/failed 记 cancelled）

另外：scanned 状态超过 --stale-min 分钟、且没有任何后续事件的关卡，只打一行提醒，不自动改（可能 worker 卡住，需要人看）。
由 launchd 每分钟跑一次；只读事件与结果，只写 handoff 状态（经 notify.py handoff-update，保证 Telegram 消息同步）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys

REPO = os.environ.get("CAMPUS_REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASSED_STEPS = {"account_ready", "filling", "submitted", "verified"}


def parse(ts: str | None) -> dt.datetime | None:
    if not ts:
        return None
    try:
        t = dt.datetime.fromisoformat(ts)
        return t if t.tzinfo else t.astimezone()
    except ValueError:
        return None


def load_events(instance: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    p = os.path.join(instance, "state", "events.jsonl")
    if not os.path.isfile(p):
        return out
    for line in open(p, encoding="utf-8"):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        out.setdefault(str(e.get("job") or ""), []).append(e)
    return out


def close_finished_spaces(instance: str) -> None:
    import re as _re
    script = "const s = await listTaskSpaces(); console.log(JSON.stringify(s.map(x => [x.id, x.name, x.ownership])));"
    try:
        r = subprocess.run(["ego-browser", "nodejs"], input=script, capture_output=True, text=True, timeout=60)
        rows = json.loads([l for l in (r.stdout + "\n" + r.stderr).splitlines() if l.startswith("[[")][-1])
    except Exception:
        return
    now = dt.datetime.now().timestamp()
    for sid, name, owner in rows:
        m = _re.match(r"campus-(\d{5})-", name or "")
        if not m or owner != "agent":
            continue
        op = os.path.join(instance, "state", "outcomes", "%s.json" % m.group(1))
        try:
            o = json.load(open(op, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if o.get("result") in ("submitted", "verified", "skipped") and now - os.path.getmtime(op) > 600:
            subprocess.run(["ego-browser", "nodejs"], capture_output=True, text=True, timeout=60,
                           input="const t = await taskSpace(%d); await t.finish({ keep: [] });" % int(sid))
            print("关闭已投完的空间 #%s %s" % (sid, name))


def main():
    ap = argparse.ArgumentParser(description="关卡兜底收尾")
    ap.add_argument("--instance", default=os.environ.get("CAMPUS_INSTANCE"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--stale-min", type=int, default=30)
    ap.add_argument("--gate-timeout-min", type=int, default=15)
    a = ap.parse_args()
    if not a.instance:
        sys.exit("先 export CAMPUS_INSTANCE=<实例目录>")

    hdir = os.path.join(a.instance, "state", "handoffs")
    if not os.path.isdir(hdir):
        return
    events = load_events(a.instance)
    handoffs = []
    for fn in sorted(os.listdir(hdir)):
        if fn.endswith(".json"):
            try:
                handoffs.append(json.load(open(os.path.join(hdir, fn), encoding="utf-8")))
            except (OSError, ValueError):
                pass
    now = dt.datetime.now().astimezone()

    for h in handoffs:
        if h.get("resolved_at") or h.get("status") in ("resolved", "cancelled", "expired"):
            continue
        hid, aid = h.get("id"), str(h.get("announcement_id") or "")
        created = parse(h.get("created_at")) or now
        reason, status = None, "resolved"

        outcome_p = os.path.join(a.instance, "state", "outcomes", "%s.json" % aid)
        # 只认关卡之后写的结果：续投同一家时，旧的 blocked 结果不能把新关卡收掉
        outcome_fresh = os.path.isfile(outcome_p) and \
            dt.datetime.fromtimestamp(os.path.getmtime(outcome_p)).astimezone() > created
        if outcome_fresh:
            try:
                res = json.load(open(outcome_p, encoding="utf-8")).get("result")
            except ValueError:
                res = None
            if res:
                reason = "作业已有结果（%s）" % res
                status = "resolved" if res in ("submitted", "verified") else "cancelled"

        if not reason:
            for e in events.get(aid, []):
                t = parse(e.get("at"))
                if not t or t <= created:
                    continue
                step = e.get("step")
                # 只认明确指向另一道关卡的 gate 事件；worker 建 handoff 后紧跟写的 gate 事件常不带 id，不能算"已过关"
                dh = str((e.get("data") or {}).get("handoff") or (e.get("data") or {}).get("handoff_id") or "")
                other_gate = step == "gate" and dh and not (dh in hid or hid in dh)
                if step in PASSED_STEPS or other_gate:
                    reason = "关卡之后有新进展：%s %s" % (step, (e.get("msg") or "")[:30])
                    break
        for h2 in handoffs:
            if reason:
                break
            if h2.get("id") != hid and str(h2.get("announcement_id") or "") == aid \
                    and (parse(h2.get("created_at")) or created) > created:
                reason = "同一作业又开了新关卡 %s" % h2.get("id")

        # 轮到了（ready）但 worker 3 分钟没确认页面就绪 → worker 已离开，作废让下一个上来
        ready_at = parse(h.get("ready_at"))
        if not reason and h.get("status") == "ready" and ready_at and (now - ready_at).total_seconds() > 180:
            reason, status = "轮到后 3 分钟 worker 未确认页面就绪（可能已离开）", "expired"

        # 关卡队列不能被一个没人理的关卡堵死：推送后 15 分钟还在 waiting 就置 expired，让下一个上来
        notified = parse(h.get("interacted_at")) or parse(h.get("notified_at"))
        # 验证码 15 分钟；确认提交/整段登录要用户看表单、收短信，给 45 分钟（否则用户看着看着就被置 expired，worker 重推形成循环）
        limit_min = 45 if h.get("kind") in ("submit_confirm", "login") else a.gate_timeout_min
        if not reason and h.get("status") in ("waiting", "scanned") and notified \
                and (now - notified).total_seconds() > limit_min * 60:
            reason, status = "推送/接管后 %d 分钟没有进展，让位给队列下一个" % limit_min, "expired"

        if not reason:
            if h.get("status") == "scanned" and (now - created).total_seconds() > a.stale_min * 60:
                print("提醒：%s（%s）已 scanned %d 分钟无后续事件，worker 可能卡住"
                      % (hid, h.get("company"), (now - created).total_seconds() // 60))
            continue

        print("%s %s（%s）→ %s：%s" % ("[dry-run]" if a.dry_run else "收尾", hid, h.get("company"), status, reason))
        if a.dry_run:
            continue
        subprocess.run(["uv", "run", "--script", os.path.join(REPO, "host", "notify.py"), "handoff-update",
                        "--instance", a.instance, "--id", hid, "--status", status,
                        "--note", "自动收尾：" + reason], capture_output=True, text=True)

    # 每 10 分钟刷新一次 JD 存档与投递统计（jd/INDEX.md），并上报排行榜数字
    if not a.dry_run and dt.datetime.now().minute % 10 == 0:
        subprocess.run(["uv", "run", "--script", os.path.join(REPO, "host", "jd-archive.py"),
                        "--instance", a.instance], capture_output=True, text=True)
        # 已投完的公司：关掉它的 ego 空间（名字 campus-<id>-公司，结果 submitted 且 10 分钟前写的）
        close_finished_spaces(a.instance)
        # 排行榜数字也每 10 分钟上报一次（原来只在 20:50 报，白天 /rank 看到的是昨晚的数）
        subprocess.run(["uv", "run", "--script", os.path.join(REPO, "host", "report.py"), "rank-report",
                        "--instance", a.instance], capture_output=True, text=True)

    # 关卡队列：有空位就推下一个（兜底；handoff-update 本身也会触发）
    if not a.dry_run and any(h.get("status") == "queued" and not h.get("resolved_at") for h in handoffs):
        r = subprocess.run(["uv", "run", "--script", os.path.join(REPO, "host", "notify.py"), "flush",
                            "--instance", a.instance], capture_output=True, text=True)
        if "推出 0 个" not in r.stdout:
            print(r.stdout.strip())


if __name__ == "__main__":
    main()
