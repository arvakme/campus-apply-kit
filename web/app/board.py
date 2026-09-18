"""board.py · 全量每日榜单：公告全量按雇主合并 + 个人状态同步 + 每日快照。

数据源（全部只读）：
  $CAMPUS_REPO/data/paperball/announcements.jsonl + meta.json   公告全量（contracts §4）
  $CAMPUS_REPO/data/tiers.yaml                                  公司 → 层级
  $CAMPUS_REPO/data/jd/index.json                               JD 抓取状态（§7）
  $CAMPUS_REPO/data/employer-aliases.yaml                       雇主主体别名（§8.4）
  $CAMPUS_INSTANCE/intent.yaml                                  届数/起始日期/城市偏好（可选，§2）
  $CAMPUS_INSTANCE/tracker.md                                   台账（经 dashboard.load_tracker）
  $CAMPUS_INSTANCE/log/battle-map.md                            score / 看板状态（§5）
  $CAMPUS_INSTANCE/state/fit/*.json                             分档（§7）
  $CAMPUS_INSTANCE/state/jobs/*.json                            作业状态（§8.2）
  $CAMPUS_INSTANCE/state/registry.jsonl                         已投登记（§8.4）
  $CAMPUS_INSTANCE/state/board/<date>.json                      每日快照（host/board-snapshot.py 产出）

容器里只有 /instance 没有仓库：/board 无 date 时优先实时构建（宿主机直跑），
否则读最新快照；?date=YYYY-MM-DD 读指定日快照；/board/diff 对比相邻两天快照。
"""
import csv
import datetime as dt
import html
import io
import json
import os
import re
import tempfile
from urllib.parse import urlsplit

import yaml

import agenda
import dashboard
import handoffs
import pages

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.environ.get("CAMPUS_REPO") or os.path.dirname(os.path.dirname(HERE))
SNAP_DIR = os.path.join("state", "board")
TODAY = lambda: dt.datetime.now().date()  # noqa: E731 容器 TZ=Asia/Shanghai

TIER_ORDER = ["大厂", "名企", "国企", "外企", "中小"]
FIT_ORDER = ["冲", "常规", "未判", "练手", "不合适"]
FIT_RANK = {"冲": 0, "常规": 1, "未判": 2, "练手": 3, "不合适": 4}
STATUS_ORDER = ["未处理", "排队", "填表中", "等我扫码", "等我确认", "已投", "已笔试", "面试", "不合适", "跳过"]
# 状态展示优先级：需要用户/在跑的排前；同一来源内取最高
STATUS_RANK = {"等我扫码": 95, "等我确认": 90, "填表中": 85, "排队": 80,
               "面试": 60, "已笔试": 55, "已投": 50, "跳过": 30, "不合适": 25, "未处理": 0}
SRC_RANK = {"job": 4, "events": 4, "registry": 3, "tracker": 2, "board": 2, "none": 1}
DONE_LABELS = {"已投", "已笔试", "面试", "跳过", "不合适"}   # 今日截止统计里视为已闭环

JD_LABEL = {"ok": "ok", "ok_ocr": "ocr", "image_only": "image_only",
            "login_required": "需登录", "gone": "gone", "missing": "缺"}
JD_RANK = {"ok": 6, "ok_ocr": 5, "image_only": 4, "login_required": 3, "gone": 2, "missing": 1}

# 投递入口域名 → 平台名（from_url 是申请入口，link 是公众号原文不算入口）
PORTAL_DOMAINS = [
    ("moka", "Moka"), ("zhiye", "北森"), ("beisen", "北森"), ("italent", "北森"),
    ("feishu", "飞书招聘"), ("hotjob", "hotjob"), ("wejob", "wejob"),
    ("nowcoder", "牛客"), ("zhaopin", "智联"), ("liepin", "猎聘"),
    ("51job", "前程无忧"), ("lagou", "拉勾"), ("zhipin", "BOSS直聘"),
    ("shixiseng", "实习僧"), ("job.ciomp", "自有门户"), ("arp.ciomp", "自有门户"),
    ("dayee", "大易"), ("workday", "Workday"), ("greenhouse", "Greenhouse"),
    ("lever.co", "Lever"), ("gllue", "谷露"), ("moseeker", "Moseeker"),
    ("wjx", "问卷星"), ("jinshuju", "金数据"),
]
EMAIL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
DIGITS_RE = re.compile(r"\d{4,}")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PAREN_RE = re.compile(r"[（(][^)）]*[)）]")
WS_RE = re.compile(r"\s+")

# 与 host/registry.py §归一化保持一致（容器里拿不到 host/，此处自带一份）
STRIP_SUFFIXES = ["股份有限公司", "有限责任公司", "集团股份有限公司", "科技有限公司", "集团有限公司",
                  "有限公司", "股份公司", "集团", "股份", "校招", "校园招聘", "公司"]


def e(v):
    return html.escape("" if v is None else str(v), quote=True)


def norm_company(s):
    return WS_RE.sub("", (s or "").strip().replace("（", "(").replace("）", ")"))


def strip_suffixes(name):
    name = PAREN_RE.sub("", name)
    changed = True
    while changed and name:
        changed = False
        for suf in STRIP_SUFFIXES:
            if name.endswith(suf) and len(name) > len(suf):
                name = name[: -len(suf)]
                changed = True
        name = PAREN_RE.sub("", name)
    return name


