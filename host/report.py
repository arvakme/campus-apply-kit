#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6", "httpx>=0.27"]
# ///
"""report.py · 每日战报：已投/在跑/待你处理/blocked 原因 Top/各门户卡点/平均耗时。

    uv run --script host/report.py [--instance DIR] [--date 2026-09-15] [--notify] [--out PATH]
    uv run --script host/report.py rank-report [--instance DIR] [--date D] [--dry-run]
        # 共享 bot 排行榜上报（contracts §10.3）：只发 {date, applied_today, applied_total}

数据源（全部离线文件，contracts §8）：
  state/jobs/*.json        作业状态机与 history（dispatcher 写）
  state/registry.jsonl     已投登记（registry.py 写）
  state/outcomes/*.json    worker 投递结果（§9.2）
  state/handoffs/*.json    等用户处理的事
输出：state/reports/<date>.md；--notify 顺带发一条 Bark（notify.py batch-done，附报告路径）。
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import shlex
import subprocess
import sys
import tempfile
from zoneinfo import ZoneInfo

REPO = os.environ.get("CAMPUS_REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "host"))
CST = ZoneInfo("Asia/Shanghai")
ACTIVE = {"dispatched", "filling", "gate_wait", "review_wait", "submitting"}
TERMINAL = {"submitted", "verified", "blocked", "skipped", "failed"}


def read_json(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return default


def parse_time(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(str(s))
    except ValueError:
        return None


def load_dir(d):
    out = []
    if not os.path.isdir(d):
        return out
    for fn in os.listdir(d):
        if fn.endswith(".json") and not fn.startswith((".", "_")):
            rec = read_json(os.path.join(d, fn), None)
            if isinstance(rec, dict):
                out.append(rec)
    return out


def read_jsonl(path):
    out = []
    if not os.path.isfile(path):
        return out
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def build_report(instance: str, date: str) -> str:
    jobs = {str(j.get("announcement_id")): j
            for j in load_dir(os.path.join(instance, "state", "jobs"))
            if j.get("announcement_id") is not None}
    registry = read_jsonl(os.path.join(instance, "state", "registry.jsonl"))
    handoffs = [h for h in load_dir(os.path.join(instance, "state", "handoffs"))
                if not h.get("resolved_at")]

    delivered, running, waiting_user, blocked, skipped_n, failed = [], [], [], [], 0, []
    parked_jobs = []
    gate_by_portal = collections.Counter()
    durations = []
    for j in jobs.values():
        hist = j.get("history") or []
        st = j.get("status")
        if j.get("parked"):
            parked_jobs.append(j)
        for h in hist:
            if h.get("to") == "gate_wait":
                gate_by_portal[j.get("portal") or "未识别门户"] += 1
        first_at = parse_time((hist[0] or {}).get("at")) if hist else parse_time(j.get("created_at"))
        last_at = parse_time((hist[-1] or {}).get("at")) if hist else None
        term_today = any(h.get("to") in ("submitted", "verified") and str(h.get("at", "")).startswith(date)
                         for h in hist)
        if st in ("submitted", "verified"):
            if term_today:
                delivered.append(j)
            if first_at and last_at:
                durations.append(((last_at - first_at).total_seconds() / 60, j))
        elif st in ACTIVE:
            running.append(j)
            if st in ("gate_wait", "review_wait"):
                waiting_user.append(j)
        elif st == "blocked":
            blocked.append(j)
        elif st == "failed":
            failed.append(j)
        elif st == "skipped":
            skipped_n += 1
    # 回写漏了的：registry 里今天登记但 job 状态没走到终局的也算进已投
    reg_today = [r for r in registry if str(r.get("at", "")).startswith(date)
                 and r.get("status") in ("submitted", "verified")]

    L = []
    L.append("# 每日战报 · %s" % date)
    L.append("")
    L.append("> host/report.py 生成；数据源 state/jobs + registry + handoffs")
    L.append("")
    L.append("## 总览")
    L.append("")
    L.append("- 今日已投：**%d** 家（作业终局 %d · registry 登记 %d）" % (
        len(delivered), len(delivered), len(reg_today)))
    L.append("- 在跑：**%d** 家 · 排队中：**%d** 家 · 待你处理：**%d** 项" % (
        len(running), sum(1 for j in jobs.values() if j.get("status") == "queued"),
        len(handoffs) + sum(1 for j in running if j.get("status") == "review_wait")))
    L.append("- blocked：**%d** · failed：%d · skipped（不合适/已投过）：%d" % (
        len(blocked), len(failed), skipped_n))
    if durations:
        avg = sum(d for d, _ in durations) / len(durations)
        L.append("- 平均每家耗时（入队→终局）：**%.0f** 分钟（%d 家样本）" % (avg, len(durations)))
    L.append("")

    if delivered or reg_today:
        L.append("## 今日已投")
        L.append("")
        for j in delivered:
            oc = j.get("outcome") or {}
            L.append("- %s（%s）%s · %s" % (j.get("company"), j["announcement_id"],
                                          oc.get("job") or "", j.get("status")))
        done_ids = {str(j["announcement_id"]) for j in delivered}
        for r in reg_today:
            ids = {str(x) for x in r.get("announcement_ids") or []}
            if not ids & done_ids:
                L.append("- %s（registry，%s）%s" % (r.get("employer"), ",".join(sorted(ids)),
                                                  r.get("job") or ""))
        L.append("")

    if running:
        L.append("## 在跑")
        L.append("")
        for j in running:
            L.append("- %s（%s）%s · %s档 · task=%s" % (
                j.get("company"), j["announcement_id"], j.get("status"),
                j.get("tier") or "-", j.get("task_id") or "-"))
        L.append("")

    pend = [j for j in running if j.get("status") in ("gate_wait", "review_wait")]
    if handoffs or pend:
        L.append("## 待你处理")
        L.append("")
        for h in handoffs:
            L.append("- %s（%s）%s：%s" % (h.get("company") or "-", h.get("announcement_id") or "-",
                                          h.get("kind") or "", h.get("action") or h.get("title") or ""))
        for j in pend:
            if not any(str(h.get("announcement_id")) == str(j["announcement_id"]) for h in handoffs):
                L.append("- %s（%s）终审提交（字段表 %s）" % (
                    j.get("company"), j["announcement_id"], j.get("field_table") or "-"))
        L.append("")

    if blocked or failed:
        L.append("## blocked / failed")
        L.append("")
        top = collections.Counter()
        for j in blocked + failed:
            note = ((j.get("history") or [{}])[-1].get("note") or "未注明").split("：")[0]
            top[note] += 1
        for reason, n in top.most_common(5):
            L.append("- %s ×%d" % (reason, n))
        L.append("")
        for j in blocked + failed:
            L.append("  - %s（%s）%s：%s" % (j.get("company"), j["announcement_id"], j.get("status"),
                                            (j.get("history") or [{}])[-1].get("note") or ""))
        L.append("")

    if gate_by_portal:
        L.append("## 各门户卡点次数（进过 gate_wait 的次数）")
        L.append("")
        for portal, n in gate_by_portal.most_common():
            L.append("- %s：%d" % (portal, n))
        L.append("")

    dstate = read_json(os.path.join(instance, "state", "dispatcher-state.json"), {})
    pauses = dstate.get("portal_pauses") or {}
    if pauses or parked_jobs:
        L.append("## 关卡 parked / 门户暂停（§8.7）")
        L.append("")
        for portal, info in sorted(pauses.items()):
            L.append("- **门户 %s 已暂停**：24h 内 parked %d 次（%s 起）；恢复用 resume"
                     % (portal, info.get("count"), str(info.get("since") or "")[:16]))
        for j in parked_jobs:
            L.append("- parked：%s（%s）门户 %s · 自 %s" % (
                j.get("company"), j["announcement_id"], j.get("portal") or "未识别",
                str(j.get("parked_at") or "")[:16]))
        L.append("")
    return "\n".join(L) + "\n"


def rank_numbers(instance: str, date: str) -> dict:
    """§10.3 排行榜数字：只数 announcement_id，不带公司/岗位。

    applied_today = 今天 result/submitted|verified 的去重公告数
    （registry.jsonl 的 announcement_ids ∪ state/outcomes/<id>.json，按 submitted_at/at 判当天）；
    applied_total = 历史上全部已投的去重公告数。
    """
    today_ids, all_ids = set(), set()
    for r in read_jsonl(os.path.join(instance, "state", "registry.jsonl")):
        if r.get("status") not in ("submitted", "verified"):
            continue
        ids = {str(x) for x in r.get("announcement_ids") or []}
        all_ids |= ids
        if str(r.get("at") or "").startswith(date):
            today_ids |= ids
    for o in load_dir(os.path.join(instance, "state", "outcomes")):
        if o.get("result") not in ("submitted", "verified"):
            continue
        aid = o.get("announcement_id")
        if aid is None:
            continue
        all_ids.add(str(aid))
        if str(o.get("submitted_at") or "").startswith(date):
            today_ids.add(str(aid))
    return {"date": date, "applied_today": len(today_ids),
            "applied_total": len(all_ids)}


def cmd_rank_report(args) -> int:
    """上报排行榜数字到共享 Worker（relay 模式；只发数字，§10.3）。"""
    if not args.instance or not os.path.isdir(args.instance):
        print("ERROR: 需要 --instance 或环境变量 CAMPUS_INSTANCE", file=sys.stderr)
        return 2
    instance = os.path.abspath(os.path.expanduser(args.instance))
    date = args.date or dt.datetime.now(CST).strftime("%Y-%m-%d")
    payload = rank_numbers(instance, date)
    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    import yaml  # noqa: E402  仅本子命令需要
    npath = os.path.join(instance, "notify.yaml")
    cfg = {}
    if os.path.isfile(npath):
        cfg = yaml.safe_load(open(npath, encoding="utf-8")) or {}
    try:
        from channels import telegram as tgchan  # noqa: E402
        http = tgchan.make_relay_http(cfg, api_base=args.relay_base, inst_dir=instance)
        # 是否上榜跟着本地 notify.yaml 走（relay 端原来只在配对那一刻记一次，事后改配置不生效）
        rank_opt = tgchan.relay_cfg(cfg).get("rank")
        if isinstance(rank_opt, bool):
            payload["rank"] = rank_opt
        resp = http.rank_report(payload)
    except Exception as exc:
        print("ERROR: rank 上报失败：%s" % exc, file=sys.stderr)
        return 1
    print("已上报 %s（today=%d total=%d，上榜=%s）→ %s"
          % (payload["date"], payload["applied_today"], payload["applied_total"],
             {True: "是", False: "否"}.get(payload.get("rank"), "沿用配对时的选择"), http.base))
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] == "rank-report":
        ap = argparse.ArgumentParser(description="排行榜上报（contracts §10.3）")
        ap.add_argument("--instance", default=os.environ.get("CAMPUS_INSTANCE"))
        ap.add_argument("--date", default=None, help="YYYY-MM-DD（默认北京时间今天）")
        ap.add_argument("--relay-base", default=None, help="Worker 地址（测试用）")
        ap.add_argument("--dry-run", action="store_true", help="只打印 payload 不上报")
        return cmd_rank_report(ap.parse_args(argv[1:]))

    ap = argparse.ArgumentParser(description="每日战报")
    ap.add_argument("--instance", default=os.environ.get("CAMPUS_INSTANCE"))
    ap.add_argument("--date", default=None, help="报告日期 YYYY-MM-DD（默认北京时间今天）")
    ap.add_argument("--out", default=None, help="默认 <instance>/state/reports/<date>.md")
    ap.add_argument("--notify", action="store_true", help="发一条 Bark（notify.py batch-done）")
    ap.add_argument("--notify-cmd",
                    default="uv run --script %s" % os.path.join(REPO, "host", "notify.py"))
    args = ap.parse_args(argv)
    if not args.instance or not os.path.isdir(args.instance):
        ap.error("需要 --instance 或环境变量 CAMPUS_INSTANCE")
    instance = os.path.abspath(os.path.expanduser(args.instance))
    date = args.date or dt.datetime.now(CST).strftime("%Y-%m-%d")
    text = build_report(instance, date)
    out = args.out or os.path.join(instance, "state", "reports", "%s.md" % date)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(out)), prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, out)
    print("战报已写 %s" % out)
    if args.notify:
        summary = [l for l in text.splitlines() if l.startswith("- ")][:6]
        body = "；".join(s.lstrip("- ") for s in summary)[:350] + " · 全文见 " + out
        p = subprocess.run(shlex.split(args.notify_cmd) +
                           ["batch-done", "--instance", instance,
                            "--title", "每日战报 %s" % date, "--body", body],
                           capture_output=True, text=True)
        if p.returncode != 0:
            print("WARN: notify 失败：%s%s" % (p.stdout, p.stderr), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
