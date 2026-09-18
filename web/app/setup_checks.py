"""/setup 前置条件：宿主机体检结果 + 容器内能查的实例检查。

宿主机能力（himalaya、container、短信转发……）容器里查不到，由 host/host-check.py 写
state/host-health.json，本模块只负责读和展示。容器内检查实例文件是否齐、必填项是否填完。
"""

import json
import os
import re

import yaml

import bank

# 材料规格以 docs/materials.md 为准；这里只按文件名认，找不到算未通过
MATERIALS = [
    ("证件照", True, re.compile(r"(id[-_ ]?photo|证件照)", re.I), (".jpg", ".jpeg", ".png")),
    ("生活照", True, re.compile(r"(life[-_ ]?photo|生活照)", re.I), (".jpg", ".jpeg", ".png")),
    ("中文简历 PDF", True, re.compile(r"(resume[-_ ]?(cn|zh)|简历)", re.I), (".pdf",)),
    ("英文简历 PDF", True, re.compile(r"(resume[-_ ]?en|\bcv\b)", re.I), (".pdf",)),
    ("成绩单 / 在读证明（可选）", False, re.compile(r"(transcript|enrol|成绩单|在读证明)", re.I), (".pdf", ".jpg", ".png")),
]


def _check(name, ok, detail, required=True):
    return {"name": name, "ok": bool(ok), "detail": detail, "required": required}


def host_health(instance):
    path = os.path.join(instance, "state", "host-health.json")
    if not os.path.isfile(path):
        return None, "还没有 state/host-health.json：在宿主机运行 uv run --script host/host-check.py"
    try:
        raw = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, "host-health.json 解析失败：%s" % exc
    checks = raw.get("checks") if isinstance(raw, dict) else raw
    out = []
    if isinstance(checks, dict):
        for k, v in checks.items():
            if isinstance(v, dict):
                out.append(_check(v.get("name") or k, v.get("ok"), v.get("detail") or "", v.get("required", True)))
            else:
                out.append(_check(k, v, ""))
    elif isinstance(checks, list):
        for v in checks:
            if isinstance(v, dict):
                out.append(_check(v.get("name") or v.get("id") or "?", v.get("ok"), v.get("detail") or "",
                                  v.get("required", True)))
    when = raw.get("checked_at") if isinstance(raw, dict) else None
    return out, ("体检时间 %s" % when) if when else ""


def instance_checks(instance):
    out = []
    # profile.md
    p = os.path.join(instance, "profile.md")
    if os.path.isfile(p):
        text = open(p, encoding="utf-8").read()
        lines = [ln for ln in text.splitlines() if ln.strip()]
        todo = len(re.findall(r"待填|TODO|<填|<在这里", text))
        out.append(_check("profile.md", len(lines) >= 5 and todo == 0,
                          "%d 行%s" % (len(lines), "，还有 %d 处待填" % todo if todo else "")))
    else:
        out.append(_check("profile.md", False, "缺文件：用 templates/profile.template.md 生成"))

    # intent.yaml
    p = os.path.join(instance, "intent.yaml")
    if os.path.isfile(p):
        try:
            it = yaml.safe_load(open(p, encoding="utf-8")) or {}
            miss = []
            if not it.get("class_types"):
                miss.append("class_types")
            if not (it.get("roles") or {}).get("core"):
                miss.append("roles.core")
            if not ((it.get("permissions") or {}).get("submit") or {}).get("default"):
                miss.append("permissions.submit.default")
            out.append(_check("intent.yaml", not miss, "缺 " + "、".join(miss) if miss else "必填字段齐全"))
        except yaml.YAMLError as exc:
            out.append(_check("intent.yaml", False, "解析失败：%s" % str(exc).splitlines()[0]))
    else:
        out.append(_check("intent.yaml", False, "缺文件：从 templates/intent.*.yaml 复制后修改"))

    # question-bank.yaml
    p = os.path.join(instance, bank.YAML_NAME)
    if os.path.isfile(p):
        try:
            doc, _ = bank.load(p)
            c = bank.completeness(doc)
            out.append(_check("question-bank.yaml 必填项", not c["missing_ids"],
                              "必填 %d/%d%s" % (c["required_filled"], c["required"],
                                              "，未填：" + "、".join(c["missing_ids"][:6]) if c["missing_ids"] else "")))
        except (bank.BankError, yaml.YAMLError) as exc:
            out.append(_check("question-bank.yaml 必填项", False, "解析失败：%s" % str(exc).splitlines()[0]))
    else:
        out.append(_check("question-bank.yaml 必填项", False, "缺文件：uv run --script host/qb.py import-md 或从模板复制"))

    out.append(_check("accounts.md", os.path.isfile(os.path.join(instance, "accounts.md")), "门户账号（仅本地）"))

    # notify.yaml（只看有没有 key，不回显）
    p = os.path.join(instance, "notify.yaml")
    if os.path.isfile(p):
        try:
            cfg = yaml.safe_load(open(p, encoding="utf-8")) or {}
            key = str(cfg.get("device_key") or "")
            ok = bool(key) and "<" not in key
            out.append(_check("notify.yaml", ok, ("device_key 已配置 · %s" % (cfg.get("bark_server") or "https://api.day.app"))
                              if ok else "device_key 未填"))
        except yaml.YAMLError:
            out.append(_check("notify.yaml", False, "解析失败"))
    else:
        out.append(_check("notify.yaml", False, "缺文件：Bark 通知配置，见 docs/contracts.md §6"))
    return out


def materials_checks(instance):
    d = os.path.join(instance, "materials")
    files = []
    if os.path.isdir(d):
        for root, _, names in os.walk(d):
            files += [os.path.relpath(os.path.join(root, n), d) for n in names if not n.startswith(".")]
    out = []
    for label, required, rx, exts in MATERIALS:
        hit = [f for f in files if rx.search(os.path.basename(f)) and f.lower().endswith(exts)]
        out.append(_check(label, bool(hit), "、".join(hit[:3]) if hit else "materials/ 下没找到（%s）" % "/".join(exts),
                          required))
    return out, os.path.isdir(d)
