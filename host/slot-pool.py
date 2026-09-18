#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# ///
"""slot-pool.py · 固定 N 个槽位的投递池：一个 agent 只投一家公司，一个槽位固定一个 ego 空间

    uv run --script host/slot-pool.py --instance DIR --queue log/B-01/queue.md [--slots 5]
        [--agent devin --model swe-2-max] [--interval 20] [--max-minutes 60] [--once] [--dry-run]

每轮：
  - 槽位有任务：看 ~/.seedmux/team/tasks/<T>/meta.json；回执了（replied:*）或超时 → 关 pane、清空该槽空间的多余标签
  - 槽位空闲：从队列取下一家（跳过已有 submitted/verified/skipped 结果的、已派过的），
    用 templates/slot-task.md 渲染工单，smx-team spawn 一个新 agent，空间固定 campus-slot-<n>
状态：$INSTANCE/state/slot-pool.json；日志：state/slot-pool.log。需要在 Seedmux pane 的 shell 里运行（smx-team 要识别派发者）。
暂停：touch $INSTANCE/state/slot-pool.pause（不派新单，在跑的做完）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time

REPO = os.environ.get("CAMPUS_REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SMX = os.environ.get("SMX_BIN") or os.path.expanduser("~/.seedmux/bin/smx-team")   # Seedmux 2026-09-18 更新后新版不再支持 --agent devin，可用 SMX_BIN 指向旧版备份
TASKS = os.path.expanduser("~/.seedmux/team/tasks")
CONFIRM_YES = ("**这家是有名公司，用户要 double check**：填完所有信息停在「确认提交」前，绝不自己点提交；写 log/fields/<id>.md 字段对照表，"
               "发 handoff --kind submit_confirm（带 --space/--page，说明写清岗位与城市），保留页面等用户（最多 45 分钟）："
               "用户自己提交 → 回读投递记录写 outcome submitted；用户 skip/超时 → outcome blocked \"等用户确认提交\"")
CONFIRM_NO = "这家是中小厂：自检后直接提交。"
PORTAL_DOC = {"官网": "README.md", "其他": "README.md", "moka": "moka.md", "feishu": "feishu.md", "飞书": "feishu.md", "beisen": "beisen.md", "北森": "beisen.md"}


def now():
    return dt.datetime.now().astimezone()


def log(inst, msg):
    line = "[%s] %s" % (now().strftime("%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    with open(os.path.join(inst, "state", "slot-pool.log"), "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def load_state(inst):
    p = os.path.join(inst, "state", "slot-pool.json")
    try:
        return json.load(open(p, encoding="utf-8"))
    except (OSError, ValueError):
        return {"slots": {}, "dispatched": []}


def save_state(inst, st):
    p = os.path.join(inst, "state", "slot-pool.json")
    tmp = p + ".tmp"
    json.dump(st, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def read_queue(path):
    rows = []
    for line in open(path, encoding="utf-8"):
        c = [x.strip() for x in line.split("|")]
        if len(c) >= 7 and re.fullmatch(r"\d{5}", c[2]):
            rows.append({"aid": c[2], "company": c[3], "kind": c[4], "portal": c[5]})
    return rows


def final(inst, aid):
    p = os.path.join(inst, "state", "outcomes", "%s.json" % aid)
    try:
        return json.load(open(p, encoding="utf-8")).get("result") in ("submitted", "verified", "skipped")
    except (OSError, ValueError):
        return False


def ego(script, timeout=90):
    r = subprocess.run(["ego-browser", "nodejs"], input=script, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def reset_space(inst, slot):
    """清空槽位空间：只留 p1 且导航到 about:blank；空间不存在就建。用户正控制时跳过。"""
    name = "campus-slot-%s" % slot
    script = """
