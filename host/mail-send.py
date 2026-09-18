#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6"]
# ///
"""mail-send.py · 把审过的邮件草稿发出去（SMTP，默认 Resend）

    uv run --script host/mail-send.py <批次目录> [--dry-run] [--limit N] [--only id,id]
        [--test-to 地址] [--interval 45] [--from "姓名 <jobs@example.com>"]

草稿格式：<批次目录>/<announcement_id>/message.json
    {announcement_id, company, to, subject, body_text, attachments:[绝对路径], role_applied, ...}

行为：
  - 已有 sent.json 的草稿跳过（可重复运行，不会重发）
  - 每日上限取实例 intent.yaml 的 permissions.email.daily_limit，统计实例 mail/ 下今天的 sent.json
  - 发送间隔 interval ± 1/3 随机抖动，避免被当成群发
  - 发成功写 sent.json，并写事件、结果、防重投登记（host/events.py、host/registry.py）
  - --test-to：把第一封草稿原样发到这个地址（主题前加【测试】），不写任何记录，用来验证发信链路与排版

凭据：SMTP 密码从钥匙串读（默认 service=campus-apply-resend），不写文件、不打印。
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import glob
import json
import os
import random
import smtplib
import subprocess
import sys
import time
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import yaml

REPO = os.environ.get("CAMPUS_REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CST = ZoneInfo("Asia/Shanghai")
MAX_ATTACH = 10 * 1024 * 1024


def die(msg: str, code: int = 2):
    print("ERROR: " + msg, file=sys.stderr)
    sys.exit(code)


def keychain(service: str) -> str:
    r = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                       capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        die("钥匙串里没有 %s：先执行 security add-generic-password -s %s -a smtp -w '<密码或 API key>'"
            % (service, service))
    return r.stdout.strip()


def load_drafts(batch: str, only: set[str] | None) -> list[tuple[str, dict]]:
    out = []
    for p in sorted(glob.glob(os.path.join(batch, "*", "message.json"))):
        d = os.path.dirname(p)
        if only and os.path.basename(d) not in only:
            continue
        try:
            msg = json.load(open(p, encoding="utf-8"))
        except ValueError as e:
            print("WARN: %s 不是合法 JSON，跳过：%s" % (p, e), file=sys.stderr)
            continue
        out.append((d, msg))
    return out


def sent_today(instance: str) -> int:
    today = dt.datetime.now(CST).date().isoformat()
    n = 0
    for p in glob.glob(os.path.join(instance, "mail", "**", "sent.json"), recursive=True):
        try:
            if json.load(open(p)).get("at", "")[:10] == today:
                n += 1
        except ValueError:
            pass
    return n


def daily_limit(instance: str) -> int:
    try:
        intent = yaml.safe_load(open(os.path.join(instance, "intent.yaml"), encoding="utf-8")) or {}
        return int(((intent.get("permissions") or {}).get("email") or {}).get("daily_limit") or 50)
    except (OSError, ValueError):
        return 50


def validate(d: str, msg: dict) -> list[str]:
    errs = []
    for k in ("to", "subject", "body_text"):
        if not str(msg.get(k) or "").strip():
            errs.append("缺 %s" % k)
    if "@" not in str(msg.get("to") or ""):
        errs.append("收件人不像邮箱")
    for a in msg.get("attachments") or []:
        if not os.path.isfile(a):
            errs.append("附件不存在：%s" % a)
        elif os.path.getsize(a) > MAX_ATTACH:
            errs.append("附件超过 10MB：%s" % os.path.basename(a))
    return errs


def build(msg: dict, sender: str, subject_prefix: str = "", to_override: str | None = None) -> EmailMessage:
    m = EmailMessage()
    m["From"] = sender
    m["To"] = to_override or msg["to"]
    m["Reply-To"] = email.utils.parseaddr(sender)[1]
    m["Subject"] = subject_prefix + msg["subject"]
    m["Date"] = email.utils.formatdate(localtime=True)
    domain = email.utils.parseaddr(sender)[1].split("@")[-1]
    m["Message-ID"] = email.utils.make_msgid(domain=domain)
    m.set_content(msg["body_text"], charset="utf-8")
    for a in msg.get("attachments") or []:
        with open(a, "rb") as fh:
            data = fh.read()
        maintype, subtype = ("application", "pdf") if a.lower().endswith(".pdf") else ("application", "octet-stream")
        # EmailMessage 会对中文文件名做 RFC 2231 编码，主流邮箱能正确显示
        m.add_attachment(data, maintype=maintype, subtype=subtype, filename=os.path.basename(a))
    return m


def record(instance: str, d: str, msg: dict, message_id: str):
    now = dt.datetime.now(CST).isoformat(timespec="seconds")
    with open(os.path.join(d, "sent.json"), "w", encoding="utf-8") as fh:
        json.dump({"at": now, "to": msg["to"], "subject": msg["subject"], "message_id": message_id},
                  fh, ensure_ascii=False, indent=1)
    aid = str(msg.get("announcement_id") or os.path.basename(d))
    company = msg.get("company") or ""
    base = ["uv", "run", "--script"]
    cmds = [
        base + [os.path.join(REPO, "host", "events.py"), "event", "--instance", instance, "--job", aid,
                "--step", "submitted", "--portal", "email", "--msg", "邮件投递已发出 → %s" % msg["to"]],
        base + [os.path.join(REPO, "host", "events.py"), "outcome", "--instance", instance, "--job", aid,
                "--result", "submitted", "--set", "company=%s" % company, "portal=email",
                "job_applied=%s" % (msg.get("role_applied") or ""), "submitted_at=%s" % now],
        base + [os.path.join(REPO, "host", "registry.py"), "add", "--employer", company, "--portal", "email",
                "--ids", aid, "--job", msg.get("role_applied") or "", "--status", "submitted"],
    ]
    for c in cmds:
        r = subprocess.run(c, capture_output=True, text=True, env={**os.environ, "CAMPUS_INSTANCE": instance})
        if r.returncode != 0:
            print("  WARN: 记录失败（邮件已发出，需手工补记）：%s" % (r.stderr or r.stdout).strip()[:160],
                  file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="发送审过的邮件草稿")
    ap.add_argument("batch", help="草稿批次目录，如 $CAMPUS_INSTANCE/mail/B-01")
    ap.add_argument("--instance", default=os.environ.get("CAMPUS_INSTANCE"))
    ap.add_argument("--from", dest="sender", default=os.environ.get("CAMPUS_MAIL_FROM"),
                    help='发件人，如 "姓名 <jobs@example.com>"（或设 CAMPUS_MAIL_FROM）')
    ap.add_argument("--smtp-host", default="smtp.resend.com")
    ap.add_argument("--smtp-port", type=int, default=465)
    ap.add_argument("--smtp-user", default="resend")
    ap.add_argument("--keychain", default="campus-apply-resend", help="钥匙串 service 名")
    ap.add_argument("--only", help="只发这些 announcement_id，逗号分隔")
    ap.add_argument("--limit", type=int, help="本次最多发几封")
    ap.add_argument("--interval", type=float, default=45.0, help="平均间隔秒数（±1/3 随机抖动）")
    ap.add_argument("--test-to", help="只把第一封发到这个地址做测试，不写任何记录")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if not a.instance:
        die("先 export CAMPUS_INSTANCE=<实例目录> 或传 --instance")
    if not a.sender:
        die('需要发件人：--from "姓名 <jobs@example.com>" 或设 CAMPUS_MAIL_FROM')
    only = set(a.only.split(",")) if a.only else None
    drafts = load_drafts(a.batch, only)
    pending = [(d, m) for d, m in drafts if not os.path.exists(os.path.join(d, "sent.json"))]
    bad = [(d, validate(d, m)) for d, m in pending]
    bad = [(d, e) for d, e in bad if e]
    if bad:
        for d, e in bad:
            print("校验失败 %s：%s" % (os.path.basename(d), "；".join(e)), file=sys.stderr)
        die("有 %d 封草稿没过校验，先修再发" % len(bad))

    cap = daily_limit(a.instance)
    already = sent_today(a.instance)
    room = max(0, cap - already)
    todo = pending[: min(room, a.limit or room)] if not a.test_to else pending[:1]
    print("草稿 %d 封 · 未发 %d 封 · 今日已发 %d/%d · 本次发 %d 封%s"
          % (len(drafts), len(pending), already, cap, len(todo), "（测试）" if a.test_to else ""))
    if not todo:
        return
    if a.dry_run:
        for d, m in todo:
            atts = ", ".join(os.path.basename(x) for x in m.get("attachments") or [])
            print("  [dry-run] %s → %s | %s | 附件：%s" % (m.get("company"), m["to"], m["subject"], atts))
        return

    smtp_secret = keychain(a.keychain)
    with smtplib.SMTP_SSL(a.smtp_host, a.smtp_port, timeout=60) as s:
        s.login(a.smtp_user, smtp_secret)
        for i, (d, m) in enumerate(todo):
            em = build(m, a.sender, "【测试】" if a.test_to else "", a.test_to)
            try:
                s.send_message(em)
            except smtplib.SMTPException as e:
                print("  ✗ %s → %s：%s" % (m.get("company"), m["to"], str(e)[:160]), file=sys.stderr)
                continue
            if a.test_to:
                print("  ✓ 测试邮件已发到 %s：%s" % (a.test_to, m["subject"]))
                return
            record(a.instance, d, m, em["Message-ID"])
            print("  ✓ %d/%d %s → %s" % (i + 1, len(todo), m.get("company"), m["to"]))
            if i + 1 < len(todo):
                time.sleep(max(5.0, random.uniform(a.interval * 2 / 3, a.interval * 4 / 3)))


if __name__ == "__main__":
    main()
