#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6"]
# ///
"""qb.py · 问题库命令行（question-bank.yaml 真源）

    uv run --script host/qb.py import-md <question-bank.md> <question-bank.yaml> [--force]
    uv run --script host/qb.py get <id> [--reveal]           # secret 默认不回显，--reveal 才打印
    uv run --script host/qb.py set <id> <answer> [--source 用户]
    uv run --script host/qb.py add <id> --question 问法 --category 身份 [--kind value] [--required] [--hint ..] [--answer ..]
    uv run --script host/qb.py list [--missing]
    uv run --script host/qb.py render-md [--out PATH]

yaml 路径默认 $CAMPUS_INSTANCE/question-bank.yaml，可用 --file 覆盖。
所有写入复用 web/app/bank.py：fcntl 锁 + sha256 版本校验 + 原子替换，写后自动重新生成 question-bank.md。
"""

import argparse
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "web", "app"))

import bank  # noqa: E402


def bank_path(args):
    if args.file:
        return os.path.abspath(args.file)
    inst = os.environ.get("CAMPUS_INSTANCE")
    if not inst:
        sys.exit("ERROR: 没有 --file，也没有设置 CAMPUS_INSTANCE")
    return os.path.join(inst, bank.YAML_NAME)


def cmd_import_md(args):
    if os.path.exists(args.yaml) and not args.force:
        sys.exit("ERROR: %s 已存在，确认覆盖请加 --force" % args.yaml)
    text = open(args.md, encoding="utf-8").read()
    doc, report = bank.import_md(text)
    bank.validate(doc)
    rows = sum(1 for ln in text.splitlines()
               if ln.lstrip().startswith("|") and not bank.SEP.match(ln)) - _header_rows(text)
    if os.path.exists(args.yaml):
        os.unlink(args.yaml)
    bank.mutate(args.yaml, lambda d: d.update(doc), render=False)
    print("md 表格数据行 %d → 导入表格条目 %d · 规则迁出 secret 条目 %d（%s）· 共 %d 条 · %d 条规则 → %s" % (
        rows, report["table_rows"], len(report["migrated"]), ",".join(report["migrated"]) or "—",
        report["items"], report["rules"], args.yaml))
    kinds = {}
    for it in doc["items"]:
        kinds[it["kind"]] = kinds.get(it["kind"], 0) + 1
    print("  类型分布：" + " · ".join("%s %d" % kv for kv in sorted(kinds.items())))
    c = bank.completeness(doc)
    print("  必填 %d，已填 %d" % (c["required"], c["required_filled"]))
    if report["unmatched"]:
        print("  以下条目未命中判定表，已按关键词兜底，请人工复核 category/kind/required：")
        for iid, q in report["unmatched"]:
            print("    - %s  %s" % (iid, q))
    if rows != report["table_rows"]:
        print("WARN: 条目数与 md 表格行数不一致", file=sys.stderr)
        return 1
    return 0


def _header_rows(text):
    lines = text.splitlines()
    return sum(1 for i, ln in enumerate(lines[:-1])
               if ln.lstrip().startswith("|") and bank.SEP.match(lines[i + 1]))


def cmd_get(args):
    doc, version = bank.load(bank_path(args))
    it = bank.find(doc, args.id)
    if it is None:
        sys.exit("ERROR: 没有这个条目：%s" % args.id)
    if it.get("kind") == "secret" and not args.reveal:
        print("%s（secret）%s" % (it["question"], "已填" if bank.is_filled(it) else "未填"))
        return 0
    print(it.get("answer") or "")
    return 0


def cmd_set(args):
    path = bank_path(args)
    doc, version = bank.load(path)
    if bank.find(doc, args.id) is None:
        sys.exit("ERROR: 没有这个条目：%s（新增用 add）" % args.id)
    new = bank.mutate(path, lambda d: bank.apply_updates(d, [{"id": args.id, "answer": args.answer}],
                                                         source=args.source), expected_version=version)
    print("已更新 %s · 版本 %s" % (args.id, new[:12]))
    return 0


