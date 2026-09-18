#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6"]
# ///
"""schedule.py · 把待办/日程同步进 macOS 日历 + 提醒事项

    uv run --script host/schedule.py sync [--instance DIR] [--calendar 秋招] [--reminders 秋招] [--dry-run]
    uv run --script host/schedule.py sync --from-agenda [...]   # 合并三来源的「我的日程」
    uv run --script host/schedule.py done <id> [--instance DIR] # next-step 或日程 id 均可
    uv run --script host/schedule.py plist                      # 生成 launchd plist（只打印，不安装）

规则（契约见 docs/inbox.md、docs/agenda.md）：
  - 默认只处理 next-steps.jsonl 里 status=open 且有 due_at / start_at 的待办；
    --from-agenda 改走 web/app/agenda.py 的三来源合并结果（待做/已约且有时间的条目）
  - 测评/笔试/面试/Offer：有开始时间按开始时间建 1 小时事件；只有截止时间 → 截止前 2 小时的 1 小时块
  - 材料/门户截止：按截止时间建块（同只有截止的口径）
  - 提醒事项：每条建两条——截止/开始前 1 天、前 2 小时（已过时刻的不再建）
  - 幂等：notes 里写标记 `campus-apply:<id>`（提醒加 :d1 / :h2 后缀），
    已存在不重复建，时间变了用 `event update` 更新；另存 state/schedule-map.json 兜底
  - `done <id>`：待办标 done + 对应提醒标完成（日历事件保留当记录）；
    日程 id 追加写 state/agenda-done.jsonl

依赖 `event` CLI（见 ~/.claude/skills/apple-events）。macOS 需要终端有
日历/提醒事项权限（系统设置 → 隐私与安全性）。无权限时 sync 直接报错，
--dry-run 仍可预览计划。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
from zoneinfo import ZoneInfo

REPO = os.environ.get("CAMPUS_REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CST = ZoneInfo("Asia/Shanghai")
MARK = "campus-apply:"
EVENT_KINDS = {"assessment": "测评", "written_test": "笔试",
               "interview": "面试", "offer": "Offer",
               "material_due": "材料截止", "portal_due": "门户截止"}
KIND_LABEL = {**EVENT_KINDS, "rejection": "拒信", "application_ack": "投递确认"}
FMT = "%Y-%m-%d %H:%M:%S"


def now() -> dt.datetime:
    return dt.datetime.now(CST)


def parse_t(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    s = str(s).strip().replace("Z", "+00:00")
    try:
        t = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=CST)


def atomic_write(path: str, text: str):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def event_cli(*a: str, timeout: int = 60) -> str:
    r = subprocess.run(["event", *a], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0 or r.stdout.startswith("Error"):
        raise RuntimeError("event %s：%s" % (a[0], (r.stdout + r.stderr).strip()[:300]))
    return r.stdout


def event_json(*a: str):
    raw = event_cli(*a, "--json")
    try:
        return json.loads(raw)
    except ValueError:
        raise RuntimeError("event 输出不是 JSON：%s" % raw[:200])


def _field(item: dict, *names):
    low = {str(k).lower(): v for k, v in item.items()}
    for n in names:
        v = low.get(n.lower())
        if v not in (None, ""):
            return v
    return None


# --------------------------------------------------------------------------
# 待办读取 / 标记
# --------------------------------------------------------------------------

def load_steps(inst: str) -> dict[str, dict]:
    """next-steps.jsonl 按 id 去重，后写覆盖先写。"""
    path = os.path.join(inst, "state", "next-steps.jsonl")
    steps: dict[str, dict] = {}
    if not os.path.isfile(path):
        return steps
    for ln in open(path, encoding="utf-8"):
        if not ln.strip():
            continue
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if r.get("id"):
            steps[r["id"]] = r
    return steps


def mark_step(inst: str, sid: str, status: str):
    path = os.path.join(inst, "state", "next-steps.jsonl")
    steps = load_steps(inst)
    if sid not in steps:
        raise SystemExit("next-steps.jsonl 里没有 %s" % sid)
    lines = []
    for r in steps.values():
        if r["id"] == sid:
            r = dict(r, status=status, completed_at=now().isoformat(timespec="seconds"))
        lines.append(json.dumps(r, ensure_ascii=False))
    atomic_write(path, "\n".join(lines) + "\n")


# --------------------------------------------------------------------------
# --from-agenda：web/app/agenda.py 的三来源合并日程
# --------------------------------------------------------------------------

def _agenda_mod():
    sys.path.insert(0, os.path.join(REPO, "web", "app"))
    import agenda
    return agenda


def load_agenda(inst: str):
    """返回 (items, warnings)。items 是 agenda.build 的原始条目（含已完成/已过期）。"""
    data = _agenda_mod().build(inst)
    return data["items"], data["warnings"]


def agenda_step(it: dict) -> dict:
    """日程条目 → plan_for 用的 step 形态。"""
    return {"id": it["id"], "kind": it["kind_key"], "company": it["company"],
            "due_at": it.get("due"), "start_at": it.get("start"),
            "link": it.get("link") or "",
            "action": it.get("action") or it["kind"], "job": ""}


# --------------------------------------------------------------------------
# 现有事件/提醒索引（按 notes 里的标记找）
# --------------------------------------------------------------------------

def existing_events(calendar: str) -> dict[str, dict]:
    start = (now() - dt.timedelta(days=120)).date().isoformat()
    end = (now() + dt.timedelta(days=730)).date().isoformat()
    out = {}
    try:
        items = event_json("calendar", "list", "--calendar", calendar,
                           "--start", start, "--end", end)
    except RuntimeError as e:
        raise RuntimeError("读日历失败（权限？）：%s" % e)
    for it in items or []:
        notes = str(_field(it, "notes", "note", "description") or "")
        m = re.search(re.escape(MARK) + r"([A-Za-z0-9_-]+)", notes)
        if m:
            out[m.group(1)] = {
                "id": _field(it, "id", "eventIdentifier", "identifier", "uid"),
                "title": _field(it, "title", "summary"),
                "start": _field(it, "start", "startDate"),
                "end": _field(it, "end", "endDate"),
            }
    return out


def existing_reminders(list_name: str) -> dict[str, dict]:
    out = {}
    try:
        items = event_json("reminders", "list", "--list", list_name, "--completed")
    except RuntimeError as e:
        raise RuntimeError("读提醒事项失败（权限？）：%s" % e)
    for it in items or []:
        notes = str(_field(it, "notes", "note") or "")
        m = re.search(re.escape(MARK) + r"([A-Za-z0-9_-]+)(?::(d1|h2))?", notes)
        if m:
            out[m.group(1) + (":" + m.group(2) if m.group(2) else "")] = {
                "id": _field(it, "id", "reminderId", "identifier", "calendarItemIdentifier"),
                "title": _field(it, "title"),
                "due": _field(it, "due", "dueDate"),
                "completed": bool(_field(it, "completed", "isCompleted")),
            }
    return out


# --------------------------------------------------------------------------
# 计划计算
# --------------------------------------------------------------------------

def plan_for(step: dict) -> dict:
    """返回 {event: {title,start,end,notes}|None, reminders: [{suffix,title,due,notes}]}。"""
    kind = step.get("kind")
    company = step.get("company") or "未知公司"
    label = KIND_LABEL.get(kind, kind or "待办")
    due, start = parse_t(step.get("due_at")), parse_t(step.get("start_at"))
    ref = due or start
    link = step.get("link") or ""
    action = step.get("action") or label
    base_notes = "%s%s\n%s · %s\n%s" % (
        MARK, step["id"], company, action, ("入口：" + link) if link else "")

    ev = None
    if kind in EVENT_KINDS and (start or due):
        if start:
            ev_start, ev_end = start, start + dt.timedelta(hours=1)
        else:  # 只有截止：截止前 2 小时的 1 小时块
            ev_start, ev_end = due - dt.timedelta(hours=2), due - dt.timedelta(hours=1)
        job = (" · " + step["job"][:24]) if step.get("job") else ""
        ev = {"title": "%s：%s%s" % (label, company, job),
              "start": ev_start, "end": ev_end, "notes": base_notes}

    reminders = []
    if ref:
        for suffix, delta, prefix in (("d1", dt.timedelta(days=1), "明天截止"),
                                      ("h2", dt.timedelta(hours=2), "2 小时后")):
            t = ref - delta
            if t < now():
                continue
            reminders.append({
                "suffix": suffix,
                "title": "%s：%s · %s" % (prefix, label, company),
                "due": t,
                "notes": "%s%s:%s\n%s" % (MARK, step["id"], suffix,
                                          "入口：" + link if link else action),
            })
    return {"event": ev, "reminders": reminders}


# --------------------------------------------------------------------------
# sync / done
# --------------------------------------------------------------------------

def load_map(inst: str) -> dict:
    try:
        return json.load(open(os.path.join(inst, "state", "schedule-map.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_map(inst: str, m: dict):
    atomic_write(os.path.join(inst, "state", "schedule-map.json"),
                 json.dumps(m, ensure_ascii=False, indent=1) + "\n")


def cmd_sync(args) -> int:
    inst = os.path.abspath(os.path.expanduser(args.instance))
    if args.from_agenda:
        items, warns = load_agenda(inst)
        for w in warns:
            print("WARN: %s" % w, file=sys.stderr)
        open_steps = [agenda_step(it) for it in items
                      if it["status"] in ("待做", "已约")
                      and (it.get("due") or it.get("start"))]
        print("日程待办（合并三来源、有时间的）：%d 条" % len(open_steps))
    else:
        steps = load_steps(inst)
        open_steps = [s for s in steps.values()
                      if s.get("status") == "open" and (parse_t(s.get("due_at")) or parse_t(s.get("start_at")))]
        print("open 且有时间的待办：%d 条" % len(open_steps))

    smap = load_map(inst)
    ev_index: dict[str, dict] = {}
    rem_index: dict[str, dict] = {}
    if not args.dry_run:
        ev_index = existing_events(args.calendar)
        rem_index = existing_reminders(args.reminders)

    n_new_ev = n_up_ev = n_new_rem = n_up_rem = 0
    for s in open_steps:
        sid = s["id"]
        plan = plan_for(s)
        entry = smap.setdefault(sid, {})

        ev = plan["event"]
        if ev:
            old = ev_index.get(sid) or {}
            old_start = parse_t(str(old.get("start") or ""))
            same = old and old_start and abs((old_start - ev["start"]).total_seconds()) < 60
            if args.dry_run:
                print("[dry-run] %s 日历事件 %s ~ %s ｜%s" % (
                    "已有" if old else "新建", ev["start"].strftime(FMT),
                    ev["end"].strftime(FMT), ev["title"]))
            elif old and same:
                entry["event"] = old["id"]
            elif old:
                event_cli("calendar", "update", "--id", str(old["id"]),
                          "--start", ev["start"].strftime(FMT), "--end", ev["end"].strftime(FMT),
                          "--title", ev["title"], "--notes", ev["notes"])
                entry["event"] = old["id"]
                n_up_ev += 1
                print("更新事件：%s" % ev["title"])
            else:
                out = event_json("calendar", "create", "--title", ev["title"],
                                 "--start", ev["start"].strftime(FMT), "--end", ev["end"].strftime(FMT),
                                 "--calendar", args.calendar, "--notes", ev["notes"])
                eid = _field(out if isinstance(out, dict) else {},
                             "id", "eventIdentifier", "identifier") or ""
                entry["event"] = eid
                n_new_ev += 1
                print("新建事件：%s（%s）" % (ev["title"], ev["start"].strftime(FMT)))

        for r in plan["reminders"]:
            key = sid + ":" + r["suffix"]
            old = rem_index.get(key) or {}
            old_due = parse_t(str(old.get("due") or ""))
            same = old and old_due and abs((old_due - r["due"]).total_seconds()) < 60
            if args.dry_run:
                print("[dry-run] %s 提醒 %s ｜%s" % (
                    "已有" if old else "新建", r["due"].strftime(FMT), r["title"]))
            elif old and same:
                entry["r_" + r["suffix"]] = old["id"]
            elif old and not old.get("completed"):
                event_cli("reminders", "update", "--id", str(old["id"]),
                          "--due", r["due"].strftime(FMT), "--title", r["title"],
                          "--notes", r["notes"])
                entry["r_" + r["suffix"]] = old["id"]
                n_up_rem += 1
                print("更新提醒：%s" % r["title"])
            elif not old:
                out = event_json("reminders", "create", "--title", r["title"],
                                 "--list", args.reminders, "--due", r["due"].strftime(FMT),
                                 "--notes", r["notes"])
                rid = _field(out if isinstance(out, dict) else {},
                             "id", "reminderId", "identifier") or ""
                entry["r_" + r["suffix"]] = rid
                n_new_rem += 1
                print("新建提醒：%s（%s）" % (r["title"], r["due"].strftime(FMT)))

    if not args.dry_run:
        save_map(inst, smap)
    print("%s：事件 新 %d / 更新 %d，提醒 新 %d / 更新 %d"
          % ("[dry-run] 计划" if args.dry_run else "完成", n_new_ev, n_up_ev, n_new_rem, n_up_rem))
    return 0


def cmd_done(args) -> int:
    inst = os.path.abspath(os.path.expanduser(args.instance))
    steps = load_steps(inst)
    sid = args.step_id
    in_steps = sid in steps
    s = steps.get(sid)
    if not in_steps:                                   # 可能是 web 端日程 id
        items, _ = load_agenda(inst)
        ag = next((it for it in items if it["id"] == sid), None)
        if ag is None:
            print("ERROR: next-steps.jsonl 与日程里都没有 %s" % sid, file=sys.stderr)
            return 2
        s = {"company": ag["company"]}
    done_n = 0
    if not args.dry_run:
        rem_index = existing_reminders(args.reminders)
        for suffix in ("d1", "h2"):
            old = rem_index.get(sid + ":" + suffix)
            if old and not old.get("completed"):
                event_cli("reminders", "update", "--id", str(old["id"]), "--completed", "true")
                done_n += 1
        if in_steps:
            mark_step(inst, sid, "done")
        else:
            _agenda_mod().mark_done(inst, sid, done=True)
    print("%s%s → done（提醒完成 %d 条；日历事件保留）"
          % ("[dry-run] " if args.dry_run else "",
             s.get("company") or sid, done_n))
    return 0


PLIST_LABEL = "com.campus-apply.schedule"


def cmd_plist(args) -> int:
    """打印 launchd plist（每 15 分钟跑一次 sync --from-agenda）。只打印，安装由用户决定。"""
    inst = os.path.abspath(os.path.expanduser(args.instance))
    repo = os.path.abspath(REPO)
    script = os.path.join(repo, "host", "schedule.py")
    log = os.path.join(inst, "log", "schedule.log")
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{PLIST_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/uv</string>
    <string>run</string>
    <string>--script</string>
    <string>{script}</string>
    <string>sync</string>
    <string>--from-agenda</string>
    <string>--instance</string>
    <string>{inst}</string>
    <string>--calendar</string><string>{args.calendar}</string>
    <string>--reminders</string><string>{args.reminders}</string>
  </array>
  <key>StartInterval</key><integer>900</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
</dict>
</plist>
"""
    sys.stdout.write(plist)
    print("# 安装（需先确认终端的日历/提醒权限）：\n"
          "#   uv run --script %s plist --instance %s > ~/Library/LaunchAgents/%s.plist\n"
          "#   launchctl bootstrap gui/$UID ~/Library/LaunchAgents/%s.plist\n"
          "# 注意：launchd 进程没有 TCC 图形会话，日历/提醒权限以实际运行为准；"
          "不行就留 cron/手动跑。" % (script, inst, PLIST_LABEL, PLIST_LABEL),
          file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="待办/日程 → 日历 + 提醒事项")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("sync", "done", "plist"):
        p = sub.add_parser(name)
        p.add_argument("--instance", default=os.environ.get("CAMPUS_INSTANCE"))
        p.add_argument("--calendar", default=None, help="默认读实例 schedule.yaml，否则 秋招")
        p.add_argument("--reminders", default=None, help="同上")
        p.add_argument("--dry-run", action="store_true")
        if name == "sync":
            p.add_argument("--from-agenda", action="store_true",
                           help="同步 web/app/agenda.py 合并后的日程（含 assessments.md/tracker 来源）")
        if name == "done":
            p.add_argument("step_id", help="next-step id 或日程 id（ag-xxxx）")
    args = ap.parse_args()
    if not args.instance or not os.path.isdir(args.instance):
        ap.error("需要 --instance 或 $CAMPUS_INSTANCE（目录要存在）")
    # 实例 schedule.yaml 配置默认日历/列表名（可缺省）
    cfg_path = os.path.join(args.instance, "schedule.yaml")
    cfg = {}
    if os.path.isfile(cfg_path):
        try:
            import yaml
            cfg = yaml.safe_load(open(cfg_path, encoding="utf-8")) or {}
        except Exception as e:
            print("WARN: schedule.yaml 解析失败（%s），用默认值" % e, file=sys.stderr)
    args.calendar = args.calendar or cfg.get("calendar") or "秋招"
    args.reminders = args.reminders or cfg.get("reminders") or "秋招"
    return {"sync": cmd_sync, "done": cmd_done, "plist": cmd_plist}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
