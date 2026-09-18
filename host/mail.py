#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6"]
# ///
"""mail.py · 收信分拣：增量读收件箱 → 规则/LLM 分类 → 抽取测评/笔试/面试信息 → 待办 + 通知

    uv run --script host/mail.py poll [--instance DIR] [--since 2d] [--account gmail]
        [--folder INBOX] [--engine codex|devin|claude] [--model M] [--limit N]
        [--no-llm] [--dry-run] [--fixtures DIR]

输入（二选一）：
  真实邮箱   himalaya 已配好的 IMAP 账户（默认 gmail，INBOX）
  --fixtures DIR  离线样例目录，里面放 .eml 文件（验收/调试用，不碰邮箱）

只读保证：只调用 `himalaya envelope list` 和 `himalaya message read --preview`
（--preview 不打 Seen 标）；不删除、不移动、不打标签、不发信。

分类（rules 先行，多分类命中/疑似招聘但没命中的交 LLM，--no-llm 时按优先级兜底）：
  assessment 测评 · written_test 笔试 · interview 面试邀约 · offer ·
  rejection 拒信 · application_ack 投递确认 · verification_code 验证码（只识别不落正文）· other

输出（写进实例目录，永不进仓库）：
  state/inbox/seen.json                 已处理 message-id → {at, kind}
  state/inbox/<date>/<hash>.json        每封一封档：分类 + 抽取 + 原文片段(≤500字)
  state/next-steps.jsonl                一行一个待办 {id, kind, company, announcement_id,
                                        due_at, start_at, link, action, source_msg,
                                        created_at, status: open|superseded|done}
通知：assessment/written_test/interview/offer 调 host/notify.py assessment；
  rejection 只进汇总不打扰；verification_code 只识别不写正文。

announcement_id 匹配：实例 tracker.md → state/registry.jsonl → 仓库 announcements.jsonl
（+ data/employer-aliases.yaml 若有）。公司名匹配不上或多候选时留空。
"""
from __future__ import annotations

import argparse
import datetime as dt
import email
import email.header
import email.policy
import email.utils
import fcntl
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
KINDS = ("assessment", "written_test", "interview", "offer",
         "rejection", "application_ack", "verification_code", "other")
ACTIONABLE = {"assessment", "written_test", "interview", "offer"}
KIND_LABEL = {"assessment": "测评", "written_test": "笔试", "interview": "面试",
              "offer": "Offer", "rejection": "拒信", "application_ack": "投递确认",
              "verification_code": "验证码", "other": "其他"}
BODY_EXCERPT = 500          # 落盘正文片段上限
LLM_EXCERPT = 1500          # 喂给 LLM 的正文片段上限
LLM_BATCH = 6
URL_RE = re.compile(r"https?://[^\s<>\"'）)】\]>,，。；;]+")
LINK_HINT = re.compile(
    r"(?i)assess|test|exam|eval|survey|interview|meeting|zoom|teams|webrtc|"
    r"beisen|moka|nowcoder|zhaopin|wecom|feishu|tencent|zhiye|italent|hire|campus|shixiseng|offer")


# --------------------------------------------------------------------------
# 小工具（与 scan.py/judge.py 同一风格）
# --------------------------------------------------------------------------

def norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "").lower()


def norm_company(s: str) -> str:
    s = (s or "").strip().replace("（", "(").replace("）", ")")
    s = re.sub(r"\s+", "", s)
    return re.sub(r"(股份有限公司|有限责任公司|有限公司|集团|校招|校园招聘)$", "", s)


