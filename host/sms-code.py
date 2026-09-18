#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""sms-code.py · 从 Mac「信息」的 chat.db 自取短信验证码（iPhone 短信转发到 Mac 时可用）

    uv run --script host/sms-code.py [--since 10m] [--sender-hint <公司/平台名>] [--wait 90] [--db PATH]
    uv run --script host/sms-code.py health [--json]
    uv run --script host/sms-code.py selftest

默认命令（取码）：
  --since    只考虑该时间窗内收到的消息；支持 90s/10m/2h/1d 或 ISO 时间戳，默认 10m
  --sender-hint  公司或平台名，用来给消息打分（匹配发送方 id 或正文【签名】），不给则任何含验证码的消息都算候选
  --wait     最多等待秒数，每 2 秒轮询直到出现匹配的验证码消息，默认 0（只查一次）
  --db       覆盖 chat.db 路径（默认 ~/Library/Messages/chat.db）

  输出（stdout 一行 JSON，验证码不外泄到日志）：
    {"code": "338474", "sender": "1069xxxxxxxxxxx", "received_at": "…", "service": "SMS",
     "score": 90, "snippet": "【示例平台】验证码：******，该验证码…（数字一律打码）"}
  找不到/超时：stdout {"code": null, "error": "…"}，退出码 2。
  退出码：0 取到码 · 2 超时/无匹配 · 3 chat.db 不存在 · 4 chat.db 不可读（多半是没开完全磁盘访问）· 5 selftest 失败

health：报告最近一条短信（SMS/RCS）与最近一条消息的本地时间，判断 iPhone→Mac 短信转发是否在工作。
  供 host-check 与 web /setup 使用；只报时间和条数，不回显内容。退出码同 3/4。

selftest：构造 attributedBody（typedstream）样例验证解析与抽码；chat.db 可读时另取一条
  text 为空的真实消息验证解析非空。只打印成功/失败与长度，不回显正文。

实现要点：
- chat.db 是 WAL 模式：连同 chat.db-wal / chat.db-shm 一起复制到临时目录再读，避免锁库、
  也能读到仍在 WAL 里的最新短信；每次轮询重新复制。
- message.text 为空时正文在 attributedBody（typedstream）：找 b'\\x84\\x01\\x2b' 标记，
  长度前缀为 1 字节、\\x81+LE16 或 \\x82+LE32，随后是 UTF-8 字节；同 blob 多段则拼接。
- 候选码 = 4–8 位（允许中间带 - 或空格，如 002-807），按"验证码/校验码/动态码/code"等
  关键词邻近度打分；年份、日期、金额、签名内的数字扣分。
- --sender-hint 给了但消息里毫无证据（发送方 id、正文、签名都不沾边）时不接受该消息，
  防止隔壁平台的验证码串进来。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time

DEFAULT_DB = os.path.expanduser("~/Library/Messages/chat.db")
APPLE_EPOCH = 978307200  # 2001-01-01 UTC
POLL_INTERVAL = 2.0

CODE_KEYWORDS = (
    "验证码", "校验码", "动态码", "动态密码", "交易码", "确认码",
    "授权码", "安全码", "短信码", "校验",
    "code", "Code", "CODE", "verification", "Verification", "OTP", "otp",
)
# 候选数字串：4–8 位有效数字，允许内部夹 - 或空格（002-807 / 47 1379）
CANDIDATE_RE = re.compile(r"(?<![\d.])(\d[\d\- ]{1,10}\d)(?![\d])")
KEYWORD_RE = re.compile("|".join(re.escape(k) for k in CODE_KEYWORDS))
SIGNATURE_RE = re.compile(r"【([^】]{1,30})】")
YEAR_RE = re.compile(r"^(19|20)\d{2}$")
# 日期形态：整串带两个分隔符（2026-09-16）；或紧邻 -/ 与数字相接（日期片段的头尾）
DATE_SHAPED_RE = re.compile(r"^\d{1,4}[-/ ]\d{1,2}[-/ ]\d{1,4}$")


def eprint(*a):
    print(*a, file=sys.stderr)


def parse_since(s: str) -> dt.datetime:
    s = s.strip()
    m = re.fullmatch(r"(\d+)\s*([smhd])", s, re.I)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = {"s": dt.timedelta(seconds=n), "m": dt.timedelta(minutes=n),
                 "h": dt.timedelta(hours=n), "d": dt.timedelta(days=n)}[unit]
        return dt.datetime.now().astimezone() - delta
    try:
        t = dt.datetime.fromisoformat(s)
        return t.astimezone() if t.tzinfo else t.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)
    except ValueError:
        raise SystemExit("ERROR: --since 只接受 90s/10m/2h/1d 或 ISO 时间戳，收到：%s" % s)


