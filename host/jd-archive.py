#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# ///
"""jd-archive.py · 投递过的岗位 JD 存档 + 投递统计（面试前翻看用）

    uv run --script host/jd-archive.py [--instance DIR] [--dry-run]

对每条 result=submitted 的投递结果（state/outcomes/*.json，门户与邮件都算）：
  - 实例 jd/ 下已有 <announcement_id>-*.md（worker 提交后保存的岗位详情页）→ 不动
  - 没有 → 用公告数据补一份 jd/<announcement_id>-<公司>-<岗位>.md：
      投递信息（岗位、门户、时间、链接）+ 公告岗位列表 + data/jd/<id>.md 公告原文（抓到才有）
      邮件投递另附草稿里记录的公告邮件要求
然后重写 jd/INDEX.md：总数、按门户/按天统计、每家一行（时间、公司、岗位、门户、JD 来源）。

只写实例目录，不碰仓库。可重复运行。
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import sys

REPO = os.environ.get("CAMPUS_REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORTAL_CN = {"moka": "Moka", "beisen": "北森", "feishu": "飞书", "email": "邮件", "zhaopin": "智联"}
BAD = re.compile(r'[\\/:*?"<>|\s]+')


def safe(s: str, n: int = 40) -> str:
    return BAD.sub("-", str(s or "").strip()).strip("-")[:n] or "未知"


def load_announcements() -> dict[str, dict]:
    out = {}
    p = os.path.join(REPO, "data", "paperball", "announcements.jsonl")
    if os.path.isfile(p):
        for line in open(p, encoding="utf-8"):
            try:
                a = json.loads(line)
            except ValueError:
                continue
            out[str(a.get("announcement_id"))] = a
    return out


def jd_body(aid: str) -> tuple[str, str]:
    """data/jd/<id>.md 的正文与抓取状态（ok/ok_ocr 才算有效）。"""
    p = os.path.join(REPO, "data", "jd", "%s.md" % aid)
    if not os.path.isfile(p):
        return "", "未抓取"
    text = open(p, encoding="utf-8").read()
    m = re.match(r"---\n(.*?)\n---\n(.*)", text, re.S)
    if not m:
        return text, "ok"
    status = (re.search(r"^status:\s*(\S+)", m.group(1), re.M) or [None, "?"])[1]
    return (m.group(2).strip(), status) if status in ("ok", "ok_ocr") else ("", status)


def main():
    ap = argparse.ArgumentParser(description="投递 JD 存档与统计")
    ap.add_argument("--instance", default=os.environ.get("CAMPUS_INSTANCE"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not a.instance:
        sys.exit("先 export CAMPUS_INSTANCE=<实例目录>")
    inst = os.path.abspath(a.instance)
    jd_dir = os.path.join(inst, "jd")
    os.makedirs(jd_dir, exist_ok=True)
    anns = load_announcements()

    rows, written = [], 0
    for p in sorted(glob.glob(os.path.join(inst, "state", "outcomes", "*.json"))):
        try:
            o = json.load(open(p, encoding="utf-8"))
        except ValueError:
            continue
        if o.get("result") not in ("submitted", "verified"):
            continue
        aid = str(o.get("announcement_id") or os.path.basename(p)[:-5])
        ann = anns.get(aid, {})
        company = o.get("company") or ann.get("company") or aid
        role = o.get("job_applied") or ""
        portal = o.get("portal") or ""
        at = (o.get("submitted_at") or "")[:16].replace("T", " ")
        existing = [f for f in glob.glob(os.path.join(jd_dir, "%s-*.md" % aid))
                    if "来源：公告补录" not in open(f, encoding="utf-8").read(400)]   # 补录的每次重生成
        for f in glob.glob(os.path.join(jd_dir, "%s-*.md" % aid)):
            if f not in existing and not a.dry_run:
                os.remove(f)
        if existing:
            src = "详情页"
            fname = os.path.basename(existing[0])
        else:
            body, status = jd_body(aid)
            mail = {}
            for mp in glob.glob(os.path.join(inst, "mail", "*", aid, "message.json")):
                try:
                    mail = json.load(open(mp, encoding="utf-8"))
                except ValueError:
                    pass
            fname = "%s-%s-%s.md" % (aid, safe(company, 30), safe(role or "未记录岗位", 40))
            lines = [
                "# %s · %s" % (company, role or "（岗位未记录）"), "",
                "> 来源：公告补录（worker 未保存岗位详情页；面试前建议打开链接看最新 JD）", "",
                "| 项 | 内容 |", "|---|---|",
                "| 公司 | %s |" % company,
                "| 投递岗位 | %s |" % (role or "—"),
                "| 投递方式 | %s |" % PORTAL_CN.get(portal, portal or "—"),
                "| 投递时间 | %s |" % (at or "—"),
                "| 城市 | %s |" % "、".join(ann.get("cities") or []),
                "| 公告 | %s |" % (ann.get("title") or "—"),
                "| 公告链接 | %s |" % (ann.get("link") or "—"),
                "| 投递入口 | %s |" % (ann.get("from_url") or "—").replace("\n", " "),
                "", "## 公告里的岗位列表", "", str(ann.get("original_jobs") or "—"), "",
            ]
            if mail.get("requirement_raw"):
                lines += ["## 邮件投递要求", "", str(mail["requirement_raw"]), ""]
            lines += ["## 公告原文", "", body if body else "（未抓到公告原文：%s）" % status, ""]
            if not a.dry_run:
                open(os.path.join(jd_dir, fname), "w", encoding="utf-8").write("\n".join(lines))
            written += 1
            src = "公告" + ("" if body else "（无原文）")
        rows.append({"at": at, "company": company, "role": role, "portal": PORTAL_CN.get(portal, portal),
                     "src": src, "file": fname})

    rows.sort(key=lambda r: r["at"], reverse=True)
    by_portal = collections.Counter(r["portal"] for r in rows)
    by_day = collections.Counter(r["at"][:10] for r in rows)
    out = ["# 投递记录与 JD", "",
           "共 **%d** 家。按方式：%s" % (len(rows), "、".join("%s %d" % kv for kv in by_portal.most_common())), "",
           "按天：%s" % "、".join("%s %d" % (d or "未知", n) for d, n in sorted(by_day.items(), reverse=True)), "",
           "JD 来源：「详情页」= worker 提交后保存的岗位详情；「公告」= 从校招公告补录（岗位细节以链接为准）。", "",
           "| 投递时间 | 公司 | 岗位 | 方式 | JD |", "|---|---|---|---|---|"]
    for r in rows:
        out.append("| %s | %s | %s | %s | [%s](%s) |" % (r["at"], r["company"], r["role"] or "—", r["portal"],
                                                        r["src"], r["file"].replace(" ", "%20")))
    if not a.dry_run:
        open(os.path.join(jd_dir, "INDEX.md"), "w", encoding="utf-8").write("\n".join(out) + "\n")
    print("投递 %d 家（%s）· 新补 JD %d 份 · 索引 jd/INDEX.md"
          % (len(rows), "、".join("%s %d" % kv for kv in by_portal.most_common()), written))


if __name__ == "__main__":
    main()
