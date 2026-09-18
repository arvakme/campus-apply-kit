#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6"]
# ///
"""scan.py · 按个人 intent.yaml 从全量公告里筛候选、打分，写 battle-map.md

    uv run --script host/scan.py [--instance DIR] [--out PATH] [--today YYYY-MM-DD]

输入：
  $CAMPUS_REPO/data/paperball/announcements.jsonl   （sync-source.py 产出，contracts §4）
  $CAMPUS_REPO/data/tiers.yaml                      公司 → 层级
  $CAMPUS_INSTANCE/intent.yaml                      方向 / 层级权重 / 紧迫度 / 城市 / 策略（contracts §2）
  $CAMPUS_INSTANCE/tracker.md                       已在台账里的 id 一律剔除（防重复投递）
输出：
  $CAMPUS_INSTANCE/log/battle-map.md（--out 可覆盖），表头见 contracts §5，可被看板解析器按表头名读取

判定与打分（写进输出文件头部口径段，便于核对）：
  1. 基础过滤：class_types ∩ intent.class_types；published_at >= published_since；expired_at 为空或 >= today
  2. 召回：intent.recall.keywords 非空时走宽召回——标题 + original_jobs 按分隔符切成岗位片段，
     含 exclude 词的片段整段作废，剩余片段命中任一 recall 词即入队（岗位名看不出方向的"开发类/技术类"
     也能进来，投不投由 host/judge.py 读 JD 后判断）；recall 为空时退回旧口径：剩余片段命中 core → 3，
     命中 ok → 2，都没有 → 不入队。
  3. 方向分（粗排用）：core=3 · ok=2 · 仅 recall 命中=1；命中词写进"命中关键词"列
  4. 层级：tiers.yaml 显式名单 → 否则 company_tags 含 国企/事业单位 → 国企 → 否则 中小；权重取 intent.tiers
  5. 紧迫：expired_at 距 today <= 7 天 × urgency.le_7d，<= 14 天 × urgency.le_14d，其余 × 1
  6. 城市：公告城市全部落在 cities.avoid 内 → 打"练手:仅X"标签、排序按练手档，不再压分；prefer 只影响同分排序
  7. score = 方向分 × 层级权重 × 紧迫系数
  8. 公司去重：同公司多条保留方向分最高、其次发布最新的一条，其余在备注记"另N条(id,…)"
  9. 策略：policies.soe=skip 剔除国企，report_only 保留但备注"国企·仅报告"；policies.intern=false 剔除标题含"实习"的公告
 10. 邮件类：from_url 或 original_jobs 里能抽出合法邮箱 → "是:<邮箱>"，否则"否"（实测 from_url 常为"邮箱投递：xx@yy"文本，不是 mailto:）
 11. 分档：读 <instance>/state/fit/*.json（host/judge.py 产出），备注列前缀 [冲]/[常规]/[练手]，
     排序 冲→常规→未判→练手（档内按 score）；判"不合适"的不入队，单列文末
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import re
import sys
import tempfile
from zoneinfo import ZoneInfo

import yaml

REPO = os.environ.get("CAMPUS_REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CST = ZoneInfo("Asia/Shanghai")
HEADERS = ["score", "发布日期", "公司", "标题", "id", "类别", "命中关键词", "expired_at", "邮件类", "看板状态", "备注"]
EMAIL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
PIECE_SPLIT = re.compile(r"[,，、;；/|｜\n\r\t]+|\s{2,}")
SOE_TAGS = {"国企", "事业单位"}


def norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "").lower()


def norm_company(s: str) -> str:
    s = (s or "").strip().replace("（", "(").replace("）", ")")
    return re.sub(r"\s+", "", s)


def load_tiers(path: str) -> tuple[dict[str, str], str]:
    data = yaml.safe_load(open(path, encoding="utf-8")) or {}
    mapping: dict[str, str] = {}
    for tier, names in (data.get("tiers") or {}).items():
        for name in names or []:
            key = norm_company(str(name))
            if key in mapping and mapping[key] != tier:
                raise SystemExit("tiers.yaml：公司 %s 同时出现在 %s 和 %s" % (name, mapping[key], tier))
            mapping[key] = tier
    return mapping, data.get("default") or "中小"


def load_tracker_ids(path: str) -> tuple[set[str], set[str]]:
    """读 tracker.md 第一张含 id 列的表，返回 (id 集合, 公司名集合)。缺文件返回空集。"""
    ids, companies = set(), set()
    if not os.path.isfile(path):
        print("WARN: 找不到 %s，不做已投递剔除" % path, file=sys.stderr)
        return ids, companies
    lines = open(path, encoding="utf-8").read().splitlines()
    for i, line in enumerate(lines[:-1]):
        if not line.lstrip().startswith("|") or not re.match(r"^\s*\|?\s*:?-{2,}", lines[i + 1]):
            continue
        headers = [c.strip() for c in line.strip().strip("|").split("|")]
        if "id" not in headers:
            continue
        idx_id = headers.index("id")
        idx_co = headers.index("公司") if "公司" in headers else None
        for row in lines[i + 2:]:
            if not row.lstrip().startswith("|"):
                break
            cells = [c.strip() for c in row.strip().strip("|").split("|")]
            if idx_id < len(cells):
                for m in re.findall(r"\d{4,}", cells[idx_id]):
                    ids.add(m)
            if idx_co is not None and idx_co < len(cells) and cells[idx_co]:
                companies.add(norm_company(re.sub(r"[（(].*?[)）]", "", cells[idx_co].replace("*", ""))))
        break
    return ids, companies


def as_date(v) -> dt.date | None:
    if v is None or v == "":
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    return dt.date.fromisoformat(str(v)[:10])


def match_direction(rec: dict, core: list[str], ok: list[str], exclude: list[str],
                    recall: list[str] | None = None):
    """返回 (方向分, 命中关键词列表, 是否有片段被排除)。

    recall 非空时走宽召回：方向分只用于粗排（core=3 · ok=2 · 仅 recall 命中=1），
    返回的方向分 0 表示连 recall 都没命中（不入队）。
    """
    text = "\n".join(x for x in (rec.get("title"), rec.get("original_jobs")) if x)
    pieces = [p for p in PIECE_SPLIT.split(text) if p.strip()]
    ex_n = [norm(x) for x in exclude]
    hits_core, hits_ok, hits_recall, excluded_any = [], [], [], False
    for p in pieces:
        pn = norm(p)
        if any(x and x in pn for x in ex_n):
            excluded_any = True
            continue
        for kw in core:
            if norm(kw) in pn and kw not in hits_core:
                hits_core.append(kw)
        for kw in ok:
            if norm(kw) in pn and kw not in hits_ok:
                hits_ok.append(kw)
        for kw in recall or []:
            if norm(kw) in pn and kw not in hits_recall:
                hits_recall.append(kw)
    if hits_core:
        return 3, hits_core + [k for k in hits_ok if k not in hits_core], excluded_any
    if hits_ok:
        return 2, hits_ok, excluded_any
    if recall is not None:
        return (1, hits_recall, excluded_any) if hits_recall else (0, [], excluded_any)
    return 0, [], excluded_any


def load_fit(dirpath: str | None) -> dict[str, dict]:
    """读 <instance>/state/fit/*.json（judge.py 产出），返回 {announcement_id: 判断结果}。"""
    out: dict[str, dict] = {}
    if not dirpath or not os.path.isdir(dirpath):
        return out
    for fn in os.listdir(dirpath):
        if not fn.endswith(".json") or fn.startswith("_"):
            continue
        try:
            d = json.load(open(os.path.join(dirpath, fn), encoding="utf-8"))
        except (OSError, ValueError):
            continue
        aid = str(d.get("announcement_id") or fn[:-5])
        if d.get("tier"):
            out[aid] = d
    return out


TIER_RANK = {"冲": 0, "常规": 1, "练手": 3}   # 未判=2


def extract_emails(rec: dict) -> list[str]:
    found: list[str] = []
    for field in ("from_url", "original_jobs"):
        v = rec.get(field) or ""
        if field == "from_url" and v.lower().startswith("mailto:"):
            v = v[7:]
        for m in EMAIL_RE.findall(v):
            m = m.rstrip(".")
            if m.lower() not in (x.lower() for x in found):
                found.append(m)
    return found


def cell(v) -> str:
    return str(v if v not in (None, "") else "").replace("|", "\\|").replace("\n", " ").strip()


def main() -> int:
    ap = argparse.ArgumentParser(description="按 intent.yaml 生成候选 battle-map.md")
    ap.add_argument("--instance", default=os.environ.get("CAMPUS_INSTANCE"), help="实例目录（默认 $CAMPUS_INSTANCE）")
    ap.add_argument("--intent", default=None, help="默认 <instance>/intent.yaml")
    ap.add_argument("--tracker", default=None, help="默认 <instance>/tracker.md")
    ap.add_argument("--out", default=None, help="默认 <instance>/log/battle-map.md")
    ap.add_argument("--data", default=os.path.join(REPO, "data", "paperball", "announcements.jsonl"))
    ap.add_argument("--tiers", default=os.path.join(REPO, "data", "tiers.yaml"))
    ap.add_argument("--today", default=None, help="计算截止/紧迫度的基准日（默认北京时间今天）")
    args = ap.parse_args()
    if not args.instance and not (args.intent and args.out):
        ap.error("需要 --instance 或环境变量 CAMPUS_INSTANCE")
    inst = os.path.abspath(os.path.expanduser(args.instance)) if args.instance else None
    intent_path = args.intent or os.path.join(inst, "intent.yaml")
    tracker_path = args.tracker or (os.path.join(inst, "tracker.md") if inst else "")
    out_path = args.out or os.path.join(inst, "log", "battle-map.md")
    today = as_date(args.today) or dt.datetime.now(CST).date()

    intent = yaml.safe_load(open(intent_path, encoding="utf-8")) or {}
    class_types = set(intent.get("class_types") or [8])
    since = as_date(intent.get("published_since"))
    roles = intent.get("roles") or {}
    core = [str(x) for x in roles.get("core") or []]
    ok = [str(x) for x in roles.get("ok") or []]
    exclude = [str(x) for x in roles.get("exclude") or []]
    recall_cfg = intent.get("recall") or {}
    recall = [str(x) for x in recall_cfg.get("keywords") or []] or None
    tier_w = {**{"大厂": 3, "名企": 2, "国企": 2, "中小": 1}, **(intent.get("tiers") or {})}
    urg = intent.get("urgency") or {}
    le7, le14 = float(urg.get("le_7d", 2)), float(urg.get("le_14d", 1.5))
    cities = intent.get("cities") or {}
    prefer = [str(c) for c in cities.get("prefer") or []]
    avoid = {str(c) for c in cities.get("avoid") or []}
    policies = intent.get("policies") or {}
    soe_policy = policies.get("soe", "apply_all")
    allow_intern = policies.get("intern", True)
    if not core and not ok and not recall:
        print("WARN: intent.roles.core/ok 与 recall.keywords 都为空，没有公告会入队", file=sys.stderr)

    tiers, default_tier = load_tiers(args.tiers)
    tracker_ids, tracker_companies = load_tracker_ids(tracker_path) if tracker_path else (set(), set())
    fit = load_fit(os.path.join(inst, "state", "fit")) if inst else {}
    meta_path = os.path.join(os.path.dirname(args.data), "meta.json")
    meta = json.load(open(meta_path, encoding="utf-8")) if os.path.isfile(meta_path) else {}

    stats = collections.Counter()
    cands, already, unfit = [], [], []
    for line in open(args.data, encoding="utf-8"):
        if not line.strip():
            continue
        rec = json.loads(line)
        stats["total"] += 1
        if not set(rec.get("class_types") or []) & class_types:
            stats["drop_class"] += 1
            continue
        pub = as_date(rec.get("published_at"))
        if since and (not pub or pub < since):
            stats["drop_since"] += 1
            continue
        exp = as_date(rec.get("expired_at"))
        if exp and exp < today:
            stats["drop_expired"] += 1
            continue
        dscore, hits, _ = match_direction(rec, core, ok, exclude, recall)
        if dscore == 0:
            stats["drop_recall" if recall else "drop_direction"] += 1
            continue
        if not allow_intern and "实习" in (rec.get("title") or ""):
            stats["drop_intern"] += 1
            continue
        company = rec.get("company") or ""
        tier = tiers.get(norm_company(company))
        if tier is None:
            tier = "国企" if SOE_TAGS & set(rec.get("company_tags") or []) else default_tier
        if tier == "国企" and soe_policy == "skip":
            stats["drop_soe"] += 1
            continue
        if str(rec["announcement_id"]) in tracker_ids:
            already.append(rec)
            stats["drop_tracker"] += 1
            continue
        f = fit.get(str(rec["announcement_id"]))
        if f and f.get("tier") == "不合适":
            unfit.append((rec, f))
            stats["drop_unfit"] += 1
            continue
        notes = []
        mult_u = 1.0
        if exp:
            days = (exp - today).days
            mult_u = le7 if days <= 7 else le14 if days <= 14 else 1.0
        ann_cities = rec.get("cities") or []
        practice = bool(avoid and ann_cities and set(ann_cities) <= avoid)
        if practice:
            notes.append("练手:仅%s" % "/".join(ann_cities))
        rank = min((prefer.index(c) for c in ann_cities if c in prefer), default=len(prefer))
        if tier == "国企" and soe_policy == "report_only":
            notes.append("国企·仅报告")
        if norm_company(company) in tracker_companies:
            notes.append("tracker已有同公司")
        score = dscore * float(tier_w.get(tier, 1)) * mult_u
        fit_tier = (f or {}).get("tier")
        if fit_tier:
            notes.insert(0, "[%s]" % fit_tier)
            stats["fit_" + fit_tier] += 1
        cands.append({"rec": rec, "dscore": dscore, "score": score, "hits": hits, "tier": tier, "pub": pub,
                      "exp": exp, "rank": rank, "notes": notes, "emails": extract_emails(rec),
                      "fit_tier": fit_tier, "practice": practice})

    # 公司去重：方向分最高 → 发布最新 → id 大
    groups: dict[str, list] = collections.defaultdict(list)
    for c in cands:
        groups[norm_company(c["rec"].get("company") or str(c["rec"]["announcement_id"]))].append(c)
    kept = []
    for items in groups.values():
        items.sort(key=lambda c: (c["dscore"], c["pub"] or dt.date.min, c["rec"]["announcement_id"]), reverse=True)
        head = items[0]
        if len(items) > 1:
            head["notes"].append("另%d条(%s)" % (len(items) - 1, ",".join(str(i["rec"]["announcement_id"]) for i in items[1:])))
        kept.append(head)
    kept.sort(key=lambda c: (TIER_RANK.get(c["fit_tier"], 3 if c["practice"] else 2),
                             -c["score"], c["rank"], -(c["pub"] or dt.date.min).toordinal(),
                             c["rec"]["announcement_id"]))

    buckets = collections.Counter()
    for c in kept:
        buckets[c["score"]] += 1
    mail_n = sum(1 for c in kept if c["emails"])
    tier_n = collections.Counter(c["tier"] for c in kept)

    out = []
    out.append("# 校招作战地图(scan.py 生成 · 公司去重 · 打分排序)")
    out.append("")
    out.append("> 生成文件，勿手改：`uv run --script host/scan.py` 重新生成。")
    out.append("")
    out.append("## 口径")
    out.append("")
    out.append("- 输入:data/paperball/announcements.jsonl(同步于 %s,共 %s 条,已含 在招+未截止+届数+发布日期 口径) · intent=%s · 基准日 %s"
               % (meta.get("synced_at", "?"), stats["total"], os.path.basename(intent_path), today.isoformat()))
    out.append("- 过滤:class_types∩%s · published_at>=%s · expired_at 为空或>=基准日;剔除 届数%d · 发布日期%d · 已截止%d"
               % (sorted(class_types), since, stats["drop_class"], stats["drop_since"], stats["drop_expired"]))
    if recall:
        out.append("- 召回(宽):标题+original_jobs 按分隔符切片,含排除词的片段作废;剩余片段命中 recall 词即入队,"
                   "方向分仅粗排 core(%s)=3 · ok(%s)=2 · 仅recall=1 · 排除(%s);未命中 %d 条"
                   % ("|".join(core) or "—", "|".join(ok) or "—", "|".join(exclude) or "—", stats["drop_recall"]))
        out.append("- recall 词:%s" % "|".join(recall))
    else:
        out.append("- 方向:标题+original_jobs 按分隔符切片,含排除词的片段作废;core(%s)=3 · ok(%s)=2 · 排除(%s);未命中 %d 条"
                   % ("|".join(core) or "—", "|".join(ok) or "—", "|".join(exclude) or "—", stats["drop_direction"]))
    out.append("- 打分=方向分×层级分(%s)×紧迫分(expired_at≤7天×%s|≤14天×%s|其他×1);城市全在 avoid[%s] 不压分,打练手标签"
               % ("|".join("%s%s" % (k, v) for k, v in tier_w.items()), le7, le14, "|".join(sorted(avoid)) or "—"))
    out.append("- 层级:data/tiers.yaml 显式名单 → company_tags 含国企/事业单位 → 国企 → 其余中小")
    out.append("- 公司去重:同公司多条公告保留方向分最高+最新一条,其余在备注列记条数")
    out.append("- 策略:soe=%s · intern=%s;已在 tracker.md 的 id 剔除(%d 条,单列文末)" % (soe_policy, allow_intern, len(already)))
    if fit:
        out.append("- 分档:state/fit 共 %d 条判断,备注列前缀档标,排序 冲→常规→未判→练手;判不合适 %d 条不入队(单列文末)"
                   % (len(fit), stats["drop_unfit"]))
    out.append("- 看板状态:公共数据不含个人投递进度,一律记—")
    out.append("")
    out.append("## 统计")
    out.append("")
    out.append("- 待投队列:**%d** 家(公司去重后,去重前 %d 条)" % (len(kept), len(cands)))
    if fit:
        out.append("- 分档:" + " · ".join("%s %d" % (t, stats["fit_" + t]) for t in ("冲", "常规", "练手") if stats["fit_" + t])
                   + " · 未判 %d · 不合适(未入队) %d" % (len(kept) - sum(stats["fit_" + t] for t in ("冲", "常规", "练手")), stats["drop_unfit"]))
    out.append("- 分数段:" + " · ".join("%s分 %d" % (("%.1f" % s).rstrip("0").rstrip("."), n)
                                     for s, n in sorted(buckets.items(), reverse=True)))
    out.append("- 层级:" + " · ".join("%s %d" % (t, tier_n[t]) for t in ("大厂", "名企", "国企", "外企", "中小") if tier_n[t]))
    out.append("- 邮件类:**%d** 条(邮箱列含地址可直接投)" % mail_n)
    out.append("")
    out.append("| " + " | ".join(HEADERS) + " |")
    out.append("|" + "---|" * len(HEADERS))
    for c in kept:
        r = c["rec"]
        out.append("| " + " | ".join(cell(x) for x in [
            "%.1f" % c["score"],
            r.get("published_at") or "—",
            r.get("company"),
            r.get("title"),
            r["announcement_id"],
            c["tier"],
            "、".join(c["hits"]),
            r.get("expired_at") or "—",
            ("是:" + "; ".join(c["emails"])) if c["emails"] else "否",
            "—",
            "；".join(c["notes"]),
        ]) + " |")
    if unfit:
        out.append("")
        out.append("## 已判不合适(LLM 读 JD 后判定,不入队)")
        out.append("")
        out.append("| 公司 | 标题 | 公告id | 理由 |")
        out.append("|---|---|---|---|")
        for r, f in sorted(unfit, key=lambda x: x[0]["announcement_id"]):
            reason = "；".join((f.get("reasons") or [])[:1])
            out.append("| %s | %s | %s | %s |" % (cell(r.get("company")), cell(r.get("title")),
                                                  r["announcement_id"], cell(reason)))
    if already:
        out.append("")
        out.append("## 已在台账(不再重复投递)")
        out.append("")
        out.append("| 公司 | 标题 | 公告id | 发布日期 |")
        out.append("|---|---|---|---|")
        for r in sorted(already, key=lambda r: r["announcement_id"]):
            out.append("| %s | %s | %s | %s |" % (cell(r.get("company")), cell(r.get("title")), r["announcement_id"],
                                                 r.get("published_at") or "—"))
    content = "\n".join(out) + "\n"

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(out_path)), prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.chmod(tmp, 0o644)
    os.replace(tmp, out_path)
    print("写出 %s：候选 %d 家（去重前 %d 条）· 邮件类 %d · 台账剔除 %d · 方向未命中 %d" % (
        out_path, len(kept), len(cands), mail_n, len(already), stats["drop_direction"]), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
