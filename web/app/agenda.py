"""agenda.py · 「我的日程」：把散在三处的测评/笔试/面试/截止合并成统一日程条目。

数据源（只读；合并优先级 next-steps > assessments.md > tracker）：
  $CAMPUS_INSTANCE/state/next-steps.jsonl   host/mail.py 抽取的测评/笔试/面试/Offer 待办
  $CAMPUS_INSTANCE/assessments.md           人工登记的测评/笔试排期表
  $CAMPUS_INSTANCE/tracker.md               台账「关键日期/记录」列里的测评/笔试与门户截止

去重：同公司（主体归一）+ 同类型 + 时间相近（同一天或相差 ≤2 天，或任一侧无时间）并为一条，
sources 记录全部出处。web「标记完成」追加写 state/agenda-done.jsonl，不改任何源文件。

解析容错：assessments.md 表头缺列/行格式漂 → 跳过该行并进 warnings（页面提示），不整页失败。

供三方使用：dashboard.py（首页区块）、board.py（行内最近日程）、本模块 /agenda 页、
host/schedule.py --from-agenda（日历/提醒同步）。只用标准库，容器与宿主机同码运行。
"""

import datetime as dt
import hashlib
import html
import json
import os
import re
import tempfile
import time
from zoneinfo import ZoneInfo

CST = ZoneInfo("Asia/Shanghai")

KINDS = {"assessment": "测评", "written_test": "笔试", "interview": "面试",
         "material_due": "材料截止", "portal_due": "门户截止", "offer": "Offer"}
STATUS = ("待做", "已约", "已完成", "已过期")
SRC_LABEL = {"next-steps": "next-steps", "assessments": "assessments.md", "tracker": "tracker.md"}
SRC_RANK = {"next-steps": 0, "assessments": 1, "tracker": 2}
MERGE_MAX_GAP = dt.timedelta(days=2)

NEXT_STEPS = os.path.join("state", "next-steps.jsonl")
DONE_FILE = os.path.join("state", "agenda-done.jsonl")

# --------------------------------------------------------------------------
# markdown 表格解析（与 dashboard.py 同口径的精简拷贝；本模块需独立可 import）
# --------------------------------------------------------------------------

SPLIT_RE = re.compile(r"(?<!\\)\|")
SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def split_row(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip().replace("\\|", "|") for c in SPLIT_RE.split(s)]


def parse_md_tables(path):
    """返回 [(headers, rows_dict)]；文件不存在返回 None，读失败抛 OSError 由调用方收。"""
    if not os.path.exists(path):
        return None
    lines = open(path, encoding="utf-8").read().splitlines()
    tables = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if line.lstrip().startswith("|") and i + 1 < n and SEP_RE.match(lines[i + 1]):
            headers = split_row(line)
            i += 2
            rows = []
            while i < n and lines[i].lstrip().startswith("|"):
                cells = split_row(lines[i])
                if len(cells) > len(headers):
                    cells = cells[: len(headers) - 1] + [" ".join(cells[len(headers) - 1:])]
                elif len(cells) < len(headers):
                    cells = cells + [""] * (len(headers) - len(cells))
                rows.append(dict(zip(headers, cells)))
                i += 1
            tables.append((headers, rows))
            continue
        i += 1
    return tables


def clean(s):
    s = (s or "").strip()
    s = s.replace("**", "").replace("`", "")
    return s.strip()


# --------------------------------------------------------------------------
# 公司名归一 / 时间解析
# --------------------------------------------------------------------------

_STRIP_TAIL = re.compile(r"(股份有限公司|有限责任公司|有限公司|集团|校招|校园招聘|公司)$")


def norm_company(s):
    s = clean(s).replace("（", "(").replace("）", ")")
    s = re.sub(r"[()（）][^()（）]*[)）]", "", s)
    s = re.sub(r"\s+", "", s)
    prev = None
    while prev != s:
        prev = s
        s = _STRIP_TAIL.sub("", s)
    return s.lower()


DASHES = {"", "—", "-", "–", "/", "N/A", "n/a", "无", "未定", "待定"}

ISO_DT = re.compile(r"(20\d{2})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})\s*[日号]?"
                    r"(?:[T\s,，]*?(\d{1,2})\s*[:：点时]\s*(\d{1,2})?)?")