def sha(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


def now_iso() -> str:
    return dt.datetime.now(CST).isoformat(timespec="seconds")


def atomic_write(path: str, text: str):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def read_json(path: str, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return default


class AppendLock:
    """追加写 mkdir 锁（contracts §8：与 web/app/bank.py 同口径）。"""

    def __init__(self, target: str):
        self.lockdir = os.path.abspath(target) + ".lockdir"

    def __enter__(self):
        for _ in range(200):
            try:
                os.mkdir(self.lockdir)
                return self
            except FileExistsError:
                import time
                time.sleep(0.05)
        raise TimeoutError("拿不到追加锁 %s" % self.lockdir)

    def __exit__(self, *exc):
        try:
            os.rmdir(self.lockdir)
        except OSError:
            pass


# --------------------------------------------------------------------------
# 邮件来源：himalaya（只读）或 fixtures .eml
# --------------------------------------------------------------------------

def himalaya(args: list[str], timeout: int = 60) -> str:
    cmd = ["himalaya"] + args
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError("himalaya %s 失败：%s" % (args[0], (r.stderr or r.stdout)[-300:]))
    return r.stdout


def parse_since(s: str) -> dt.date:
    """--since 支持 2d / 12h / 1w / YYYY-MM-DD。"""
    m = re.match(r"^(\d+)\s*([dhw])$", s or "")
    today = dt.datetime.now(CST).date()
    if m:
        n, unit = int(m.group(1)), m.group(2)
        days = n * {"d": 1, "h": 0, "w": 7}[unit]
        if unit == "h":
            return today - dt.timedelta(days=1) if n <= 72 else today - dt.timedelta(days=n // 24 + 1)
        return today - dt.timedelta(days=days)
    return dt.date.fromisoformat(s)


def _parse_rendered(raw: str) -> tuple[dict[str, str], str]:
    """himalaya message read --preview -o json 输出是 JSON 字符串：头部 + 空行 + 正文。"""
    text = raw
    try:
        text = json.loads(raw)
    except ValueError:
        pass
    headers: dict[str, str] = {}
    head, _, body = text.partition("\n\n")
    last = None
    for line in head.splitlines():
        if line[:1] in " \t" and last:
            headers[last] += " " + line.strip()
        elif ":" in line:
            last, _, v = line.partition(":")
            last = last.strip()
            headers[last] = v.strip()
    return headers, body.strip()


def list_himalaya(account: str, folder: str, since: dt.date, limit: int | None) -> list[dict]:
    """envelope list 分页拉取（只读，不改 flag）。"""
    out, page = [], 1
    while True:
        try:
            raw = himalaya(["envelope", "list", "-a", account, "-f", folder,
                            "-o", "json", "-s", "200", "-p", str(page),
                            "after", since.isoformat()])
        except RuntimeError as e:
            # 邮件数正好是每页上限的整数倍时，himalaya 会对下一页报 "page N out of bounds"
            if "out of bounds" in str(e):
                break
            raise
        try:
            batch = json.loads(raw)
        except ValueError:
            raise RuntimeError("envelope list 输出不是 JSON：%s" % raw[:200])
        if not isinstance(batch, list) or not batch:
            break
        out += batch
        if len(batch) < 200 or (limit and len(out) >= limit):
            break
        page += 1
    return out[:limit] if limit else out


def fetch_himalaya(env: dict, account: str, folder: str) -> dict:
    """message read --preview：不打 Seen 标。返回统一 msg dict。"""
    raw = himalaya(["message", "read", str(env["id"]), "-a", account, "-f", folder,
                    "--preview", "-o", "json",
                    "-H", "Message-ID", "-H", "Date", "-H", "From",
                    "-H", "Subject", "-H", "To"], timeout=90)
    headers, body = _parse_rendered(raw)
    frm = env.get("from") or {}
    to = env.get("to") or {}
    return {
        "env_id": str(env.get("id") or ""),
        "message_id": (headers.get("Message-ID") or "").strip() or None,
        "subject": headers.get("Subject") or env.get("subject") or "",
        "from_name": frm.get("name") or "",
        "from_addr": frm.get("addr") or "",
        "to_addr": to.get("addr") or "",
        "date": env.get("date") or headers.get("Date") or "",
        "flags": env.get("flags") or [],
        "body": strip_html(body),
    }


def strip_html(text: str) -> str:
    if "<" not in (text or "")[:400] and "</" not in text:
        return text
    if re.search(r"</(div|p|table|br|html|body|span)>", text, re.I):
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
        text = re.sub(r"(?i)<br\s*/?>", "\n", text)
        text = re.sub(r"(?i)</(p|div|tr|li|h\d)>", "\n", text)
        text = re.sub(r"<[^>]+>", " ", text)
        import html as _html
        text = _html.unescape(text)
    return re.sub(r"[ \t]+\n", "\n", text).strip()


def load_fixtures(d: str) -> list[dict]:
    out = []
    for fn in sorted(os.listdir(d)):
        if not fn.lower().endswith(".eml"):
            continue
        p = os.path.join(d, fn)
        with open(p, "rb") as fh:
            m = email.message_from_binary_file(fh, policy=email.policy.default)
        body = ""
        if m.is_multipart():
            plain = html_txt = ""
            for part in m.walk():
                ctype = part.get_content_type()
                if ctype == "text/plain" and not plain:
                    plain = str(part.get_content())
                elif ctype == "text/html" and not html_txt:
                    html_txt = strip_html(str(part.get_content()))
            # 纯文本太薄（占位/签名档）时并入 html 文本，否则用纯文本
            body = plain + "\n" + html_txt if len(plain.strip()) < 120 and html_txt else (plain or html_txt)
        else:
            body = m.get_content()
            if m.get_content_type() == "text/html":
                body = strip_html(str(body))
        def hdr(name):
            v = m.get(name)
            return str(v) if v is not None else ""
        frm = email.utils.parseaddr(hdr("From"))
        out.append({
            "env_id": "fixture:" + fn,
            "message_id": hdr("Message-ID").strip() or "fixture:" + fn,
            "subject": hdr("Subject"),
            "from_name": frm[0], "from_addr": frm[1],
            "to_addr": email.utils.parseaddr(hdr("To"))[1],
            "date": hdr("Date") or fn,
            "flags": [], "body": strip_html(str(body)),
        })
    return out


# --------------------------------------------------------------------------
# 规则分类
# --------------------------------------------------------------------------

RULES: list[tuple[str, re.Pattern]] = [
    ("verification_code", re.compile(
        r"验证码|校验码|动态码|动态口令|邮箱验证|登录验证|注册验证|手机验证|"
        r"verification\s*code|verify\s*(your|code)|security\s*code", re.I)),
    ("offer", re.compile(
        r"录用通知|拟录用|聘用通知|录取通知书|offer\s*(letter|通知|call)|"
        r"恭喜您?(通过|被录用)|录用函", re.I)),
    ("interview", re.compile(
        r"面试|面谈|约面|interview|视频面|电话面|群面|一面|二面|终面|HR面", re.I)),
    ("written_test", re.compile(
        r"笔试|机试|在线考试|统一笔试|考试通知|written\s*test|examination|online\s*test", re.I)),
    ("assessment", re.compile(
        r"测评|评测|人才测评|素质测评|能力测评|性格测评|综合测评|"
        r"assessment|aptitude\s*test|personality", re.I)),
    ("rejection", re.compile(
        r"很遗憾|未通过|感谢信|人才库|婉拒|未被录取|未进入面试|暂不匹配|"
        r"未能入选|不予录用|regret|unfortunately|not\s*(moving|proceed)|rejected", re.I)),
    ("application_ack", re.compile(
        r"投递成功|已收到|收到您?的?(简历|申请|投递)|申请已提交|报名成功|"
        r"确认收到|投递确认|感谢您的?投递|application\s*received|"
        r"thank\s*you\s*for\s*(your\s*)?apply|we\s*have\s*received", re.I)),
]
VERIFICATION_PRIORITY = {"verification_code": 0, "offer": 1, "interview": 2,
                         "written_test": 3, "assessment": 4, "rejection": 5,
                         "application_ack": 6, "other": 7}
RULES_DICT = dict(RULES)
NOISE_SENDER = re.compile(r"@(github\.com|noreply\.github\.com|linkedin\.com|.*\.linkedin\.com|google\.com|"
                          r"accounts\.google\.com|slack\.com|notion\.so|resend\.com|anthropic\.com|"
                          # 订阅类邮件平台：newsletter 正文里的 interview/面试 字样不是招聘进展
                          r"(.*\.)?substack\.com|(.*\.)?medium\.com|(.*\.)?beehiiv\.com|(.*\.)?mcsv\.net|"
                          r"(.*\.)?mailchimp\.com|(.*\.)?convertkit\.com|(.*\.)?ghost\.io|(.*\.)?x\.com|(.*\.)?twitter\.com"
                          r")$|notifications@")
MARKETING = re.compile(r"猎头|myzhiluren|zhiyeapp|校招启动|邀请你处理|如约开启|【智联推荐】|推荐.*校招|限时|直播", re.I)
RECRUIT_HINT = re.compile(
    r"招聘|校招|校园招聘|投递|应聘|职位|岗位|简历|hr@|hr\.|career|campus|recruit|"
    r"join\s*us|人才|入职|offer|面试|笔试|测评", re.I)


def earliest_hit(text: str, hits: list[str]) -> str | None:
    """正文里最先出现的类别——通常就是"当前要办的那一步"。"""
    best, pos = None, 1 << 30
    for k in hits:
        m = RULES_DICT[k].search(text)
        if m and m.start() < pos:
            best, pos = k, m.start()
    return best


def classify_rules(msg: dict) -> tuple[str | None, list[str], bool]:
    """返回 (kind, hits, needs_llm)。kind=None 或 needs_llm=True → 交 LLM。"""
    subject = msg.get("subject") or ""
    sender_addr = (msg.get("from_addr") or "").lower()
    if NOISE_SENDER.search(sender_addr):
        return "other", [], False                 # GitHub/LinkedIn 等通知信里的"interview/面试"字样不算招聘进展
    text = subject + "\n" + (msg.get("body") or "")[:3000]
    hits = [k for k, pat in RULES if pat.search(text)]
    if MARKETING.search(subject + " " + sender_addr) and not re.search(r"投递成功|已收到|感谢(你|您)投递", text):
        return "other", hits, False               # 猎头/平台群发的"AI 面试开启、邀请你处理"是拉新，不是真实邀约
    if not hits:
        # 一封没命中规则但看着像招聘域的邮件，宁多判不漏判
        sender = (msg.get("from_name") or "") + " " + (msg.get("from_addr") or "")
        if RECRUIT_HINT.search(subject + " " + sender):
            return None, [], True
        return "other", [], False
    if len(hits) == 1:
        return hits[0], hits, False
    # 验证码与其他类并存时，优先看其他类（验证码只是签名档/附加验证）
    non_vc = [h for h in hits if h != "verification_code"]
    if len(non_vc) == 1 and "verification_code" in hits:
        body = msg.get("body") or ""
        # 正文里有明显验证码样式（独立成行 4-8 位数字）才判验证码
        if not re.search(r"(?m)^\s*\d{4,8}\s*$", body):
            return non_vc[0], hits, False
        return None, hits, True
    # 多类命中：主题命中的那一类是主要目的（如"测评通知"正文顺带提笔试）
    subj_hits = [k for k, pat in RULES if pat.search(subject)]
    if len(subj_hits) == 1:
        return subj_hits[0], hits, False
    # 拒信必含"感谢投递"类措辞——rejection 比 application_ack 更具体，直接判拒信
    if "rejection" in hits and "application_ack" in hits:
        return "rejection", hits, False
    return None, hits, True


# --------------------------------------------------------------------------
# 抽取：公司 / 时间 / 链接 / 动作 / announcement_id
# --------------------------------------------------------------------------

def build_company_index(inst: str) -> tuple[dict[str, str], dict[str, list[int]]]:
    """返回 (alias→canonical_norm, canonical_norm→[announcement_ids])。

    来源：实例 tracker.md、state/registry.jsonl、仓库 announcements.jsonl、
    data/employer-aliases.yaml（可缺）。"""
    alias: dict[str, str] = {}
    ids: dict[str, list[int]] = {}

    def add(name, aid):
        k = norm_company(name)
        if not k:
            return
        alias.setdefault(k, k)
        if aid is not None:
            ids.setdefault(k, [])
            if aid not in ids[k]:
                ids[k].append(aid)

    # tracker.md：| 公司 | id | … 第一张表
    tp = os.path.join(inst, "tracker.md")
    if os.path.isfile(tp):
        for line in open(tp, encoding="utf-8"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 2 and cells[0] and cells[0] not in ("公司", "---") \
                    and not set(cells[0]) <= set("-"):
                m = re.match(r"^\d{3,}$", cells[1])
                if m:
                    add(cells[0], int(cells[1]))
    # registry.jsonl
    rp = os.path.join(inst, "state", "registry.jsonl")
    if os.path.isfile(rp):
        for ln in open(rp, encoding="utf-8"):
            if not ln.strip():
                continue
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            for nm in (r.get("employer"), r.get("brand"), r.get("employer_key")):
                if nm:
                    add(nm, None)
            for aid in r.get("announcement_ids") or []:
                if r.get("employer"):
                    add(r["employer"], aid)
    # announcements.jsonl
    ap = os.path.join(REPO, "data", "paperball", "announcements.jsonl")
    if os.path.isfile(ap):
        for ln in open(ap, encoding="utf-8"):
            if not ln.strip():
                continue
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            add(r.get("company"), r.get("announcement_id"))
    # 别名表（可缺；格式 {version, aliases: {公告名: 雇主主体名}}）
    yp = os.path.join(REPO, "data", "employer-aliases.yaml")
    if os.path.isfile(yp):
        try:
            data = yaml.safe_load(open(yp, encoding="utf-8")) or {}
            amap = data.get("aliases") if isinstance(data, dict) else data
            if isinstance(amap, dict):
                for a, c in amap.items():
                    if a and c:
                        alias[norm_company(str(a))] = norm_company(str(c))
        except yaml.YAMLError:
            pass
    return alias, ids


# 正文里的工具/平台名不是雇主（腾讯会议链接、飞书问卷等），匹配前屏蔽
PRODUCT_MASK = re.compile(
    r"腾讯会议|腾讯文档|企业微信|微信群|飞书|钉钉|牛客|智联招聘|前程无忧|实习僧|应届生求职网")


def match_company(msg: dict, extracted: str, alias: dict, ids: dict) -> tuple[str, int | None, list[int]]:
    """从抽取公司名/发件人/主题/正文里找台账公司。返回 (公司, announcement_id, 候选id)。"""
    # 可信度从高到低：LLM/规则抽取 > 主题 > 发件人显示名 > 正文前 800 字
    pools = [
        extracted or "",
        (msg.get("subject") or "") + " " + (msg.get("from_name") or ""),
        PRODUCT_MASK.sub(" ", (msg.get("body") or "")[:800]),
    ]
    best = ""
    for hay_raw in pools:
        hay = norm_company(hay_raw)
        for name in sorted(set(alias), key=len, reverse=True):
            if name and name in hay:
                best = alias.get(name, name)
                break
        if best:
            break
    if not best and extracted:
        best = alias.get(norm_company(extracted), norm_company(extracted))
    if not best:
        return (extracted or ""), None, []
    found = ids.get(best) or []
    aid = found[0] if len(set(found)) == 1 else None
    return best, aid, sorted(set(found))


DATE_PATTERNS = [
    # 日期与时间之间常夹着"（周一）""(北京时间)"等括注，要跳过
    re.compile(r"(20\d{2})\s*[年\-/\.]\s*(\d{1,2})\s*[月\-/\.]\s*(\d{1,2})\s*[日号]?\s*"
               r"(?:[（(][^）)]{0,8}[）)]\s*)?"
               r"(?:(\d{1,2})\s*[:：点时]\s*(\d{1,2})?)?"),
    re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]\s*"
               r"(?:[（(][^）)]{0,8}[）)]\s*)?"
               r"(?:(\d{1,2})\s*[:：点时]\s*(\d{1,2})?)?"),
    re.compile(r"(?<![\d/])(20\d{2}-\d{1,2}-\d{1,2})[T\s](\d{1,2}):(\d{2})(?::(\d{2}))?"),
]
DUE_HINT = re.compile(r"截止|之前|前完成|有效期|有效|(?<!邀)请于|勿晚于|deadline|by\s|内完成|前登录|前参加|逾期")
START_HINT = re.compile(r"开始时间|考试时间|面试时间|定于|安排在|于.*举行|start|开始|时间[:：]|场次")
CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
          "八": 8, "九": 9, "十": 10, "十五": 15, "二十": 20, "三十": 30}
REL_RE = re.compile(r"(\d+|[一两二三四五六七八九十]{1,3})\s*(个)?\s*(小时|h|H|天|日|工作日)\s*(?:之?内|以内)")


def _mk(y, mo, d, h, mi, default_h, default_mi=0):
    try:
        return dt.datetime(int(y), int(mo), int(d),
                           int(h) if h else default_h,
                           int(mi) if mi else default_mi, tzinfo=CST)
    except ValueError:
        return None


def msg_datetime(msg: dict) -> dt.datetime | None:
    """邮件发送时刻：兼容 ISO（himalaya envelope）与 RFC2822（eml Date 头）。"""
    s = str(msg.get("date") or "").strip()
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=CST)
    except ValueError:
        pass
    try:
        t = email.utils.parsedate_to_datetime(s)
        return t.astimezone(CST) if t.tzinfo else t.replace(tzinfo=CST)
    except (TypeError, ValueError):
        return None


