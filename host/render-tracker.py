#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6"]
# ///
"""render-tracker.py · 台账与测评表的生成器（契约 docs/contracts.md §9.3）。

    uv run --script host/render-tracker.py                     # 生成 tracker.generated.md / assessments.generated.md
    uv run --script host/render-tracker.py --check             # 与手写版逐行比对，有差异退出码 1（迁移期对账）
    uv run --script host/render-tracker.py --write-live        # 直接覆盖 tracker.md / assessments.md（迁移完成后才用）
    uv run --script host/render-tracker.py --stdout            # 只打印不落盘

输入（全部在实例 state/ 下，只读）：
  state/jobs/<id>.json       作业状态机（dispatcher 写，§8.2）
  state/outcomes/<id>.json   终局结果（worker 写，§9.2）
  state/registry.jsonl       已投登记（§8.4）
  state/events.jsonl         事件流（§9.1，给在跑行的「记录」列补最近一步）
  state/next-steps.jsonl     邮件 + outcome 汇合的待办（assessments 表数据源之一）
  data/paperball/announcements.jsonl、data/tiers.yaml（仓库侧兜底公司名/层级）

生成物表头与手写 tracker.md / assessments.md 完全一致，dashboard.py / registry.parse_tracker
能直接读。迁移期手写版仍是真源：本脚本只写 *.generated.md 供对照；--write-live 是切换开关，
默认关闭，切前必须 --check 对账并把差异分类处理完。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
from zoneinfo import ZoneInfo

REPO = os.environ.get("CAMPUS_REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import events as ev  # noqa: E402
import registry as reg  # noqa: E402

import yaml  # noqa: E402

CST = ZoneInfo("Asia/Shanghai")
TRACKER_HEADERS = ["公司", "id", "层/类别", "岗位", "门户/方式", "状态", "关键日期", "记录"]
ASSESS_HEADERS = ["公司", "类型", "链接/入口", "期限", "来源", "状态"]

# tracker 状态词（spec §6.2 口径）：候选/查岗/填表中/待提交/已投递/待笔试/已笔试/面试/Offer/不合适/已停止/待投递·x
JOB_STATUS_TEXT = {
    "queued": "排队", "dispatched": "查岗", "filling": "填表中",
    "submitting": "待提交（提交中）", "review_wait": "待提交·待终审",
    "held": "已停止（暂停）",
}
GATE_LABEL = {"wechat_qr": "扫码", "qr": "扫码", "captcha": "图形验证码", "face": "人脸",
              "sms_code": "短信码", "register": "注册", "login": "登录", "attachments": "材料",
              "submit_confirm": "提交确认"}
PORTAL_LABEL = {"beisen": "北森", "moka": "Moka", "feishu": "飞书招聘", "zhilian": "智联",
                "hotjob": "hotjob", "wejob": "wejob", "nowcoder": "牛客", "hik": "海康门户",
                "weaver": "泛微门户", "official": "官网", "self": "用户自投", "email": "邮件"}
NS_LABEL = {"assessment": "测评", "written_test": "笔试", "interview": "面试",
            "offer": "Offer", "other": "待办"}
UNFIT_HINT = ("不合适", "不符", "不含", "不招", "截止", "exclude", "届")


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


def cell(v):
    """单元格内容清洗：竖线转义、换行压平。"""
    return str(v if v is not None else "").replace("|", "\\|").replace("\n", " ").strip()


def atomic_write_text(path, text):
    fd, tmp = tempfile.mkstemp(prefix=".%s." % os.path.basename(path), suffix=".tmp",
                               dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def fmt_day(iso):
    """ISO → MM-DD；无时间部分的原样返回。"""
    if not iso:
        return ""
    s = str(iso)
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})", s)
    if m:
        return "%s-%s %s:%s" % (m.group(2), m.group(3), m.group(4), m.group(5))
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    return "%s-%s" % (m.group(2), m.group(3)) if m else s


# --------------------------------------------------------------------------
# 数据装载
# --------------------------------------------------------------------------

def load_jobs(instance):
    d = os.path.join(instance, "state", "jobs")
    out = {}
    if not os.path.isdir(d):
        return out
    for fn in os.listdir(d):
        if not fn.endswith(".json") or fn.startswith((".", "_")):
            continue
        rec = ev.read_json(os.path.join(d, fn), None)
        if isinstance(rec, dict) and rec.get("announcement_id") is not None:
            out[str(rec["announcement_id"])] = rec
    return out


def load_announcements(repo):
    return {str(r.get("announcement_id")): r
            for r in ev.read_jsonl(os.path.join(repo, "data", "paperball", "announcements.jsonl"))
            if r.get("announcement_id") is not None}


def load_tiers(repo):
    path = os.path.join(repo, "data", "tiers.yaml")
    if not os.path.isfile(path):
        return {}, "中小"
    data = yaml.safe_load(open(path, encoding="utf-8")) or {}
    mapping = {}
    for tier, names in (data.get("tiers") or {}).items():
        for name in names or []:
            mapping[reg.norm_text(str(name)).lower()] = tier
    return mapping, data.get("default") or "中小"


def last_events_by_job(instance):
    out = {}
    for rec in ev.load_events(instance):
        j = str(rec.get("job") or "")
        if j:
            out[j] = rec          # events.jsonl 按时间追加，最后一条即最新
    return out


# --------------------------------------------------------------------------
# tracker 生成
# --------------------------------------------------------------------------

def status_text(job, outcome):
    """作业+终局 → tracker「状态」列文本。"""
    if outcome:
        result = outcome.get("result") or ""
        reason = (outcome.get("reason") or "").strip()
        if result == "submitted":
            return "**已投递**"
        if result == "skipped":
            if any(h in reason for h in UNFIT_HINT):
                return "**不合适**（%s）" % reason if reason else "**不合适**"
            return "**已停止**（%s）" % reason if reason else "**已停止**"
        if result == "failed":
            return "**已停止·失败**（%s）" % reason if reason else "**已停止·失败**"
        # blocked：沿用手写口径「待投递·<原因>」，典型是待材料
        if reason:
            return "**待投递·%s**" % (reason if reason.startswith("待") else "阻塞：" + reason)
        return "**待投递·阻塞**"
    st = (job or {}).get("status") or ""
    if st in ("submitted", "verified"):
        return "**已投递**"
    if st == "gate_wait":
        kind = (((job or {}).get("gate") or {}).get("kind")) or ""
        return "填表中（等用户·%s）" % GATE_LABEL.get(kind, kind or "处理")
    if st in ("blocked",):
        return "**待投递·阻塞**"
    if st == "skipped":
        return "**已停止**"
    if st == "failed":
        return "**已停止·失败**"
    return JOB_STATUS_TEXT.get(st, st or "候选")


def key_date(job, outcome, ann):
    """tracker「关键日期」列。"""
    deadline = (outcome or {}).get("deadline") or (job or {}).get("expired_at") \
        or (ann or {}).get("expired_at") or ""
    if outcome and outcome.get("result") == "submitted":
        s = "%s 提交" % fmt_day(outcome.get("submitted_at") or now_iso())
        dl = fmt_day(deadline)
        if dl:
            s += "；门户截止 %s" % dl
        return s
    dl = fmt_day(deadline)
    return "门户截止 %s" % dl if dl else "—"


def record_text(job, outcome, last_ev):
    """tracker「记录」列：证据路径 + 门户状态 + 最近一步。"""
    parts = []
    if outcome:
        parts += [str(p) for p in outcome.get("evidence") or [] if p]
        bits = []
        if outcome.get("portal_status"):
            bits.append("门户=%s" % outcome["portal_status"])
        if outcome.get("candidate_id"):
            bits.append("candidateId=%s" % outcome["candidate_id"])
        for s in outcome.get("next_steps") or []:
            label = NS_LABEL.get(s.get("kind") or "", s.get("kind") or "待办")
            due = fmt_day(s.get("due_at") or s.get("start_at") or "")
            bits.append("**%s%s**" % (label, " %s 前" % due if due else "（时间未定）"))
        if outcome.get("reason") and outcome.get("result") not in ("blocked", "skipped", "failed"):
            bits.append(str(outcome["reason"]))
        if bits:
            parts.append("；".join(bits))
    if not parts and last_ev and last_ev.get("msg"):
        parts.append("最近：%s %s" % (fmt_day(last_ev.get("at")), last_ev["msg"]))
    return ";".join(parts) or "—"


def build_tracker_rows(instance, repo):
    jobs = load_jobs(instance)
    outcomes = ev.load_outcomes(instance)
    anns = load_announcements(repo)
    tier_map, default_tier = load_tiers(repo)
    last_ev = last_events_by_job(instance)
    registry = reg.load_registry(instance)

    aids = set(jobs) | set(outcomes)
    reg_free = []                      # 没有 announcement_id 的登记行
    for rec in registry:
        rids = [str(x) for x in rec.get("announcement_ids") or []]
        if rids:
            aids.update(rids)
        else:
            reg_free.append(rec)

    rows = []
    for aid in sorted(aids, key=lambda x: int(x) if x.isdigit() else 0):
        job, outcome = jobs.get(aid), outcomes.get(aid)
        ann = anns.get(aid) or {}
        regrec = next((r for r in registry
                       if aid in [str(x) for x in r.get("announcement_ids") or []]), None)

        company = (outcome or {}).get("company") or (job or {}).get("company") \
            or (regrec or {}).get("brand") or (regrec or {}).get("employer") \
            or ann.get("company") or "?"
        tier = (job or {}).get("company_tier") \
            or tier_map.get(reg.norm_text(company).lower()) or default_tier
        role = (outcome or {}).get("job_applied") \
            or "、".join((job or {}).get("fit_roles") or []) \
            or (regrec or {}).get("job") or ann.get("title") or ""
        portal_raw = (outcome or {}).get("portal") or (job or {}).get("portal") \
            or (regrec or {}).get("portal") or ""
        portal = PORTAL_LABEL.get(portal_raw, portal_raw or "—")

        if outcome is None and job is None and regrec is not None:
            # 只有 registry 登记的（比如 import-tracker 回填的），直接成已投递行
            status = "**已投递**" if regrec.get("status") in ("submitted", "verified") \
                else "**已停止**"
            kdate = "%s 提交" % fmt_day(regrec.get("at") or "")
            record = "registry 登记"
        else:
            status = status_text(job, outcome)
            kdate = key_date(job, outcome, ann)
            record = record_text(job, outcome, last_ev.get(aid))

        # 排序键：最近活动时间（提交时间 > 作业最近历史 > registry at > 创建时间）
        act = (outcome or {}).get("submitted_at") or ""
        if not act and job:
            hist = job.get("history") or []
            act = (hist[-1].get("at") if hist else "") or job.get("created_at") or ""
        if not act and regrec:
            act = regrec.get("at") or ""
        if not act:
            act = ann.get("published_at") or ""
        rows.append({"公司": company, "id": aid, "层/类别": tier, "岗位": role,
                     "门户/方式": portal, "状态": status, "关键日期": kdate,
                     "记录": record, "_act": act})

    for rec in reg_free:
        company = rec.get("brand") or rec.get("employer") or "?"
        rows.append({"公司": company, "id": "—",
                     "层/类别": tier_map.get(reg.norm_text(company).lower()) or default_tier,
                     "岗位": rec.get("job") or "", "门户/方式": PORTAL_LABEL.get(
                         rec.get("portal") or "", rec.get("portal") or "—"),
                     "状态": "**已投递**" if rec.get("status") in ("submitted", "verified") else "**已停止**",
                     "关键日期": "%s 提交" % fmt_day(rec.get("at") or ""),
                     "记录": "registry 登记", "_act": rec.get("at") or ""})

    rows.sort(key=lambda r: (r["_act"], r["id"]), reverse=True)
    return rows


def render_tracker_md(rows, name, generated_at):
    out = ["# %s · 投递总台账（本文件由 host/render-tracker.py 生成，勿手改）" % name, "",
           "> 每行一家公司；状态机：候选 → 查岗 → 填表 → 待提交 → 已投递 →（测评/笔试/面试/Offer）；另有 不合适/已停止",
           "> 数据源：state/jobs/ + state/outcomes/ + state/registry.jsonl（contracts §9.3）；生成于 %s" % generated_at,
           "",
           "| " + " | ".join(TRACKER_HEADERS) + " |",
           "|" + "---|" * len(TRACKER_HEADERS)]
    for r in rows:
        out.append("| " + " | ".join(cell(r[h]) for h in TRACKER_HEADERS) + " |")
    out += ["", "<!-- generated by render-tracker.py at %s -->" % generated_at, ""]
    return "\n".join(out)


# --------------------------------------------------------------------------
# assessments 生成
# --------------------------------------------------------------------------

def merge_next_steps(instance):
    """next-steps.jsonl（按 id 折叠）+ outcomes.next_steps（同 id 去重）→ 待办行。"""
    items = {}
    for rec in ev.load_next_steps(instance):       # jsonl 已按 id 折叠（后写覆盖）
        items[rec["id"]] = dict(rec, _src="mail")
    for aid, outcome in ev.load_outcomes(instance).items():
        for s in outcome.get("next_steps") or []:
            sid = s.get("id") or ev.next_step_id(aid, s)
            if sid in items:
                continue
            items[sid] = {"id": sid, "kind": s.get("kind") or "other",
                          "company": outcome.get("company") or "",
                          "announcement_id": outcome.get("announcement_id"),
                          "job": outcome.get("job_applied") or "",
                          "due_at": s.get("due_at"), "start_at": s.get("start_at"),
                          "link": s.get("link") or "", "note": s.get("note") or "",
                          "source_msg": "outcome:%s" % aid,
                          "created_at": outcome.get("submitted_at") or "",
                          "status": "open", "_src": "outcome"}
    # 同公告同 kind 同链接/期限的重复待办去重（邮件与 worker 都可能上报同一测评）
    seen, out = set(), []
    for rec in items.values():
        key = (str(rec.get("announcement_id") or ""), rec.get("kind") or "",
               re.sub(r"\s+", "", rec.get("link") or rec.get("due_at") or ""))
        if key[2] and key in seen:
            continue
        seen.add(key)
        out.append(rec)
    return out


def build_assess_rows(instance, now=None):
    now = now or dt.datetime.now(CST)
    rows = []
    for rec in merge_next_steps(instance):
        st = rec.get("status") or "open"
        if st in ("superseded", "cancelled"):
            continue
        due, start = parse_time(rec.get("due_at")), parse_time(rec.get("start_at"))
        if st == "open":
            if due and due < now:
                st_text = "已过期"
            elif not (rec.get("link") or "").strip():
                st_text = "待做（等链接）"
            else:
                st_text = "待做"
        else:
            st_text = {"done": "已完成"}.get(st, st)
        if due:
           期限 = "**%s 前**" % fmt_day(rec["due_at"])
        elif start:
            期限 = "**批次日 %s**" % fmt_day(rec["start_at"])
        else:
            期限 = "未定"
        src = rec.get("source_msg") or ""
        if src.startswith("outcome:"):
            src_text = "worker 门户记录"
        elif "@" in src:
            src_text = "邮件 %s" % fmt_day(rec.get("created_at") or "")
        else:
            src_text = src or "—"
        aid = rec.get("announcement_id")
        rows.append({"公司": "%s %s" % (rec.get("company") or "?", aid) if aid else (rec.get("company") or "?"),
                     "类型": NS_LABEL.get(rec.get("kind") or "", rec.get("kind") or "待办"),
                     "链接/入口": rec.get("link") or "链接未到",
                     "期限": 期限, "来源": src_text, "状态": st_text,
                     "_due": (due or start or dt.datetime.max.replace(tzinfo=CST)),
                     "_open": 0 if st == "open" else 1})
    rows.sort(key=lambda r: (r["_open"], r["_due"], r["公司"]))
    return rows


def render_assess_md(rows, name, generated_at):
    out = ["# %s · 测评/笔试排期表（本文件由 host/render-tracker.py 生成，勿手改）" % name, "",
           "> 投递后产生的测评与笔试统一登记；数据源：state/next-steps.jsonl + state/outcomes/ 的 next_steps（contracts §9.3）",
           "> 状态：待做 | 已约 | 已完成 | 已过期；生成于 %s" % generated_at, "",
           "| " + " | ".join(ASSESS_HEADERS) + " |",
           "|" + "---|" * len(ASSESS_HEADERS)]
    for r in rows:
        out.append("| " + " | ".join(cell(r[h]) for h in ASSESS_HEADERS) + " |")
    out += ["", "<!-- generated by render-tracker.py at %s -->" % generated_at, ""]
    return "\n".join(out)


# --------------------------------------------------------------------------
# --check 对账
# --------------------------------------------------------------------------

def status_bucket(text):
    """状态文本归类成桶，手写 vs 生成比桶不比字面。"""
    t = re.sub(r"[*`]", "", text or "")
    for key in ("已投递", "已笔试", "待笔试", "面试", "Offer", "不合适", "已停止", "暂缓",
                "待投递", "待提交", "待核验", "填表", "查岗", "候选", "排队"):
        if key in t:
            return {"待核验": "待投递", "待提交": "填表", "候选": "排队"}.get(key, key)
    return t or "空"


def row_keys(row, id_col="id", company_col="公司"):
    """一行 tracker/assessments 的匹配键：id 优先，无 id 用雇主主体键。"""
    aid = re.sub(r"\D", "", row.get(id_col) or "")
    if aid:
        return "id:" + aid
    k = reg.employer_key(row.get(company_col) or "")
    return "key:" + k if k else ""


def parse_hand_table(path, must_have):
    """读手写 md 里含全部 must_have 表头的第一张表 → (headers, [行dict])。"""
    if not os.path.isfile(path):
        return None, []
    lines = open(path, encoding="utf-8").read().splitlines()
    for i, line in enumerate(lines[:-1]):
        if not line.lstrip().startswith("|"):
            continue
        if not re.match(r"^\s*\|?\s*:?-{2,}", lines[i + 1]):
            continue
        headers = [c.strip() for c in line.strip().strip("|").split("|")]
        if not all(h in headers for h in must_have):
            continue
        rows = []
        for r in lines[i + 2:]:
            if not r.lstrip().startswith("|"):
                break
            cells = [c.strip() for c in r.strip().strip("|").split("|")]
            cells += [""] * (len(headers) - len(cells))
            rows.append(dict(zip(headers, cells[:len(headers)])))
        return headers, rows
    return None, []


def check_tracker(hand_path, gen_rows):
    headers, hand = parse_hand_table(hand_path, ["公司", "id", "状态"])
    report = []
    gen_by_key = {}
    for r in gen_rows:
        gen_by_key.setdefault(row_keys(r), r)
    hand_by_key = {}
    for r in hand:
        k = row_keys(r)
        if k:
            hand_by_key.setdefault(k, r)

    only_hand = [r for k, r in hand_by_key.items() if k not in gen_by_key]
    only_gen = [r for k, r in gen_by_key.items() if k not in hand_by_key]
    field_diffs = []
    for k in sorted(set(hand_by_key) & set(gen_by_key)):
        h, g = hand_by_key[k], gen_by_key[k]
        diffs = []
        hb, gb = status_bucket(h.get("状态")), status_bucket(g.get("状态"))
        if hb != gb:
            # 手写已投递、生成也是已投递/笔试面试类 → 同桶视为一致
            same_cluster = {hb, gb} <= {"已投递", "已笔试", "待笔试", "面试", "Offer"}
            if not same_cluster:
                diffs.append("状态：手写「%s」⇄ 生成「%s」" % (h.get("状态"), g.get("状态")))
        # 关键日期：只比有没有「已投递日/门户截止」的时间差，文本不逐字比
        h_dates = set(re.findall(r"\d{4}-\d{2}-\d{2}|\d{2}-\d{2}", h.get("关键日期") or ""))
        g_dates = set(re.findall(r"\d{4}-\d{2}-\d{2}|\d{2}-\d{2}", g.get("关键日期") or ""))
        if h_dates and g_dates and not (h_dates & g_dates):
            diffs.append("关键日期：手写 %s ⇄ 生成 %s" % (h.get("关键日期"), g.get("关键日期")))
        if diffs:
            field_diffs.append((h.get("公司") or g.get("公司"), k, diffs))
    return hand, gen_rows, only_hand, only_gen, field_diffs


def check_assess(hand_path, gen_rows):
    headers, hand = parse_hand_table(hand_path, ["公司", "类型", "状态"])
    gen_comps = [re.sub(r"\s+", "", r["公司"]) for r in gen_rows]
    hand_comps = [re.sub(r"\s+", "", r.get("公司") or "") for r in hand]
    only_hand = [r for r, c in zip(hand, hand_comps)
                 if not (c and any(c in g for g in gen_comps))]
    only_gen = [r for r, c in zip(gen_rows, gen_comps)
                if not (c and any(c == h or c.startswith(h) or h in c for h in hand_comps if h))]
    return hand, gen_rows, only_hand, only_gen


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="台账/测评表生成器（contracts §9.3）")
    ap.add_argument("--instance", default=os.environ.get("CAMPUS_INSTANCE"),
                    help="实例目录（默认 $CAMPUS_INSTANCE）")
    ap.add_argument("--repo", default=REPO, help="仓库根（默认脚本所在仓库）")
    ap.add_argument("--check", action="store_true",
                    help="与手写 tracker.md/assessments.md 逐行比对，只读不写；有差异退出码 1")
    ap.add_argument("--write-live", action="store_true",
                    help="直接覆盖 tracker.md/assessments.md（迁移完成后才用，默认关）")
    ap.add_argument("--stdout", action="store_true", help="生成内容打到 stdout，不落盘")
    args = ap.parse_args()

    instance = os.path.abspath(os.path.expanduser(args.instance)) if args.instance else None
    if not instance or not os.path.isdir(instance):
        print("ERROR: 实例目录不存在，传 --instance 或设置 CAMPUS_INSTANCE", file=sys.stderr)
        return 2
    repo = os.path.abspath(args.repo)

    generated_at = now_iso()
    t_rows = build_tracker_rows(instance, repo)
    a_rows = build_assess_rows(instance)
    tracker_md = render_tracker_md(t_rows, "tracker.md", generated_at)
    assess_md = render_assess_md(a_rows, "assessments.md", generated_at)

    if args.stdout:
        print(tracker_md)
        print(assess_md)
        return 0

    if args.check:
        hand_t = os.path.join(instance, "tracker.md")
        hand_a = os.path.join(instance, "assessments.md")
        hand, gen, only_hand, only_gen, diffs = check_tracker(hand_t, t_rows)
        print("== tracker 对账 ==")
        print("手写 %d 行 · 生成 %d 行" % (len(hand), len(gen)))
        if only_hand:
            print("只在手写版（%d）：" % len(only_hand))
            for r in only_hand:
                print("  - %s（id=%s，%s）" % (r.get("公司"), r.get("id") or "—",
                                              re.sub(r"[*`]", "", r.get("状态") or "")))
        if only_gen:
            print("只在生成版（%d）：" % len(only_gen))
            for r in only_gen:
                print("  + %s（id=%s，%s）" % (r["公司"], r["id"], re.sub(r"[*`]", "", r["状态"])))
        if diffs:
            print("共有行字段不一致（%d）：" % len(diffs))
            for company, k, ds in diffs:
                print("  ~ %s（%s）：%s" % (company, k, "；".join(ds)))
        n_t = len(only_hand) + len(only_gen) + len(diffs)

        hand_a_rows, gen_a, oh_a, og_a = check_assess(hand_a, a_rows)
        print("\n== assessments 对账 ==")
        print("手写 %d 行 · 生成 %d 行" % (len(hand_a_rows), len(gen_a)))
        if oh_a:
            print("只在手写版（%d）：" % len(oh_a))
            for r in oh_a:
                print("  - %s（%s，%s）" % (r.get("公司"), r.get("类型"), r.get("状态")))
        if og_a:
            print("只在生成版（%d）：" % len(og_a))
            for r in og_a:
                print("  + %s（%s，%s）" % (r["公司"], r["类型"], r["状态"]))
        n_a = len(oh_a) + len(og_a)

        print("\n差异合计：tracker %d 项 · assessments %d 项" % (n_t, n_a))
        if n_t + n_a == 0:
            print("一致，可以 --write-live 切换。")
            return 0
        print("（迁移期差异多属正常：手写版含历史行与自由文本，生成版只覆盖 state/ 里的结构化数据）")
        return 1

    suffix = "" if args.write_live else ".generated"
    t_path = os.path.join(instance, "tracker%s.md" % suffix)
    a_path = os.path.join(instance, "assessments%s.md" % suffix)
    if args.write_live:
        print("注意：--write-live 直接覆盖 tracker.md/assessments.md", file=sys.stderr)
    for path, text in ((t_path, tracker_md), (a_path, assess_md)):
        atomic_write_text(path, text)
    print("已生成：%s（%d 行）、%s（%d 行）" % (t_path, len(t_rows), a_path, len(a_rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