MD_DT = re.compile(r"(?<![\d/-])(\d{1,2})\s*[-/月.]\s*(\d{1,2})\s*[日号]?"
                   r"(?:[T\s,，]+(\d{1,2})\s*[:：点时]\s*(\d{1,2})?)?(?![\d/-])")
DUE_HINT = re.compile(r"前|截止|之前|内|到期|期限|勿晚|deadline", re.I)
START_HINT = re.compile(r"批次日|开始|开考|举行|定于|安排|场次|考试日|面试日")


def _mk(y, mo, d, h, mi):
    try:
        return dt.datetime(int(y), int(mo), int(d),
                           int(h) if h else 0, int(mi) if mi else 0, tzinfo=CST)
    except (ValueError, TypeError):
        return None


def extract_times(text, today):
    """从一段文本里抽 [(datetime|None, has_time, is_due)]。MM-DD 缺年补今年，过早就进明年。"""
    text = clean(text)
    out = []
    for m in ISO_DT.finditer(text):
        y, mo, d, h, mi = m.groups()
        ctx = text[max(0, m.start() - 16):m.end() + 4]
        t = _mk(y, mo, d, h, mi)
        out.append((t, bool(h), bool(DUE_HINT.search(ctx)) and not bool(START_HINT.search(ctx))))
    for m in MD_DT.finditer(text):
        mo, d, h, mi = m.groups()
        ctx = text[max(0, m.start() - 16):m.end() + 4]
        t = _mk(today.year, mo, d, h, mi)
        if t and t.date() < today - dt.timedelta(days=100):
            t = _mk(today.year + 1, mo, d, h, mi)
        out.append((t, bool(h), bool(DUE_HINT.search(ctx)) and not bool(START_HINT.search(ctx))))
    return [(t, ht, due) for t, ht, due in out if t]


def pick_when(text, kind_key, today):
    """返回 (due_iso, start_iso, date_only)。多个截止取最晚（更早的保守口径留在 note）；
    材料/门户截止类无 hint 也按截止处理；其余无 hint 的日期按开始（无时刻则当天 09:00）。"""
    found = extract_times(text, today)
    if not found:
        return None, None, False
    dues = [t for t, _ht, is_due in found if is_due]
    if not dues and kind_key in ("material_due", "portal_due"):
        dues = [t for t, _ht, _d in found]
    if dues:
        due = max(dues)
        has_time = next(ht for t, ht, _d in found if t == due)
        if not has_time:
            due = due.replace(hour=23, minute=59)
        return due.isoformat(timespec="seconds"), None, not has_time
    start = min(t for t, _ht, _d in found)
    has_time = next(ht for t, ht, _d in found if t == start)
    if not has_time:
        start = start.replace(hour=9, minute=0)
    return None, start.isoformat(timespec="seconds"), not has_time


URL_RE = re.compile(r"https?://[^\s<>\"'）)】\]>,，。；;]+")
AID_TAIL = re.compile(r"^(.*?)[\s,，]*(\d{4,6})\s*$")


def first_url(text):
    m = URL_RE.search(clean(text))
    return m.group(0).rstrip(".,;:!?）。，；") if m else ""


def split_company_aid(text):
    """「公司名 + 可选尾挂公告 id」→ (name, aid|None)。"""
    s = clean(text)
    m = AID_TAIL.match(s)
    if m and re.search(r"[\u4e00-\u9fa5A-Za-z]", m.group(1)):
        try:
            return m.group(1).strip(), int(m.group(2))
        except ValueError:
            pass
    return s, None


# --------------------------------------------------------------------------
# 三个来源 → 候选条目
# --------------------------------------------------------------------------

def _item(source, company, aid, kind_key, due, start, date_only, link, action, note,
          status="待做", status_raw=""):
    return {"id": None, "company": company, "ckey": norm_company(company),
            "aids": [aid] if aid else [], "kind_key": kind_key, "kind": KINDS[kind_key],
            "due": due, "start": start, "date_only": bool(date_only),
            "link": link or "", "action": action or KINDS[kind_key], "note": note or "",
            "sources": [source], "status": status, "status_raw": status_raw or status}