def load_aliases(repo):
    path = os.path.join(repo, "data", "employer-aliases.yaml")
    if not os.path.isfile(path):
        return {}
    try:
        data = yaml.safe_load(open(path, encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}
    return {strip_suffixes(norm_text(str(k))).lower(): strip_suffixes(norm_text(str(v))).lower()
            for k, v in (data.get("aliases") or {}).items()}


def norm_text(s):
    return norm_company(s)


def employer_key(name, aliases):
    base = strip_suffixes(norm_text(name)).lower()
    return aliases.get(base, base)


def as_date(v):
    if v in (None, ""):
        return None
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def extract_emails(rec):
    found = []
    for field in ("from_url", "original_jobs"):
        v = rec.get(field) or ""
        if field == "from_url" and v.lower().startswith("mailto:"):
            v = v[7:]
        for m in EMAIL_RE.findall(v):
            m = m.rstrip(".")
            if m.lower() not in (x.lower() for x in found):
                found.append(m)
    return found


def entry_type(rec):
    """投递入口：(类型标签, 入口链接, 邮箱列表)。邮箱 > 已知平台域名 > 官网/表单。"""
    emails = extract_emails(rec)
    if emails:
        return "邮箱", "", emails
    url = (rec.get("from_url") or "").strip()
    try:
        host = urlsplit(url if "//" in url else "//" + url).netloc.lower()
    except ValueError:
        host = ""
    if host:
        for key, name in PORTAL_DOMAINS:
            if key in host:
                return name, url, []
        return "官网/表单", url, []
    if url:
        return "官网/表单", "", []        # 文本型入口（"邮箱投递：…"但抽不出邮箱等）
    return "—", "", []


def jd_label(status):
    return JD_LABEL.get(status or "", status or "缺")


# --------------------------------------------------------------------------
# 数据源加载
# --------------------------------------------------------------------------

def _drain_warnings():
    """dashboard 的 WARNINGS 是全局的，取增量返回，避免重复/串页。"""
    out = list(dashboard.WARNINGS)
    del dashboard.WARNINGS[:]
    return out


def load_intent(instance):
    path = os.path.join(instance, "intent.yaml") if instance else ""
    if not path or not os.path.isfile(path):
        return {}
    try:
        return yaml.safe_load(open(path, encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}


def load_announcements(repo, class_types, since):
    """全部公告（含已截止——榜单要展示'全量'，截止与否由行内状态表达）。"""
    path = os.path.join(repo, "data", "paperball", "announcements.jsonl")
    out = []
    for line in open(path, encoding="utf-8"):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if class_types and not (set(rec.get("class_types") or []) & class_types):
            continue
        pub = as_date(rec.get("published_at"))
        if since and (not pub or pub < since):
            continue
        out.append(rec)
    return out


def load_meta(repo):
    path = os.path.join(repo, "data", "paperball", "meta.json")
    try:
        return json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def load_tiers(repo):
    data = yaml.safe_load(open(os.path.join(repo, "data", "tiers.yaml"), encoding="utf-8")) or {}
    mapping = {}
    for tier, names in (data.get("tiers") or {}).items():
        for name in names or []:
            mapping[norm_company(str(name))] = tier
    return mapping, data.get("default") or "中小"


def load_jd_index(repo):
    path = os.path.join(repo, "data", "jd", "index.json")
    try:
        raw = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): (v or {}).get("status") or "" for k, v in raw.items() if isinstance(v, dict)}


def load_fit(instance):
    """state/fit/*.json → {announcement_id: fit}（judge.py 产出，可能不存在）。"""
    out = {}
    d = os.path.join(instance, "state", "fit")
    if not os.path.isdir(d):
        return out
    for fn in os.listdir(d):
        if not fn.endswith(".json") or fn.startswith("_") or fn.startswith("."):
            continue
        try:
            rec = json.load(open(os.path.join(d, fn), encoding="utf-8"))
        except (OSError, ValueError):
            continue
        aid = str(rec.get("announcement_id") or fn[:-5])
        if rec.get("tier"):
            out[aid] = rec
    return out


def load_jobs(instance):
    """state/jobs/<id>.json → {announcement_id: job}（dispatcher 产出，可能不存在）。"""
    out = {}
    d = os.path.join(instance, "state", "jobs")
    if not os.path.isdir(d):
        return out
    for fn in os.listdir(d):
        if not fn.endswith(".json") or fn.startswith("."):
            continue
        try:
            rec = json.load(open(os.path.join(d, fn), encoding="utf-8"))
        except (OSError, ValueError):
            continue
        aid = str(rec.get("announcement_id") or fn[:-5])
        out[aid] = rec
    return out


def load_registry(instance):
    path = os.path.join(instance, "state", "registry.jsonl")
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
        if isinstance(rec, dict) and rec.get("employer_key"):
            out.append(rec)
    return out