def extract_times(msg: dict, ref: dt.datetime | None = None) -> tuple[str | None, str | None]:
    """从主题+正文抽时间，返回 (due_at, start_at) ISO；截止类默认 23:59，开始类默认 09:00。

    "N 小时/天内"相对时间以**邮件发送时刻**为基准（不是处理时刻）。"""
    text = (msg.get("subject") or "") + "\n" + (msg.get("body") or "")[:3000]
    if ref is None:
        ref = msg_datetime(msg) or dt.datetime.now(CST)
    year = ref.year
    dues, starts = [], []
    for m in DATE_PATTERNS[0].finditer(text):
        y, mo, d, h, mi = m.groups()
        ctx = text[max(0, m.start() - 20):m.start()]
        is_due = bool(DUE_HINT.search(ctx)) and not START_HINT.search(ctx)
        t = _mk(y, mo, d, h, mi, 23 if is_due else 9, 59 if (is_due and not h) else 0)
        if t:
            (dues if is_due else starts).append(t)
    for m in DATE_PATTERNS[1].finditer(text):
        mo, d, h, mi = m.groups()
        ctx = text[max(0, m.start() - 20):m.start()]
        is_due = bool(DUE_HINT.search(ctx)) and not START_HINT.search(ctx)
        t = _mk(year, mo, d, h, mi, 23 if is_due else 9, 59 if (is_due and not h) else 0)
        if t and t < ref - dt.timedelta(days=120):
            t = _mk(year + 1, mo, d, h, mi, 23 if is_due else 9, 59 if (is_due and not h) else 0)
        if t:
            (dues if is_due else starts).append(t)
    for m in DATE_PATTERNS[2].finditer(text):
        ymd, h, mi, _s = m.groups()
        ctx = text[max(0, m.start() - 20):m.start()]
        is_due = bool(DUE_HINT.search(ctx)) and not START_HINT.search(ctx)
        try:
            t = dt.datetime.fromisoformat("%s %s:%s" % (ymd, h, mi)).replace(tzinfo=CST)
        except ValueError:
            continue
        (dues if is_due else starts).append(t)
    # 区间写法：A 到 B（-、–、~、至、到）→ A 是开始、B 是截止，覆盖上面按上下文的猜测
    rng = re.compile(
        r"((?:20\d{2}\s*[年/-]\s*)?\d{1,2}\s*[月/-]\s*\d{1,2}\s*[日号]?\s*"
        r"(?:\d{1,2}\s*[:：]\s*\d{1,2})?)\s*[-–—~～至到]\s*"
        r"((?:20\d{2}\s*[年/-]\s*)?\d{1,2}\s*[月/-]\s*\d{1,2}\s*[日号]?\s*"
        r"(?:\d{1,2}\s*[:：]\s*\d{1,2})?)")
    for rm in rng.finditer(text):
        pair = []
        for side, default_h, default_mi in ((rm.group(1), 0, 0), (rm.group(2), 23, 59)):
            dm = re.search(r"(?:(20\d{2})\s*[年/-]\s*)?(\d{1,2})\s*[月/-]\s*(\d{1,2})"
                           r"\s*[日号]?\s*(?:(\d{1,2})\s*[:：]\s*(\d{1,2}))?", side)
            if not dm:
                pair = []
                break
            y, mo, d, h, mi = dm.groups()
            pair.append(_mk(y or ref.year, mo, d, h, mi, default_h, default_mi))
        if len(pair) == 2 and all(pair) and pair[0] <= pair[1]:
            starts.append(pair[0])
            dues.append(pair[1])

    # 带标签的时间优先："测评时间：2026年9月21日（周一）19:00-22:00"
    label = re.compile(r"(测评|笔试|考试|面试|作答|答题)\s*时间\s*[:：]")
    windows = [(lm.end(), lm.end() + 80) for lm in label.finditer(text)]
    if windows:
        def in_window(pos):
            return any(a <= pos <= b for a, b in windows)
        lab_starts, lab_dues = [], []
        for pat_i, pat in enumerate(DATE_PATTERNS[:2]):
            for lm in pat.finditer(text):
                if not in_window(lm.start()):
                    continue
                g = lm.groups()
                y, mo, d, h, mi = (g if pat_i == 0 else (ref.year,) + g)
                t = _mk(y, mo, d, h, mi, 9, 0)
                if t:
                    lab_starts.append(t)
        if lab_starts:
            starts = lab_starts          # 标签内的时间说了算
            if lab_dues:
                dues = lab_dues

    m = REL_RE.search(text)
    if m and DUE_HINT.search(text[max(0, m.start() - 25):m.end() + 15]):
        raw = m.group(1)
        n = int(raw) if raw.isdigit() else CN_NUM.get(raw, 0)
        unit = m.group(3)
        if not n:
            n = None
        if n:
            delta = dt.timedelta(hours=n) if unit in ("小时", "h", "H") else dt.timedelta(days=n)
            dues.append(ref + delta)
    due = min(dues).isoformat(timespec="seconds") if dues else None
    start = min(starts).isoformat(timespec="seconds") if starts else None
    return due, start


