"""看板数据：从实例目录的 markdown 真源解析出看板 JSON。

与早期的静态看板脚本相比有三处差异：
  - 实例目录由参数传入（容器里是 /instance），警告按次收集而不是全局列表
  - 记录链接改指向 web 的 /raw/ 只读路由
  - 看板主人从 $CAMPUS_OWNER 或 profile.md 第一行标题取，仓库里不写任何人名
"""

import datetime as dt
import json
import os
import re

import agenda

TARGET_PER_DAY = int(os.environ.get("CAMPUS_TARGET_PER_DAY", "8"))

WARNINGS = []   # build() 每次调用前清空；server 渲染串行加锁，不会串数据


def warn(msg):
    WARNINGS.append(msg)


# --------------------------------------------------------------------------
# markdown 表格解析
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
    """返回文件里所有 markdown 表格：[(headers, rows_as_dict, rows_as_list, footnotes)]"""
    if not os.path.exists(path):
        return None
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError as exc:
        warn("读取失败 %s：%s" % (path, exc))
        return None
    tables = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.lstrip().startswith("|") and i + 1 < n and SEP_RE.match(lines[i + 1]):
            headers = split_row(line)
            i += 2
            rows_list, rows_dict = [], []
            while i < n and lines[i].lstrip().startswith("|"):
                cells = split_row(lines[i])
                if len(cells) > len(headers):       # 多出的单元格并进最后一列（tracker 里有一行手写多打了一个 |）
                    cells = cells[: len(headers) - 1] + [" ".join(cells[len(headers) - 1:])]
                elif len(cells) < len(headers):
                    cells = cells + [""] * (len(headers) - len(cells))
                rows_list.append(cells)
                rows_dict.append(dict(zip(headers, cells)))
                i += 1
            foot = []
            j = i
            while j < n and len(foot) < 3:
                t = lines[j].strip()
                if t and not t.startswith("#") and not t.startswith("|"):
                    foot.append(t)
                elif t.startswith("#") or t.startswith("|"):
                    break
                j += 1
            tables.append((headers, rows_dict, rows_list, foot))
            continue
        i += 1
    return tables


def pick_table(tables, must_have, what, path):
    """挑出含全部指定表头的表；按表头名取列，不按位置。"""
    if tables is None:
        warn("找不到文件：%s（%s 区块显示占位）" % (path, what))
        return None
    if not tables:
        warn("%s 里没有 markdown 表格（%s 区块显示占位）" % (path, what))
        return None
    for headers, rows_dict, rows_list, foot in tables:
        if all(h in headers for h in must_have):
            return headers, rows_dict, rows_list, foot
    headers = tables[0][0]
    missing = [h for h in must_have if h not in headers]
    warn("%s 缺列 %s（实际表头：%s）" % (path, "/".join(missing), " | ".join(headers)))
    return None


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