def load_battle_map(root, year):
    """复用 dashboard 的解析：score 按 id 取 + 任何含 id/看板状态 的表（含文末'已投'段）。"""
    path = os.path.join(root, "log", "battle-map.md")
    tables = dashboard.parse_md_tables(path)
    score, bstatus, notes = {}, {}, {}
    if not tables:
        return score, bstatus, notes
    for headers, rows_dict, _, _ in tables:
        if "id" not in headers:
            continue
        for r in rows_dict:
            aid = dashboard.clean(r.get("id", ""))
            if not aid:
                continue
            if "score" in headers:
                try:
                    score[aid] = max(score.get(aid, 0.0), float(dashboard.clean(r.get("score", "")) or 0))
                except ValueError:
                    pass
            if "看板状态" in headers:
                v = dashboard.clean(r.get("看板状态", ""))
                if v and not dashboard.blank(v):
                    bstatus[aid] = v
            if "备注" in headers:
                v = dashboard.clean(r.get("备注", ""))
                if v:
                    notes[aid] = v
    return score, bstatus, notes


# --------------------------------------------------------------------------
def load_event_status(instance):
    """state/events.jsonl + outcomes/ + handoffs/ → {announcement_id: 展示标签}（契约 §9）。

    dispatcher 未运行、由主持人手工派单时 state/jobs 为空，看板仍要看到"在填表/等我扫码/已投/跳过"。
    """
    out = {}
    ev_path = os.path.join(instance, "state", "events.jsonl")
    latest = {}
    if os.path.isfile(ev_path):
        for line in open(ev_path, encoding="utf-8"):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            aid = str(e.get("job") or "")
            if aid:
                latest[aid] = e
    open_gate = set()
    hd = os.path.join(instance, "state", "handoffs")
    if os.path.isdir(hd):
        for fn in os.listdir(hd):
            if not fn.endswith(".json"):
                continue
            try:
                h = json.load(open(os.path.join(hd, fn), encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not h.get("resolved_at") and h.get("status") not in ("resolved", "cancelled", "expired"):
                open_gate.add(str(h.get("announcement_id") or ""))
    step_label = {"claim": "填表中", "portal_detected": "填表中", "account_ready": "填表中",
                  "filling": "填表中", "field_blocked": "等我确认", "gate": "等我扫码",
                  "submitted": "已投", "verified": "已投", "failed": "等我确认"}
    for aid, e in latest.items():
        lab = step_label.get(e.get("step") or "")
        if lab == "等我扫码" and aid not in open_gate:
            lab = "填表中"          # 关卡已处理完，worker 在继续
        if lab:
            out[aid] = lab
    od = os.path.join(instance, "state", "outcomes")
    if os.path.isdir(od):
        for fn in os.listdir(od):
            if not fn.endswith(".json"):
                continue
            try:
                o = json.load(open(os.path.join(od, fn), encoding="utf-8"))
            except (OSError, ValueError):
                continue
            aid = str(o.get("announcement_id") or fn[:-5])
            res = o.get("result") or ""
            out[aid] = {"submitted": "已投", "verified": "已投", "skipped": "跳过",
                        "blocked": "等我确认", "failed": "等我确认"}.get(res, out.get(aid))
    for aid in open_gate:
        if out.get(aid) != "已投":
            out[aid] = "等我扫码"
    return out


# 我的状态：state/jobs > events/outcomes > registry > tracker/battle-map > 未处理
# --------------------------------------------------------------------------

JOB_LABEL = {"queued": "排队", "dispatched": "填表中", "filling": "填表中",
             "submitting": "填表中", "review_wait": "等我确认",
             "submitted": "已投", "verified": "已投", "skipped": "跳过"}


def job_label(job):
    st = job.get("status") or ""
    if st == "gate_wait":
        kind = ((job.get("gate") or {}).get("kind") or "")
        return "等我确认" if kind == "submit_confirm" else "等我扫码"
    if st in ("blocked", "failed", "held"):
        return "等我确认"
    return JOB_LABEL.get(st, "排队")


def tracker_label(row):
    """tracker 行 → 展示标签。已投递的再看下一步里有没有笔试/面试。"""
    s = dashboard.clean(row.get("status", ""))
    if "不合适" in s:
        return "不合适"
    if "已停止" in s or "暂缓" in s:
        return "跳过"
    if "已投递" in s:
        nxt = row.get("next") or ""
        if "面试" in nxt or re.search(r"[一二三]面", nxt):
            return "面试"
        if "笔试" in nxt or "测评" in nxt:
            return "已笔试"
        return "已投"
    if "填表" in s or "待提交" in s:
        return "填表中"
    if "待投递" in s or "待核验" in s:
        return "等我确认"
    if "候选" in s or "查岗" in s:
        return "未处理"
    return "未处理" if not s else "填表中"


BOARD_STAGE = {"已投递": "已投", "待笔试": "已笔试", "已笔试": "已笔试", "测评": "已笔试",
               "一面": "面试", "二面": "面试", "三面": "面试", "面试": "面试",
               "Offer": "面试", "offer": "面试",
               "不合适": "不合适", "已停止": "跳过", "待投递": "排队"}


def board_label(text):
    t = dashboard.clean(text)
    for key, label in BOARD_STAGE.items():
        if key in t:
            return label
    return None


def pick_status(entries):
    """entries: [(source, label, raw)] → 按 来源优先级 > 状态rank 取一条；已投可被台账里的笔试/面试升级。"""
    best = None
    for src, label, raw in entries:
        if not label:
            continue
        key = (SRC_RANK.get(src, 0), STATUS_RANK.get(label, 0))
        if best is None or key > best[0]:
            best = (key, src, label, raw)
    if best is None:
        return {"label": "未处理", "raw": "", "src": "none"}
    _, src, label, raw = best
    if label == "已投":
        for s2, l2, r2 in entries:
            if s2 in ("tracker", "board") and l2 in ("已笔试", "面试"):
                return {"label": l2, "raw": r2 or raw, "src": src}
    return {"label": label, "raw": raw, "src": src}


# --------------------------------------------------------------------------
# 构建
# --------------------------------------------------------------------------

def _best_fit(fits):
    """同雇主多条公告的分档：冲 > 常规 > 未判 > 练手 > 不合适。"""
    tiers = [f.get("tier") for f in fits if f.get("tier")]
    if not tiers:
        return "未判"
    return min(tiers, key=lambda t: FIT_RANK.get(t, 2))


def _jd_of_group(aids, jd_index):
    best, brank = "", -1
    for aid in aids:
        st = jd_index.get(aid) or "missing"
        r = JD_RANK.get(st, 0)
        if r > brank:
            best, brank = st, r
    return best or "missing"


def build(repo, instance, today=None):
    """返回 (data, stats)。data 即 /api/board 的响应体，也是快照文件 payload。"""
    today = today or TODAY()
    if isinstance(today, str):
        today = dt.date.fromisoformat(today)
    year = today.year
    warn = []

    intent = load_intent(instance)
    class_types = set(intent.get("class_types") or [8])
    since = as_date(intent.get("published_since")) or dt.date(2026, 7, 1)
    cities_cfg = intent.get("cities") or {}
    avoid = {str(c) for c in cities_cfg.get("avoid") or []}

    meta = load_meta(repo)
    anns = load_announcements(repo, class_types, since)
    tiers_map, default_tier = load_tiers(repo)
    aliases = load_aliases(repo)
    jd_index = load_jd_index(repo)

    tracker_rows, tracker_rel, tracker_ok = dashboard.load_tracker(instance, year)
    warn += _drain_warnings()
    score_map, board_status, bm_notes = load_battle_map(instance, year)
    warn += _drain_warnings()
    bm_ok = os.path.isfile(os.path.join(instance, "log", "battle-map.md"))
    fit_map = load_fit(instance)
    jobs = load_jobs(instance)
    registry = load_registry(instance)
    ev_status = load_event_status(instance)
    reg_by_key = {}
    for r in registry:
        reg_by_key.setdefault(r.get("employer_key"), []).append(r)

    # tracker 行 → 按 id / 雇主主体归组
    tracker_by_id, tracker_by_key = {}, {}
    for row in tracker_rows:
        for aid in DIGITS_RE.findall(row.get("id") or ""):
            tracker_by_id.setdefault(aid, []).append(row)
        k = employer_key(row.get("name") or "", aliases)
        if k:
            tracker_by_key.setdefault(k, []).append(row)

    # 公告 → 雇主分组
    groups = {}
    for rec in anns:
        aid = str(rec.get("announcement_id"))
        company = rec.get("company") or "?"
        key = employer_key(company, aliases) or norm_company(company) or aid
        g = groups.setdefault(key, {"key": key, "names": [], "anns": []})
        g["anns"].append(rec)
        if company not in g["names"]:
            g["names"].append(company)

    rows = []
    for key, g in groups.items():
        ganns = g["anns"]
        aids = [str(r.get("announcement_id")) for r in ganns]

        # 主公告：tracker/jobs 命中的优先，其次最新发布
        def _primary_key(r):
            aid = str(r.get("announcement_id"))
            hit = 1 if (aid in tracker_by_id or aid in jobs or aid in score_map) else 0
            return (hit, as_date(r.get("published_at")) or dt.date.min, int(r.get("announcement_id") or 0))
        primary = max(ganns, key=_primary_key)

        # 层级：显式名单 → company_tags 国企信号 → 默认；组内取排名最高的
        def _tier(r):
            t = tiers_map.get(norm_company(r.get("company") or ""))
            if t is None:
                t = "国企" if {"国企", "事业单位"} & set(r.get("company_tags") or []) else default_tier
            return t
        tier = min((_tier(r) for r in ganns), key=lambda t: TIER_ORDER.index(t) if t in TIER_ORDER else len(TIER_ORDER))

        fits = [fit_map[a] for a in aids if a in fit_map]
        fit_tier = _best_fit(fits)
        if fit_tier == "未判" and avoid:
            # 与 scan.py 同口径：公告城市全落 avoid → 练手信号
            if all((r.get("cities") or []) and set(r.get("cities")) <= avoid for r in ganns):
                fit_tier = "练手"
        roles, reasons, risks = [], [], []
        for f in fits:
            for x in f.get("roles") or []:
                if x and x not in roles:
                    roles.append(x)
            for x in f.get("reasons") or []:
                if x and x not in reasons:
                    reasons.append(x)
            for x in f.get("risks") or []:
                if x and x not in risks:
                    risks.append(x)
        fit_score = max((f.get("fit") or 0 for f in fits), default=0)
        if not roles:
            jobs_text = primary.get("original_jobs") or ""
            # original_jobs 常见格式「岗位1,岗位2\\专业列表」，只取岗位段
            jobs_text = jobs_text.split("\\\\")[0]
            roles = [p.strip() for p in re.split(r"[,，、;；/|｜]+", jobs_text) if p.strip()][:3]

        cities = []
        for r in ganns:
            for c in r.get("cities") or []:
                if c and c not in cities:
                    cities.append(c)

        # 截止：与 dashboard 同口径——未投递行 tracker「关键日期」的日期优先（门户截止比公告 expired_at 准），
        # 否则取组内最近的公告 expired_at
        trs0 = list(tracker_by_key.get(key, []))
        for aid in aids:
            trs0 += tracker_by_id.get(aid, [])
        tr_dls = [tr["deadline"] for tr in trs0 if tr.get("deadline")]
        if tr_dls:
            deadline = min(tr_dls)
        else:
            deadlines = [d for d in (as_date(r.get("expired_at")) for r in ganns) if d]
            deadline = min(deadlines).isoformat() if deadlines else ""
        pubs = [as_date(r.get("published_at")) for r in ganns]
        pubs = [d for d in pubs if d]
        published = max(pubs).isoformat() if pubs else ""
        is_new = any(d == today for d in pubs)

        entry, entry_url, mails = entry_type(primary)
        # 组里任何一条是邮件类都算邮箱入口可见
        if entry != "邮箱":
            all_mails = []
            for r in ganns:
                all_mails += extract_emails(r)
            if all_mails:
                mails = list(dict.fromkeys(all_mails))

        # 我的状态
        entries = []
        for aid in aids:
            job = jobs.get(aid)
            if job:
                entries.append(("job", job_label(job), job.get("status") or ""))
            elif aid in ev_status:
                entries.append(("events", ev_status[aid], "events"))
        for r in reg_by_key.get(key, []):
            st = r.get("status") or ""
            entries.append(("registry", "已投" if st in ("submitted", "verified") else "跳过",
                            st))
        trs = list(tracker_by_key.get(key, []))
        for aid in aids:
            trs += tracker_by_id.get(aid, [])
        seen_tr = set()
        for tr in trs:
            if id(tr) in seen_tr:
                continue
            seen_tr.add(id(tr))
            entries.append(("tracker", tracker_label(tr), tr.get("status") or ""))
        for aid in aids:
            if aid in board_status:
                entries.append(("board", board_label(board_status[aid]), board_status[aid]))
        status = pick_status(entries)

        score = max((score_map.get(a) or 0.0 for a in aids), default=0.0)

        sub_anns = []
        for r in sorted(ganns, key=lambda x: str(x.get("announcement_id"))):
            aid = str(r.get("announcement_id"))
            f = fit_map.get(aid)
            et, eu, em = entry_type(r)
            st = ""
            if aid in jobs:
                st = job_label(jobs[aid])
            elif aid in tracker_by_id:
                st = tracker_label(tracker_by_id[aid][0])
            elif aid in board_status:
                st = board_label(board_status[aid]) or ""
            sub_anns.append({
                "id": aid, "title": r.get("title") or "",
                "pub": r.get("published_at") or "", "exp": r.get("expired_at") or "",
                "link": r.get("link") or "", "apply": eu,
                "entry": et, "mails": em,
                "cities": r.get("cities") or [],
                "jd": jd_label(jd_index.get(aid)),
                "fit": (f or {}).get("tier") or "",
                "fitRoles": (f or {}).get("roles") or [],
                "st": st,
            })

        names = g["names"]
        rows.append({
            "key": key,
            "company": dashboard.clean(primary.get("company") or names[0]),
            "subs": [n for n in names if n != dashboard.clean(primary.get("company") or names[0])],
            "tier": tier,
            "fit": fit_tier,
            "fitScore": fit_score,
            "roles": roles[:6],
            "reasons": reasons[:3],
            "risks": risks[:3],
            "cities": cities,
            "deadline": deadline,
            "published": published,
            "isNew": bool(is_new),
            "entry": entry,
            "apply": entry_url or (primary.get("from_url") or ""),
            "link": primary.get("link") or "",
            "mails": mails[:3],
            "jd": jd_label(_jd_of_group(aids, jd_index)),
            "status": status,
            "score": round(score, 1),
            "ids": aids,
            "anns": sub_anns,
        })

    # 我的日程：每家公司最近一条未闭环日程（优先按公告 id 命中，其次雇主主体名）
    ag_data = agenda.build(instance)
    warn += ag_data.get("warnings") or []
    ag_ids, ag_keys = {}, {}
    for it in ag_data["items"]:
        if it["status"] in ("已完成", "已过期"):
            continue
        for a in it.get("aids") or []:
            ag_ids.setdefault(str(a), it)
        k = employer_key(it["company"], aliases)
        if k:
            ag_keys.setdefault(k, it)
    for r in rows:
        it = None
        for aid in r["ids"]:
            if aid in ag_ids:
                it = ag_ids[aid]
                break
        it = it or ag_keys.get(r["key"])
        r["agenda"] = ({"id": it["id"], "kind": it["kind"], "when": it["when"],
                       "days": it["days"], "date_only": it["date_only"],
                       "status": it["status"], "link": it["link"]} if it else None)

    # 默认排序：未处理优先 → 档 → 截止近（未来升序，已过期的排在未来之后、无截止之前）→ 分数 → 发布新
    today_iso = today.isoformat()

    def sort_key(r):
        untreated = 0 if r["status"]["label"] == "未处理" else 1
        fr = FIT_RANK.get(r["fit"], 2)
        dl = r["deadline"]
        dl_key = (2, "") if not dl else ((1, dl) if dl < today_iso else (0, dl))
        return (untreated, fr, dl_key, -r["score"],
                _neg_date(r["published"]) if r["published"] else float("inf"), r["key"])
    rows.sort(key=sort_key)
    for i, r in enumerate(rows):
        r["rank"] = i + 1

    labels = {}
    for r in rows:
        labels[r["status"]["label"]] = labels.get(r["status"]["label"], 0) + 1
    stats = {
        "total": len(rows),
        "announcements": len(anns),
        "todayNew": sum(1 for r in rows if r["isNew"]),
        "applied": sum(1 for r in rows if r["status"]["label"] in ("已投", "已笔试", "面试")),
        "running": sum(1 for r in rows if r["status"]["label"] in ("排队", "填表中")),
        "waiting": sum(1 for r in rows if r["status"]["label"] in ("等我扫码", "等我确认")),
        "dueToday": sum(1 for r in rows if r["deadline"] == today.isoformat()
                        and r["status"]["label"] not in DONE_LABELS),
        "labels": labels,
        "trackerRows": len(tracker_rows),
        "trackerUnmatched": sum(1 for tr in tracker_rows
                               if not DIGITS_RE.findall(tr.get("id") or "")
                               and employer_key(tr.get("name") or "", aliases) not in groups),
        "jobs": len(jobs), "registry": len(registry), "fit": len(fit_map),
    }
    entries_present = sorted({r["entry"] for r in rows})
    city_freq = {}
    for r in rows:
        for c in r["cities"]:
            city_freq[c] = city_freq.get(c, 0) + 1

    data = {
        "asOf": today.isoformat(),
        "generatedAt": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "syncedAt": meta.get("synced_at") or "",
        "annCount": meta.get("count") or len(anns),
        "since": since.isoformat(),
        "rows": rows,
        "stats": stats,
        "filterVals": {
            "statuses": [s for s in STATUS_ORDER if s in labels] ,
            "fits": FIT_ORDER,
            "tiers": [t for t in TIER_ORDER if any(r["tier"] == t for r in rows)],
            "entries": entries_present,
            "cities": [c for c, _ in sorted(city_freq.items(), key=lambda x: -x[1])[:40]],
        },
        "sources": [
            {"path": "data/paperball/announcements.jsonl", "ok": True},
            {"path": "data/tiers.yaml", "ok": True},
            {"path": "data/jd/index.json", "ok": bool(jd_index)},
            {"path": tracker_rel, "ok": tracker_ok},
            {"path": "log/battle-map.md", "ok": bm_ok},
            {"path": "state/fit/", "ok": bool(fit_map)},
            {"path": "state/jobs/", "ok": bool(jobs)},
            {"path": "state/registry.jsonl", "ok": bool(registry)},
            {"path": "intent.yaml", "ok": bool(intent)},
            {"path": "assessments.md", "ok": os.path.isfile(os.path.join(instance, "assessments.md"))},
            {"path": "state/next-steps.jsonl", "ok": os.path.isfile(os.path.join(instance, "state", "next-steps.jsonl"))},
        ],
        "warnings": warn,
    }
    return data, stats


def _neg_date(iso):
    """日期字符串转可升序比较的负键（新的排前）。"""
    try:
        return -dt.date.fromisoformat(iso).toordinal()
    except ValueError:
        return 0


# --------------------------------------------------------------------------
# 快照
# --------------------------------------------------------------------------

def snapshot_dir(instance):
    return os.path.join(instance, SNAP_DIR)


def snapshot_path(instance, date):
    return os.path.join(snapshot_dir(instance), "%s.json" % date)


def write_snapshot(instance, date, data):
    d = snapshot_dir(instance)
    os.makedirs(d, exist_ok=True)
    path = snapshot_path(instance, date)
    fd, tmp = tempfile.mkstemp(prefix=".snap-", suffix=".tmp", dir=d)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"date": date, "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                   "payload": data}, fh, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path


def list_snapshots(instance):
    d = snapshot_dir(instance)
    out = []
    if os.path.isdir(d):
        for fn in os.listdir(d):
            m = re.match(r"^(\d{4}-\d{2}-\d{2})\.json$", fn)
            if m:
                out.append(m.group(1))
    return sorted(out)


def load_snapshot(instance, date):
    """返回 payload（注入 _snapshot 标记），没有则 None。"""
    if not DATE_RE.match(date or ""):
        return None
    try:
        raw = json.load(open(snapshot_path(instance, date), encoding="utf-8"))
    except (OSError, ValueError):
        return None
    payload = raw.get("payload") if isinstance(raw, dict) else None
    if not isinstance(payload, dict) or "rows" not in payload:
        return None
    payload["_snapshot"] = {"date": raw.get("date") or date,
                            "generated_at": raw.get("generated_at") or "",
                            "historical": True}
    payload["dates"] = list_snapshots(instance)
    return payload


def repo_available(repo):
    return os.path.isfile(os.path.join(repo, "data", "paperball", "announcements.jsonl"))


def current(instance, repo=None):
    """无 date 的 /board：仓库可达 → 实时构建；否则 → 最新快照。返回 (payload, info)。"""
    repo = repo or REPO
    dates = list_snapshots(instance)
    if repo_available(repo):
        data, _ = build(repo, instance)
        data["_snapshot"] = {"date": "", "generated_at": data["generatedAt"],
                             "historical": False, "live": True}
        data["dates"] = dates
        return data, {"mode": "live", "dates": dates}
    if dates:
        payload = load_snapshot(instance, dates[-1])
        if payload is not None:
            return payload, {"mode": "snapshot", "date": dates[-1], "dates": dates}
    return None, {"mode": "none", "dates": dates}


def source_paths(instance, repo=None):
    """参与 /board 实时构建的文件（缓存按 mtime 失效）。"""
    repo = repo or REPO
    paths = [
        os.path.join(repo, "data", "paperball", "announcements.jsonl"),
        os.path.join(repo, "data", "paperball", "meta.json"),
        os.path.join(repo, "data", "tiers.yaml"),
        os.path.join(repo, "data", "employer-aliases.yaml"),
        os.path.join(repo, "data", "jd", "index.json"),
        os.path.join(instance, "tracker.md"),
        os.path.join(instance, "intent.yaml"),
        os.path.join(instance, "log", "battle-map.md"),
        os.path.join(instance, "state", "registry.jsonl"),
        os.path.join(instance, "state", "fit"),
        os.path.join(instance, "state", "jobs"),
        os.path.join(instance, "state", "events.jsonl"),
        os.path.join(instance, "state", "outcomes"),
        os.path.join(instance, "state", "handoffs"),
        snapshot_dir(instance),
    ]
    paths += agenda.source_paths(instance)
    for sub in ("fit", "jobs", "outcomes", "handoffs"):
        d = os.path.join(instance, "state", sub)
        if os.path.isdir(d):
            paths += [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".json")]
    d = snapshot_dir(instance)
    if os.path.isdir(d):
        paths += [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".json")]
    return paths