def extract_links(body: str) -> tuple[str | None, list[str]]:
    links = []
    for u in URL_RE.findall(body or ""):
        u = u.rstrip(".,;:!?）。，；")
        if re.search(r"(?i)unsubscribe|privacy|policy|mailto:", u):
            continue
        if u not in links:
            links.append(u)
    primary = next((u for u in links if LINK_HINT.search(u)), links[0] if links else None)
    return primary, links[:10]


FROM_NOISE = re.compile(
    r"(?i)招聘|校招|校园招聘|人才招聘|人事|人力|HR|team|官方|平台|中心|考试|测评|"
    r"通知|系统|人才|组|委员会|部|办公室|项目组")


TAIL_NOISE = re.compile(r"(?:第[一二三四五六七八九十\d]+批|线上|在线|官方|校园|全球|全国)$")


GENERIC_COMPANY = re.compile(r"^(?:笔试|面试|测评|考试|在线|线上|温馨|提醒|通知|邀请|邀约|安排|确认|结果|反馈|"
                             r"校招|校园|招聘|人才|HR|系统|官方|recruiting|careers?|talent|noreply|no-reply|\s|[·:：\-_])+$", re.I)
MAIL_PROVIDERS = {"gmail", "outlook", "hotmail", "qq", "163", "126", "foxmail", "icloud", "yahoo", "sina",
                  "mokahr", "zhiye", "beisen", "feishu", "hackerrank", "nowcoder", "mtasv", "pstmrk", "sendgrid", "amazonses"}


