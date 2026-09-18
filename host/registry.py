#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6"]
# ///
"""registry.py · 已投登记（防重投），契约见 docs/contracts.md §8.4。

    uv run --script host/registry.py add --instance $CAMPUS_INSTANCE \
        --employer 示例集团 --brand 示例子品牌 --portal moka --account <sha256前12位> \
        --ids 10001 --job 后端开发工程师 [--status submitted] [--at ISO]
    uv run --script host/registry.py check 示例子品牌 [--portal moka] [--instance DIR]
    uv run --script host/registry.py import-tracker /path/to/tracker.md [--instance DIR] [--dry-run]
    uv run --script host/registry.py list [--instance DIR]

存储：$CAMPUS_INSTANCE/state/registry.jsonl，一行一次投递：
  {employer, employer_key, brand, portal, account, announcement_ids, job, status, at}
- employer_key：去掉"股份有限公司/集团/校招"等后缀、括号内容后的主体名；别名表 data/employer-aliases.yaml
- dispatcher 派单前必查：同 employer_key + 同 portal 已有 submitted/verified → 不派，作业标 skipped
- 追加写用 flock + mkdir 双层锁（同 web/app/bank.py，跨宿主机/容器挂载都安全）

check 退出码：命中 0，未命中 1，用法/文件错误 2。输出只打印匹配行，不回显 account。
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import sys
import time

import yaml

REPO = os.environ.get("CAMPUS_REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY_NAME = "registry.jsonl"
# 归一化时剥掉的公司名尾巴，长的在前避免残留（"股份有限公司"先于"股份"）
STRIP_SUFFIXES = ["股份有限公司", "有限责任公司", "集团股份有限公司", "科技有限公司", "集团有限公司",
                  "有限公司", "股份公司", "集团", "股份", "校招", "校园招聘", "公司"]
PAREN_RE = re.compile(r"[（(][^)）]*[)）]")
WS_RE = re.compile(r"\s+")
DIGITS_RE = re.compile(r"\d{4,}")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# tracker「门户/方式」列 → registry.portal 短名。映射不上就取括号前的文本原样小写。
PORTAL_MAP = {
    "北森": "beisen", "北森zhiye": "beisen", "moka": "moka", "飞书": "feishu", "飞书招聘": "feishu",
    "飞书ats": "feishu", "智联": "zhilian", "智联校招": "zhilian", "智联scrd": "zhilian",
    "hotjob": "hotjob", "wejob": "wejob", "牛客": "nowcoder",
    "海康门户": "hik", "海康": "hik", "泛微门户": "weaver", "泛微": "weaver",
    "官网": "official", "官网表单": "official", "自有门户": "official", "用户自投": "self",
    "国家能源同门户": "official", "邮件": "email", "邮箱": "email",
}
ACTIVE_STATUSES = {"submitted", "verified"}      # 已投成的登记；再投同一雇主主体算重复


class RegistryError(Exception):
    pass


# --------------------------------------------------------------------------
# 归一化
# --------------------------------------------------------------------------

def norm_text(s: str) -> str:
    return WS_RE.sub("", (s or "").strip().replace("（", "(").replace("）", ")"))


def strip_suffixes(name: str) -> str:
    """反复剥公司名尾巴；剥完括号内容再剥一轮（"XX(子公司)有限公司"这种）。"""
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


def load_aliases(path: str | None = None) -> dict[str, str]:
    """data/employer-aliases.yaml：品牌/子公司公告名 → 雇主主体名。键值都归一化。"""
    path = path or os.path.join(REPO, "data", "employer-aliases.yaml")
    if not os.path.isfile(path):
        return {}
    data = yaml.safe_load(open(path, encoding="utf-8")) or {}
    out = {}
    for k, v in (data.get("aliases") or {}).items():
        out[strip_suffixes(norm_text(str(k))).lower()] = strip_suffixes(norm_text(str(v))).lower()
    return out


def employer_key(name: str, aliases: dict[str, str] | None = None) -> str:
    """主体键：括号/后缀剥掉 → 别名表 → 小写。空名返回 ""。"""
    base = strip_suffixes(norm_text(name)).lower()
    if not base:
        return ""
    if aliases is None:
        aliases = load_aliases()
    return aliases.get(base, base)


def norm_portal(s: str) -> str:
    """门户列 → 短名；映射不上用括号前文本小写，只留字母数字。"""
    t = norm_text(s).split("(")[0].lower()
    t = PORTAL_MAP.get(t, t)
    return re.sub(r"[^a-z0-9一-鿿]+", "", t)


def account_hash(account: str) -> str:
    return hashlib.sha256((account or "").strip().encode("utf-8")).hexdigest()[:12] if account else ""


SHA12_RE = re.compile(r"^[0-9a-f]{12}$")


def norm_account(account: str) -> str:
    """account 只存 sha256 前 12 位；传入已是哈希前缀则原样保留，否则先哈希再存。"""
    a = (account or "").strip()
    if not a:
        return ""
    return a if SHA12_RE.match(a) else account_hash(a)


# --------------------------------------------------------------------------
# 读写
# --------------------------------------------------------------------------

def registry_path(instance: str) -> str:
    return os.path.join(instance, "state", REGISTRY_NAME)


class DirLock:
    """mkdir 锁（virtiofs 上原子）+ flock 锁（同侧并发）。STALE 秒未释放视为崩溃遗留。"""

    STALE = 60
    WAIT = 10

    def __init__(self, path):
        self.dir = path + ".lockdir"
        self.lockfile = path + ".lock"
        self.fh = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.dir), exist_ok=True)
        self.fh = open(self.lockfile, "a+")
        fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)
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
                    raise RegistryError("registry 被占用超过 %d 秒（%s）" % (self.WAIT, self.dir))
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            os.rmdir(self.dir)
        except OSError:
            pass
        if self.fh:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            self.fh.close()


def load_registry(instance: str) -> list[dict]:
    path = registry_path(instance)
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


def append_record(instance: str, rec: dict) -> None:
    path = registry_path(instance)
    with DirLock(path):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def hit(records: list[dict], ekey: str, portal: str | None = None,
        statuses: set[str] = ACTIVE_STATUSES) -> list[dict]:
    """同 employer_key + 同 portal 且 status 在 statuses 内的记录。portal=None 匹配所有门户。"""
    pkey = norm_portal(portal) if portal else None
    return [r for r in records
            if r.get("employer_key") == ekey
            and (pkey is None or norm_portal(r.get("portal") or "") == pkey)
            and (not statuses or r.get("status") in statuses)]


def add(instance: str, employer: str, brand: str = "", portal: str = "", account: str = "",
        announcement_ids: list | None = None, job: str = "", status: str = "submitted",
        at: str | None = None, aliases: dict | None = None) -> dict:
    """追加一条已投登记；同一 (employer_key, portal, id集合) 已有同状态记录时不重复写，返回已有记录。"""
    if not employer:
        raise RegistryError("add 需要 --employer")
    ekey = employer_key(employer, aliases)
    ids = sorted({int(x) for x in (announcement_ids or []) if str(x).strip().isdigit()})
    rec = {"employer": employer, "employer_key": ekey, "brand": brand or "",
           "portal": norm_portal(portal) or portal or "", "account": norm_account(account),
           "announcement_ids": ids, "job": job or "", "status": status,
           "at": at or dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    with DirLock(registry_path(instance)):
        for old in load_registry(instance):
            same = (old.get("employer_key") == ekey
                    and norm_portal(old.get("portal") or "") == norm_portal(rec["portal"])
                    and old.get("status") == status
                    and ids and sorted(old.get("announcement_ids") or []) == ids)
            if same:
                return old
        with open(registry_path(instance), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


# --------------------------------------------------------------------------
# tracker 回填
# --------------------------------------------------------------------------

def parse_tracker(path: str) -> list[dict]:
    """读 tracker.md 第一张含 id 列的表，返回行字典列表（公司/id/岗位/门户方式/状态/关键日期/记录）。"""
    lines = open(path, encoding="utf-8").read().splitlines()
    rows = []
    for i, line in enumerate(lines[:-1]):
        if not line.lstrip().startswith("|"):
            continue
        nxt = lines[i + 1]
        if not re.match(r"^\s*\|?\s*:?-{2,}", nxt):
            continue
        headers = [c.strip() for c in line.strip().strip("|").split("|")]
        if "id" not in headers or "公司" not in headers:
            continue
        for row in lines[i + 2:]:
            if not row.lstrip().startswith("|"):
                break
            cells = [c.strip() for c in row.strip().strip("|").split("|")]
            if len(cells) < len(headers):
                cells += [""] * (len(headers) - len(cells))
            rows.append(dict(zip(headers, cells)))
        break
    return rows


def import_tracker(instance: str, tracker: str, dry_run: bool = False) -> tuple[int, int]:
    """把 tracker 里状态含「已投递」的行回填进 registry。返回 (新增数, 跳过数)。"""
    aliases = load_aliases()
    existing = load_registry(instance)
    seen = {(r.get("employer_key"), tuple(sorted(r.get("announcement_ids") or []))) for r in existing}
    added = skipped = 0
    for row in parse_tracker(tracker):
        if "已投递" not in (row.get("状态") or ""):
            continue
        company = row.get("公司") or ""
        if not company:
            skipped += 1
            continue
        ekey = employer_key(company, aliases)
        ids = [int(x) for x in DIGITS_RE.findall(row.get("id") or "")]
        key = (ekey, tuple(sorted(ids)))
        if key in seen:
            skipped += 1
            continue
        m = DATE_RE.search(row.get("关键日期") or "")
        brand = PAREN_RE.search(company)
        rec = {"employer": strip_suffixes(norm_text(company)) or norm_text(company),
               "employer_key": ekey,
               "brand": brand.group(0).strip("()") if brand else "",
               "portal": norm_portal(row.get("门户/方式") or row.get("门户") or ""),
               "account": "", "announcement_ids": ids,
               "job": re.sub(r"\*\*", "", row.get("岗位") or "")[:80],
               "status": "submitted",
               "at": (m.group(0) + "T12:00:00+08:00") if m else
                     dt.datetime.now().astimezone().isoformat(timespec="seconds")}
        seen.add(key)
        if dry_run:
            added += 1
            continue
        append_record(instance, rec)
        added += 1
    return added, skipped


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="已投登记（防重投）")
    ap.add_argument("--instance", default=os.environ.get("CAMPUS_INSTANCE"),
                    help="实例目录（默认 $CAMPUS_INSTANCE）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="追加一条已投登记")
    p_add.add_argument("--employer", required=True, help="雇主主体名（公告公司名也行，会归一化）")
    p_add.add_argument("--brand", default="", help="公告上的品牌/子公司名")
    p_add.add_argument("--portal", default="", help="门户短名或 tracker 门户列原文")
    p_add.add_argument("--account", default="", help="账号 sha256 前 12 位（不是明文）")
    p_add.add_argument("--ids", default="", help="announcement_id，逗号分隔")
    p_add.add_argument("--job", default="")
    p_add.add_argument("--status", default="submitted",
                     choices=["submitted", "verified", "stopped"])
    p_add.add_argument("--at", default=None, help="ISO 时间，默认当前")

    p_ck = sub.add_parser("check", help="查雇主主体是否已投过（命中退出码 0）")
    p_ck.add_argument("employer")
    p_ck.add_argument("--portal", default=None, help="限定门户；不传则所有门户都算")
    p_ck.add_argument("--any-status", action="store_true", help="不止 submitted/verified")

    p_im = sub.add_parser("import-tracker", help="从 tracker.md 回填已投记录")
    p_im.add_argument("tracker", help="tracker.md 路径（先用副本测）")
    p_im.add_argument("--dry-run", action="store_true")

    sub.add_parser("list", help="打印全部登记（account 不回显）")

    args = ap.parse_args()
    instance = os.path.abspath(os.path.expanduser(args.instance)) if args.instance else None
    if not instance or not os.path.isdir(instance):
        print("ERROR: 实例目录不存在，传 --instance 或设置 CAMPUS_INSTANCE", file=sys.stderr)
        return 2

    if args.cmd == "add":
        ids = [x for x in re.split(r"[,\s]+", args.ids or "") if x]
        try:
            rec = add(instance, args.employer, brand=args.brand, portal=args.portal,
                      account=args.account, announcement_ids=ids, job=args.job,
                      status=args.status, at=args.at)
        except RegistryError as exc:
            print("ERROR: %s" % exc, file=sys.stderr)
            return 2
        out = dict(rec)
        out["account"] = "***" if out.get("account") else ""
        print(json.dumps(out, ensure_ascii=False))
        return 0

    if args.cmd == "check":
        ekey = employer_key(args.employer)
        records = hit(load_registry(instance), ekey, args.portal,
                      statuses=None if args.any_status else ACTIVE_STATUSES)
        for r in records:
            r = dict(r)
            r["account"] = "***" if r.get("account") else ""
            print(json.dumps(r, ensure_ascii=False))
        if not records:
            print("未命中：%s（employer_key=%s%s）" % (
                args.employer, ekey, " portal=%s" % norm_portal(args.portal) if args.portal else ""),
                file=sys.stderr)
            return 1
        return 0

    if args.cmd == "import-tracker":
        if not os.path.isfile(args.tracker):
            print("ERROR: 找不到 %s" % args.tracker, file=sys.stderr)
            return 2
        added, skipped = import_tracker(instance, os.path.abspath(args.tracker), args.dry_run)
        print("import-tracker：新增 %d 条，跳过 %d 条（已在登记或无公司名/未投递行）%s"
              % (added, skipped, "（dry-run 未写入）" if args.dry_run else ""))
        return 0

    if args.cmd == "list":
        for r in load_registry(instance):
            r = dict(r)
            r["account"] = "***" if r.get("account") else ""
            print(json.dumps(r, ensure_ascii=False))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
