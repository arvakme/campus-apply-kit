"""handoff 记录读写（契约 docs/contracts.md §6）。

记录在 $CAMPUS_INSTANCE/state/handoffs/<id>.json。host/notify.py 负责创建，web 负责展示和写 resolved_at。
两边共用本模块，只依赖标准库。
"""

import datetime as dt
import json
import os
import re
import tempfile

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
KIND_SHORT = {"wechat_qr": "qr", "qr": "qr", "captcha": "captcha", "face": "face",
              "sms_code": "sms", "submit_confirm": "submit", "login": "login"}
KIND_LABEL = {"wechat_qr": "微信扫码", "qr": "扫码", "captcha": "图形验证码", "face": "人脸识别",
              "sms_code": "短信验证码", "submit_confirm": "提交前确认", "login": "登录"}


def now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def handoff_dir(instance):
    return os.path.join(instance, "state", "handoffs")


def atomic_write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".%s." % os.path.basename(path), suffix=".tmp",
                               dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def make_id(announcement_id, kind, when=None):
    when = when or dt.datetime.now()
    parts = [when.strftime("%Y%m%d-%H%M")]
    if announcement_id not in (None, ""):
        parts.append(re.sub(r"[^A-Za-z0-9]+", "", str(announcement_id)) or "x")
    parts.append(KIND_SHORT.get(kind or "", re.sub(r"[^A-Za-z0-9]+", "", kind or "") or "handoff"))
    return "-".join(parts)


def path_for(instance, hid):
    if not ID_RE.match(hid or ""):
        raise ValueError("非法 handoff id")
    return os.path.join(handoff_dir(instance), hid + ".json")


def load(instance, hid):
    try:
        with open(path_for(instance, hid), encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return None


def list_all(instance):
    d = handoff_dir(instance)
    out = []
    if not os.path.isdir(d):
        return out
    for fn in os.listdir(d):
        if not fn.endswith(".json") or fn.startswith("."):
            continue
        try:
            with open(os.path.join(d, fn), encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(rec, dict) and rec.get("id"):
            out.append(rec)
    out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return out


ACTIVE = ("waiting", "scanned")        # 真正需要用户动手的
QUEUED = ("queued", "ready")           # 排队中，还没推给用户


def is_active(r):
    return r.get("status") in ACTIVE and not r.get("resolved_at")


def open_count(instance):
    """「待我处理」数字：只数等用户动手的（过期/取消/排队中的不算）。"""
    return sum(1 for r in list_all(instance) if is_active(r))


def unique_id(instance, hid):
    base, n = hid, 1
    while os.path.exists(path_for(instance, hid)):
        n += 1
        hid = "%s-%d" % (base, n)
    return hid


def resolve(instance, hid):
    rec = load(instance, hid)
    if rec is None:
        return None
    if not rec.get("resolved_at"):
        rec["resolved_at"] = now_iso()
        atomic_write_json(path_for(instance, hid), rec)
    return rec


STATUS_LABEL = {"queued": "排队中", "ready": "轮到了，等 worker 确认页面", "waiting": "等你处理", "scanned": "你已接管/已扫码，等 worker 确认",
                "expired": "已过期", "resolved": "已处理", "cancelled": "已取消"}


def update(instance, hid, **fields):
    """§8.6 时效协议的增量字段更新（status/expires_at/refresh_count/shot/action/chain…）。

    只更新传入的键；返回更新后的记录，hid 不存在返回 None。
    """
    rec = load(instance, hid)
    if rec is None:
        return None
    rec.update(fields)
    atomic_write_json(path_for(instance, hid), rec)
    return rec


def find_open(instance, announcement_id):
    """某个公告最新一条未 resolved 的 handoff（handoff-update 按公告 id 定位用）。"""
    cands = [r for r in list_all(instance)
             if str(r.get("announcement_id")) == str(announcement_id)
             and not r.get("resolved_at")]
    return cands[0] if cands else None
