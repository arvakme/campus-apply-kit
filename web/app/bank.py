"""问题库（question-bank.yaml）读写核心。

host/qb.py 和 web 容器共用这一份实现，避免两套逻辑：
  - 读：load() 返回 (doc, version)，version 是文件字节的 sha256
  - 写：mutate() 统一走 fcntl 锁 + mkdir 锁（跨容器挂载）+ 版本校验 + 临时文件原子替换，写完重新生成 question-bank.md
  - 对外视图：public_view() 去掉 secret 条目的 answer（网页和 API 只写不读）
  - 迁移：import_md() 把旧 question-bank.md 转成 yaml 结构（逐条人工判定表见 CLASSIFY）

契约见 docs/contracts.md §3。依赖仅 pyyaml。
"""

import datetime as dt
import fcntl
import hashlib
import os
import re
import tempfile
import time

import yaml

CATEGORIES = ["身份", "联系", "教育", "家庭", "声明", "偏好", "经历", "账号"]
KINDS = ["value", "rule", "secret"]
MD_NAME = "question-bank.md"
YAML_NAME = "question-bank.yaml"


class Conflict(Exception):
    """版本号不匹配：文件在读取之后被别人改过。"""

    def __init__(self, current):
        super().__init__("question-bank.yaml 已被修改，当前版本 %s" % current[:12])
        self.current = current


class BankError(Exception):
    pass


# --------------------------------------------------------------------------
# 读
# --------------------------------------------------------------------------

def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def empty_doc():
    return {"version": 1, "items": [], "rules": []}


def parse(data):
    if not data.strip():
        return empty_doc()
    doc = yaml.safe_load(data.decode("utf-8")) or {}
    if not isinstance(doc, dict):
        raise BankError("question-bank.yaml 顶层必须是映射")
    doc.setdefault("version", 1)
    doc["items"] = doc.get("items") or []
    doc["rules"] = doc.get("rules") or []
    return doc


def load(path):
    """返回 (doc, version)。文件不存在时返回空库，version 为空字节的 sha256。"""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        data = b""
    return parse(data), sha256_bytes(data)


def is_filled(item):
    ans = item.get("answer")
    return ans is not None and str(ans).strip() != ""


def completeness(doc):
    items = doc.get("items", [])
    req = [i for i in items if i.get("required")]
    missing = [i for i in req if not is_filled(i)]
    return {"total": len(items), "required": len(req), "required_filled": len(req) - len(missing),
            "missing_ids": [i["id"] for i in missing]}


def _masker(doc):
    """secret 答案可能在规则文本、其他答案里被顺带引用（如家庭隐私规则里写了父母姓名），一并打码。"""
    secrets = sorted({str(i.get("answer")).strip() for i in doc.get("items", [])
                      if i.get("kind") == "secret" and is_filled(i) and len(str(i.get("answer")).strip()) >= 2},
                     key=len, reverse=True)

    def mask(v):
        if not isinstance(v, str):
            return v
        for s in secrets:
            v = v.replace(s, "〈secret〉")
        return v
    return mask


def public_view(doc):
    """网页 / API 用：secret 条目只给 filled 标记，不给 answer。"""
    mask = _masker(doc)
    items = []
    for it in doc.get("items", []):
        out = {k: mask(v) for k, v in it.items() if k != "answer"}
        out["filled"] = is_filled(it)
        if it.get("kind") != "secret":
            out["answer"] = mask(it.get("answer"))
        items.append(out)
    rules = [{"id": r.get("id"), "text": mask(r.get("text"))} for r in doc.get("rules", [])]
    return {"version": doc.get("version", 1), "items": items, "rules": rules,
            "completeness": completeness(doc)}


# --------------------------------------------------------------------------
# 写
# --------------------------------------------------------------------------

def dump(doc):
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=1000,
                          default_flow_style=False).encode("utf-8")


def _atomic_write(path, data):
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".%s." % os.path.basename(path), suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if os.path.exists(path):
            os.chmod(tmp, os.stat(path).st_mode & 0o777)
        else:
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def validate(doc):
    seen = set()
    for it in doc.get("items", []):
        iid = it.get("id")
        if not iid or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", str(iid)):
            raise BankError("非法 id：%r（只允许小写字母、数字、连字符）" % iid)
        if iid in seen:
            raise BankError("id 重复：%s" % iid)
        seen.add(iid)
        if not it.get("question"):
            raise BankError("%s 缺 question" % iid)
        if it.get("kind") not in KINDS:
            raise BankError("%s 的 kind 必须是 %s" % (iid, "/".join(KINDS)))
        if it.get("category") not in CATEGORIES:
            raise BankError("%s 的 category 必须是 %s" % (iid, "/".join(CATEGORIES)))


