#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6"]
# ///
"""events.py · 数据回流写入端：worker 每一步写结构化事件，终局写 outcome（契约 docs/contracts.md §9）。

    uv run --script host/events.py event --job 10001 --step claim --msg "接单，开 TaskSpace"
    uv run --script host/events.py event --job 10001 --step portal_detected --portal beisen --msg "北森 zhiye"
    uv run --script host/events.py event --job 10001 --step gate --data kind=wechat_qr --msg "等用户扫码"
    uv run --script host/events.py outcome --job 10001 --result submitted --portal beisen \\
        --job-applied "后端开发工程师" --portal-status "简历筛选-进行中" --candidate-id C123456 \\
        --deadline 2026-10-06 --evidence\\
        --next-step "kind=assessment,due_at=2026-10-01T18:00:00+08:00,link=https://example.com/assessment,note=72h 内完成"
    uv run --script host/events.py outcome --job 10002 --result blocked --reason "缺成绩单/学生证，待材料"
    uv run --script host/events.py show --job 10001
    uv run --script host/events.py tail [--n 50] [--job 10001]
    uv run --script host/events.py stats [--since 1d]

写入位置（实例目录，永不进仓库）：
  state/events.jsonl            一行一事件 {at, job, by, pane, step, portal, msg, data}（§9.1，mkdir 锁追加）
  state/outcomes/<id>.json      终局结果（§9.2，原子替换；重复写覆盖，以最后一版为准）
  state/next-steps.jsonl        outcome 里的 next_steps 会同步追加到这里（source_msg="outcome:<id>"，
                                与 mail.py 的邮件待办同口径去重：同公司同 kind 的旧 open 标 superseded）

worker 必须写事件的时机：接单 claim、认出门户 portal_detected、登录完成 account_ready、
开始填表 filling、缺字段 field_blocked、遇到关卡 gate、提交成功 submitted、回读核验 verified、
失败或放弃 failed；其他说明用 note。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
from zoneinfo import ZoneInfo

REPO = os.environ.get("CAMPUS_REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import registry as reg  # noqa: E402

CST = ZoneInfo("Asia/Shanghai")
EVENTS_NAME = "events.jsonl"
OUTCOMES_DIR = "outcomes"
NEXT_STEPS_NAME = "next-steps.jsonl"

# §9.1：step 枚举
STEPS = ("claim", "portal_detected", "account_ready", "filling", "field_blocked",
         "gate", "submitted", "verified", "failed", "note")
STEP_LABEL = {"claim": "接单", "portal_detected": "认出门户", "account_ready": "登录完成",
              "filling": "填表中", "field_blocked": "缺字段", "gate": "遇到关卡",
              "submitted": "提交成功", "verified": "回读核验", "failed": "失败/放弃", "note": "备注"}
BYS = ("worker", "dispatcher", "mail", "user")
# §9.2：result 枚举；blocked/skipped/failed 必须带 reason
RESULTS = ("submitted", "blocked", "skipped", "failed")
NS_KINDS = ("assessment", "written_test", "interview", "offer", "other")
NS_ACTION = {"assessment": "完成在线测评", "written_test": "参加笔试",
             "interview": "参加面试", "offer": "查看 Offer", "other": "处理"}


class EventsError(Exception):
    pass


class AP(argparse.ArgumentParser):
    """参数错误给中文。"""

    def error(self, message):
        m = re.search(r"the following arguments are required: (.+)", message)
        if m:
            message = "缺少必填参数：%s" % m.group(1)
        m = re.search(r"argument (\S+): invalid choice: '([^']+)' \(choose from (.+)\)", message)
        if m:
            message = "参数 %s 的取值 %s 不合法（可选：%s）" % (m.group(1), m.group(2), m.group(3))
        m = re.search(r"argument (\S+): invalid int value: '([^']+)'", message)
        if m:
            message = "参数 %s 需要整数，收到 %s" % (m.group(1), m.group(2))
        m = re.search(r"unrecognized arguments: (.+)", message)
        if m:
            message = "不认识的参数：%s" % m.group(1)
        self.print_usage(sys.stderr)
        print("参数错误：%s" % message, file=sys.stderr)
        raise SystemExit(2)


def die(msg):
    print("ERROR: %s" % msg, file=sys.stderr)
    raise SystemExit(2)


def now_iso():
    return dt.datetime.now(CST).isoformat(timespec="seconds")


def parse_time(s):
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(str(s))
        return t if t.tzinfo else t.replace(tzinfo=CST)
    except ValueError:
        return None


def parse_since(s):
    """1d / 12h / 30m / 1w / YYYY-MM-DD → datetime；空返回 None。"""
    s = (s or "").strip()
    if not s:
        return None
    m = re.fullmatch(r"(\d+)\s*([dmhw])", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"m": dt.timedelta(minutes=n), "h": dt.timedelta(hours=n),
                 "d": dt.timedelta(days=n), "w": dt.timedelta(weeks=n)}[unit]
        return dt.datetime.now(CST) - delta
    t = parse_time(s)
    if t:
        return t
    raise EventsError("--since 格式不认识：%s（支持 30m/12h/1d/1w/YYYY-MM-DD）" % s)


def parse_kv(pairs, what="--data"):
    """["k=v", ...] → dict；值尝试按 JSON 解析（数字/布尔），否则保留字符串。"""
    out = {}
    for p in pairs or []:
        if "=" not in p:
            raise EventsError("%s 需要 k=v 格式，收到 %r" % (what, p))
        k, v = p.split("=", 1)
        k = k.strip()
        if not k:
            raise EventsError("%s 的键为空：%r" % (what, p))
        try:
            out[k] = json.loads(v)
        except ValueError:
            out[k] = v
    return out


# --------------------------------------------------------------------------
# 路径与读写
# --------------------------------------------------------------------------

def instance_dir(args):
    inst = getattr(args, "instance", None) or os.environ.get("CAMPUS_INSTANCE")
    if not inst:
        die("需要 --instance 或环境变量 CAMPUS_INSTANCE")
    inst = os.path.abspath(os.path.expanduser(inst))
    if not os.path.isdir(inst):
        die("实例目录不存在：%s" % inst)
    return inst


def events_path(instance):
    return os.path.join(instance, "state", EVENTS_NAME)


def outcome_path(instance, aid):
    return os.path.join(instance, "state", OUTCOMES_DIR, "%s.json" % aid)


def next_steps_path(instance):
    return os.path.join(instance, "state", NEXT_STEPS_NAME)


def read_json(path, default=None):
    try:
        return json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return default


def atomic_write(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".%s." % os.path.basename(path), suffix=".tmp",
                               dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


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


def append_event(instance, rec):
    """追加一条事件（mkdir+flock 双层锁，同 web/app/bank.py / host/registry.py 口径）。"""
    path = events_path(instance)
    with reg.DirLock(path):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def load_events(instance, job=None, since=None):
    """读事件流；job 按 announcement_id 过滤，since 为 datetime。"""
    out = []
    for rec in read_jsonl(events_path(instance)):
        if job is not None and str(rec.get("job")) != str(job):
            continue
        if since is not None:
            t = parse_time(rec.get("at"))
            if t is None or t < since:
                continue
        out.append(rec)
    return out


def load_outcome(instance, aid):
    rec = read_json(outcome_path(instance, aid), None)
    return rec if isinstance(rec, dict) else None


def load_outcomes(instance):
    d = os.path.join(instance, "state", OUTCOMES_DIR)
    out = {}
    if not os.path.isdir(d):
        return out
    for fn in os.listdir(d):
        if not fn.endswith(".json") or fn.startswith(".") or fn.startswith("_"):
            continue
        rec = read_json(os.path.join(d, fn), None)
        if isinstance(rec, dict) and rec.get("announcement_id") is not None:
            out[str(rec["announcement_id"])] = rec
    return out


def load_next_steps(instance):
    """next-steps.jsonl 按 id 折叠（后写覆盖先写，同 schedule.py），返回按时间序的列表。"""
    by_id = {}
    order = []
    for rec in read_jsonl(next_steps_path(instance)):
        rid = rec.get("id")
        if not rid:
            continue
        if rid not in by_id:
            order.append(rid)
        by_id[rid] = rec
    return [by_id[r] for r in order]


def norm_company(s):
    return re.sub(r"\s+", "", (s or "").strip().replace("（", "(").replace("）", ")"))


# --------------------------------------------------------------------------
# event / outcome 构建
# --------------------------------------------------------------------------

def make_event(job, step, by="worker", pane="", portal="", msg="", data=None, at=None):
    rec = {"at": at or now_iso(), "job": int(job), "by": by or "worker",
           "pane": pane or "", "step": step}
    if portal:
        rec["portal"] = portal
    if msg:
        rec["msg"] = msg
    if data:
        rec["data"] = data
    return rec


def job_record(instance, aid):
    return read_json(os.path.join(instance, "state", "jobs", "%s.json" % aid), None)


def announcement_record(aid, data_path=None):
    path = data_path or os.path.join(REPO, "data", "paperball", "announcements.jsonl")
    if not os.path.isfile(path):
        return None
    for rec in read_jsonl(path):
        if str(rec.get("announcement_id")) == str(aid):
            return rec
    return None


def next_step_id(aid, step):
    return "ns-" + hashlib.sha1(
        ("outcome|%s|%s|%s|%s" % (aid, step.get("kind") or "",
                                  step.get("due_at") or "", step.get("link") or ""))
        .encode("utf-8")).hexdigest()[:12]


def make_outcome(job, result, fields, next_steps):
    rec = {"announcement_id": int(job), "result": result}
    for k in ("company", "employer_key", "portal", "job_applied", "submitted_at",
              "portal_status", "candidate_id", "deadline", "resume", "reason"):
        if fields.get(k) not in (None, ""):
            rec[k] = fields[k]
    if fields.get("attachments"):
        rec["attachments"] = fields["attachments"]
    if fields.get("evidence"):
        rec["evidence"] = fields["evidence"]
    if next_steps:
        for s in next_steps:
            s["id"] = next_step_id(job, s)
        rec["next_steps"] = next_steps
    for k, v in (fields.get("extra") or {}).items():
        rec[k] = v
    return rec


def mirror_next_steps(instance, outcome):
    """outcome.next_steps → state/next-steps.jsonl（去重口径同 mail.py：同公司同 kind 的 open 标 superseded）。"""
    steps = outcome.get("next_steps") or []
    if not steps:
        return 0
    path = next_steps_path(instance)
    existing = read_jsonl(path)
    by_id = {r.get("id") for r in existing}
    written = 0
    with reg.DirLock(path):
        with open(path, "a", encoding="utf-8") as fh:
            for s in steps:
                sid = s.get("id") or next_step_id(outcome["announcement_id"], s)
                for old in existing:
                    if old.get("status") == "open" and old.get("kind") == s.get("kind") \
                            and norm_company(old.get("company") or "") == norm_company(outcome.get("company") or "") \
                            and old.get("id") != sid:
                        old["status"] = "superseded"     # 同步内存视图，避免重复标记
                        old["superseded_by"] = sid
                        fh.write(json.dumps(old, ensure_ascii=False) + "\n")
                if sid in by_id:
                    continue
                row = {"id": sid, "kind": s.get("kind") or "other",
                       "company": outcome.get("company") or "",
                       "announcement_id": outcome["announcement_id"],
                       "job": outcome.get("job_applied") or "",
                       "due_at": s.get("due_at"), "start_at": s.get("start_at"),
                       "link": s.get("link") or "",
                       "action": s.get("note") or NS_ACTION.get(s.get("kind") or "", "处理"),
                       "source_msg": "outcome:%s" % outcome["announcement_id"],
                       "created_at": now_iso(), "status": "open"}
                if s.get("note"):
                    row["note"] = s["note"]
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                existing.append(row)
                by_id.add(sid)
                written += 1
    return written


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------

def cmd_event(args):
    instance = instance_dir(args)
    if args.step not in STEPS:
        die("--step 只能是：%s（收到 %r）" % ("/".join(STEPS), args.step))
    if args.by not in BYS:
        die("--by 只能是：%s（收到 %r）" % ("/".join(BYS), args.by))
    try:
        data = parse_kv(args.data)
    except EventsError as exc:
        die(str(exc))
    rec = make_event(args.job, args.step, by=args.by,
                     pane=args.pane or os.environ.get("SEEDMUX_PANE_ID") or "",
                     portal=args.portal, msg=args.msg, data=data, at=args.at)
    append_event(instance, rec)
    print("已记录：%s job=%s step=%s（%s）%s" % (
        rec["at"], rec["job"], rec["step"], STEP_LABEL.get(rec["step"], ""),
        (" · " + rec["msg"]) if rec.get("msg") else ""))
    return 0


def cmd_outcome(args):
    instance = instance_dir(args)
    if args.result not in RESULTS:
        die("--result 只能是：%s（收到 %r）" % ("/".join(RESULTS), args.result))
    if args.result in ("blocked", "skipped", "failed") and not (args.reason or "").strip():
        die("result=%s 必须带 --reason 说明原因" % args.result)
    try:
        extra = parse_kv(args.set_kv, what="--set")
    except EventsError as exc:
        die(str(exc))
    # 解析 --next-step kind=...,due_at=...,link=...,note=...
    steps = []
    for raw in args.next_step or []:
        try:
            s = parse_kv([p for p in raw.split(",") if p.strip()], what="--next-step")
        except EventsError as exc:
            die(str(exc))
        s = {str(k): ("" if v is None else str(v)) for k, v in s.items()}
        if not s.get("kind"):
            die("--next-step 缺 kind：%r（格式 kind=assessment,due_at=ISO,link=URL,note=说明）" % raw)
        if s["kind"] not in NS_KINDS:
            die("--next-step 的 kind 只能是：%s（收到 %r）" % ("/".join(NS_KINDS), s["kind"]))
        if s.get("due_at") and parse_time(s["due_at"]) is None:
            die("--next-step 的 due_at 不是 ISO 时间：%r" % s["due_at"])
        steps.append(s)

    aid = int(args.job)
    fields = {
        "company": args.company, "employer_key": args.employer_key, "portal": args.portal,
        "job_applied": args.job_applied, "submitted_at": args.submitted_at,
        "portal_status": args.portal_status, "candidate_id": args.candidate_id,
        "deadline": args.deadline, "resume": args.resume, "reason": args.reason,
        "attachments": args.attachment or [], "evidence": args.evidence or [],
        "extra": extra,
    }
    # 公司名兜底：参数 → 作业记录 → 公告库
    if not fields["company"]:
        job = job_record(instance, aid) or {}
        fields["company"] = job.get("company") or (announcement_record(aid) or {}).get("company") or ""
    if not fields["employer_key"] and fields["company"]:
        fields["employer_key"] = reg.employer_key(fields["company"])
    if args.result == "submitted" and not fields["submitted_at"]:
        fields["submitted_at"] = now_iso()

    outcome = make_outcome(aid, args.result, fields, steps)
    existed = os.path.isfile(outcome_path(instance, aid))
    atomic_write(outcome_path(instance, aid), outcome)
    n_ns = mirror_next_steps(instance, outcome)
    print("已写入 %s（result=%s%s，next_steps=%d 条%s）" % (
        os.path.relpath(outcome_path(instance, aid), instance), outcome["result"],
        "，覆盖旧版" if existed else "", len(outcome.get("next_steps") or []),
        "，同步 next-steps.jsonl +%d" % n_ns if n_ns else ""))
    return 0


def fmt_event_line(rec):
    t = (rec.get("at") or "")[5:16].replace("T", " ")
    pane = (rec.get("pane") or "")[:8]
    extra = ""
    if rec.get("data"):
        extra = " " + json.dumps(rec["data"], ensure_ascii=False, separators=(",", ":"))
    return "%s  job=%s  %-15s %s%s%s%s" % (
        t, rec.get("job"), rec.get("step"),
        ("by=%s" % rec.get("by")) + ("/%s" % pane if pane else ""),
        ("  portal=%s" % rec.get("portal")) if rec.get("portal") else "",
        ("  " + rec.get("msg")) if rec.get("msg") else "", extra)


def cmd_show(args):
    instance = instance_dir(args)
    aid = args.job
    outcome = load_outcome(instance, aid)
    print("== outcome %s ==" % aid)
    if outcome:
        print(json.dumps(outcome, ensure_ascii=False, indent=2))
    else:
        print("（还没有 state/outcomes/%s.json）" % aid)
    evs = load_events(instance, job=aid)
    print("\n== 事件 %d 条 ==" % len(evs))
    for rec in evs:
        print(fmt_event_line(rec))
    return 0


def cmd_tail(args):
    instance = instance_dir(args)
    evs = load_events(instance, job=args.job)
    if args.n and len(evs) > args.n:
        evs = evs[-args.n:]
    for rec in evs:
        print(fmt_event_line(rec))
    if not evs:
        print("（还没有事件）")
    return 0


def cmd_stats(args):
    instance = instance_dir(args)
    try:
        since = parse_since(args.since)
    except EventsError as exc:
        die(str(exc))
    evs = load_events(instance, since=since)
    outcomes = load_outcomes(instance)

    by_step, by_job, by_day = {}, {}, {}
    last_by_job = {}
    for rec in evs:
        by_step[rec.get("step") or "?"] = by_step.get(rec.get("step") or "?", 0) + 1
        j = str(rec.get("job"))
        by_job[j] = by_job.get(j, 0) + 1
        last_by_job[j] = rec
        day = (rec.get("at") or "")[:10]
        by_day[day] = by_day.get(day, 0) + 1
    by_result = {}
    for o in outcomes.values():
        by_result[o.get("result") or "?"] = by_result.get(o.get("result") or "?", 0) + 1

    span = "全部" if since is None else "自 %s" % since.strftime("%m-%d %H:%M")
    print("事件 %d 条（%s）· 作业 %d 个 · outcome %d 份" % (len(evs), span, len(by_job), len(outcomes)))
    if by_step:
        print("按步骤：" + "，".join("%s %d" % (STEP_LABEL.get(k, k), v)
                                    for k, v in sorted(by_step.items(), key=lambda x: -x[1])))
    if by_result:
        print("按结果：" + "，".join("%s %d" % (k, v) for k, v in sorted(by_result.items())))
    if by_day:
        print("按天：" + "，".join("%s %d" % (d, by_day[d]) for d in sorted(by_day)))
    if last_by_job:
        print("各作业最新：")
        for j, rec in sorted(last_by_job.items(),
                             key=lambda x: x[1].get("at") or "", reverse=True):
            o = outcomes.get(j)
            tail = " · outcome=%s" % o.get("result") if o else ""
            print("  job=%s %s%s%s" % (j, (rec.get("at") or "")[5:16].replace("T", " "),
                                       STEP_LABEL.get(rec.get("step"), rec.get("step")), tail))
    return 0


def main():
    ap = AP(description="数据回流：写事件 / 写终局 outcome / 查看（契约 docs/contracts.md §9）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--instance", default=os.environ.get("CAMPUS_INSTANCE"),
                       help="实例目录（默认 $CAMPUS_INSTANCE）")

    p_ev = sub.add_parser("event", help="追加一条事件到 state/events.jsonl")
    common(p_ev)
    p_ev.add_argument("--job", required=True, type=int, help="announcement_id")
    p_ev.add_argument("--step", required=True,
                      help="步骤：" + "/".join(STEPS))
    p_ev.add_argument("--by", default="worker", help="谁写的：worker/dispatcher/mail/user，默认 worker")
    p_ev.add_argument("--pane", default="", help="pane UUID（默认取 $SEEDMUX_PANE_ID）")
    p_ev.add_argument("--portal", default="", help="门户短名，如 beisen/moka")
    p_ev.add_argument("--msg", default="", help="给人看的一行话")
    p_ev.add_argument("--data", nargs="*", default=[], metavar="k=v",
                      help="结构化补充字段，可多个（值会尝试按 JSON 解析）")
    p_ev.add_argument("--at", default=None, help="ISO 时间，默认当前（测试用）")

    p_oc = sub.add_parser("outcome", help="写终局结果 state/outcomes/<id>.json（worker 每单写一次）")
    common(p_oc)
    p_oc.add_argument("--job", required=True, type=int, help="announcement_id")
    p_oc.add_argument("--result", required=True,
                      help="结果：submitted/blocked/skipped/failed；后三个必须带 --reason")
    p_oc.add_argument("--company", default="", help="公司名（缺省从作业/公告数据补）")
    p_oc.add_argument("--employer-key", default="", help="雇主主体键（缺省按公司名归一化）")
    p_oc.add_argument("--portal", default="", help="门户短名")
    p_oc.add_argument("--job-applied", default="", help="实际投递的岗位名")
    p_oc.add_argument("--submitted-at", default=None, help="提交时间 ISO；result=submitted 缺省取当前")
    p_oc.add_argument("--portal-status", default="", help="门户回读到的状态，如 简历筛选-进行中")
    p_oc.add_argument("--candidate-id", default="", help="门户 candidateId/报名号")
    p_oc.add_argument("--deadline", default="", help="门户截止日 YYYY-MM-DD")
    p_oc.add_argument("--resume", default="", choices=["cn", "en"], help="用的哪版简历")
    p_oc.add_argument("--attachment", action="append", default=[], help="上传过的附件名，可重复")
    p_oc.add_argument("--next-step", action="append", default=[],
                      metavar="kind=K,due_at=T,link=U,note=N",
                      help="投递后待办（测评/笔试/面试），可重复")
    p_oc.add_argument("--reason", default="", help="blocked/skipped/failed 必填；submitted 可写备注")
    p_oc.add_argument("--evidence", action="append", default=[],
                      help="证据路径（log/ops/…、log/shots/…），可重复")
    p_oc.add_argument("--set", dest="set_kv", nargs="*", default=[], metavar="k=v",
                      help="追加顶层字段（扩展用，值按 JSON 解析）")

    p_sh = sub.add_parser("show", help="看某作业的 outcome + 全部事件")
    common(p_sh)
    p_sh.add_argument("--job", required=True, type=int)

    p_tl = sub.add_parser("tail", help="最近事件流（默认全部，--n 截尾部）")
    common(p_tl)
    p_tl.add_argument("--n", type=int, default=0, help="只看最近 N 条")
    p_tl.add_argument("--job", type=int, default=None, help="只看某作业")

    p_st = sub.add_parser("stats", help="事件与结果统计")
    common(p_st)
    p_st.add_argument("--since", default="", help="30m/12h/1d/1w/YYYY-MM-DD，默认全部")

    args = ap.parse_args()
    return {"event": cmd_event, "outcome": cmd_outcome, "show": cmd_show,
            "tail": cmd_tail, "stats": cmd_stats}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