# --------------------------------------------------------------------------
# diff
# --------------------------------------------------------------------------

def diff_payload(cur, base):
    """两天快照 payload → {added, removed, changed}。"""
    cur_rows = {r["key"]: r for r in (cur or {}).get("rows", [])}
    base_rows = {r["key"]: r for r in (base or {}).get("rows", [])}
    added = [r for k, r in cur_rows.items() if k not in base_rows]
    removed = [r for k, r in base_rows.items() if k not in cur_rows]
    changed = []
    for k, r in cur_rows.items():
        old = base_rows.get(k)
        if not old:
            continue
        diffs = []
        if r["status"]["label"] != old["status"]["label"]:
            diffs.append("状态 %s → %s" % (old["status"]["label"], r["status"]["label"]))
        if r["fit"] != old["fit"]:
            diffs.append("档 %s → %s" % (old["fit"], r["fit"]))
        if r["deadline"] != old["deadline"]:
            diffs.append("截止 %s → %s" % (old["deadline"] or "—", r["deadline"] or "—"))
        if diffs:
            changed.append({"key": k, "company": r["company"], "what": "；".join(diffs),
                            "label": r["status"]["label"]})
    for lst in (added, removed):
        lst.sort(key=lambda r: r.get("published") or "", reverse=True)
    return {"added": added, "removed": removed, "changed": changed,
            "cur": (cur or {}).get("_snapshot", {}).get("date") or (cur or {}).get("asOf") or "",
            "base": (base or {}).get("_snapshot", {}).get("date") or (base or {}).get("asOf") or ""}


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------