def from_next_steps(inst, warns):
    path = os.path.join(inst, NEXT_STEPS)
    if not os.path.isfile(path):
        return [], False
    steps = {}
    try:
        for ln in open(path, encoding="utf-8"):
            if not ln.strip():
                continue
            try:
                r = json.loads(ln)
            except ValueError:
                warns.append("state/next-steps.jsonl 有一行不是合法 JSON，已跳过")
                continue
            if isinstance(r, dict) and r.get("id"):
                steps[r["id"]] = r          # 同一 id 后写覆盖先写（与 schedule.py 同口径）
    except OSError as exc:
        warns.append("读取 %s 失败：%s" % (NEXT_STEPS, exc))
        return [], True
    out = []
    for s in steps.values():
        st = s.get("status")
        if st in ("superseded", "dismissed"):      # dismissed：误判的待办（如 GitHub 通知、猎头群发）
            continue
        kind = s.get("kind")
        if kind not in KINDS:
            continue
        due, start = s.get("due_at"), s.get("start_at")
        for t in (due, start):
            if t and not _parse_iso(t):
                warns.append("next-steps %s 时间无法解析：%s" % (s.get("id"), t))
                break
        status = "已完成" if st == "done" else "待做"
        it = _item("next-steps", clean(s.get("company") or "未知公司"), s.get("announcement_id"),
                   kind, due, start, False, s.get("link"), s.get("action"), "",
                   status=status)
        it["id"] = s["id"]
        out.append(it)
    return out, True


ASSESS_KIND = [("测评", "assessment"), ("评测", "assessment"), ("笔试", "written_test"),
               ("机试", "written_test"), ("考试", "written_test"), ("面试", "interview"),
               ("面谈", "interview")]
ASSESS_STATUS = {"待做": "待做", "已约": "已约", "已完成": "已完成", "已过期": "已过期"}


def from_assessments(inst, today, warns):
    """assessments.md 表格 → 候选。容错：表缺关键列整表跳过；行字段缺失/类型不识别跳过该行。"""
    rel = "assessments.md"
    path = os.path.join(inst, rel)
    try:
        tables = parse_md_tables(path)
    except OSError as exc:
        warns.append("读取 assessments.md 失败：%s" % exc)
        return [], False
    if tables is None:
        return [], False
    if not tables:
        warns.append("assessments.md 里没有 markdown 表格（日程缺这一块来源）")
        return [], True
    out = []
    for headers, rows in tables:
        if not ("公司" in headers and "类型" in headers):
            continue                       # 别的表（备查等）不是日程表，静默跳过
        need = [h for h in ("公司", "类型", "期限", "状态") if h not in headers]
        if need:
            warns.append("assessments.md 日程表缺列 %s，整表跳过" % "/".join(need))
            continue
        link_col = next((h for h in headers if "链接" in h or "入口" in h), None)
        src_col = "来源" if "来源" in headers else None
        for i, r in enumerate(rows):
            where = "assessments.md 第 %d 行" % (i + 1)
            name, aid = split_company_aid(r.get("公司", ""))
            if not name:
                warns.append("%s：公司为空，已跳过" % where)
                continue
            ktext = clean(r.get("类型", ""))
            kind = next((k for kw, k in ASSESS_KIND if kw in ktext), None)
            if kind is None:
                warns.append("%s：类型「%s」不识别，已跳过" % (where, ktext or "空"))
                continue
            raw_status = clean(r.get("状态", ""))
            status = ASSESS_STATUS.get(raw_status, "待做")
            due, start, date_only = pick_when(r.get("期限", ""), kind, today)
            note_bits = [x for x in (clean(r.get("期限", "")) if not due and not start else "",
                                     raw_status if raw_status not in ASSESS_STATUS else "",
                                     clean(r.get(src_col, "")) if src_col else "") if x]
            it = _item("assessments", name, aid, kind, due, start, date_only,
                       first_url(r.get(link_col, "")) if link_col else "",
                       "", "；".join(note_bits), status=status,
                       status_raw=raw_status)
            it["type_raw"] = ktext
            out.append(it)
    return out, True


TRACKER_NEXT = re.compile(r"测评|笔试|面试")
SEG_SPLIT = re.compile(r"[;；,，\n]")