def clean_company(name: str, msg: dict) -> str:
    """公司名兜底：抽出来的是「笔试温馨提醒」这种通用词时，退回发件域名；顺手去掉 Recruiting/Careers 这类尾巴。"""
    name = re.sub(r"(?i)\s*(recruiting|recruitment|careers?|talent(\s*acquisition)?|campus)$", "", (name or "").strip()).strip()
    if name and not GENERIC_COMPANY.match(name):
        return name
    addr = (msg.get("from_addr") or (msg.get("from") or {}).get("addr") or "") if isinstance(msg.get("from"), dict) else (msg.get("from_addr") or "")
    labels = addr.rsplit("@", 1)[-1].lower().split(".")
    core = [x for x in labels[:-1] if x not in ("mail", "email", "hr", "campus", "career", "careers", "jobs", "com", "co")]
    if core and core[-1] not in MAIL_PROVIDERS:
        return core[-1].upper() if core[-1].isascii() and len(core[-1]) <= 8 else core[-1]
    return ""


def guess_company(msg: dict) -> str:
    """主题 > 发件人显示名 > 正文开头，粗抽一个公司名供索引匹配。"""
    subject = msg.get("subject") or ""
    for pat in (r"【([^】]{2,20})】",
                r"^([\u4e00-\u9fa5A-Za-z0-9]{2,25}?)(?:在线测评|线上测评|线上笔试|在线笔试|笔试|面试|测评|校招|校园招聘|招聘|人才)",
                r"([\u4e00-\u9fa5A-Za-z0-9]{2,25}?)(?:校园招聘|校招|招聘)(?:面试|笔试|测评|通知|邀请)"):
        m = re.search(pat, subject)
        if m:
            return TAIL_NOISE.sub("", m.group(1).strip())
    c = FROM_NOISE.sub("", msg.get("from_name") or "").strip(" ·-_—")
    if len(c) >= 2:
        return c
    for pat in (r"【([^】]{2,20})】", r"\[([^\[\]]{2,20})\]",
                r"([\u4e00-\u9fa5A-Za-z0-9]{2,25}?)(?:招聘|校招|校园招聘)(?:组|委员会|团队|HR)?[：:，。\n]"):
        m = re.search(pat, (msg.get("body") or "")[:600])
        if m:
            return m.group(1).strip()
    return ""