CSV_FIELDS = ["排名", "公司", "层级", "档", "建议岗位", "城市", "截止", "发布", "投递入口", "JD",
              "我的状态", "状态来源", "公告id", "投递链接", "公告链接"]


def to_csv(data):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_FIELDS)
    for r in (data or {}).get("rows", []):
        w.writerow([
            r["rank"], r["company"], r["tier"], r["fit"], "、".join(r["roles"]),
            "、".join(r["cities"]), r["deadline"], r["published"], r["entry"], r["jd"],
            r["status"]["label"], r["status"].get("src") or "",
            " ".join(r["ids"]), r["apply"], r["link"],
        ])
    return "\ufeff" + buf.getvalue()  # BOM：让 Excel/表格软件按 UTF-8 识别


# --------------------------------------------------------------------------
# 页面渲染
# --------------------------------------------------------------------------

NAV = [("/", "看板"), ("/board", "榜单"), ("/agenda", "日程"),
       ("/bank", "问题库"), ("/setup", "前置条件"), ("/handoffs", "待我处理")]

TEMPLATE = os.path.join(HERE, "templates", "board.html")

BOARD_CSS = """
  .wrap { max-width: 1180px; }
  .diff-sec h2 { margin-top: 26px; }
  .diff-list { border-top: var(--lw-grid) solid var(--grid); }
  .diff-row { display: grid; grid-template-columns: 1fr auto; gap: 4px 14px; padding: 8px 0;
              border-bottom: var(--lw-grid) solid var(--grid); align-items: baseline; }
  .diff-row .w { font-family: var(--font-mono); font-size: 12px; color: var(--ink-soft); }
  .diff-row .c { color: var(--ink-faint); font-size: 12.5px; }
  .dnew { font-family: var(--font-mono); font-size: 11.5px; color: var(--blue); }
"""