def decode_attributed_body(blob: bytes) -> str | None:
    """typedstream 里的 NSString 载荷：b'\\x84\\x01\\x2b' 标记 + 长度前缀 + UTF-8。

    长度前缀：0x81 → LE16，0x82 → LE32，其余 1 字节即长度。多段拼接。
    """
    if not isinstance(blob, (bytes, bytearray)):
        return None
    out, pos = [], 0
    while True:
        m = blob.find(b"\x84\x01\x2b", pos)
        if m < 0:
            break
        i = m + 3
        flag = blob[i]
        if flag == 0x81:
            ln = int.from_bytes(blob[i + 1:i + 3], "little"); start = i + 3
        elif flag == 0x82:
            ln = int.from_bytes(blob[i + 1:i + 5], "little"); start = i + 5
        else:
            ln = flag; start = i + 1
        end = start + ln
        if end > len(blob):
            break
        try:
            out.append(blob[start:end].decode("utf-8"))
        except UnicodeDecodeError:
            pass
        pos = end
    text = "".join(out).strip("\x00").strip()
    return text or None


def mask(text: str, keep: int = 80) -> str:
    """打码：所有数字变 *，收敛空白，截断。绝不回显完整短信。"""
    t = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\d", "*", t)[:keep]


URL_RE = re.compile(r"(?:https?://|www\.)\S+")


def score_code_candidates(text: str) -> list[tuple[int, str]]:
    """返回 [(score, code)]，只含分数>0 的候选；分数越高越像验证码。"""
    url_spans = [m.span() for m in URL_RE.finditer(text)]
    results = []
    for m in CANDIDATE_RE.finditer(text):
        raw = m.group(1)
        code = re.sub(r"\D", "", raw)
        if not (4 <= len(code) <= 8):
            continue
        lo, hi = m.span()
        prev = text[lo - 1] if lo else ""
        nxt = text[hi] if hi < len(text) else ""
        # URL 里的数字（端口、路径号）永远不会是验证码
        if any(s <= lo < e for s, e in url_spans):
            continue
        # 日期/编号形态直接排除：整串两个分隔符（2026-09-16），或作为日期片段的头尾
        if DATE_SHAPED_RE.match(raw):
            continue
        if prev in "-/" and re.search(r"\d{1,4}$", text[max(0, lo - 4):lo - 1]):
            continue
        if nxt in "-/" and (text[hi + 1:hi + 2] or "").isdigit():
            continue
        score = 10
        ctx = text[max(0, lo - 15):min(len(text), hi + 15)]
        kw = KEYWORD_RE.search(ctx)
        if kw:
            score += 50
            # 关键词紧邻引导："验证码为：X" / "code: X" 是 canonical 形态，再加分
            if re.search(r"(?:" + "|".join(re.escape(k) for k in CODE_KEYWORDS)
                         + r")\D{0,8}$", text[:lo]):
                score += 25
            elif re.match(r"\D{0,8}(?:" + "|".join(re.escape(k) for k in CODE_KEYWORDS)
                          + r")", text[hi:]):
                score += 10
        elif YEAR_RE.match(code):
            score -= 50  # 裸年份只在没有关键词佐证时扣分（真码也可能是 1964）
        if (prev and prev in "￥$") or (nxt and nxt in "元%"):
            score -= 30
        # 出现在【签名】里的是号码不是码
        for sig in SIGNATURE_RE.finditer(text):
            if sig.start() <= lo < sig.end():
                score -= 30
                break
        if score > 0:
            results.append((score, code))
    results.sort(key=lambda x: -x[0])
    return results