def cmd_add(args):
    path = bank_path(args)

    def fn(d):
        if bank.find(d, args.id):
            raise bank.BankError("id 已存在：%s" % args.id)
        d["items"].append({
            "id": args.id, "question": args.question, "aliases": args.alias or [],
            "category": args.category, "kind": args.kind, "required": args.required,
            "hint": args.hint or "", "answer": args.answer or "",
            "updated": bank.today(), "source": args.source,
        })

    new = bank.mutate(path, fn)
    print("已新增 %s · 版本 %s" % (args.id, new[:12]))
    return 0


def cmd_alias(args):
    """给条目加/删别名。fill-moka 等批量填表靠「字段名 = 问题或别名」直接命中，别名不能在两个条目间重复。"""
    path = bank_path(args)

    def fn(d):
        item = bank.find(d, args.id)
        if item is None:
            raise bank.BankError("没有这个条目：%s" % args.id)
        al = [a for a in (item.get("aliases") or []) if a not in (args.remove or [])]
        for a in args.add or []:
            clash = [e["id"] for e in d["items"] if e["id"] != args.id
                     and a in [e.get("question", "")] + (e.get("aliases") or [])]
            if clash:
                raise bank.BankError("别名「%s」已属于 %s，先从那边 --remove" % (a, "、".join(clash)))
            if a not in al:
                al.append(a)
        item["aliases"] = al
        if args.question:
            item["question"] = args.question
        item["updated"] = bank.today()

    new = bank.mutate(path, fn)
    print("已更新 %s 的别名 · 版本 %s" % (args.id, new[:12]))
    return 0


def cmd_list(args):
    doc, version = bank.load(bank_path(args))
    for it in doc["items"]:
        filled = bank.is_filled(it)
        if args.missing and filled:
            continue
        mark = "✓" if filled else "·"
        req = "必填" if it.get("required") else "    "
        print("%s %s %-4s %-7s %-28s %s" % (mark, req, it["category"], it["kind"], it["id"], it["question"]))
    c = bank.completeness(doc)
    print("版本 %s · 共 %d 条 · 必填 %d/%d" % (version[:12], c["total"], c["required_filled"], c["required"]))
    return 0


def cmd_render_md(args):
    path = bank_path(args)
    doc, _ = bank.load(path)
    out = args.out or os.path.join(os.path.dirname(path), bank.MD_NAME)
    bank._atomic_write(out, bank.render_md(doc).encode("utf-8"))
    print("写出 %s（%d 条）" % (out, len(doc["items"])))
    return 0


def main():
    ap = argparse.ArgumentParser(description="问题库 question-bank.yaml 命令行")
    ap.add_argument("--file", help="question-bank.yaml 路径（默认 $CAMPUS_INSTANCE/question-bank.yaml）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("import-md", help="旧 question-bank.md → yaml")
    p.add_argument("md")
    p.add_argument("yaml")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_import_md)

    p = sub.add_parser("get")
    p.add_argument("id")
    p.add_argument("--reveal", action="store_true", help="打印 secret 答案（只在本机终端用）")
    p.set_defaults(fn=cmd_get)

    p = sub.add_parser("set")
    p.add_argument("id")
    p.add_argument("answer")
    p.add_argument("--source", default="用户")
    p.set_defaults(fn=cmd_set)

    p = sub.add_parser("add")
    p.add_argument("id")
    p.add_argument("--question", required=True)
    p.add_argument("--category", required=True, choices=bank.CATEGORIES)
    p.add_argument("--kind", default="value", choices=bank.KINDS)
    p.add_argument("--required", action="store_true")
    p.add_argument("--hint")
    p.add_argument("--answer")
    p.add_argument("--alias", action="append")
    p.add_argument("--source", default="用户")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("alias", help="加/删别名（同一别名只能属于一个条目）")
    p.add_argument("id")
    p.add_argument("--add", action="append")
    p.add_argument("--remove", action="append")
    p.add_argument("--question", help="顺带改问题标题（如给教育条目标上 硕士/本科，fill-moka 靠它分阶段）")
    p.set_defaults(fn=cmd_alias)

    p = sub.add_parser("list")
    p.add_argument("--missing", action="store_true", help="只列未填")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("render-md", help="yaml → 只读 md 视图")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_render_md)

    args = ap.parse_args()
    try:
        return args.fn(args)
    except bank.Conflict as exc:
        print("ERROR: 版本冲突，%s；请重试" % exc, file=sys.stderr)
        return 3
    except bank.BankError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