class _DirLock:
    """跨宿主机/容器的互斥锁：mkdir 在 virtiofs 上是原子的，fcntl.flock 却不会穿过 Apple container 的挂载。

    实测宿主机持 flock 时容器内照样拿得到，所以两层都要：flock 管同侧并发，mkdir 锁管两侧之间。
    持锁超过 STALE 秒视为进程崩溃遗留，自动清理。
    """
    STALE = 30
    WAIT = 10

    def __init__(self, path):
        self.dir = path + ".lockdir"

    def __enter__(self):
        deadline = time.time() + self.WAIT
        while True:
            try:
                os.mkdir(self.dir)
                return self
            except FileExistsError:
                try:
                    if time.time() - os.stat(self.dir).st_mtime > self.STALE:
                        os.rmdir(self.dir)
                        continue
                except OSError:
                    continue
                if time.time() > deadline:
                    raise BankError("问题库被占用超过 %d 秒（%s），稍后重试" % (self.WAIT, self.dir))
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            os.rmdir(self.dir)
        except OSError:
            pass


def mutate(path, fn, expected_version=None, render=True):
    """加锁读 → 校验版本 → fn(doc) 就地修改 → 原子写 → 重新生成 md。返回新版本号。

    expected_version 为 None 时不做版本校验（CLI 单次 set 这类读改写都在锁内完成的场景）。
    """
    lock_path = path + ".lock"
    with open(lock_path, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            with _DirLock(path):
                doc, current = load(path)
                if expected_version is not None and expected_version != current:
                    raise Conflict(current)
                fn(doc)
                validate(doc)
                data = dump(doc)
                _atomic_write(path, data)
                if render:
                    md_path = os.path.join(os.path.dirname(os.path.abspath(path)), MD_NAME)
                    _atomic_write(md_path, render_md(doc).encode("utf-8"))
                return sha256_bytes(data)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def today():
    return dt.date.today().isoformat()


def find(doc, item_id):
    for it in doc.get("items", []):
        if it.get("id") == item_id:
            return it
    return None


def apply_updates(doc, updates, source="网页"):
    """updates: [{"id", "answer"?, "clear"?, "hint"?, "required"?}]。secret 的空 answer 视为不改。"""
    for up in updates:
        it = find(doc, up.get("id"))
        if it is None:
            raise BankError("没有这个条目：%s" % up.get("id"))
        changed = False
        if up.get("clear"):
            it["answer"] = ""
            changed = True
        elif "answer" in up and up["answer"] is not None:
            ans = str(up["answer"])
            if it.get("kind") == "secret" and ans.strip() == "":
                pass
            elif ans != str(it.get("answer") or ""):
                it["answer"] = ans
                changed = True
        for key in ("hint", "required"):
            if key in up and up[key] != it.get(key):
                it[key] = up[key]
                changed = True
        if changed:
            it["updated"] = today()
            it["source"] = source


# --------------------------------------------------------------------------
# md 视图
# --------------------------------------------------------------------------

def _cell(v):
    s = "" if v is None else str(v)
    return s.replace("\r", "").replace("\n", "<br>").replace("|", "\\|")


def render_md(doc):
    lines = [
        "<!-- 生成文件，勿手改：由 question-bank.yaml 生成（host/qb.py render-md 或 web /bank 保存时自动生成） -->",
        "# 投递问题库（Question Bank · 只读视图）",
        "",
        "> 真源是同目录 question-bank.yaml。改答案用 `uv run --script host/qb.py set <id> <答案>` 或网页 /bank。",
        "> worker 规则：填表遇到 profile.md 没有的字段 → 先查本文件 → 有答案直接填 → 没有才 blocked 问主持人。",
        "",
    ]
    items = doc.get("items", [])
    cats = [c for c in CATEGORIES if any(i.get("category") == c for i in items)]
    cats += sorted({i.get("category") for i in items} - set(CATEGORIES))
    for cat in cats:
        lines += ["## %s" % cat, "",
                  "| 问题(字段名/问法) | 答案 | 记录日期 | 来源 | id | 类型 | 必填 | 填写提示 |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        for it in items:
            if it.get("category") != cat:
                continue
            lines.append("| %s |" % " | ".join(_cell(x) for x in (
                it.get("question"), it.get("answer"), it.get("updated"), it.get("source"),
                it.get("id"), it.get("kind"), "是" if it.get("required") else "否", it.get("hint"))))
        lines.append("")
    lines += ["## 通用默认规则", ""]
    for r in doc.get("rules", []):
        lines.append("- %s <!-- rule:%s -->" % (str(r.get("text", "")).replace("\n", " "), r.get("id")))
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 旧 md → yaml（逐条人工判定）
# --------------------------------------------------------------------------

# (匹配关键字（全部命中）, id, category, kind, required, hint)
# 判定原则：身份证、密码、住址、学号、父母信息、手机/微信/QQ、出生日期、籍贯、档案、高中 → secret；
# 答案是"遇到 X 怎么选"的规则性描述 → rule；其余 → value。hint 只写填法，绝不写答案本身。
CLASSIFY = [
    (("默认登录", "邮箱"), "login-email", "账号", "value", True, "注册/登录优先用邮箱方式，统一用这个邮箱"),
    (("出生日期",), "birth-date", "身份", "secret", True, "与身份证一致，格式 YYYY-MM-DD"),
    (("证件号码",), "id-card-number", "身份", "secret", True, "18 位身份证号，末位 X 大写"),
    (("民族",), "ethnicity", "身份", "value", True, "填写与身份证一致的民族"),
    (("英语等级成绩",), "english-cet-score", "教育", "value", False, "填最高等级考试的分数"),
    (("英语等级",), "english-level", "教育", "value", True, "下拉选项里选最高已通过的等级"),
    (("期望年薪",), "expected-salary-year", "偏好", "value", True, "区间控件填区间；单值控件填上限"),
    (("招聘信息来源",), "info-source", "偏好", "rule", True, "所有公司统一口径"),
    (("邮寄地址",), "mailing-address", "联系", "secret", True, "省市区 + 详细地址"),
    (("籍贯",), "native-place", "身份", "secret", True, "省 + 市"),
    (("学号", "硕士"), "student-id-master", "教育", "secret", False, "硕士学校学号"),
    (("学号", "本科"), "student-id-bachelor", "教育", "secret", False, "本科学校学号"),
    (("院系", "硕士"), "department-master", "教育", "value", False, "硕士院系全称"),
    (("院系", "本科"), "department-bachelor", "教育", "value", False, "本科院系全称"),
    (("专业排名",), "major-rank", "教育", "rule", True, "选项档位不匹配时按规则就近取"),
    (("硕士类型",), "master-type", "教育", "value", True, "学术型 / 专业型"),
    (("研究方向", "硕士"), "research-master", "教育", "value", False, "硕士研究方向"),
    (("研究方向", "本科"), "research-bachelor", "教育", "value", False, "本科一般无研究方向"),
    (("重大疾病",), "decl-disease", "声明", "value", True, "注意题干正反问法"),
    (("不良记录",), "decl-bad-record", "声明", "value", True, "注意题干正反问法"),
    (("股权",), "decl-equity", "声明", "value", True, "注意题干正反问法"),
    (("本公司",), "decl-relative-in-company", "声明", "value", True, "注意题干正反问法"),
    (("同行业",), "decl-relative-in-industry", "声明", "value", True, "注意题干正反问法"),
    (("处罚记录",), "decl-school-punishment", "声明", "value", True, "注意题干正反问法"),
    (("父亲姓名",), "father-name", "家庭", "secret", True, "家庭成员姓名用真实值"),
    (("母亲姓名",), "mother-name", "家庭", "secret", True, "家庭成员姓名用真实值"),
    (("父亲手机",), "father-phone", "家庭", "secret", False, "按家庭成员隐私规则用占位值"),
    (("母亲手机",), "mother-phone", "家庭", "secret", False, "按家庭成员隐私规则用占位值"),
    (("父亲工作单位",), "father-employer", "家庭", "secret", False, "按家庭成员隐私规则用占位值"),
    (("母亲工作单位",), "mother-employer", "家庭", "secret", False, "按家庭成员隐私规则用占位值"),
    (("自我评价",), "self-evaluation", "经历", "value", True, "按字数限制截断，不改事实"),
    (("到岗时间",), "onboard-time", "偏好", "rule", True, "选项里选最快；开放填写按规则"),
    (("入职前实习",), "pre-onboard-intern", "偏好", "value", False, "是 / 否"),
    (("手机号码",), "phone", "联系", "secret", True, "11 位大陆手机号；与 profile 不一致时以这里为准"),
    (("体重",), "weight", "身份", "value", False, "单位 kg"),
    (("身高",), "height", "身份", "value", False, "单位 cm"),
    (("婚姻状况",), "marital-status", "身份", "value", True, "未婚 / 已婚"),
    (("通信", "地址"), "contact-address", "联系", "secret", True, "省市区 + 详细地址"),
    (("意向", "城市"), "preferred-cities", "偏好", "rule", True, "按优先级从高到低选"),
    (("海外工作",), "accept-overseas", "偏好", "value", False, "是 / 否"),
    (("期望月薪",), "expected-salary-month", "偏好", "rule", True, "由期望年薪换算，注意控件单位"),
    (("微信",), "wechat", "联系", "secret", False, "微信号"),
    (("QQ",), "qq", "联系", "secret", False, "QQ 号"),
    (("爱好特长",), "hobbies", "经历", "value", False, "2~3 项即可"),
    (("资格证书",), "certificates", "经历", "value", False, "职业资格证，不含四六级；没有就留空不编"),
    (("本科", "GPA"), "gpa-bachelor", "教育", "value", True, "表单要什么进制填什么进制"),
    (("GPA",), "gpa-rule", "教育", "rule", False, "以简历 PDF 为准，没有就留空"),
    (("成绩单",), "transcript-attachments", "声明", "rule", False, "附件类硬性要求的处理方式"),
    (("调剂",), "accept-adjustment", "偏好", "value", True, "是 / 否"),
    (("硕士毕业时间",), "graduation-date-master", "教育", "value", True, "填真实毕业日期，届数随企业口径"),
    (("IELTS",), "ielts", "教育", "value", False, "有雅思栏位时填"),
    (("毕业设计", "本科"), "thesis-bachelor", "经历", "value", False, "本科毕设题目"),
    (("毕业设计", "硕士"), "thesis-master", "经历", "value", False, "硕士毕设/项目"),
    (("专业名称",), "major-mapping-master", "教育", "rule", True, "门户专业库里没有原名时按映射优先级选"),
    (("联系人",), "internship-referee", "经历", "rule", False, "实习证明人一律用占位信息"),
    (("实习单位", "城市"), "internship-city", "经历", "value", False, "实习所在城市"),
    (("现居住地",), "current-city", "联系", "secret", True, "省-市-区"),
    (("现居详细地址",), "current-address", "联系", "secret", False, "街道级详细地址"),
    (("入党",), "party-join-date", "身份", "value", False, "年月控件只填到月；同时影响政治面貌"),
    (("档案",), "hukou-archive-unit", "身份", "secret", False, "人事档案存放单位全称"),
    (("高中",), "high-school", "教育", "secret", False, "学校、所在地、起止年月"),
    (("助学贷款",), "student-loan", "声明", "value", False, "是 / 否"),
    (("境外学习天数",), "overseas-study-days", "教育", "value", False, "整数天数"),
    (("境外停留天数",), "overseas-stay-days", "教育", "value", False, "整数天数"),
    (("CET4",), "cet4", "教育", "value", False, "分数 + 考试年月"),
    (("六级获得时间",), "cet6-date", "教育", "value", False, "年月控件只填到月"),
    (("获奖情况", "优先"), "awards", "经历", "value", False, "名称 · 级别 · 日期，优先填前 3 条"),
    (("获奖情况", "备用"), "awards-backup", "经历", "value", False, "栏位不够时补充"),
    (("在校实践",), "campus-practice", "经历", "value", False, "项目名 + 起止年月"),
    (("在校职务",), "campus-role", "经历", "value", False, "职务 + 学校 + 任期"),
]

SECRET_WORDS = re.compile(r"身份证|证件|密码|地址|住址|学号|父亲|母亲|父母|家属|微信|QQ|手机|电话|出生|籍贯|档案|高中")
PASSWORD_TOKEN = re.compile(r"[A-Za-z0-9!@#$%^&*._-]*[@#$%^&*!][A-Za-z0-9!@#$%^&*._-]*")


def _norm(s):
    return s.replace("（", "(").replace("）", ")").replace("：", ":")


def classify(question):
    q = _norm(question)
    for keys, iid, cat, kind, req, hint in CLASSIFY:
        if all(k in q for k in keys):
            return {"id": iid, "category": cat, "kind": kind, "required": req, "hint": hint}, True
    kind = "secret" if SECRET_WORDS.search(q) else "value"
    return {"id": None, "category": "身份", "kind": kind, "required": False, "hint": ""}, False


def _split_row(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip().replace("\\|", "|").replace("<br>", "\n") for c in re.split(r"(?<!\\)\|", s)]


SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _slug(text, used):
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "item"
    n, cand = 1, base
    while cand in used:
        n += 1
        cand = "%s-%d" % (base, n)
    used.add(cand)
    return cand


def import_md(text):
    """返回 (doc, report)。report 列出未命中判定表、需要人工复核的条目。"""
    lines = text.splitlines()
    items, rules, unmatched, used = [], [], [], set()
    in_rules = False
    heading_cat = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#"):
            in_rules = "通用默认规则" in line
            title = line.lstrip("#").strip()
            heading_cat = title if title in CATEGORIES else None
            i += 1
            continue
        if line.lstrip().startswith("|") and i + 1 < len(lines) and SEP.match(lines[i + 1]):
            headers = _split_row(line)
            i += 2
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                cells = _split_row(lines[i])
                cells += [""] * (len(headers) - len(cells))
                row = dict(zip(headers, cells))
                question = cells[0]
                spec, hit = classify(question)
                # 已经是生成视图时，保留视图里的 id/类型/必填/提示（round-trip）
                if row.get("id"):
                    spec = {"id": row["id"], "category": heading_cat or spec["category"],
                            "kind": row.get("类型") or spec["kind"],
                            "required": row.get("必填", "") == "是", "hint": row.get("填写提示", "")}
                    hit = True
                iid = spec["id"]
                if iid is None or iid in used:
                    iid = _slug(spec["id"] or "item", used)
                    if not hit:
                        unmatched.append((iid, question))
                else:
                    used.add(iid)
                items.append({
                    "id": iid, "question": question, "aliases": [],
                    "category": spec["category"], "kind": spec["kind"], "required": spec["required"],
                    "hint": spec["hint"], "answer": cells[1] if len(cells) > 1 else "",
                    "updated": cells[2] if len(cells) > 2 else "", "source": cells[3] if len(cells) > 3 else "",
                })
                i += 1
            continue
        if in_rules and line.lstrip().startswith("- "):
            body = line.lstrip()[2:].strip()
            m = re.search(r"<!-- rule:([a-z0-9-]+) -->\s*$", body)
            rid = None
            if m:
                rid = m.group(1)
                body = body[: m.start()].rstrip()
            rules.append({"id": rid, "text": body})
        i += 1

    rule_ids = set()
    for n, r in enumerate(rules, 1):
        if not r["id"]:
            r["id"] = _rule_id(r["text"], n, rule_ids)
        rule_ids.add(r["id"])

    table_rows = len(items)
    migrated = _extract_password_rules(items, rules, used)
    doc = {"version": 1, "items": items, "rules": rules}
    return doc, {"items": len(items), "table_rows": table_rows, "migrated": migrated,
                 "rules": len(rules), "unmatched": unmatched}


RULE_IDS = [("密码", "register-password"), ("声明", "declaration-wording"), ("来源", "info-source"),
            ("到岗", "onboard-and-intern"), ("排名", "major-rank"), ("硕士类型", "master-type"),
            ("家庭成员", "family-privacy")]


def _rule_id(text, n, used):
    for key, rid in RULE_IDS:
        if key in text and rid not in used:
            return rid
    return "rule-%d" % n


def _extract_password_rules(items, rules, used):
    """规则里出现的明文密码挪进 secret 条目，规则文本只留引用，网页展示规则时不泄漏。返回新增条目 id。"""
    migrated = []
    for r in rules:
        if "密码" not in r["text"]:
            continue
        tokens = [t for t in PASSWORD_TOKEN.findall(r["text"])
                  if len(t) >= 8 and re.search(r"\d", t) and re.search(r"[A-Za-z]", t)]
        if not tokens:
            continue
        if find({"items": items}, "register-password") is None and "注册密码" in r["text"]:
            items.append({
                "id": "register-password", "question": "新门户注册密码（统一）", "aliases": ["注册密码", "设置密码"],
                "category": "账号", "kind": "secret", "required": True,
                "hint": "新注册统一用这个；已有账号以 accounts.md 为准",
                "answer": tokens[0], "updated": today(), "source": "由旧问题库规则迁移",
            })
            used.add("register-password")
            migrated.append("register-password")
            r["text"] = r["text"].replace(tokens[0], "〈见 secret 条目 register-password〉", 1)
            tokens = tokens[1:]
        for t in tokens:
            r["text"] = r["text"].replace(t, "〈见 accounts.md〉")
    return migrated