def norm(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()


def message_score(text: str, sender: str, hint: str | None,
                  codes: list[tuple[int, str]]) -> int:
    if not codes or codes[0][0] < 50:
        return 0
    score = 50
    if hint:
        h = norm(hint)
        sigs = " ".join(SIGNATURE_RE.findall(text))
        if h and h in norm(sigs):
            score += 40
        elif h and (h in norm(text) or h in norm(sender)):
            score += 20
        else:
            return 0  # 给了提示但完全对不上：拒绝，防串码
    return score


def open_copy(db_path: str):
    """复制 chat.db 及 WAL/SHM 到临时目录再以只读 URI 打开。返回 (conn, tmpdir)。"""
    if not os.path.exists(db_path):
        raise FileNotFoundError(db_path)
    tmp = tempfile.mkdtemp(prefix="sms-code-")
    try:
        for suffix in ("", "-wal", "-shm"):
            src = db_path + suffix
            if os.path.exists(src):
                shutil.copyfile(src, os.path.join(tmp, "chat.db" + suffix))
        con = sqlite3.connect("file:%s?mode=ro" % os.path.join(tmp, "chat.db"), uri=True)
        con.execute("select 1 from message limit 1")
        return con, tmp
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def fetch_messages(con, since_apple: float) -> list[dict]:
    """since_apple：Apple 纪元（2001-01-01）起的秒数。date 为纳秒；极老库可能是秒，双判。"""
    out = []
    rows = con.execute(
        """select m.ROWID, m.date, m.service, m.is_from_me, m.text, m.attributedBody,
                  h.id
           from message m left join handle h on h.ROWID = m.handle_id
           where m.is_from_me = 0
           order by m.date desc""",
    ).fetchall()
    for rid, date_raw, service, _from_me, text, body, sender in rows:
        if not date_raw:
            continue
        ts = date_raw / 1e9 if date_raw > 1e12 else date_raw  # → Apple 纪元秒
        if ts < since_apple:
            break  # 按 date desc，之后只会更旧
        content = text if text else (decode_attributed_body(body) if body else None)
        if not content:
            continue
        out.append({
            "rowid": rid,
            "received_at": dt.datetime.fromtimestamp(
                APPLE_EPOCH + ts, tz=dt.datetime.now().astimezone().tzinfo),
            "service": service or "",
            "sender": sender or "unknown",
            "text": content,
        })
    return out


def find_code(db_path: str, since: dt.datetime, hint: str | None):
    threshold = since.timestamp() - APPLE_EPOCH
    try:
        con, tmp = open_copy(db_path)
    except FileNotFoundError:
        return None, ("找不到 chat.db：%s\n  这台 Mac 没有短信数据库，或路径不对（可用 --db 指定）。"
                      % db_path), 3
    except (sqlite3.Error, PermissionError, OSError) as e:
        return None, ("无法读取 chat.db：%s\n  多半是完全磁盘访问权限（Full Disk Access）没授予运行本脚本的程序\n"
                      "  （终端 App / ego lite / seedmux 宿主）。到 系统设置 → 隐私与安全性 → 完全磁盘访问 里打开后重试。"
                      % e), 4
    try:
        best = None
        for msg in fetch_messages(con, threshold):
            codes = score_code_candidates(msg["text"])
            sc = message_score(msg["text"], msg["sender"], hint, codes)
            if sc <= 0:
                continue
            cand = (sc, msg, codes[0][1])
            if best is None or (sc, msg["received_at"]) > (best[0], best[1]["received_at"]):
                best = cand
        if best:
            sc, msg, code = best
            return {"code": code, "sender": msg["sender"],
                    "received_at": msg["received_at"].isoformat(timespec="seconds"),
                    "service": msg["service"], "score": sc,
                    "snippet": mask(msg["text"])}, None, 0
        return None, ("时间窗内没有匹配%s的验证码短信" % ("「%s」" % hint if hint else "")), 2
    finally:
        con.close()
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_code(args) -> int:
    since = parse_since(args.since)
    deadline = time.monotonic() + args.wait
    while True:
        result, err, rc = find_code(args.db, since, args.sender_hint)
        if result or rc in (3, 4) or time.monotonic() >= deadline:
            if result:
                print(json.dumps(result, ensure_ascii=False))
            else:
                print(json.dumps({"code": None, "error": (err or "").splitlines()[0]},
                                 ensure_ascii=False))
                if err:
                    eprint(err)
            return rc if not result else 0
        time.sleep(POLL_INTERVAL)


def cmd_health(args) -> int:
    try:
        con, tmp = open_copy(args.db)
    except FileNotFoundError:
        eprint("找不到 chat.db：%s（本机无短信库或用 --db 指定）" % args.db)
        return 3
    except (sqlite3.Error, PermissionError, OSError) as e:
        eprint("无法读取 chat.db：%s\n多半是完全磁盘访问权限没授予运行方；到 系统设置 → 隐私与安全性 → 完全磁盘访问 里打开后重试。" % e)
        return 4
    try:
        row_sms = con.execute(
            "select max(date) from message where is_from_me=0 and service in ('SMS','RCS')"
        ).fetchone()[0]
        row_any = con.execute(
            "select max(date) from message where is_from_me=0"
        ).fetchone()[0]
        cnt7d = con.execute(
            "select count(*) from message where is_from_me=0 and service in ('SMS','RCS')"
            " and date >= ?",
            (int((time.time() - APPLE_EPOCH - 7 * 86400) * 1e9),),
        ).fetchone()[0]
    finally:
        con.close()
        shutil.rmtree(tmp, ignore_errors=True)

    def iso(raw):
        if not raw:
            return None
        ts = raw / 1e9 if raw > 1e12 else raw
        return dt.datetime.fromtimestamp(
            APPLE_EPOCH + ts, tz=dt.datetime.now().astimezone().tzinfo).isoformat(timespec="seconds")

    last_sms, last_any = iso(row_sms), iso(row_any)
    stale = True
    if last_sms:
        age_h = (time.time() - dt.datetime.fromisoformat(last_sms).timestamp()) / 3600
        stale = age_h > 72
    report = {"last_sms_at": last_sms, "last_message_at": last_any,
              "sms_count_7d": cnt7d, "stale": stale}
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print("最近一条短信：%s" % (last_sms or "从未收到"))
        print("最近一条消息：%s" % (last_any or "从未收到"))
        print("近 7 天短信条数：%d" % cnt7d)
        if last_sms is None:
            print("短信转发：未观察到任何短信（iPhone 未开转发，或本机不是接收端）")
        elif stale:
            print("短信转发：最近一条已超过 72 小时，转发可能已断；在 iPhone 设置 → 信息 → 短信转发 里确认本机已勾选")
        else:
            print("短信转发：正常")
    return 0


def cmd_selftest(args) -> int:
    # 1) 构造 typedstream 样例：NSString\x01\x95\x84\x01\x2b + \x81 LE16 + UTF-8
    body_text = "【测试平台】您的验证码为：836-214，5 分钟内有效，请勿泄露。"
    payload = body_text.encode("utf-8")
    blob = (b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x19NSMutableAttributedString\x00"
            b"\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84"
            b"\x0fNSMutableString\x01\x84\x84\x08NSString\x01\x95\x84\x01\x2b\x81"
            + len(payload).to_bytes(2, "little") + payload + b"\x86\x86")
    got = decode_attributed_body(blob)
    if got != body_text:
        eprint("selftest FAIL: attributedBody 解析不符：%r" % (got and got[:40]))
        return 5
    codes = score_code_candidates(got)
    if not codes or codes[0][1] != "836214":
        eprint("selftest FAIL: 样例抽码不符：%r" % (codes[:3] if codes else None))
        return 5
    print("selftest: 合成 attributedBody 解析 + 抽码 通过")

    # 2) 真实库验证（库可读时）：取一条 text 为空的消息解析非空
    if os.path.exists(args.db):
        try:
            con, tmp = open_copy(args.db)
        except Exception as e:
            print("selftest: 真实库不可读（%s），跳过第 2 步" % e)
            return 0
        try:
            row = con.execute(
                "select attributedBody from message where text is null"
                " and attributedBody is not null and length(attributedBody)>50 limit 1"
            ).fetchone()
        finally:
            con.close()
            shutil.rmtree(tmp, ignore_errors=True)
        if row:
            s = decode_attributed_body(row[0])
            if s:
                print("selftest: 真实 attributedBody 样例解析 通过（%d 字符）" % len(s))
            else:
                eprint("selftest FAIL: 真实 attributedBody 解析为空")
                return 5
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sms-code.py",
        description="从 Mac 信息 chat.db 自取短信验证码（只读）。子命令：health / selftest；不带子命令即取码。",
    )
    p.add_argument("--since", default="10m",
                   help="时间窗：90s/10m/2h/1d 或 ISO 时间戳，默认 10m")
    p.add_argument("--sender-hint", default=None,
                   help="公司/平台名，匹配发送方 id 或正文【签名】；不给则任何含验证码消息都算候选")
    p.add_argument("--wait", type=float, default=0,
                   help="最多等待秒数，每 2 秒轮询一次，默认 0（只查一次）")
    p.add_argument("--db", default=DEFAULT_DB, help="chat.db 路径")
    return p


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("health", "selftest"):
        sub = argv[0]
        sp = argparse.ArgumentParser(prog="sms-code.py %s" % sub)
        sp.add_argument("--db", default=DEFAULT_DB)
        if sub == "health":
            sp.add_argument("--json", action="store_true")
        args = sp.parse_args(argv[1:])
        return cmd_health(args) if sub == "health" else cmd_selftest(args)
    return cmd_code(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