# --------------------------------------------------------------------------
# LLM 兜底（沿用 judge.py 的三引擎封装）
# --------------------------------------------------------------------------

LLM_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["results"],
    "properties": {"results": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["key", "kind", "company", "job", "due_at", "start_at", "link", "action"],
        "properties": {
            "key": {"type": "string"},
            "kind": {"type": "string", "enum": list(KINDS)},
            "company": {"type": "string"},
            "job": {"type": "string"},
            "due_at": {"type": "string"},
            "start_at": {"type": "string"},
            "link": {"type": "string"},
            "action": {"type": "string"},
        }}}},
}


def build_llm_prompt(msgs: list[dict]) -> str:
    seg = ["你是校招邮件分拣器。把下面每封邮件分类并抽取关键信息。", "",
           "## 分类（kind）",
           "assessment=测评/人才测评 · written_test=笔试/在线考试 · interview=面试邀约 · "
           "offer=录用通知 · rejection=拒信/感谢信 · application_ack=投递成功确认/已收简历 · "
           "verification_code=验证码/登录验证 · other=营销/通知/与求职无关", "",
           "## 输出 JSON",
           '{"results":[{"key":"<邮件key>","kind":"…","company":"公司名",'
           '"job":"岗位（没有则空）","due_at":"截止时间 ISO，如 2026-10-01T18:00:00+08:00（没有则空）",'
           '"start_at":"开始/考试时间 ISO（没有则空）","link":"最重要的入口链接（没有则空）",'
           '"action":"需要收件人做什么，一句话"}]}',
           "时间一律 Asia/Shanghai（+08:00）；只输出 JSON，不要用工具，不要读写文件。", "",
           "## 邮件"]
    for m in msgs:
        seg.append("### key=%s" % m["key"])
        seg.append("发件人：%s <%s>" % (m.get("from_name") or "", m.get("from_addr") or ""))
        seg.append("主题：%s" % (m.get("subject") or ""))
        seg.append("日期：%s" % (m.get("date") or ""))
        seg.append("正文（截断）：\n%s" % (m.get("body") or "")[:LLM_EXCERPT])
        seg.append("")
    return "\n".join(seg)


def run_engine(engine: str, prompt: str, model: str | None, workdir: str, timeout: int = 600) -> str:
    if engine == "codex":
        schema_f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(LLM_SCHEMA, schema_f)
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


def parse_json(text: str):
    text = (text or "").strip()
    for pat in (r"```(?:json)?\s*(\{.*\}|\[.*\])\s*```", r"(\{.*\})", r"(\[.*\])"):
        m = re.search(pat, text, re.S)
        if m:
            try:
                return json.loads(m.group(1))
            except ValueError:
                continue
    return None


def llm_classify(msgs: list[dict], engine: str, model: str | None, workdir: str) -> dict[str, dict]:
    """对拿不准的邮件批量问 LLM，返回 key→抽取结果。失败返回 {}。"""
    out: dict[str, dict] = {}
    for i in range(0, len(msgs), LLM_BATCH):
        batch = msgs[i:i + LLM_BATCH]
        try:
            raw = run_engine(engine, build_llm_prompt(batch), model, workdir)
            parsed = parse_json(raw)
            items = parsed.get("results") if isinstance(parsed, dict) else parsed
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            print("WARN: LLM 分类失败（%s），按规则优先级兜底" % str(e)[:160], file=sys.stderr)
            items = None
        for m in batch:
            out[m["key"]] = {}
        if isinstance(items, list):
            by_key = {str(x.get("key")): x for x in items if isinstance(x, dict)}
            for m in batch:
                it = by_key.get(m["key"])
                if not it or it.get("kind") not in KINDS:
                    continue
                out[m["key"]] = {
                    "kind": it["kind"],
                    "company": str(it.get("company") or ""),
                    "job": str(it.get("job") or ""),
                    "due_at": _norm_iso(it.get("due_at"), end_of_day=True),
                    "start_at": _norm_iso(it.get("start_at")),
                    "link": str(it.get("link") or ""),
                    "action": str(it.get("action") or ""),
                }
    return out


