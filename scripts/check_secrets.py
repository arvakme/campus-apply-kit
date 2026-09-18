#!/usr/bin/env python3
"""check_secrets.py · 提交前扫描仓库，防止实例里的个人信息漏进 git。

用法:
  python3 scripts/check_secrets.py [--instance DIR]

两层检查:
  1. 通用正则：身份证号、大陆手机号、疑似密码
  2. 实例字面量：从实例目录的 question-bank / accounts / profile 里抽出敏感答案，
     在仓库里逐个查找（不写死在本脚本里，避免脚本自己泄漏）

只输出 文件:行号 和规则名，从不回显命中内容。命中任何一条退出码为 1。
"""
import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

GENERIC = [
    ("身份证号", re.compile(r"(?<!\d)\d{17}[\dXx](?![\dXx])")),
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("疑似密码", re.compile(r"(?i)(password|passwd|密码)\s*[:=：]\s*[^\s<`'\"]{6,}")),
]
# 模板里允许出现的明显占位号
ALLOW = {"13800000000", "13900000000", "13800138000"}
# 公开招聘数据：公告里本来就有 HR 校招专线手机号、URL 里的 18 位数字 id，通用正则必然误报，
# 这些路径只跑第 2 层"实例字面量"检查（维护者本人的手机号/邮箱/证件号仍会被拦下）
GENERIC_SKIP_PREFIXES = ("data/paperball/", "data/jd/")

SENSITIVE_Q = re.compile(
    r"证件|身份证|地址|住址|姓名|学号|密码|微信|QQ|手机|电话|邮箱|出生|档案|高中|籍贯|联系人"
)
SPLIT = re.compile(r"[、，,;；()（）|/\s:：=]+")
# 只放与任何人无关的通用词；你自己问题库里的省市、职业这类词会误报时，写进 scripts/secrets-allow.txt
GENERIC_WORDS = {"用户", "默认", "占位假号", "编造"}


def repo_files():
    out = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return [f for f in out.splitlines() if f and os.path.isfile(os.path.join(ROOT, f))]


def tokens_from(text):
    for part in SPLIT.split(text):
        part = part.strip("*`\"'。.")
        if part in GENERIC_WORDS:
            continue
        if (not part.isascii() and len(part) >= 3) or (
            part.isascii() and len(part) >= 6 and re.search(r"\d", part)
        ):
            yield part


def literals(instance):
    found = set()
    if not instance:
        return found
    qb = os.path.join(instance, "question-bank.md")
    if os.path.isfile(qb):
        for line in open(qb, encoding="utf-8"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            # 生成版问题库第 6 列是类型：rule 条目多是普通中文说明，中文短语会大量误报；
            # 但规则里也可能夹着号码、日期、邮箱，所以只抽带数字的 ASCII 片段和邮箱
            is_rule = len(cells) >= 6 and cells[5].strip() == "rule"
            if len(cells) >= 2 and SENSITIVE_Q.search(cells[0]):
                if is_rule:
                    found.update(t for t in tokens_from(cells[1]) if t.isascii())
                    found.update(re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", cells[1]))
                else:
                    found.update(tokens_from(cells[1]))
            if "密码" in line:
                found.update(m for m in re.findall(r"[\w@#$%^&*.!-]{8,}", line) if re.search(r"\d", m))
    for name in ("accounts.md", "profile.md"):
        p = os.path.join(instance, name)
        if not os.path.isfile(p):
            continue
        text = open(p, encoding="utf-8").read()
        found.update(re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", text))
        found.update(re.findall(r"(?<!\d)1[3-9]\d{9}(?!\d)", text))
        found.update(m for m in re.findall(r"[\w@#$%^&*.!-]{8,}", text)
                     if re.search(r"\d", m) and re.search(r"[A-Za-z]", m) and re.search(r"[@#$%^&*!]", m))
    allow_file = os.path.join(ROOT, "scripts", "secrets-allow.txt")
    allowed = set()
    if os.path.isfile(allow_file):
        allowed = {l.strip() for l in open(allow_file, encoding="utf-8") if l.strip() and not l.startswith("#")}
    return {
        t for t in found
        if len(t) >= 3 and t not in allowed and not re.fullmatch(r"\d{4}-\d{2}(-\d{2})?", t)
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance", default=os.environ.get("CAMPUS_INSTANCE"))
    args = ap.parse_args()

    lits = literals(args.instance)
    hits = 0
    for rel in repo_files():
        if rel == "scripts/check_secrets.py":
            continue
        try:
            lines = open(os.path.join(ROOT, rel), encoding="utf-8").read().splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        public_data = rel.startswith(GENERIC_SKIP_PREFIXES)
        generic = [] if public_data else GENERIC
        # 公开公告/JD 里地名、学校名很常见（如 OCR 出来的"某省某市"），中文字面量必然误报；
        # 这些路径只拦 ASCII 字面量（手机号、邮箱、证件号、账号）
        file_lits = [l for l in lits if l.isascii()] if public_data else lits
        for no, line in enumerate(lines, 1):
            for rule, rx in generic:
                for m in rx.finditer(line):
                    if m.group(0) not in ALLOW:
                        print(f"{rel}:{no}  [{rule}]")
                        hits += 1
            for lit in file_lits:
                if lit in line:
                    print(f"{rel}:{no}  [实例字面量]")
                    hits += 1
                    break
    src = f"实例字面量 {len(lits)} 个" if lits else "未指定实例，只跑通用正则"
    print(f"check_secrets: {hits} 处命中 · {src}", file=sys.stderr)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