const s = await listTaskSpaces();
const sp = s.find(x => x.name === %s);
if (sp && sp.ownership !== "agent") { console.log("BUSY " + sp.ownership); }
else {
  const t = await taskSpace(%s);
  const tabs = await t.tabs();
  for (const x of tabs) { if (x.label !== "p1") { try { await t.page(x.label).close(); } catch (e) {} } }
  try { await t.page("p1").goto("about:blank"); } catch (e) {}
  console.log("READY " + t.spaceId);
}
""" % (json.dumps(name), json.dumps(name))
    try:
        rc, out = ego(script)
    except subprocess.TimeoutExpired:
        return False
    if "BUSY" in out:
        log(inst, "槽位 %s 的空间在用户手里，等用户点 Return to agent" % slot)
        return False
    return "READY" in out


def close_pane(pane):
    for text, enter in (("\x1b", False), ("\x1b", False), ("/exit", True), ("exit", True)):
        args = [SMX, "send", "--to", pane, "--text", text]
        if not enter:
            args.append("--no-enter")
        subprocess.run(args, capture_output=True, text=True)
        time.sleep(3)


FAMOUS_TAGS = {"一线大厂", "冷门大厂", "腰部名企"}   # 项目约定：不含宽泛的「行业翘楚」
_TAGS = {}


def company_tags(aid):
    if not _TAGS:
        p = os.path.join(REPO, "data", "paperball", "announcements.jsonl")
        for line in open(p, encoding="utf-8"):
            try:
                a = json.loads(line)
            except ValueError:
                continue
            _TAGS[str(a.get("announcement_id"))] = a.get("company_tags") or []
    return _TAGS.get(str(aid), [])


def needs_confirm(inst, row):
    """有名公司（大厂/名企标签）或 intent 点名的公司 → 用户 double check。"""
    if FAMOUS_TAGS & set(company_tags(row["aid"])):
        return True
    try:
        text = open(os.path.join(inst, "intent.yaml"), encoding="utf-8").read()
        m = re.search(r"user_confirm_companies:\s*\[([^\]]*)\]", text)
        names = [x.strip() for x in m.group(1).split(",")] if m else []
    except OSError:
        names = []
    return any(n and (n in row["company"] or row["company"] in n) for n in names)


def render(inst, slot, row):
    tpl = open(os.path.join(REPO, "templates", "slot-task.md"), encoding="utf-8").read()
    doc = PORTAL_DOC.get(row["portal"].lower(), PORTAL_DOC.get(row["portal"], "README.md"))
    body = (tpl.replace("{company}", row["company"]).replace("{aid}", row["aid"]).replace("{slot}", str(slot))
            .replace("{kind}", row["kind"]).replace("{space}", "campus-slot-%s" % slot)
            .replace("{portal_doc}", doc).replace("{instance}", os.path.abspath(inst)).replace("{repo}", REPO)
            .replace("{queue}", row.get("queue") or "队列文件")
            .replace("{confirm}", CONFIRM_YES if needs_confirm(inst, row) else CONFIRM_NO))
    p = os.path.join(inst, "state", "slot-tasks", "slot%s-%s.md" % (slot, row["aid"]))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w", encoding="utf-8").write(body)
    return p


def spawn(a, task_file):
    cmd = [SMX, "spawn", "--agent", a.agent, "--cwd", a.instance, "--task-file", task_file,
           "--direction", "right", "--acceptance", "true"]
    # 不传 --scope：seedmux 拿派单时与交活时的「脏文件表」比，实例目录是 5 个 worker + 邮件任务共用的，
    # 每小时的自动快照还会把几百个脏文件一次变干净，全被算成本任务越界，回执一律被降成 failed
    if a.model:
        cmd[4:4] = ["--model", a.model]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    m = re.search(r"task=(T-\w+) pane=([\w-]+)", r.stdout)
    return (m.group(1), m.group(2)) if m else (None, (r.stderr or r.stdout)[-200:])


RATE_RE = re.compile(r"rate limit|Something went wrong|Connection lost, retrying", re.I)
_LAST_NUDGE = {"at": 0.0}


def nudge_if_rate_limited(inst, slot, cur, st):
    """Devin 并发多时会触发模型限流并停在 "Send a message to retry"：自动发「继续」。
    全池每 30 秒最多叫醒一个（错开，避免一起醒来再次限流）；同一槽位 3 分钟内不重复叫。"""
    t = time.time()
    if t - _LAST_NUDGE["at"] < 30 or t - float(cur.get("nudged_at") or 0) < 180:
        return
    r = subprocess.run([SMX, "capture", cur["pane"], "-n", "12"], capture_output=True, text=True)
    tail = r.stdout or ""
    if not RATE_RE.search(tail) or "retry" not in tail.lower():
        return
    subprocess.run([SMX, "send", "--to", cur["pane"], "--text",
                    "继续。刚才触发了模型限流，已恢复，从中断处接着做（先读页面确认当前状态，用户可能已处理完关卡）。"],
                   capture_output=True, text=True)
    _LAST_NUDGE["at"] = t
    st.setdefault("rate_limits", []).append(t)
    st["rate_limits"] = [x for x in st["rate_limits"] if t - x < 1800]
    cur["nudged_at"] = t
    cur["nudges"] = int(cur.get("nudges") or 0) + 1
    save_state(inst, st)
    log(inst, "槽位 %s：%s 触发模型限流，已自动叫醒（第 %d 次）" % (slot, cur["company"], cur["nudges"]))


def throttled(st, a):
    """自适应限流：10 分钟内叫醒过 ≥3 次，说明并发太高——暂停派新单，直到连续 10 分钟没再限流。
    在跑的继续做完，实际并发自然降下来。"""
    t = time.time()
    hits = [x for x in st.get("rate_limits") or [] if t - x < 600]
    if len(hits) >= 3:
        if not st.get("throttled_since"):
            st["throttled_since"] = t
        return True
    if st.get("throttled_since"):
        st.pop("throttled_since", None)
    return False


def gate_active(inst, aid):
    import glob as _g
    for p in _g.glob(os.path.join(inst, "state", "handoffs", "*-%s-*.json" % aid)):
        try:
            if json.load(open(p, encoding="utf-8")).get("status") in ("queued", "ready", "waiting", "scanned"):
                return True
        except (OSError, ValueError):
            pass
    return False


def tick(a, st):
    inst = a.instance
    qpath = os.path.join(inst, a.queue) if not os.path.isabs(a.queue) else a.queue
    queue = read_queue(qpath)
    for r in queue:
        r["queue"] = qpath
    paused = os.path.exists(os.path.join(inst, "state", "slot-pool.pause"))
    for slot in map(str, range(1, a.slots + 1)):
        cur = st["slots"].get(slot)
        if cur:
            meta = {}
            try:
                meta = json.load(open(os.path.join(TASKS, cur["task"], "meta.json"), encoding="utf-8"))
            except (OSError, ValueError):
                pass
            status = str(meta.get("status") or "")
            if not status.startswith("replied") and not a.dry_run:
                nudge_if_rate_limited(inst, slot, cur, st)
            age = (now() - dt.datetime.fromisoformat(cur["started"])).total_seconds() / 60
            limit = a.max_minutes * (2 if gate_active(inst, cur["aid"]) else 1)   # 在等用户（关卡/确认）时放宽到 2 倍
            if status.startswith("replied") or age > limit:
                why = status if status.startswith("replied") else "超时 %d 分钟" % age
                # 回执状态会被 seedmux 的 scope 检查误降成 failed（共享实例目录里别人的改动也算越界），以 outcome 为准
                try:
                    oc = json.load(open(os.path.join(inst, "state", "outcomes", "%s.json" % cur["aid"]), encoding="utf-8"))
                    why = "%s（回执 %s）" % (oc.get("result") or "无结果", why)
                except (OSError, ValueError):
                    why = "无 outcome（回执 %s）" % why
                log(inst, "槽位 %s：%s（%s）结束 → %s" % (slot, cur["company"], cur["aid"], why))
                if not a.dry_run:
                    close_pane(cur["pane"])
                st["slots"].pop(slot)
                save_state(inst, st)
            else:
                continue
        if paused or throttled(st, a):
            continue
        nxt = next((r for r in queue if r["aid"] not in st["dispatched"] and not final(inst, r["aid"])), None)
        if not nxt:
            continue
        if a.dry_run:
            log(inst, "[dry-run] 槽位 %s ← %s（%s）" % (slot, nxt["company"], nxt["aid"]))
            st["dispatched"].append(nxt["aid"])
            continue
        if not reset_space(inst, slot):
            continue
        task, pane = spawn(a, render(inst, slot, nxt))
        if not task:
            log(inst, "槽位 %s 派单失败：%s" % (slot, pane))
            continue
        st["dispatched"].append(nxt["aid"])
        st["slots"][slot] = {"task": task, "pane": pane, "aid": nxt["aid"], "company": nxt["company"],
                             "started": now().isoformat(timespec="seconds")}
        save_state(inst, st)
        log(inst, "槽位 %s ← %s（%s）task=%s" % (slot, nxt["company"], nxt["aid"], task))


def main():
    ap = argparse.ArgumentParser(description="固定槽位投递池")
    ap.add_argument("--instance", default=os.environ.get("CAMPUS_INSTANCE"))
    ap.add_argument("--queue", required=True)
    ap.add_argument("--slots", type=int, default=5)
    ap.add_argument("--agent", default="devin")
    ap.add_argument("--model", default="swe-2-max")
    ap.add_argument("--interval", type=int, default=20)
    ap.add_argument("--max-minutes", type=int, default=60)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not a.instance:
        sys.exit("先 export CAMPUS_INSTANCE=<实例目录>")
    a.instance = os.path.abspath(a.instance)
    st = load_state(a.instance)
    while True:
        try:
            st = load_state(a.instance)          # 每轮重读：允许人工改 state（重排、延时）
            tick(a, st)
        except Exception as exc:  # 常驻进程不因单轮异常退出
            log(a.instance, "本轮异常：%s" % exc)
        if a.once:
            break
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