def _norm_iso(v, end_of_day: bool = False) -> str | None:
    s = str(v or "").strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        t = dt.datetime.fromisoformat(s)
    except ValueError:
        m = re.match(r"(20\d{2}-\d{1,2}-\d{1,2})[ T](\d{1,2})?:?(\d{2})?", s)
        if not m:
            return None
        t = dt.datetime.fromisoformat("%s %s:%s" % (m.group(1), m.group(2) or "9", m.group(3) or "0"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=CST)
    # LLM 常给纯日期的 00:00——截止语义上是当天结束
    if end_of_day and t.hour == 0 and t.minute == 0 and t.second == 0:
        t = t.replace(hour=23, minute=59)
    return t.astimezone(CST).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def msg_key(msg: dict) -> str:
    return msg.get("message_id") or ("env:" + str(msg.get("env_id")))


def notify_assessment(inst: str, repo: str, rec: dict, dry_run: bool) -> None:
    """assessment/written_test/interview/offer → notify.py assessment。"""
    ncfg = os.path.join(inst, "notify.yaml")
    kind_label = KIND_LABEL.get(rec["kind"], rec["kind"])
    title = "%s · %s" % (kind_label, rec.get("company") or "未知公司")
    when = rec.get("due_at") or rec.get("start_at") or ""
    when_label = "截止" if rec.get("due_at") else "时间"   # 只有开始时间（如笔试开考）时不写"截止"
    body = "%s%s" % (rec.get("action") or kind_label,
                     " · %s %s" % (when_label, when[:16].replace("T", " ")) if when else "")
    cmd = ["uv", "run", "--script", os.path.join(repo, "host", "notify.py"), "assessment",
           "--instance", inst, "--company", rec.get("company") or "",
           "--title", title, "--body", body, "--date", when]
    if rec.get("announcement_id"):
        cmd += ["--id", str(rec["announcement_id"])]
    if rec.get("link"):
        cmd += ["--url", rec["link"]]
    if dry_run:
        # 严格只读：不真正调用 notify.py（它会创建 .notify.lock 写实例）
        print("[dry-run] 通知：%s | %s" % (title, body))
        return
    if not os.path.isfile(ncfg):
        print("通知（跳过：实例无 notify.yaml）：%s | %s" % (title, body))
        return
    try:
        subprocess.run(cmd, timeout=30, cwd=repo)
    except (OSError, subprocess.TimeoutExpired) as e:
        print("WARN: notify 调用失败 %s" % e, file=sys.stderr)


def poll(args) -> int:
    inst = os.path.abspath(os.path.expanduser(args.instance))
    state = os.path.join(inst, "state")
    seen_path = os.path.join(state, "inbox", "seen.json")
    seen = read_json(seen_path, {})

    # ---- 收信 ----
    if args.fixtures:
        msgs = load_fixtures(args.fixtures)
        print("fixtures：%d 封（%s）" % (len(msgs), args.fixtures), file=sys.stderr)
    else:
        since = parse_since(args.since)
        envs = list_himalaya(args.account, args.folder, since, args.limit)
        print("收件箱 %s 之后共 %d 封（account=%s folder=%s）"
              % (since, len(envs), args.account, args.folder), file=sys.stderr)
        msgs = []
        for env in envs:
            key_guess = str(env.get("id"))
            # 先按 env_id 粗查 seen，避免重复下载正文；message-id 为准的精查在分类后
            try:
                msgs.append(fetch_himalaya(env, args.account, args.folder))
            except (RuntimeError, subprocess.TimeoutExpired) as e:
                # 单封读超时/失败只跳过这封，不中断整轮（超大附件邮件会拖到 90s 上限）
                print("WARN: 读取 %s 失败：%s" % (key_guess, str(e)[:120]), file=sys.stderr)

    # ---- 过滤已处理 ----
    fresh = []
    for m in msgs:
        m["key"] = msg_key(m)
        if m["key"] in seen and not getattr(args, "redo", False):
            continue
        fresh.append(m)
    skipped = len(msgs) - len(fresh)
    if skipped:
        print("跳过已处理 %d 封" % skipped, file=sys.stderr)

    alias, id_index = build_company_index(inst)

    # ---- 规则分类 ----
    ambiguous = []
    for m in fresh:
        kind, hits, needs_llm = classify_rules(m)
        m["rule_hits"] = hits
        m["needs_llm"] = needs_llm
        if kind is None or needs_llm:
            ambiguous.append(m)
        m["kind"] = kind

    # ---- LLM 兜底 ----
    llm_out: dict[str, dict] = {}
    if ambiguous and not args.no_llm:
        llm_out = llm_classify(ambiguous, args.engine, args.model, inst)
    for m in ambiguous:
        res = llm_out.get(m["key"]) or {}
        if res.get("kind"):
            m["kind"] = res["kind"]
            m["via"] = "llm"
            m["llm"] = res
        else:
            text = (m.get("subject") or "") + "\n" + (m.get("body") or "")[:3000]
            m["kind"] = m["kind"] or earliest_hit(text, m["rule_hits"]) or "other"
            m["via"] = "rules_fallback"

    # ---- 抽取 + 输出 ----
    counts: dict[str, int] = {}
    rows = []
    new_steps = []
    for m in fresh:
        kind = m["kind"]
        counts[kind] = counts.get(kind, 0) + 1
        llm = m.get("llm") or {}
        if kind == "verification_code":
            excerpt, links, primary = "", [], None
            company, job, due, start, action = guess_company(m), "", None, None, ""
        else:
            excerpt = re.sub(r"\s+", " ", m.get("body") or "")[:BODY_EXCERPT]
            primary, links = extract_links(m.get("body") or "")
            due, start = extract_times(m)
            company = guess_company(m)
            job = ""
            action = {"assessment": "完成在线测评", "written_test": "参加笔试",
                      "interview": "参加面试", "offer": "查看录用通知"}.get(kind, "")
        if llm:
            company = llm.get("company") or company
            job = llm.get("job") or job
            due = llm.get("due_at") or due
            start = llm.get("start_at") or start
            action = llm.get("action") or action
            if llm.get("link"):
                primary = llm["link"]
        company = clean_company(company, m)
        company, aid, aid_cands = match_company(m, company, alias, id_index)

        rec = {
            "message_id": m.get("message_id"), "envelope_id": m.get("env_id"),
            "account": args.account, "folder": args.folder,
            "date": m.get("date"), "from": {"name": m.get("from_name"), "addr": m.get("from_addr")},
            "subject": m.get("subject"), "kind": kind,
            "via": m.get("via") or ("llm" if m["key"] in llm_out and llm_out[m["key"]] else "rules"),
            "rule_hits": m.get("rule_hits") or [],
            "company": company, "job": job, "due_at": due, "start_at": start,
            "link": primary, "links": links, "action": action,
            "announcement_id": aid, "announcement_id_candidates": aid_cands if not aid else [],
            "excerpt": excerpt, "excerpt_chars": len(excerpt),
            "processed_at": now_iso(),
        }
        # 落盘单封档（验证码也记元信息但无正文）
        day = (msg_datetime(m) or dt.datetime.now(CST)).date().isoformat()
        h = sha(m["key"])
        rec["file"] = "state/inbox/%s/%s.json" % (day, h)
        rows.append(rec)

        if kind in ACTIONABLE:
            step = {"id": "ns-" + sha(m["key"] + "|" + kind),
                    "kind": kind, "company": company,
                    "announcement_id": aid, "job": job,
                    "due_at": due, "start_at": start, "link": primary,
                    "action": action or KIND_LABEL[kind], "source_msg": m["key"],
                    "created_at": now_iso(), "status": "open"}
            new_steps.append(step)
            rec["next_step_id"] = step["id"]

    # ---- 写状态（dry-run 只打印） ----
    expected = {}
    if args.fixtures:
        ep = os.path.join(args.fixtures, "expected.json")
        if os.path.isfile(ep):
            expected = read_json(ep, {})
    n_right = 0
    print("\n== 分拣结果（%d 封新邮件） ==" % len(rows))
    for rec in rows:
        fn = rec["envelope_id"].replace("fixture:", "")
        mark = ""
        if fn in expected:
            mark = "  ✓" if expected[fn] == rec["kind"] else "  ✗期望=%s" % expected[fn]
            n_right += expected[fn] == rec["kind"]
        print("  [%s] %s | %s | 公司=%s | due=%s | start=%s | via=%s%s" % (
            KIND_LABEL.get(rec["kind"], rec["kind"]), rec["subject"][:42],
            (rec["from"]["name"] or rec["from"]["addr"])[:20],
            rec["company"] or "—",
            (rec["due_at"] or "—")[:16], (rec["start_at"] or "—")[:16],
            rec["via"], ("  " + fn + mark) if args.fixtures else mark))
    print("汇总：" + "，".join("%s %d" % (KIND_LABEL.get(k, k), v)
                             for k, v in sorted(counts.items())), end="")
    print("（另跳过已处理 %d）\n" % skipped if skipped else "\n")
    if expected:
        if rows:
            print("准确率：%d/%d（期望覆盖 %d 封）" % (n_right, len(rows), len(expected)))
        else:
            print("本次无新邮件（全部在 seen.json），准确率需清实例 state/inbox 后重跑")

    if args.dry_run:
        print("[dry-run] 不写 state、不发通知。%d 条待办会被创建。"
              % len(new_steps))
        for s in new_steps:
            print("[dry-run] 待办：%s · %s · due=%s" % (s["kind"], s["company"], s["due_at"]))
            notify_assessment(inst, REPO, s, dry_run=True)
        return 0

    for rec in rows:
        atomic_write(os.path.join(inst, rec["file"]),
                     json.dumps(rec, ensure_ascii=False, indent=1) + "\n")
        seen[rec["message_id"] or ("env:" + str(rec["envelope_id"]))] = {
            "at": rec["processed_at"], "kind": rec["kind"], "file": rec["file"]}

    if new_steps:
        ns_path = os.path.join(state, "next-steps.jsonl")
        existing = []
        if os.path.isfile(ns_path):
            existing = [json.loads(ln) for ln in open(ns_path, encoding="utf-8") if ln.strip()]
        with AppendLock(ns_path):
            for s in new_steps:
                # 同公司同类旧 open 待办标记 superseded（新邮件口径优先）
                for old in existing:
                    if old.get("status") == "open" and old.get("kind") == s["kind"] \
                            and norm_company(old.get("company") or "") == norm_company(s["company"] or "") \
                            and old.get("id") != s["id"]:
                        old["status"] = "superseded"
                        old["superseded_by"] = s["id"]
                        with open(ns_path, "a", encoding="utf-8") as fh:
                            fh.write(json.dumps(old, ensure_ascii=False) + "\n")
                prev = None
                for e in existing:
                    if e.get("id") == s["id"]:
                        prev = e
                key = ("due_at", "start_at", "link", "action", "kind", "company", "announcement_id")
                changed = prev is not None and any(prev.get(k) != s.get(k) for k in key)
                if prev is None or changed:
                    with open(ns_path, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps(s, ensure_ascii=False) + "\n")
                    existing.append(s)
    atomic_write(seen_path, json.dumps(seen, ensure_ascii=False, indent=1) + "\n")

    for s in new_steps:
        notify_assessment(inst, REPO, s, dry_run=False)

    print("写入：%d 封档 + %d 条待办 → %s" % (len(rows), len(new_steps), state))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="收信分拣：测评/笔试/面试 → 待办 + 通知")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("poll", help="增量读收件箱")
    p.add_argument("--instance", default=os.environ.get("CAMPUS_INSTANCE"))
    p.add_argument("--since", default="2d", help="2d / 12h / 1w / YYYY-MM-DD")
    p.add_argument("--redo", action="store_true",
                   help="忽略 seen.json 重新处理（改了抽取规则后回补历史邮件）")
    p.add_argument("--account", default="gmail")
    p.add_argument("--folder", default="INBOX")
    p.add_argument("--engine", choices=["codex", "devin", "claude"], default="codex")
    p.add_argument("--model", default=None)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--no-llm", action="store_true", help="规则拿不准时按优先级兜底，不调 LLM")
    p.add_argument("--dry-run", action="store_true", help="只打印分类结果，不写状态不发通知")
    p.add_argument("--fixtures", default=None, help="离线 .eml 目录（不碰邮箱）")
    args = ap.parse_args()

    if args.cmd == "poll":
        if not args.fixtures:
            if not args.instance:
                ap.error("需要 --instance 或 $CAMPUS_INSTANCE")
            if not os.path.isdir(args.instance):
                ap.error("实例目录不存在：%s" % args.instance)
        else:
            args.instance = args.instance or os.environ.get("CAMPUS_INSTANCE") or "."
        return poll(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