def _nav(active, instance):
    cnt = handoffs.open_count(instance)
    return "".join('<a href="%s"%s>%s%s</a>' % (
        href, ' class="on"' if href == active else "", label,
        '<span class="cnt">%d</span>' % cnt if href == "/handoffs" and cnt else "")
        for href, label in NAV)


def shell(title, active, body, instance, extra_css="", script=""):
    """与 pages.shell 同构，但导航带「榜单」（pages.NAV 不在本任务范围内，不改动它）。"""
    nav = _nav(active, instance)
    return ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">"
            "<title>%s</title><style>%s%s</style></head><body><div class=\"wrap\">"
            "<nav class=\"nav\">%s</nav>%s</div>%s</body></html>") % (
        e(title), pages.BASE_CSS, extra_css, nav, body, script)


def page(payload, instance):
    tpl = open(TEMPLATE, encoding="utf-8").read()
    nav = _nav("/board", instance)
    out = tpl.replace("/*__NAV__*/", nav)
    return out.replace("/*__DATA__*/",
                       json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/"))


def missing_page(instance, info, date=""):
    dates = info.get("dates") or []
    hint = ""
    if date:
        hint = "没有 <span class=\"num\">%s</span> 的快照。" % e(date)
    elif info.get("mode") == "none":
        hint = ("容器内读不到仓库数据，也还没有榜单快照。宿主机先跑一次 "
                "<code>uv run --script host/board-snapshot.py --instance $CAMPUS_INSTANCE</code>。")
    links = " · ".join('<a href="/board?date=%s">%s</a>' % (d, d) for d in reversed(dates[-14:]))
    body = ('<h1>每日榜单</h1><p class="sub">%s</p>'
            '<div class="missing">%s</div>%s'
            '<p class="sub" style="margin-top:18px"><a href="/board">← 最新榜单</a> · <a href="/board/diff">对比</a></p>'
            % (e("全量公告榜单与我的投递状态同步"), hint,
               ('<h2>已有快照 <span class="hint"><span class="num">%d</span> 天</span></h2><p class="sub">%s</p>'
                % (len(dates), links)) if dates else ""))
    return shell("每日榜单", "/board", body, instance)


def diff_page(instance, cur, base, info):
    if cur is None or base is None:
        dates = info.get("dates") or []
        links = " · ".join('<a href="/board/diff?date=%s">%s</a>' % (d, d) for d in reversed(dates[-14:]))
        need = "至少需要两天快照才能对比。宿主机每天跑 <code>uv run --script host/board-snapshot.py</code> 即可积累。"
        body = ('<h1>榜单对比</h1><p class="sub">与前一天相比的新增 / 下线 / 状态变化</p>'
                '<div class="missing">%s</div>%s'
                % (need, ('<p class="sub" style="margin-top:16px">已有快照：%s</p>' % links) if dates else ""))
        return shell("榜单对比", "/board", body, instance, BOARD_CSS)

    d = diff_payload(cur, base)
    head = ('<h1>榜单对比</h1><p class="sub"><span class="num">%s</span> → <span class="num">%s</span>'
            ' · 新增 <span class="num">%d</span> · 下线 <span class="num">%d</span> · 状态变化 <span class="num">%d</span>'
            ' · <a href="/board?date=%s">当天榜单</a></p>'
            % (e(d["base"] or "—"), e(d["cur"] or "—"), len(d["added"]), len(d["removed"]), len(d["changed"]),
               e(d["cur"])))

    def row(r, what=""):
        return ('<div class="diff-row"><div><b>%s</b>%s<span class="c">%s%s</span></div>'
                '<span class="w">%s</span></div>'
                % (e(r.get("company")), ' <span class="tag">%s</span>' % e(r.get("tier")) if r.get("tier") else "",
                   e(r.get("published") or ""), (" · " + e(what)) if what else "",
                   e(r["status"]["label"] if isinstance(r.get("status"), dict) else "")))

    parts = [head, '<div class="diff-sec">']
    parts.append('<h2>新增 <span class="hint"><span class="num">%d</span> 家</span></h2>' % len(d["added"]))
    parts.append('<div class="diff-list">%s</div>' % "".join(row(r) for r in d["added"])
                 if d["added"] else '<div class="missing">无新增</div>')
    parts.append('<h2>下线 <span class="hint">公告撤下或截止，<span class="num">%d</span> 家</span></h2>' % len(d["removed"]))
    parts.append('<div class="diff-list">%s</div>' % "".join(row(r) for r in d["removed"])
                 if d["removed"] else '<div class="missing">无下线</div>')
    parts.append('<h2>状态变化 <span class="hint"><span class="num">%d</span> 家</span></h2>' % len(d["changed"]))
    parts.append('<div class="diff-list">%s</div>'
                 % "".join('<div class="diff-row"><div><b>%s</b><span class="c"> · %s</span></div>'
                           '<span class="w">%s</span></div>' % (e(x["company"]), e(x["what"]), e(x["label"]))
                           for x in d["changed"])
                 if d["changed"] else '<div class="missing">无变化</div>')
    parts.append('</div><p class="sub" style="margin-top:18px"><a href="/board">← 最新榜单</a></p>')
    return shell("榜单对比", "/board", "".join(parts), instance, BOARD_CSS)
