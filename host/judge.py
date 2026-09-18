#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6"]
# ///
"""judge.py · 用 LLM 读 JD 给公告分档（冲/常规/练手/不合适），写 $CAMPUS_INSTANCE/state/fit/<id>.json

    uv run --script host/judge.py --instance $CAMPUS_INSTANCE [--engine codex|devin|claude]
        [--ids 1,2,3] [--limit N] [--incremental] [--dry-run] [--batch-size N] [--model M]

输入：
  $CAMPUS_INSTANCE/intent.yaml     targets / recall / roles / cities / judgment_notes
  $CAMPUS_INSTANCE/profile.md      求职者画像（可省）
  $CAMPUS_INSTANCE/materials/resume-cn.pdf（或 materials/ 下首个 PDF）pdftotext 抽简历文本，没有就只凭 profile
  $CAMPUS_REPO/data/paperball/announcements.jsonl   公告元数据
  $CAMPUS_REPO/data/jd/<id>.md     JD 正文（status=ok 才用；否则用 original_jobs，结果 basis=jobs_only）

输出（每条一个文件）：
  $CAMPUS_INSTANCE/state/fit/<announcement_id>.json
  {announcement_id, company, tier: 冲|常规|练手|不合适, fit: 0-100, roles: [...],
   reasons: [≤3], risks: [...], basis: jd|jobs_only, model, intent_sha, jd_sha, judged_at}
  失败记 state/fit/_errors.jsonl。

缓存键 (announcement_id, jd_sha, intent_sha)：三者都没变不重判。
--incremental 只处理没有有效判断结果的公告；--dry-run 只打印 prompt 不调用 LLM。

分档后校验（代码强制，不只靠 prompt）：
  建议岗位全部命中 roles.exclude → 不合适；targets 命中且非不合适 → 冲；
  公告城市全在 cities.avoid → 练手；冲 需要 fit≥85，常规 需要 fit≥60，否则降档。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from zoneinfo import ZoneInfo

import yaml

REPO = os.environ.get("CAMPUS_REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CST = ZoneInfo("Asia/Shanghai")
TIERS = ("冲", "常规", "练手", "不合适")
JD_CHARS = 4000          # 每条 JD 喂给 LLM 的上限
PROFILE_CHARS = 3000
RESUME_CHARS = 4000
PIECE_SPLIT = re.compile(r"[,，、;；/|｜\n\r\t]+|\s{2,}")

SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["results"],
    "properties": {"results": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["announcement_id", "tier", "fit", "roles", "reasons", "risks"],
        "properties": {
            "announcement_id": {"type": "integer"},
            "tier": {"type": "string", "enum": list(TIERS)},
            "fit": {"type": "integer", "minimum": 0, "maximum": 100},
            "roles": {"type": "array", "items": {"type": "string"}},
            "reasons": {"type": "array", "items": {"type": "string"}},
            "risks": {"type": "array", "items": {"type": "string"}},
        }}}},
}


def norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "").lower()


def norm_company(s: str) -> str:
    s = (s or "").strip().replace("（", "(").replace("）", ")")
    return re.sub(r"\s+", "", s)


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def now_iso() -> str:
    return dt.datetime.now(CST).isoformat(timespec="seconds")


def atomic_write(path: str, text: str):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def load_scores(battle_map: str) -> dict[str, float]:
    """从 battle-map.md 读 id→score（scan 粗排），解析失败返回空表。"""
    out: dict[str, float] = {}
    if not os.path.isfile(battle_map):
        return out
    lines = open(battle_map, encoding="utf-8").read().splitlines()
    for i, line in enumerate(lines[:-1]):
        if not line.lstrip().startswith("|") or not re.match(r"^\s*\|?\s*:?-{2,}", lines[i + 1]):
            continue
        headers = [c.strip() for c in line.strip().strip("|").split("|")]
        if "score" not in headers or "id" not in headers:
            continue
        is_, iid = headers.index("score"), headers.index("id")
        for row in lines[i + 2:]:
            if not row.lstrip().startswith("|"):
                break
            cells = [c.strip() for c in row.strip().strip("|").split("|")]
            if max(is_, iid) < len(cells):
                try:
                    out[cells[iid]] = float(cells[is_])
                except ValueError:
                    pass
        break
    return out


def load_jd(jd_dir: str, aid: int) -> tuple[str, str, str]:
    """返回 (body, status, jd_sha)。文件缺失或非 ok → body 为空。"""
    p = os.path.join(jd_dir, "%s.md" % aid)
    if not os.path.isfile(p):
        return "", "missing", ""
    raw = open(p, encoding="utf-8").read()
    m = re.match(r"^---\n(.*?)\n---\n", raw, re.S)
    fm = {}
    body = raw
    if m:
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            fm = {}
        body = raw[m.end():].strip()
    status = str(fm.get("status") or "missing")
    if status not in ("ok", "ok_ocr") or not body:
        return "", status, str(fm.get("content_sha256") or "")
    return body, "ok", sha(body)


def resume_text(inst: str) -> str:
    """materials/resume-cn.pdf → 否则 materials/ 下首个 PDF，pdftotext 抽文字。"""
    mdir = os.path.join(inst, "materials")
    cands = [os.path.join(mdir, "resume-cn.pdf")]
    if os.path.isdir(mdir):
        cands += [os.path.join(mdir, f) for f in sorted(os.listdir(mdir))
                  if f.lower().endswith(".pdf")]
    for p in cands:
        if not os.path.isfile(p):
            continue
        try:
            r = subprocess.run(["pdftotext", "-layout", p, "-"],
                               capture_output=True, text=True, timeout=30)
            txt = re.sub(r"\n{3,}", "\n\n", r.stdout).strip()
            if len(txt) > 100:
                return txt[:RESUME_CHARS]
        except (OSError, subprocess.TimeoutExpired):
            continue
    return ""


def recalled(rec: dict, intent: dict) -> bool:
    """与 scan.py 同一口径：recall 非空走宽召回，否则 core/ok 命中。"""
    roles = intent.get("roles") or {}
    exclude = [norm(x) for x in roles.get("exclude") or []]
    recall = [norm(x) for x in (intent.get("recall") or {}).get("keywords") or []]
    text = "\n".join(x for x in (rec.get("title"), rec.get("original_jobs")) if x)
    pieces = [p for p in PIECE_SPLIT.split(text) if p.strip()]
    good = [p for p in pieces if not any(x and x in norm(p) for x in exclude)]
    if recall:
        return any(k and k in norm(p) for p in good for k in recall)
    return any(k and k in norm(p) for p in good
               for k in [norm(x) for x in (roles.get("core") or []) + (roles.get("ok") or [])])


def build_prompt(batch: list[dict], intent: dict, profile: str, resume: str) -> str:
    roles = intent.get("roles") or {}
    cities = intent.get("cities") or {}
    seg = []
    seg.append("你是校招投递顾问。根据求职者画像和意向，对下面每条校招公告判断要不要投、投哪个岗位、归哪一档。")
    seg.append("")
    seg.append("## 求职者画像")
    seg.append(profile[:PROFILE_CHARS] or "（无 profile）")
    seg.append("")
    seg.append("## 简历摘要")
    seg.append(resume or "（无简历文本，判断依据降级）")
    seg.append("")
    seg.append("## 求职意向")
    seg.append("- 心仪公司 targets：%s" % ("、".join(str(x) for x in intent.get("targets") or []) or "（无）"))
    seg.append("- 核心方向 core：%s" % ("、".join(str(x) for x in roles.get("core") or []) or "（无）"))
    seg.append("- 可接受方向 ok：%s" % ("、".join(str(x) for x in roles.get("ok") or []) or "（无）"))
    seg.append("- 明确排除 exclude：%s" % ("、".join(str(x) for x in roles.get("exclude") or []) or "（无）"))
    seg.append("- 城市偏好 prefer：%s" % ("、".join(str(x) for x in cities.get("prefer") or []) or "（无）"))
    seg.append("- 不想去的城市 avoid：%s（公告城市全落这里归练手档，照投不排除）"
               % ("、".join(str(x) for x in cities.get("avoid") or []) or "（无）"))
    notes = intent.get("judgment_notes") or []
    if notes:
        seg.append("- 判断备注：")
        seg += ["  · %s" % n for n in notes]
    seg.append("")
    seg.append("## 分档定义（策略：广撒网——只要有可投岗位就投，不看是否完全对口）")
    seg.append("- 冲：心仪公司且方向不排斥；或核心方向高匹配（fit≥85）")
    seg.append("- 常规：有目标方向岗位、值得投")
    seg.append("- 练手：城市全在不想去列表 / 岗位只边缘相关 / 公司吸引力一般但可投——照投，当面试练手")
    seg.append("- 不合适只允许三种硬情况：")
    seg.append("  ① 公告明确不招目标届数（class_types 不含目标届数）；")
    seg.append("  ② 公告里完全没有目标方向的岗位（如 dev 方向没有任何开发类岗位）；")
    seg.append("  ③ 岗位全部属于排除大类（算法研究/硬件/销售/柜员/客服类）。")
    seg.append("  除这三种外一律不得判不合适——证据弱、方向边缘、城市不喜都归练手。")
    seg.append("")
    seg.append("## 硬性规则（违反即错）")
    seg.append("- 你的主要职责是：**从岗位里选出最该投的那一个写进 roles**，再分档、记风险。")
    seg.append("  广撒网策略下\"不完全对口\"不是拒绝理由，只在 roles/risks 里体现取舍。")
    seg.append("- 届数认定随企业口径：公告 class_types 含目标届数即视为届数符合。class_time 的毕业时间窗口、")
    seg.append("  留服认证时间等与求职者真实毕业时间不一致，只能写进 risks（\"届数存疑：…\"），")
    seg.append("  **不得据此判不合适或降档**")
    seg.append("- 同理：城市写\"优先\"类表述、学历写\"硕士优先\"、专业不完全对口，都只进 risks，不降档")
    seg.append("")
    seg.append("## 公告")
    for rec, body, basis in batch:
        seg.append("### 公告 %s" % rec["announcement_id"])
        seg.append("公司：%s | 标签：%s | 届数：%s | 学历：%s" % (
            rec.get("company"), "/".join(rec.get("company_tags") or []),
            rec.get("class_time") or "—", "/".join(rec.get("degrees") or [])))
        seg.append("标题：%s" % rec.get("title"))
        seg.append("岗位列表：%s" % (rec.get("original_jobs") or "—"))
        seg.append("城市：%s | 发布：%s | 截止：%s" % (
            "/".join(rec.get("cities") or []) or "—",
            rec.get("published_at") or "—", rec.get("expired_at") or "—"))
        seg.append("投递入口：%s" % (rec.get("from_url") or "—"))
        if basis == "jd":
            seg.append("JD 正文：\n%s" % body[:JD_CHARS])
        else:
            seg.append("（无 JD 正文，只按岗位列表判断）")
        seg.append("")
    seg.append("## 输出")
    seg.append('只输出一个 JSON 对象 {"results":[…]}，每条公告一项：')
    seg.append('{"announcement_id":<int>,"tier":"冲|常规|练手|不合适","fit":<0-100>,'
               '"roles":["建议投的具体岗位"],"reasons":["≤3条"],"risks":["届数存疑/需附件/仅限某城市等"]}')
    seg.append("不要输出 JSON 以外的任何内容，不要使用工具，不要读写文件。")
    return "\n".join(seg)


def parse_json(text: str):
    text = text.strip()
    for pat in (r"```(?:json)?\s*(\{.*\}|\[.*\])\s*```", r"(\{.*\})", r"(\[.*\])"):
        m = re.search(pat, text, re.S)
        if m:
            try:
                return json.loads(m.group(1))
            except ValueError:
                continue
    return None


def run_engine(engine: str, prompt: str, model: str | None, workdir: str, timeout: int = 900) -> str:
    """返回 LLM 原始输出文本；失败抛 RuntimeError。"""
    if engine == "codex":
        schema_f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(SCHEMA, schema_f)
        schema_f.close()
        out_f = tempfile.mktemp(suffix=".txt")
        cmd = ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check",
               "-C", workdir, "--output-schema", schema_f.name, "-o", out_f, "-"]
        if model:
            cmd[2:2] = ["-m", model]
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout)
        os.unlink(schema_f.name)
        if r.returncode != 0:
            raise RuntimeError("codex exit %s: %s" % (r.returncode, (r.stderr or r.stdout)[-300:]))
        if os.path.isfile(out_f):
            return open(out_f, encoding="utf-8").read()
        return r.stdout
    if engine == "devin":
        cmd = ["devin", "-p", prompt, "--permission-mode", "auto"]
        if model:
            cmd += ["--model", model]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=workdir)
        if r.returncode != 0:
            raise RuntimeError("devin exit %s: %s" % (r.returncode, (r.stderr or "")[-300:]))
        return r.stdout
    if engine == "claude":
        cmd = ["claude", "-p", prompt, "--output-format", "text"]
        if model:
            cmd += ["--model", model]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=workdir)
        if r.returncode != 0:
            raise RuntimeError("claude exit %s: %s" % (r.returncode, (r.stderr or "")[-300:]))
        return r.stdout
    raise RuntimeError("未知引擎 %s" % engine)


def validate(item: dict, ids: set[int]) -> dict | None:
    try:
        aid = int(item.get("announcement_id"))
    except (TypeError, ValueError):
        return None
    if aid not in ids or item.get("tier") not in TIERS:
        return None
    try:
        fit = max(0, min(100, int(item.get("fit"))))
    except (TypeError, ValueError):
        return None
    return {"announcement_id": aid, "tier": item["tier"], "fit": fit,
            "roles": [str(x) for x in item.get("roles") or []][:8],
            "reasons": [str(x) for x in item.get("reasons") or []][:3],
            "risks": [str(x) for x in item.get("risks") or []][:6]}


def hits_exclude(text: str, exclude_n: list) -> bool:
    """text 是否命中排除词。先去掉括号注释与'非/不含/排除+词'的否定写法，
    避免'非嵌入式软件开发'被'嵌入式'误判。"""
    t = norm(re.sub(r"[（(][^）)]*[）)]", "", text or ""))
    for x in exclude_n:
        for pre in ("非", "不含", "排除", "限非"):
            t = t.replace(pre + x, "")
    return any(x and x in t for x in exclude_n)


def postcheck(item: dict, rec: dict, intent: dict) -> dict:
    """分档后校验：exclude 强判不合适；targets→冲；avoid 城市→练手；fit 阈值降档。
    广撒网策略：不合适只认三个硬依据（全排除 / 届数硬排除 / 完全没有目标方向岗位），
    其余理由（毕业时间窗口、城市、学历"优先"、专业不对口、匹配证据弱）只能进 risks，
    不合适一律降为练手。"""
    roles = intent.get("roles") or {}
    exclude_n = [norm(x) for x in roles.get("exclude") or []]
    targets_n = {norm_company(str(x)) for x in intent.get("targets") or []}
    avoid = {str(c) for c in (intent.get("cities") or {}).get("avoid") or []}
    tier = item["tier"]

    if tier != "不合适" and item["roles"] and all(
            hits_exclude(r, exclude_n) for r in item["roles"]):
        tier = "不合适"
        item["reasons"] = (item["reasons"] + ["建议岗位全部命中排除词，强制不合适"])[:3]
    if tier == "不合适":
        # 广撒网策略：不合适只认三个硬依据，全部只看结构字段、不看 LLM 措辞。
        # ①岗位全部命中排除大类 ②公告明确不含目标届数 ③完全没有目标方向岗位
        text = rec.get("original_jobs") or rec.get("title") or ""
        pieces = [p for p in PIECE_SPLIT.split(text) if p.strip()]
        hard_exclude = (bool(item["roles"]) and all(
            hits_exclude(r, exclude_n) for r in item["roles"])) or (
            bool(pieces) and all(hits_exclude(p, exclude_n) for p in pieces))
        want_cls = set(intent.get("class_types") or [8])
        rec_cls = set(rec.get("class_types") or [])
        hard_cohort = bool(rec_cls) and rec_cls.isdisjoint(want_cls)
        family = [norm(x) for x in (intent.get("recall") or {}).get("keywords") or []] or \
                 [norm(x) for x in (roles.get("core") or []) + (roles.get("ok") or [])]
        hard_no_family = bool(pieces) and not any(
            k and k in norm(p) for p in pieces for k in family)
        if not (hard_exclude or hard_cohort or hard_no_family):
            tier = "练手"
            item["risks"] = (item["risks"] + [
                "不合适依据不足：非全排除、届数未排除、有目标方向岗位，按广撒网降为练手"])[:6]
    if tier != "不合适":
        if norm_company(rec.get("company") or "") in targets_n:
            tier = "冲"
        elif avoid and rec.get("cities") and set(rec["cities"]) <= avoid:
            tier = "练手"
        elif tier == "冲" and item["fit"] < 85:
            tier = "常规"
        elif tier == "常规" and item["fit"] < 60:
            tier = "练手"
    item["tier"] = tier
    return item


def main() -> int:
    ap = argparse.ArgumentParser(description="LLM 读 JD 分档 → state/fit/<id>.json")
    ap.add_argument("--instance", default=os.environ.get("CAMPUS_INSTANCE"))
    ap.add_argument("--intent", default=None)
    ap.add_argument("--engine", choices=["codex", "devin", "claude"], default="codex")
    ap.add_argument("--model", default=None)
    ap.add_argument("--data", default=os.path.join(REPO, "data", "paperball", "announcements.jsonl"))
    ap.add_argument("--jd-dir", default=os.path.join(REPO, "data", "jd"))
    ap.add_argument("--ids", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--batch-size", type=int, default=8, help="每次 LLM 调用判几条（5–10）")
    ap.add_argument("--budget", type=int, default=None,
                    help="本次最多 LLM 调用（批）数；--incremental 时默认 60")
    ap.add_argument("--order", choices=["score", "published"], default="published",
                    help="score=按 scan 粗排从高到低判（读实例 battle-map.md）；published=按发布日期")
    ap.add_argument("--incremental", action="store_true", help="只处理没有有效判断的公告")
    ap.add_argument("--dry-run", action="store_true", help="只打印 prompt，不调用")
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args()
    if not args.instance:
        ap.error("需要 --instance 或 $CAMPUS_INSTANCE")
    inst = os.path.abspath(os.path.expanduser(args.instance))
    intent_path = args.intent or os.path.join(inst, "intent.yaml")
    intent = yaml.safe_load(open(intent_path, encoding="utf-8")) or {}
    intent_sha = sha(open(intent_path, "rb").read().hex())
    fit_dir = os.path.join(inst, "state", "fit")
    err_path = os.path.join(fit_dir, "_errors.jsonl")
    os.makedirs(fit_dir, exist_ok=True)

    class_types = set(intent.get("class_types") or [8])
    since = intent.get("published_since")
    today = dt.datetime.now(CST).date()

    anns = {}
    for line in open(args.data, encoding="utf-8"):
        if line.strip():
            r = json.loads(line)
            anns[r["announcement_id"]] = r

    # 选公告：--ids 指定；否则按 intent 过滤 + 宽召回
    if args.ids:
        recs = [anns[i] for i in (int(x) for x in re.split(r"[,，\s]+", args.ids) if x.strip()) if i in anns]
    else:
        recs = []
        for r in anns.values():
            if not set(r.get("class_types") or []) & class_types:
                continue
            if since and (not r.get("published_at") or r["published_at"] < str(since)[:10]):
                continue
            exp = r.get("expired_at")
            if exp and exp < today.isoformat():
                continue
            if not recalled(r, intent):
                continue
            recs.append(r)
        recs.sort(key=lambda r: r.get("published_at") or "", reverse=True)
    if args.limit:
        recs = recs[: args.limit]

    # 附 JD / 缓存检查
    todo = []
    n_cached = 0
    for rec in recs:
        aid = rec["announcement_id"]
        body, jd_status, jd_sha = load_jd(args.jd_dir, aid)
        basis = "jd" if body else "jobs_only"
        key_sha = jd_sha or sha("jobs_only:" + (rec.get("original_jobs") or ""))
        old_path = os.path.join(fit_dir, "%s.json" % aid)
        if os.path.isfile(old_path):
            try:
                old = json.load(open(old_path, encoding="utf-8"))
            except (OSError, ValueError):
                old = {}
            if old.get("jd_sha") == key_sha and old.get("intent_sha") == intent_sha:
                n_cached += 1
                continue
        todo.append((rec, body, basis, key_sha))

    if args.order == "score":
        scores = load_scores(os.path.join(inst, "log", "battle-map.md"))
        todo.sort(key=lambda t: (-scores.get(str(t[0]["announcement_id"]), 0.0),
                                 t[0]["announcement_id"]))
    print("待判 %d 条（缓存命中跳过 %d）" % (len(todo), n_cached), file=sys.stderr)

    profile = ""
    pf = os.path.join(inst, "profile.md")
    if os.path.isfile(pf):
        profile = open(pf, encoding="utf-8").read()
    resume = resume_text(inst)

    bs = max(1, min(10, args.batch_size))
    budget = args.budget if args.budget is not None else (60 if args.incremental else None)
    batches = [todo[i:i + bs] for i in range(0, len(todo), bs)]
    if budget is not None and len(batches) > budget:
        print("预算 %d 批：本次只判前 %d 条，剩余 %d 条留待下轮" % (
            budget, budget * bs, len(todo) - budget * bs), file=sys.stderr)
        batches = batches[:budget]
    if args.dry_run:
        for i, b in enumerate(batches):
            print("===== batch %d (%d 条) =====" % (i + 1, len(b)))
            print(build_prompt([(r, bd, ba) for r, bd, ba, _ in b], intent, profile, resume))
        return 0

    n_ok = n_err = 0
    for bi, b in enumerate(batches, 1):
        prompt = build_prompt([(r, bd, ba) for r, bd, ba, _ in b], intent, profile, resume)
        ids = {r["announcement_id"] for r, _, _, _ in b}
        items = None
        last_err = ""
        for attempt in (1, 2):
            try:
                raw = run_engine(args.engine, prompt, args.model, inst, args.timeout)
                parsed = parse_json(raw)
                if isinstance(parsed, dict):
                    parsed = parsed.get("results")
                if isinstance(parsed, list):
                    items = {v["announcement_id"]: v
                             for v in (validate(x, ids) for x in parsed) if v}
                    if items:
                        break
                last_err = "输出不是合法 JSON/缺字段"
            except (RuntimeError, subprocess.TimeoutExpired) as e:
                last_err = str(e)[:200]
            items = None
        if items is None:
            n_err += len(b)
            with open(err_path, "a", encoding="utf-8") as fh:
                for rec, _, basis, _ in b:
                    fh.write(json.dumps({"announcement_id": rec["announcement_id"],
                                         "company": rec.get("company"), "engine": args.engine,
                                         "error": last_err, "at": now_iso()},
                                        ensure_ascii=False) + "\n")
            print("batch %d 失败：%s" % (bi, last_err), file=sys.stderr)
            continue
        for rec, body, basis, key_sha in b:
            aid = rec["announcement_id"]
            it = items.get(aid)
            if it is None:
                with open(err_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"announcement_id": aid, "company": rec.get("company"),
                                         "engine": args.engine, "error": "批内缺该条结果",
                                         "at": now_iso()}, ensure_ascii=False) + "\n")
                n_err += 1
                continue
            it = postcheck(it, rec, intent)
            doc = {"announcement_id": aid, "company": rec.get("company"), "tier": it["tier"],
                   "fit": it["fit"], "roles": it["roles"], "reasons": it["reasons"],
                   "risks": it["risks"], "basis": basis,
                   "model": args.model or args.engine, "intent_sha": intent_sha,
                   "jd_sha": key_sha, "judged_at": now_iso()}
            atomic_write(os.path.join(fit_dir, "%s.json" % aid),
                         json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
            n_ok += 1
        print("batch %d/%d 完成（累计 ok %d · err %d）" % (bi, len(batches), n_ok, n_err), file=sys.stderr)
    print("完成：判 %d · 失败 %d · 缓存跳过 %d" % (n_ok, n_err, n_cached), file=sys.stderr)
    return 0 if n_err == 0 else (1 if n_ok == 0 else 0)


if __name__ == "__main__":
    sys.exit(main())