FULL_DATE = re.compile(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b")
MD_DATE = re.compile(r"(?<![\d-])(\d{1,2})-(\d{1,2})(?![\d-])")
DASHES = {"", "—", "-", "–", "/", "N/A", "n/a", "无"}


def clean(s):
    """去掉 markdown 加粗/行内代码标记。"""
    s = (s or "").strip()
    s = s.replace("**", "").replace("`", "")
    return s.strip()


def blank(s):
    return clean(s) in DASHES


def first_date(text, year):
    """取第一个 2026-MM-DD；没有则取第一个 MM-DD 并补年份。"""
    if not text:
        return ""
    m = FULL_DATE.search(text)
    if m:
        return "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = MD_DATE.search(text)
    if m:
        mm, dd = int(m.group(1)), int(m.group(2))
        if 1 <= mm <= 12 and 1 <= dd <= 31:
            return "%04d-%02d-%02d" % (year, mm, dd)
    return ""


def norm_date(text, year):
    if blank(text):
        return ""
    return first_date(clean(text), year)


LAYER_MAP = {"大厂": "大厂", "国企": "国企", "中小": "中小", "名企": "名企", "名企(外企)": "名企",
             "名企（外企）": "名企", "知名": "名企", "外企": "名企"}


def norm_layer(raw):
    v = clean(raw)
    if v in LAYER_MAP:
        return LAYER_MAP[v]
    for key, val in LAYER_MAP.items():
        if key and key in v:
            return val
    return v or "未分类"


CITIES = ("北京 上海 广州 深圳 杭州 南京 苏州 成都 武汉 西安 重庆 天津 长沙 合肥 郑州 青岛 大连 厦门 济南 福州 "
          "东莞 珠海 宁波 无锡 佛山 沈阳 哈尔滨 昆明 南昌 贵阳 太原 石家庄 长春 南宁 海口 兰州 银川 西宁 "
          "乌鲁木齐 呼和浩特 香港 澳门 台北 常州 嘉兴 温州 绍兴 台州 金华 烟台 潍坊 徐州 保定 廊坊 中山 惠州 "
          "泉州 南通 扬州 镇江 芜湖 株洲 湘潭 洛阳 唐山 秦皇岛 包头 柳州 桂林 湛江 汕头 江门 泰州 盐城 淮安 "
          "连云港 宿迁 绵阳 遵义 襄阳 宜昌 赣州 九江 新乡 三亚").split()
PAREN = re.compile(r"[（(]([^（()）]*)[)）]")


def pick_city(role):
    """城市优先从岗位名括号里取；括号内不是城市（如「系统研发方向」「2027届」）时再扫全文。"""
    text = clean(role)
    for seg in PAREN.findall(text):
        head = re.split(r"[,，;；/·]", seg)[0].strip()
        for c in CITIES:
            if head.startswith(c) or head == c:
                return c
        for part in re.split(r"[,，;；/·]", seg):
            for c in CITIES:
                if part.strip().startswith(c):
                    return c
    for c in CITIES:
        if c in text:
            return c
    return ""



def prefer_cities(root):
    """intent.yaml 的 cities.prefer（不依赖 yaml 库，只抠这一行）。"""
    p = os.path.join(root, "intent.yaml")
    try:
        m = re.search(r"^\s*prefer:\s*\[([^\]]*)\]", open(p, encoding="utf-8").read(), re.M)
    except OSError:
        return []
    return [x.strip().strip("'\"") for x in m.group(1).split(",")] if m else []

PORTAL_KEYS = [("zhaopin", "智联"), ("智联", "智联"), ("hotjob", "智联"), ("Moka", "Moka"), ("moka", "Moka"),
               ("北森", "北森"), ("beisen", "北森"), ("zhiye", "北森"), ("飞书", "飞书"), ("feishu", "飞书"),
               ("邮件", "邮件"), ("email", "邮件"), ("用户自投", "自投"), ("本人", "自投"),
               ("海康", "官网/自建"), ("wejob", "官网/自建"), ("自有门户", "官网/自建"), ("同门户", "官网/自建"),
               ("泛微", "官网/自建"), ("官网", "官网/自建"), (".ac.cn", "官网/自建"), (".com", "官网/自建"),
               ("official", "官网/自建")]


def norm_portal(raw):
    v = clean(raw)
    for key, val in PORTAL_KEYS:
        if key in v:
            return val
    v = re.split(r"[（(\s]", v)[0]
    return v or "—"


NEXT_KEY = re.compile(r"笔试|测评|面试")
SEG_SPLIT = re.compile(r"[;；,，]")
LOGPATH = re.compile(r"(log/[\w./-]+\.md)")


def next_step(*texts):
    for text in texts:
        for seg in SEG_SPLIT.split(clean(text or "")):
            seg = seg.strip()
            if seg and NEXT_KEY.search(seg) and not LOGPATH.search(seg):
                return seg
    return ""


def status_cls(status):
    s = clean(status)
    if "已投递" in s:
        return "s-applied"
    if "已停止" in s or "暂缓" in s or "不合适" in s:
        return "s-hold"
    return "s-form"


# --------------------------------------------------------------------------
# 各数据源
# --------------------------------------------------------------------------

def load_tracker(root, year):
    path = os.path.join(root, "tracker.md")
    rel = "tracker.md"
    tables = parse_md_tables(path)
    table = pick_table(tables, ["公司", "id", "状态"], "已投明细 / 统计卡", rel)
    if table is None:
        return [], rel, False
    headers, rows, _, _ = table
    for col in ("层/类别", "岗位", "门户/方式", "关键日期", "记录"):
        if col not in headers:
            warn("tracker.md 缺列「%s」，该列按空处理" % col)
    out = []
    for r in rows:
        name = clean(r.get("公司", ""))
        if not name:
            continue
        status = clean(r.get("状态", "")) or "未标"
        cls = status_cls(status)
        keydate = r.get("关键日期", "")
        record = r.get("记录", "")
        applied = first_date(clean(keydate), year) if cls == "s-applied" else ""
        deadline_local = "" if cls == "s-applied" else first_date(clean(keydate), year)
        records = [{"text": os.path.basename(p), "href": "/raw/" + p} for p in LOGPATH.findall(record)]
        role = clean(r.get("岗位", ""))
        out.append({
            "name": name,
            "id": clean(r.get("id", "")),
            "layer": norm_layer(r.get("层/类别", "")),
            "layerRaw": clean(r.get("层/类别", "")),
            "role": role,
            "city": pick_city(role),
            "portal": norm_portal(r.get("门户/方式", "")),
            "status": status,
            "statusCls": cls,
            "applied": applied,
            "deadline": deadline_local,          # 之后被 battle-map 的 expired_at 覆盖/补齐
            "next": next_step(record, keydate),
            "nextDate": "",
            "records": records,
            "keyDate": clean(keydate),
        })
    for c in out:
        c["nextDate"] = first_date(c["next"], year) if c["next"] else ""
    return out, rel, True


def load_battle_map(root, year):
    path = os.path.join(root, "log", "battle-map.md")
    rel = "log/battle-map.md"
    tables = parse_md_tables(path)
    need = ["score", "公司", "标题", "id", "类别", "expired_at", "邮件类"]
    table = pick_table(tables, need, "候选队列", rel)
    if table is None:
        return {"missing": True, "file": rel, "total": 0, "rows": [], "fields": []}, rel, False
    headers, rows, _, _ = table
    for col in ("命中关键词", "看板状态"):
        if col not in headers:
            warn("battle-map.md 缺列「%s」，该列按空处理" % col)
    out = []
    for r in rows:
        mail_raw = clean(r.get("邮件类", ""))
        mail = ""
        if mail_raw.startswith("是"):
            mail = mail_raw.split(":", 1)[-1].split("：", 1)[-1].strip() or "是"
        out.append({
            "score": clean(r.get("score", "")),
            "company": clean(r.get("公司", "")),
            "title": clean(r.get("标题", "")),
            "id": clean(r.get("id", "")),
            "cat": norm_layer(r.get("类别", "")),
            "kw": clean(r.get("命中关键词", "")),
            "exp": norm_date(r.get("expired_at", ""), year),
            "mail": mail,
            "board": "" if blank(r.get("看板状态", "")) else clean(r.get("看板状态", "")),
            "posted": norm_date(r.get("发布日期", ""), year),
        })
    return {"missing": False, "file": rel, "total": len(out), "rows": out}, rel, True


def load_latest_batch(root, year):
    logdir = os.path.join(root, "log")
    rel_default = "log/B-NN.md"
    if not os.path.isdir(logdir):
        warn("找不到目录 log/（在跑批次区块显示占位）")
        return {"missing": True, "file": rel_default}, rel_default, False
    cands = []
    for fn in os.listdir(logdir):
        m = re.match(r"^B-(\d+)\.md$", fn)
        if m:
            cands.append((int(m.group(1)), fn))
    if not cands:
        warn("log/ 下没有 B-NN.md 批次文件（在跑批次区块显示占位）")
        return {"missing": True, "file": rel_default}, rel_default, False
    cands.sort()
    fn = cands[-1][1]
    rel = "log/" + fn
    path = os.path.join(logdir, fn)
    tables = parse_md_tables(path)
    table = pick_table(tables, ["公司", "状态"], "在跑批次", rel)
    if table is None:
        return {"missing": True, "file": rel}, rel, False
    headers, rows_dict, rows_list, foot = table
    title = ""
    for line in open(path, encoding="utf-8"):
        if line.startswith("#"):
            title = line.lstrip("#").strip()
            break
    ddl_col = next((h for h in headers if "截止" in h), None)
    if ddl_col is None:
        warn("%s 没有「截止」列，批次待办不带日期" % rel)
    clean_rows = [[clean(c) for c in row] for row in rows_list]
    cls = [status_cls(r.get("状态", "")) for r in rows_dict]
    blockers = []
    for r in rows_dict:
        st = clean(r.get("状态", ""))
        if re.search(r"阻塞|待用户|待材料|handOff|handoff", st, re.I):
            blockers.append({
                "name": clean(r.get("公司", "")),
                "role": "",
                "date": norm_date(r.get(ddl_col, ""), year) if ddl_col else "",
                "what": re.split(r"[;；]", st)[0][:48],
            })
    return ({"missing": False, "file": rel, "title": title, "headers": headers,
             "rows": clean_rows, "statusCol": headers.index("状态"), "statusCls": cls,
             "blockers": blockers, "footnote": foot[0] if foot else ""}, rel, True)


STEP_CN = {"claim": "接单", "portal_detected": "认出门户", "account_ready": "已登录", "filling": "填表中",
           "field_blocked": "缺字段", "gate": "等人工关卡", "submitted": "已提交", "verified": "已核验",
           "failed": "失败", "note": "备注", "skipped": "已跳过"}


def load_live_batch(root, now):
    """24 小时内有事件的作业 → 在跑批次表（worker 实时写入，契约 §9）。没有事件时返回 None。"""
    ev = os.path.join(root, "state", "events.jsonl")
    if not os.path.isfile(ev):
        return None
    since = now - dt.timedelta(hours=24)
    first, last, steps = {}, {}, {}
    for line in open(ev, encoding="utf-8"):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        try:
            t = dt.datetime.fromisoformat((e.get("at") or "")[:19])
        except ValueError:
            continue
        if t < since:
            continue
        if (e.get("portal") or "") == "email":
            continue                     # 邮件投递在"邮件队列"区块单独展示
        aid = str(e.get("job") or "")
        first.setdefault(aid, (t, e))
        steps.setdefault(aid, set()).add(e.get("step") or "")
        if aid not in last or t >= last[aid][0]:   # 补记事件可能乱序追加，按时间取最新
            last[aid] = (t, e)
    if not last:
        return None
    idx = ann_index()
    rows, cls = [], []
    for aid, (t, e) in sorted(last.items(), key=lambda kv: kv[1][0], reverse=True):
        o = {}
        op = os.path.join(root, "state", "outcomes", "%s.json" % aid)
        if os.path.isfile(op):
            try:
                o = json.load(open(op, encoding="utf-8"))
            except ValueError:
                o = {}
        res = o.get("result")
        step = e.get("step") or ""
        if res == "submitted":                    # 有结果时步骤跟结果走，不被补记的中间事件带偏
            step = "verified" if "verified" in steps[aid] else "submitted"
        elif res == "skipped":
            step = "skipped"
        status = {"submitted": "已投递", "skipped": "跳过", "blocked": "卡住", "failed": "失败"}.get(res) \
            or ("已投递" if step in ("submitted", "verified") else "等人工" if step == "gate" else "进行中")
        company = o.get("company") or (idx.get(aid) or {}).get("company") or aid
        mins = int((t - first[aid][0]).total_seconds() // 60)
        pane = (e.get("pane") or e.get("by") or "").split("/")[-1][:8]
        rows.append([company, aid, norm_portal(o.get("portal") or e.get("portal") or ""), status,
                     STEP_CN.get(step, step), pane, "%d 分钟" % mins, t.strftime("%H:%M"),
                     clean(o.get("reason") or e.get("msg") or "")[:60]])
        cls.append("s-applied" if status == "已投递" else "s-hold" if status in ("跳过", "失败") else "s-form")
    headers = ["公司", "announcement_id", "门户", "状态", "当前步骤", "worker", "耗时", "最近", "说明"]
    return {"missing": False, "file": "state/events.jsonl", "title": "实时 · 24 小时内有动作的作业",
            "headers": headers, "rows": rows, "statusCol": 3, "statusCls": cls, "blockers": [],
            "footnote": "由 worker 写入的事件流与投递结果实时生成；邮件投递见下方邮件队列。"}


def load_mail_batches(root):
    """mail/<批次>/<id>/message.json + sent.json → 邮件队列表。"""
    base = os.path.join(root, "mail")
    if not os.path.isdir(base):
        return None
    rows, batches = [], []
    for b in sorted(os.listdir(base)):
        bd = os.path.join(base, b)
        if not os.path.isdir(bd):
            continue
        batches.append(b)
        for aid in sorted(os.listdir(bd)):
            mp = os.path.join(bd, aid, "message.json")
            if not os.path.isfile(mp):
                continue
            try:
                m = json.load(open(mp, encoding="utf-8"))
            except ValueError:
                continue
            sp = os.path.join(bd, aid, "sent.json")
            st = "待发送"
            if os.path.isfile(sp):
                try:
                    st = "已发送 " + json.load(open(sp, encoding="utf-8")).get("at", "")[5:16].replace("T", " ")
                except ValueError:
                    st = "已发送"
            rows.append([b, clean(m.get("company", "")), clean(m.get("role_applied", "")),
                         clean(m.get("to", "")), clean(m.get("subject", "")), st])
    if not rows:
        return None
    rows.sort(key=lambda r: (not r[5].startswith("待"), r[5]), reverse=False)
    sent = sum(1 for r in rows if r[5].startswith("已发送"))
    return {"missing": False, "file": "mail/%s" % ",".join(batches),
            "headers": ["批次", "公司", "岗位", "收件邮箱", "主题", "状态"], "rows": rows,
            "summary": "共 %d 封 · 已发送 %d · 待发送 %d" % (len(rows), sent, len(rows) - sent)}


def load_email_queue(root):
    path = os.path.join(root, "log", "email-queue.md")
    rel = "log/email-queue.md"
    tables = parse_md_tables(path)
    table = pick_table(tables, ["公司", "收件邮箱", "状态"], "邮件队列", rel)
    if table is None:
        return {"missing": True, "file": rel, "headers": [], "rows": []}, rel, False
    headers, _, rows_list, _ = table
    return {"missing": False, "file": rel, "headers": headers,
            "rows": [[clean(c) for c in row] for row in rows_list]}, rel, True


def load_announcements(root):
    """可选：log/announcements.json。没有就只显示 id。"""
    path = os.path.join(root, "log", "announcements.json")
    if not os.path.exists(path):
        return {}, None, False
    try:
        raw = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError) as exc:
        warn("announcements.json 解析失败：%s（候选行不加链接）" % exc)
        return {}, "log/announcements.json", False
    items = raw if isinstance(raw, list) else raw.get("announcements", [])
    out = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        key = str(it.get("announcement_id", "")).strip()
        if key:
            out[key] = {"link": it.get("link") or "", "apply": it.get("from_url") or "",
                        "exp": it.get("expired_at") or ""}
    return out, "log/announcements.json", True


# --------------------------------------------------------------------------
# 派生
# --------------------------------------------------------------------------

def derive(companies, pool, ann, year):
    by_id = {c["id"]: c for c in companies if c["id"]}
    # 截止：tracker 没有截止列；未投递行「关键日期」里的日期优先，
    # 其余（含全部已投递行）用 battle-map 的 expired_at 按 id 补齐
    if not pool.get("missing"):
        exp_by_id = {r["id"]: r["exp"] for r in pool["rows"] if r["id"] and r["exp"]}
        for c in companies:
            if not c["deadline"] and c["id"] in exp_by_id:
                c["deadline"] = exp_by_id[c["id"]]
    for c in companies:
        if not c["deadline"] and c["id"] in ann and ann[c["id"]].get("exp"):
            c["deadline"] = first_date(ann[c["id"]]["exp"], year)
        if c["next"] and not c["nextDate"]:
            c["nextDate"] = first_date(c["next"], year)

    fields = ["score", "company", "title", "id", "cat", "kw", "exp", "mail", "board", "done", "link", "apply"]
    rows = []
    if not pool.get("missing"):
        for r in pool["rows"]:
            tr = by_id.get(r["id"])
            done = ""
            if tr:
                done = "已投" if tr["statusCls"] == "s-applied" else tr["status"]
            a = ann.get(r["id"], {})
            rows.append([r["score"], r["company"], r["title"], r["id"], r["cat"], r["kw"],
                         r["exp"], r["mail"], r["board"], done, a.get("link", ""), a.get("apply", "")])
    pool_out = {"missing": pool.get("missing", False), "file": pool.get("file", "log/battle-map.md"),
                "total": len(rows), "fields": fields, "rows": rows}
    return pool_out


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def owner_label(root):
    env = os.environ.get("CAMPUS_OWNER")
    if env:
        return env
    path = os.path.join(root, "profile.md")
    try:
        for line in open(path, encoding="utf-8"):
            if line.startswith("# "):
                return re.split(r"\s*[·|｜]\s*", line[2:].strip())[0]
    except OSError:
        pass
    return "我的投递"


_ANN_CACHE = {}


def ann_index():
    """仓库 data/ 的公告与层级：announcement_id → {company, cities, tier}。容器里 CAMPUS_REPO=/repo。"""
    repo = os.environ.get("CAMPUS_REPO") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(repo, "data", "paperball", "announcements.jsonl")
    tiers_path = os.path.join(repo, "data", "tiers.yaml")
    key = tuple(os.path.getmtime(p) if os.path.exists(p) else 0 for p in (path, tiers_path))
    if _ANN_CACHE.get("key") == key:
        return _ANN_CACHE["val"]
    tier_of = {}
    try:
        import yaml
        for t, cs in ((yaml.safe_load(open(tiers_path, encoding="utf-8")) or {}).get("tiers") or {}).items():
            for c in cs or []:
                tier_of[c] = t
    except Exception:
        pass
    out = {}
    if os.path.isfile(path):
        for line in open(path, encoding="utf-8"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            tags = " ".join(r.get("company_tags") or [])
            # 与投递池「要用户 double check」同口径：一线/冷门大厂→大厂，腰部名企/外企→名企（「行业翘楚」太宽，不算）
            tier = tier_of.get(r.get("company")) or (
                "国企" if re.search(r"国企|事业单位", tags) else
                "大厂" if re.search(r"一线大厂|冷门大厂", tags) else
                "名企" if re.search(r"腰部名企|外企", tags) else "中小")
            out[str(r.get("announcement_id"))] = {"company": r.get("company") or "", "cities": r.get("cities") or [],
                                                  "tier": tier}
    _ANN_CACHE.update(key=key, val=out)
    return out


PORTAL_SHORT = {"email": "邮件", "moka": "Moka", "beisen": "北森", "feishu": "飞书", "hik": "海康",
                "zhilian": "智联", "hotjob": "hotjob", "official": "官网"}


def load_structured(root, companies):
    """state/registry.jsonl + outcomes + events → 补进 tracker 里没有的投递（契约 §9）。

    邮件投递、worker 新投的门户单都先落在这里；tracker 迁移成生成物之前，看板用它补齐统计与节奏图。
    """
    known_ids = {c["id"] for c in companies if c.get("id")}
    known_names = {c["name"] for c in companies}
    added, seen = [], set()

    idx = ann_index()

    def add(aid, name, portal, role, day, status, cls):
        info = idx.get(str(aid)) or {}
        name = name if (name and not name.startswith("公告 ")) else (info.get("company") or name)
        if not name or aid in known_ids or name in known_names or (aid or name) in seen:
            return
        seen.add(aid or name)
        city = pick_city(role)
        if city in ("", "未标", "其他") and info.get("cities"):
            # 岗位名没写城市（多为邮件投递）：公告多城市时取意向里最靠前的那个，而不是公告列的第一个（常是北京）
            pref = [c for c in prefer_cities(root) if c in info["cities"]]
            city = pref[0] if pref else info["cities"][0]
        added.append({"name": name, "id": aid, "layer": info.get("tier") or "中小", "layerRaw": "", "role": role,
                      "city": city, "portal": norm_portal(PORTAL_SHORT.get(portal, portal or "—")),
                      "status": status, "statusCls": cls, "applied": day if cls == "s-applied" else "",
                      "deadline": "", "next": "", "nextDate": "", "records": [], "keyDate": day})

    reg = os.path.join(root, "state", "registry.jsonl")
    if os.path.isfile(reg):
        for line in open(reg, encoding="utf-8"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("status") not in ("submitted", "verified"):
                continue
            ids = [str(x) for x in r.get("announcement_ids") or []]
            add(ids[0] if ids else "", r.get("employer") or r.get("brand") or "", r.get("portal") or "",
                r.get("job") or "", (r.get("at") or "")[:10], "已投递", "s-applied")
    od = os.path.join(root, "state", "outcomes")
    if os.path.isdir(od):
        for fn in sorted(os.listdir(od)):
            if not fn.endswith(".json"):
                continue
            try:
                o = json.load(open(os.path.join(od, fn), encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if o.get("result") in ("submitted", "verified"):
                add(str(o.get("announcement_id") or fn[:-5]), o.get("company") or "", o.get("portal") or "",
                    o.get("job_applied") or "", (o.get("submitted_at") or "")[:10], "已投递", "s-applied")
    ev = os.path.join(root, "state", "events.jsonl")
    if os.path.isfile(ev):
        latest = {}
        for line in open(ev, encoding="utf-8"):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            latest[str(e.get("job") or "")] = e
        done = {c["id"] for c in added}
        for aid, e in latest.items():
            if aid in done or e.get("step") in ("submitted", "verified", "failed"):
                continue
            if os.path.isfile(os.path.join(od, "%s.json" % aid)):
                continue
            name = ""
            m = re.search(r"#\d+\s*([^,，\s]+)|B-\d{2}候选\d+\s*([^,，\s]+)", e.get("msg") or "")
            if m:
                name = m.group(1) or m.group(2)
            try:
                stale = (dt.datetime.now() - dt.datetime.fromisoformat((e.get("at") or "")[:19])).total_seconds() > 7200
            except ValueError:
                stale = True
            add(aid, name or ("公告 %s" % aid), e.get("portal") or "", "", (e.get("at") or "")[:10],
                "搁置（没投完）" if stale else "填表中", "s-hold" if stale else "s-form")
    return added



def load_slots(root):
    """state/slot-pool.json → 真正在跑的槽位（公司、开始时间）。没有投递池时返回 None。"""
    p = os.path.join(root, "state", "slot-pool.json")
    try:
        d = json.load(open(p, encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return [{"slot": k, "company": v.get("company"), "aid": v.get("aid"), "started": v.get("started")}
            for k, v in sorted((d.get("slots") or {}).items())]

def source_paths(root):
    """参与看板渲染的文件（缓存按它们的 mtime 失效）。"""
    paths = [os.path.join(root, p) for p in
             ("tracker.md", "profile.md", "log/battle-map.md", "log/email-queue.md", "log/announcements.json",
              "state/registry.jsonl", "state/events.jsonl", "state/slot-pool.json")]
    mail = os.path.join(root, "mail")
    if os.path.isdir(mail):
        paths.append(mail)
        for b in os.listdir(mail):
            bd = os.path.join(mail, b)
            if os.path.isdir(bd):
                paths.append(bd)
                paths += [os.path.join(bd, x) for x in os.listdir(bd) if os.path.isdir(os.path.join(bd, x))]
    od = os.path.join(root, "state", "outcomes")
    paths.append(od)
    paths += agenda.source_paths(root)
    logdir = os.path.join(root, "log")
    paths.append(logdir)                    # 新增 B-NN.md 会改目录 mtime
    if os.path.isdir(logdir):
        paths += [os.path.join(logdir, f) for f in os.listdir(logdir) if re.match(r"^B-\d+\.md$", f)]
    return paths


def build(root, now=None):
    """返回 (data, stats)。data 注入模板，stats 与原 build-dashboard.py 的 stdout 统计口径一致。"""
    del WARNINGS[:]
    now = now or dt.datetime.now()
    year = now.year

    companies, tracker_rel, tracker_ok = load_tracker(root, year)
    pool_raw, bm_rel, bm_ok = load_battle_map(root, year)
    batch, batch_rel, batch_ok = load_latest_batch(root, year)
    live = load_live_batch(root, now)
    if live:
        batch, batch_rel, batch_ok = live, live["file"], True
    email, email_rel, email_ok = load_email_queue(root)
    mailq = load_mail_batches(root)
    if mailq and (email.get("missing") or not email.get("rows")):
        email, email_rel, email_ok = mailq, mailq["file"], True
    ann, ann_rel, ann_ok = load_announcements(root)
    pool = derive(companies, pool_raw, ann, year)
    ag = agenda.build(root)
    for w in ag.get("warnings") or []:
        warn(w)

    sources = [{"path": tracker_rel, "ok": tracker_ok}, {"path": bm_rel, "ok": bm_ok},
               {"path": batch_rel, "ok": batch_ok}, {"path": email_rel, "ok": email_ok}]
    if ann_rel:
        sources.append({"path": ann_rel, "ok": ann_ok})
    sources += [s for s in ag["sources"] if s["path"] != "tracker.md"]

    companies = companies + load_structured(root, companies)
    applied = [c for c in companies if c["statusCls"] == "s-applied"]
    running = [c for c in companies if c["statusCls"] == "s-form"]
    applied_days = sorted({c["applied"] for c in applied if c["applied"]})

    mtimes = [os.path.getmtime(p) for p in source_paths(root) if os.path.isfile(p)]
    data_at = dt.datetime.fromtimestamp(max(mtimes)) if mtimes else now
    data = {
        "owner": owner_label(root),
        "asOf": now.strftime("%Y-%m-%d"),
        "generatedAt": data_at.strftime("%Y-%m-%d %H:%M"),
        "targetPerDay": TARGET_PER_DAY,
        "rhythmNote": "投递日取自 tracker「关键日期」列，以及 worker/邮件写入的投递结果与防重投登记。",
        "warnings": list(WARNINGS),
        "sources": sources,
        "trackerFile": tracker_rel,
        "companies": companies,
        "agenda": {"items": ag["items"], "doneFile": ag["doneFile"]},
        "pool": pool,
        "batch": batch,
        "email": email,
        "slots": load_slots(root),
        "interviews": 0,
        "offers": 0,
    }
    stats = {
        "tracker_rows": len(companies), "applied": len(applied), "running": len(running),
        "applied_days": applied_days, "pool": pool["total"], "pool_missing": pool["missing"],
        "batch_file": batch.get("file", "—"), "batch_rows": len(batch.get("rows", [])),
        "email_rows": len(email.get("rows", [])),
    }
    return data, stats


def stats_line(s):
    """与原脚本 stdout 同格式，便于对账。"""
    return "tracker %d 行 · 已投递 %d · 在跑 %d · 投递日 %s\n候选池 %d 行%s · 批次 %s %d 行 · 邮件队列 %d 行" % (
        s["tracker_rows"], s["applied"], s["running"], ",".join(s["applied_days"]) or "—",
        s["pool"], "（缺失）" if s["pool_missing"] else "", s["batch_file"], s["batch_rows"], s["email_rows"])


def render(data, template_text):
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return template_text.replace("/*__DATA__*/", payload)