def _tracker_rows(inst, warns):
    rel = "tracker.md"
    path = os.path.join(inst, rel)
    try:
        tables = parse_md_tables(path)
    except OSError as exc:
        warns.append("读取 tracker.md 失败：%s" % exc)
        return [], False
    if not tables:
        if tables is None:
            return [], False
        warns.append("tracker.md 里没有 markdown 表格")
        return [], True
    for headers, rows in tables:
        if all(h in headers for h in ("公司", "id", "状态")):
            return rows, True
    warns.append("tracker.md 里没有含 公司/id/状态 的表")
    return [], True


def _submitted_ids(inst):
    """已经投上的公告 id（state/outcomes）。tracker.md 是手写台账，投递池投上后不一定有人回去改状态列，
    只看台账会对已投公司继续报「门户截止」。"""
    done = set()
    d = os.path.join(inst, "state", "outcomes")
    for n in (os.listdir(d) if os.path.isdir(d) else []):
        if not n.endswith(".json"):
            continue
        try:
            if json.load(open(os.path.join(d, n), encoding="utf-8")).get("result") in ("submitted", "verified"):
                done.add(n[:-5])
        except (OSError, ValueError):
            pass
    return done


def from_tracker(inst, today, warns):
    rows, ok = _tracker_rows(inst, warns)
    submitted = _submitted_ids(inst)
    out = []
    for r in rows:
        name, aid_col = split_company_aid(r.get("公司", ""))
        if not name:
            continue
        status = clean(r.get("状态", ""))
        if "已停止" in status or "不合适" in status or "暂缓" in status:
            continue
        try:
            aid = int(clean(r.get("id", "")))
        except ValueError:
            aid = aid_col
        applied = "已投递" in status or "笔试" in status or "面试" in status or "Offer" in status
        keydate = r.get("关键日期", "")
        record = r.get("记录", "")
        # 测评/笔试/面试 片段（任何状态都可能出现）
        for seg in SEG_SPLIT.split(clean(keydate) + "；" + clean(record)):
            seg = seg.strip().strip("*")
            if not seg or not TRACKER_NEXT.search(seg):
                continue
            if re.search(r"log/|\.md", seg):
                continue
            kind = "written_test" if "笔试" in seg else ("interview" if "面试" in seg else "assessment")
            if "待通知" in seg or "待告" in seg:
                due = start = None
                date_only = False
            else:
                due, start, date_only = pick_when(seg, kind, today)
            out.append(_item("tracker", name, aid, kind, due, start, date_only,
                             "", "", seg))
        # 未投递行的门户截止（待材料 → 材料截止）
        if not applied and str(aid) not in submitted:
            due, start, date_only = pick_when(keydate, "portal_due", today)
            if due or start:
                kind = "material_due" if "待材料" in status else "portal_due"
                note = clean(keydate)
                out.append(_item("tracker", name, aid, kind, due, start, date_only,
                                 "", "", note))
    return out, ok


# --------------------------------------------------------------------------
# 合并 / 状态 / 输出
# --------------------------------------------------------------------------

def _when_dt(it):
    t = _parse_iso(it.get("due") or it.get("start"))
    return t


def _parse_iso(s):
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=CST)


def _mergeable(a, b):
    if a["ckey"] != b["ckey"] or a["kind_key"] != b["kind_key"]:
        return False
    ta, tb = _when_dt(a), _when_dt(b)
    if ta is None or tb is None:
        return True
    if a.get("date_only") or b.get("date_only"):
        return ta.date() == tb.date() or abs(ta - tb) <= MERGE_MAX_GAP
    return abs(ta - tb) <= MERGE_MAX_GAP


def _merge(group, it):
    """把 it 并进 group（就地）。字段优先级按来源：next-steps > assessments > tracker。"""
    members = group["_members"] + [it]
    members.sort(key=lambda m: SRC_RANK[m["sources"][0]])

    def pick(field):
        for m in members:
            v = m.get(field)
            if v not in (None, "", []):
                return v
        return None

    g = {"id": members[0]["id"], "company": pick("company"), "ckey": group["ckey"],
         "aids": sorted({a for m in members for a in m["aids"]}),
         "kind_key": group["kind_key"], "kind": group["kind"],
         "due": pick("due"), "start": pick("start"),
         "date_only": any(m["date_only"] for m in members) and not any(
             (m.get("due") or m.get("start")) and not m["date_only"] for m in members),
         "link": pick("link") or "", "action": pick("action") or "",
         "note": "；".join(dict.fromkeys(m["note"] for m in members if m["note"])),
         "sources": sorted({s for m in members for s in m["sources"]},
                           key=lambda s: SRC_RANK.get(s, 9)),
         "status": pick("status") or "待做", "status_raw": pick("status_raw") or ""}
    if members[0].get("type_raw"):
        g["type_raw"] = members[0]["type_raw"]
    g["_members"] = members
    return g


def load_done(inst):
    """state/agenda-done.jsonl → {id: status}（后写覆盖）。"""
    path = os.path.join(inst, DONE_FILE)
    out = {}
    if not os.path.isfile(path):
        return out
    try:
        for ln in open(path, encoding="utf-8"):
            if not ln.strip():
                continue
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if isinstance(r, dict) and r.get("id"):
                out[r["id"]] = r
    except OSError:
        pass
    return out


class AppendLock:
    """追加写 mkdir 锁（与 web/app/bank.py、host/mail.py 同口径）。"""

    def __init__(self, target):
        self.lockdir = os.path.abspath(target) + ".lockdir"

    def __enter__(self):
        for _ in range(200):
            try:
                os.mkdir(self.lockdir)
                return self
            except FileExistsError:
                time.sleep(0.05)
        raise TimeoutError("拿不到追加锁 %s" % self.lockdir)

    def __exit__(self, *exc):
        try:
            os.rmdir(self.lockdir)
        except OSError:
            pass


ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,64}$")


def mark_done(inst, entry_id, done=True):
    """追加一条状态记录到 state/agenda-done.jsonl；done=False 取消完成标记。"""
    if not ID_RE.match(entry_id or ""):
        raise ValueError("非法日程 id")
    path = os.path.join(inst, DONE_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rec = {"id": entry_id, "status": "done" if done else "open",
           "at": dt.datetime.now(CST).isoformat(timespec="seconds")}
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    with AppendLock(path):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
    return rec


def _gen_id(it):
    w = it.get("due") or it.get("start") or "none"
    raw = "%s|%s|%s" % (it["ckey"], it["kind_key"], w[:10])
    return "ag-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def build(inst, now=None):
    """返回 {"items":[...], "warnings":[...], "sources":[...]}。items 已排序、带 days。"""
    now = now or dt.datetime.now(CST)
    today = now.date()
    warns = []

    steps, ns_ok = from_next_steps(inst, warns)
    assess, as_ok = from_assessments(inst, today, warns)
    track, tr_ok = from_tracker(inst, today, warns)

    groups = []
    for it in steps + assess + track:
        idx = next((i for i, g in enumerate(groups) if _mergeable(g, it)), None)
        if idx is None:
            groups.append(_merge({"_members": [], "ckey": it["ckey"],
                                  "kind_key": it["kind_key"], "kind": it["kind"]}, it))
        else:
            groups[idx] = _merge(groups[idx], it)

    done_map = load_done(inst)
    items = []
    for g in groups:
        g.pop("_members", None)
        if not g["id"]:
            g["id"] = _gen_id(g)
        mark = done_map.get(g["id"])
        if mark:
            g["status"] = "已完成" if mark.get("status") == "done" else "待做"
        w = _when_dt(g)
        g["when"] = w.isoformat(timespec="seconds") if w else None
        g["days"] = (w.date() - today).days if w else None
        if g["status"] in ("待做", "已约") and w and w < now:
            g["status"] = "已过期"
        if g["status_raw"] and g["status_raw"] != g["status"] \
                and g["status_raw"] not in g["note"]:
            g["note"] = (g["note"] + "；" if g["note"] else "") + "原状态：" + g["status_raw"]
        items.append(g)

    def sort_key(it):
        grp = {"待做": 0, "已约": 0, "已过期": 1, "已完成": 2}.get(it["status"], 0)
        w = _when_dt(it)
        if grp == 0:
            wk = (0, w) if w else (1, dt.datetime.max.replace(tzinfo=CST))
        elif grp == 1:
            wk = (0, -w.timestamp()) if w else (1, 0)     # 已过期：最近过期的排前
        else:
            wk = (0, w) if w else (1, dt.datetime.max.replace(tzinfo=CST))
        return (grp, wk, it["company"])
    items.sort(key=sort_key)

    sources = [{"path": "state/next-steps.jsonl", "ok": ns_ok},
               {"path": "assessments.md", "ok": as_ok},
               {"path": "tracker.md", "ok": tr_ok}]
    return {"items": items, "warnings": warns, "sources": sources,
            "doneFile": DONE_FILE, "asOf": today.isoformat()}


def source_paths(inst):
    """参与日程构建的文件（缓存按 mtime 失效）。"""
    return [os.path.join(inst, "assessments.md"), os.path.join(inst, "tracker.md"),
            os.path.join(inst, NEXT_STEPS), os.path.join(inst, DONE_FILE)]


def _nearer(cur, it):
    """两条里保留 when 更早的（都无时间时保留先来的）。"""
    if cur is None:
        return it
    far = dt.datetime.max.replace(tzinfo=CST)
    return it if (_when_dt(it) or far) < (_when_dt(cur) or far) else cur


def nearest_by_company(inst, items=None):
    """{"ids": {aid: item}, "keys": {ckey: item}} —— 每家公司最近一条未闭环日程（供 /board 行内用）。"""
    if items is None:
        items = build(inst)["items"]
    by_id, by_key = {}, {}
    for it in items:
        if it["status"] in ("已完成", "已过期"):
            continue
        if it.get("ckey"):
            by_key[it["ckey"]] = _nearer(by_key.get(it["ckey"]), it)
        for a in it.get("aids") or []:
            by_id[a] = _nearer(by_id.get(a), it)
    return {"ids": by_id, "keys": by_key}


# --------------------------------------------------------------------------
# /agenda 页面（服务端渲染 shell + 前端筛选，风格同 board.py）
# --------------------------------------------------------------------------

def e(v):
    return html.escape("" if v is None else str(v), quote=True)


NAV = [("/", "看板"), ("/board", "榜单"), ("/agenda", "日程"),
       ("/bank", "问题库"), ("/setup", "前置条件"), ("/handoffs", "待我处理")]

AGENDA_CSS = """
  .warnbox { border: var(--lw-card) solid var(--seq2); color: var(--seq2); background: transparent; padding: 10px 14px; font-size: 13px; margin-bottom: 26px; }
  .warnbox b { display: block; font-size: 12px; letter-spacing: .04em; margin-bottom: 4px; }
  .warnbox div { font-family: var(--font-mono); font-size: 12px; }
  .filters { display: flex; flex-wrap: wrap; align-items: center; gap: 9px 16px; border: var(--lw-card) solid var(--ink); background: var(--card-bg); box-shadow: 3px 4px 10px var(--card-shadow); padding: 11px 13px; margin-bottom: 8px; }
  .fg { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .fg > .fl { font-size: 12px; color: var(--ink-faint); letter-spacing: .04em; }
  .chip { font-family: var(--font-serif); font-size: 12.5px; line-height: 1.4; color: var(--ink-soft); background: transparent; border: var(--lw-content) solid var(--grid); border-radius: 0; padding: 2px 9px; cursor: pointer; }
  .chip:hover { border-color: var(--ink-soft); color: var(--ink); }
  .chip.on { border-color: var(--ink); color: var(--ink); font-weight: 700; }
  .fsearch { font-family: var(--font-serif); font-size: 13px; color: var(--ink); background: var(--canvas); border: var(--lw-content) solid var(--grid); border-radius: 0; padding: 3px 8px; min-width: 150px; }
  .fsearch:focus { outline: none; border-color: var(--ink); }
  .toggle { display: inline-flex; gap: 5px; align-items: center; cursor: pointer; font-size: 13px; color: var(--ink-soft); }
  .qmeta { font-family: var(--font-mono); font-size: 12px; color: var(--ink-faint); margin: 9px 0 0; }
  footer { margin-top: 30px; font-size: 12.5px; color: var(--ink-faint); }
  @media (max-width: 640px) { .fsearch { min-width: 0; width: 100%; } }
  .aglist { border-top: var(--lw-grid) solid var(--grid); }
  .ag { display: grid; grid-template-columns: 84px 1fr auto; gap: 4px 14px; padding: 9px 0;
        border-bottom: var(--lw-grid) solid var(--grid); align-items: baseline; }
  .ag .dd { font-family: var(--font-mono); font-variant-numeric: tabular-nums; font-size: 14px; font-weight: 600; }
  .ag .dd.hot { color: var(--seq3); }
  .ag .dd.warm { color: var(--seq2); }
  .ag .who { min-width: 0; }
  .ag .who b { font-weight: 700; }
  .ag .who .what { color: var(--ink-soft); font-size: 13.5px; }
  .ag .meta { font-family: var(--font-mono); font-size: 12px; color: var(--ink-soft); white-space: nowrap; text-align: right; }
  .ag .note { grid-column: 2 / 4; color: var(--ink-faint); font-size: 12.5px; }
  .ag.xp .who, .ag.xp .dd, .ag.xp .meta { color: var(--ink-faint); }
  .ag.xp .dd { font-weight: 400; }
  .ag.done .who, .ag.done .dd, .ag.done .meta { color: var(--ink-faint); }
  .ag.done .who b { text-decoration: line-through; }
  .agbtn { font-family: var(--font-serif); font-size: 12px; color: var(--ink); background: var(--card-bg);
           border: var(--lw-content) solid var(--ink-faint); border-radius: 0; padding: 1px 9px; cursor: pointer; }
  .agbtn:hover { border-color: var(--ink); }
  .agbtn:disabled { color: var(--ink-faint); cursor: default; }
  @media (max-width: 640px) {
    .ag { grid-template-columns: 62px 1fr; }
    .ag .meta { grid-column: 2; text-align: left; white-space: normal; }
    .ag .note { grid-column: 2; }
  }
"""

AGENDA_JS = r"""
(function(){
'use strict';
const D = window.AGENDA || {};
const ITEMS = D.items || [];
const $ = s => document.querySelector(s);
const esc = s => String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const md = iso => iso ? iso.slice(5,10) : '';
const hm = iso => (iso && iso.length >= 16) ? iso.slice(11,16) : '';
const state = { kind:'all', st:'all', showDone:false, q:'' };

function whenLabel(it){
  if(!it.when) return '待定';
  return md(it.when) + (it.date_only ? '' : ' ' + hm(it.when));
}
function ddLabel(it){
  if(it.status==='已完成') return '已完成';
  if(it.status==='已过期') return '已过';
  if(it.days==null) return '待定';
  if(it.days<0) return '已过';
  return 'D-' + it.days;
}
function ddCls(it){
  if(it.status!=='待做' && it.status!=='已约') return '';
  if(it.days==null || it.days<0) return '';
  return it.days<=3 ? 'hot' : it.days<=7 ? 'warm' : '';
}
function rowCls(it){ return it.status==='已完成' ? 'done' : it.status==='已过期' ? 'xp' : ''; }

function filtered(){
  const q = state.q.trim().toLowerCase();
  return ITEMS.filter(it=>{
    if(!state.showDone && it.status==='已完成') return false;
    if(state.kind!=='all' && it.kind!==state.kind) return false;
    if(state.st!=='all'){
      if(state.st==='pending' && !(it.status==='待做'||it.status==='已约')) return false;
      else if(state.st!=='pending' && it.status!==state.st) return false;
    }
    if(q){
      const hay = (it.company+' '+it.kind+' '+(it.note||'')+' '+(it.aids||[]).join(' ')).toLowerCase();
      if(!hay.includes(q)) return false;
    }
    return true;
  });
}
function render(){
  const list = filtered();
  $('#agenda').innerHTML = list.length ? list.map(it=>{
    const link = it.link ? ` <a href="${esc(it.link)}" target="_blank" rel="noopener">入口</a>` : '';
    const done = it.status==='已完成';
    const btn = `<button class="agbtn" data-id="${esc(it.id)}" data-done="${done?0:1}">${done?'撤销':'完成'}</button>`;
    return `<div class="ag ${rowCls(it)}"><div class="dd ${ddCls(it)}">${esc(ddLabel(it))}</div>`
      + `<div class="who"><b>${esc(it.company)}</b> <span class="tag">${esc(it.kind)}</span>`
      + `<span class="what">${esc(it.action||it.kind)}${link} · ${esc(whenLabel(it))}</span></div>`
      + `<div class="meta">${btn} <span class="tag blue">${esc(it.status)}</span><br>${esc((it.sources||[]).join(' + '))}</div>`
      + (it.note ? `<div class="note">${esc(it.note)}</div>` : '')
      + `</div>`;
  }).join('') : '<div class="missing">没有符合条件的日程</div>';
  const pend = ITEMS.filter(i=>i.status==='待做'||i.status==='已约').length;
  $('#qmeta').textContent = `显示 ${list.length} / ${ITEMS.length} 条 · 待办 ${pend} 条`;
}
document.querySelectorAll('[data-kind]').forEach(b=>b.addEventListener('click', ()=>{
  state.kind = b.dataset.kind;
  document.querySelectorAll('[data-kind]').forEach(x=>x.classList.toggle('on', x===b));
  render();
}));
document.querySelectorAll('[data-st]').forEach(b=>b.addEventListener('click', ()=>{
  state.st = b.dataset.st;
  document.querySelectorAll('[data-st]').forEach(x=>x.classList.toggle('on', x===b));
  render();
}));
$('#showdone').addEventListener('change', ev=>{ state.showDone = ev.target.checked; render(); });
$('#q').addEventListener('input', ev=>{ state.q = ev.target.value; render(); });
$('#agenda').addEventListener('click', async ev=>{
  const b = ev.target.closest('.agbtn'); if(!b) return;
  b.disabled = true;
  try {
    const r = await fetch('/api/agenda/done', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({id: b.dataset.id, done: b.dataset.done === '1'})});
    if(r.ok){ location.reload(); return; }
    b.disabled = false; b.textContent = '失败 ' + r.status;
  } catch(err){ b.disabled = false; b.textContent = '失败'; }
});
render();
})();
"""


def page(inst, data=None):
    """组装 /agenda HTML。data 缺省时现场 build。"""
    import handoffs
    import pages
    data = data or build(inst)
    items = data["items"]
    warns = data.get("warnings") or []

    def nav():
        cnt = handoffs.open_count(inst)
        return "".join('<a href="%s"%s>%s%s</a>' % (
            href, ' class="on"' if href == "/agenda" else "", label,
            '<span class="cnt">%d</span>' % cnt if href == "/handoffs" and cnt else "")
            for href, label in NAV)

    kinds = [k for k in ("测评", "笔试", "面试", "材料截止", "门户截止", "Offer")
             if any(i["kind"] == k for i in items)]
    parts = ['<h1>我的日程</h1>',
             '<p class="sub">合并自 <span class="num">%s</span> · 标记完成只写 <code>state/agenda-done.jsonl</code>，不改源文件</p>'
             % e(" + ".join(s["path"] for s in data["sources"]))]
    if warns:
        parts.append('<div class="warnbox"><b>解析提示</b>%s</div>'
                     % "".join("<div>%s</div>" % e(w) for w in warns))
    parts.append('<div class="filters">'
                 '<div class="fg"><span class="fl">类型</span>'
                 '<button class="chip on" data-kind="all">全部</button>%s</div>'
                 '<div class="fg"><span class="fl">状态</span>'
                 '<button class="chip on" data-st="all">全部</button>'
                 '<button class="chip" data-st="pending">待办</button>'
                 '<button class="chip" data-st="已过期">已过期</button>'
                 '<button class="chip" data-st="已完成">已完成</button></div>'
                 '<div class="fg"><input class="fsearch" type="search" id="q" placeholder="搜公司 / 备注 / id" autocomplete="off"></div>'
                 '<div class="fg"><label class="toggle"><input type="checkbox" class="cbx" id="showdone"> 显示已完成</label></div>'
                 '</div><p class="qmeta" id="qmeta"></p>'
                 % "".join('<button class="chip" data-kind="%s">%s</button>' % (e(k), e(k)) for k in kinds))
    parts.append('<div class="aglist" id="agenda"></div>')
    parts.append('<footer>真源：<span class="num">%s</span> · 同步进日历用 <code>schedule.py sync --from-agenda</code></footer>'
                 % e(" · ".join(s["path"] + ("" if s["ok"] else "（缺失）") for s in data["sources"])))

    payload = {"items": items, "asOf": data.get("asOf")}
    script = ('<script>window.AGENDA=%s;</script><script>%s</script>'
              % (json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/"),
                 AGENDA_JS))
    return ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">"
            "<title>我的日程</title><style>%s%s</style></head><body><div class=\"wrap\">"
            "<nav class=\"nav\">%s</nav>%s</div>%s</body></html>") % (
        pages.BASE_CSS, AGENDA_CSS, nav(), "".join(parts), script)
