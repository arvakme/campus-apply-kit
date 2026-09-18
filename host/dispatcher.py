#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6"]
# ///
"""dispatcher.py · 常驻调度器：把"主持人手动派单/盯进度/催 worker/汇总"变成常驻进程。

    uv run --script host/dispatcher.py [--instance DIR]            # 常驻，默认 30s 一轮
    uv run --script host/dispatcher.py --once                      # 只跑一轮（测试/排查用）
    uv run --script host/dispatcher.py --dry-run --once            # 只打印将做的动作
    uv run --script host/dispatcher.py status                      # 打印作业表
    uv run --script host/dispatcher.py cmd approve --target 10001  # 往 commands.jsonl 追加一条指令
    uv run --script host/dispatcher.py install-plist --dry-run     # 打印 launchd plist（不安装）

职责（契约 docs/contracts.md §8，设计说明 docs/dispatcher.md）：
- 读 state/autonomy.json（不存在则建默认 auto 广撒网）、state/fit/、state/jobs/、state/commands.jsonl
- 入队：mode=auto 按 tier_modes 自动建作业；supervised/manual 只为用户 approve 过的建作业
- 派单前查 state/registry.jsonl + tracker 已投列防重投；daily_cap / max_parallel 限流；
  截止近优先 → 冲→常规→练手 → 分数高优先
- 派单：渲染 templates/worker-task.md → smx-team assign 复用空闲 worker pane，否则 spawn
- 推进：只看回执/meta.json/约定标记文件（log/fields/<id>.md = review_wait；
  state/handoffs/*.json 未 resolved = gate_wait；state/outcomes/<id>.json = 终局），不解析 pane 输出
- 自停巡检：pane idle 且作业 filling/dispatched 超过 --nudge-after-min → nudge，最多 --max-nudges 次后 blocked
- 指令：approve/skip/hold/resume/refresh/takeover/submit/mode/pause/unpause/reply → 消费进
  commands.done.jsonl；reply/submit/refresh/takeover 走 1 秒短轮询不等大循环（§8.6 时效）
- 关卡兜底（§8.7）：handoff expired 且 refresh_count 用尽 → 作业 parked、释放 pane 派下一家；
  scanned 超 60 秒提醒 worker 检测登录态；同门户 24h parked≥3 暂停该门户；
  parked 超 48h → skipped:gate_timeout（截止前 2 天提醒一次）；
  state/tgbot.heartbeat mtime 超 2 分钟 → Bark（每小时至多一次；5 分钟二次提醒由 tgbot 负责）
- 夜间（autonomy.night）：硬门槛只排队不推送，结束时一次性汇总推送
- submitted/verified 写 registry；状态变化走 notify.py（dispatcher 发 blocked/batch-done/每日汇总）
- 单实例锁（state/dispatcher.lock flock）；崩溃重启从 state/ 恢复，已有 task_id 的作业绝不重派

测试注入点（都不影响真实环境）：
  --worker-cmd PATH   替换 smx-team（stub 脚本模拟 spawn/assign/send/panes）
  --notify-cmd PATH   替换 notify.py
  --tasks-dir DIR     seedmux 任务目录（默认 ~/.seedmux/team/tasks）
  --now ISO           固定"当前时间"（夜间/超时测试用）
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from zoneinfo import ZoneInfo

import yaml

REPO = os.environ.get("CAMPUS_REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "web", "app"))

import registry as reg  # noqa: E402

try:
    import handoffs  # noqa: E402
except ImportError:  # web/app 不在时给最小实现
    handoffs = None

CST = ZoneInfo("Asia/Shanghai")

# 作业状态机（contracts §8.2）
STATUSES = {"queued", "dispatched", "filling", "gate_wait", "review_wait", "submitting",
            "submitted", "verified", "blocked", "skipped", "failed", "held"}
ACTIVE = {"dispatched", "filling", "gate_wait", "review_wait", "submitting"}   # 占着 pane
WORKING = {"dispatched", "filling", "submitting"}                             # worker 应该在干活，可 nudge
TERMINAL = {"submitted", "verified", "blocked", "skipped", "failed"}
TIER_ORDER = {"冲": 0, "常规": 1, "练手": 2}
COMMANDS = {"approve", "skip", "hold", "resume", "refresh", "takeover", "submit", "mode",
            "pause", "unpause", "reply"}
# §8.6：这几类指令要秒级响应（用户正拿着手机等），走 1 秒短轮询，不等 30 秒大循环
FAST_COMMANDS = {"reply", "submit", "refresh", "takeover"}
HARD_GATE_KINDS = {"wechat_qr", "qr", "captcha", "face", "sms_code", "register", "login",
                   "attachments"}

DEFAULT_AUTONOMY = {
    "mode": "auto", "paused": False,
    "tier_modes": {"冲": "supervised", "常规": "auto", "练手": "auto"},
    "max_parallel": 5, "daily_cap": 50,
    "night": {"start": "00:30", "end": "08:00", "gate_policy": "queue"},
}

# §8.7 兜底参数
SCANNED_CHECK_SEC = 60          # scanned 后页面未跳转多久 → 叫 worker 检测登录态
PARK_TIMEOUT_SEC = 48 * 3600    # parked 超过 → skipped:gate_timeout
PARK_REMIND_SEC = 2 * 86400     # 公告截止前这么久仍 parked → 提醒一次
PORTAL_PARK_WINDOW = 24 * 3600  # 同门户 parked 计数窗口
PORTAL_PARK_LIMIT = 3           # 窗口内 parked ≥N → 暂停该门户
TGBOT_HB_STALE_SEC = 120        # tgbot.heartbeat mtime 超此 → 判 bot 挂
TGBOT_HB_NOTIFY_SEC = 3600      # bot 挂通知节流

# 投递链接域名 → 门户短名（portals/README.md 识别特征；同门户卡关统计用）
PORTAL_DOMAINS = [
    ("app.mokahr.com", "moka"), ("mokahr.com", "moka"),
    ("zhiye.com", "beisen"), ("jobs.feishu.cn", "feishu"),
    ("wecruit.hotjob.cn", "hotjob"), ("hotjob.cn", "hotjob"),
    ("wejob.chinatelecom.com.cn", "chinatelecom-wejob"),
    ("campushr.hikvision.com", "hik"), ("hikvision.com", "hik"),
    ("zhaopin.chnenergy.com.cn", "chnenergy"), ("chnenergy.com.cn", "chnenergy"),
    ("zhaopin.com", "zhilian"), ("cmbchina.com", "cmbchina"),
    ("abchina.com.cn", "abchina"), ("icbc.com.cn", "icbc"),
    ("jsbchina.cn", "jsbchina"), ("hsbank.com.cn", "hsbank"),
]


def now_iso(now: dt.datetime | None = None) -> str:
    return (now or dt.datetime.now().astimezone()).isoformat(timespec="seconds")


def parse_time(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(str(s))
        return t if t.tzinfo else t.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)
    except ValueError:
        return None


def norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


# --------------------------------------------------------------------------
# 小工具：json/jsonl、锁、日志
# --------------------------------------------------------------------------

def read_json(path: str, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return default


def atomic_write(path: str, obj) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".%s." % os.path.basename(path), suffix=".tmp",
                               dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def append_jsonl(path: str, rec: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with reg.DirLock(path):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_jsonl(path: str) -> list[dict]:
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
        if isinstance(rec, dict):
            out.append(rec)
    return out


class Log:
    """stdout + state/dispatcher.log 双写；--dry-run 的动作也进日志（前缀 [dry-run]）。"""

    def __init__(self, path: str, quiet: bool = False):
        self.path, self.quiet = path, quiet
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def __call__(self, msg: str):
        line = "%s %s" % (now_iso(), msg)
        if not self.quiet:
            print(line)
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


# --------------------------------------------------------------------------
# 状态装载
# --------------------------------------------------------------------------

def state_dir(instance: str, *parts) -> str:
    return os.path.join(instance, "state", *parts)


def load_autonomy(instance: str) -> dict:
    path = state_dir(instance, "autonomy.json")
    au = read_json(path, None)
    if au is None:
        au = dict(DEFAULT_AUTONOMY)
        au["tier_modes"] = dict(DEFAULT_AUTONOMY["tier_modes"])
        au["night"] = dict(DEFAULT_AUTONOMY["night"])
        au["updated_by"] = "dispatcher"
        au["updated_at"] = now_iso()
        atomic_write(path, au)
    else:
        for k, v in DEFAULT_AUTONOMY.items():
            au.setdefault(k, v)
        au["tier_modes"] = {**DEFAULT_AUTONOMY["tier_modes"], **(au.get("tier_modes") or {})}
        au["night"] = {**DEFAULT_AUTONOMY["night"], **(au.get("night") or {})}
    return au


def load_jobs(instance: str) -> dict[str, dict]:
    d = state_dir(instance, "jobs")
    out = {}
    if not os.path.isdir(d):
        return out
    for fn in os.listdir(d):
        if not fn.endswith(".json") or fn.startswith(".") or fn.startswith("_"):
            continue
        job = read_json(os.path.join(d, fn), None)
        if isinstance(job, dict) and job.get("announcement_id") is not None:
            job.setdefault("history", [])
            out[str(job["announcement_id"])] = job
    return out


def save_job(instance: str, job: dict) -> None:
    atomic_write(state_dir(instance, "jobs", "%s.json" % job["announcement_id"]), job)


def job_transition(instance: str, job: dict, to: str, by: str = "dispatcher",
                   note: str = "", now: dt.datetime | None = None, log: Log | None = None) -> bool:
    old = job.get("status")
    if old == to:
        return False
    job["status"] = to
    job["history"].append({"at": now_iso(now), "from": old, "to": to, "by": by, "note": note})
    if to not in WORKING:
        job["stuck_since"] = None
    save_job(instance, job)
    if log:
        log("job %s %s: %s → %s%s" % (job["announcement_id"], job.get("company") or "", old, to,
                                    "（%s）" % note if note else ""))
    return True


def load_fit(instance: str) -> dict[str, dict]:
    d = state_dir(instance, "fit")
    out = {}
    if not os.path.isdir(d):
        return out
    for fn in os.listdir(d):
        if not fn.endswith(".json") or fn.startswith("_"):
            continue
        f = read_json(os.path.join(d, fn), None)
        if isinstance(f, dict) and f.get("tier"):
            out[str(f.get("announcement_id") or fn[:-5])] = f
    return out


def load_announcements(path: str) -> dict[str, dict]:
    out = {}
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
        out[str(rec.get("announcement_id"))] = rec
    return out


def load_tiers(path: str) -> tuple[dict[str, str], str]:
    data = yaml.safe_load(open(path, encoding="utf-8")) or {} if os.path.isfile(path) else {}
    mapping = {}
    for tier, names in (data.get("tiers") or {}).items():
        for name in names or []:
            mapping[reg.norm_text(str(name)).lower()] = tier
    return mapping, data.get("default") or "中小"


def tracker_applied(tracker_path: str) -> set[str]:
    """tracker.md 里状态含「已投递」的公司主体键集合。"""
    keys = set()
    if not os.path.isfile(tracker_path):
        return keys
    for row in reg.parse_tracker(tracker_path):
        if "已投递" in (row.get("状态") or "") and row.get("公司"):
            k = reg.employer_key(row["公司"])
            if k:
                keys.add(k)
    return keys


def derive_user_marks(instance: str) -> tuple[set[str], set[str]]:
    """从 commands.done.jsonl 重放用户标记：approved（approve 过且未被后续 skip 撤销）、skipped。"""
    approved, skipped = set(), set()
    for cmd in read_jsonl(state_dir(instance, "commands.done.jsonl")):
        tid = str(cmd.get("target") or "")
        if not tid:
            continue
        if cmd.get("action") == "approve":
            approved.add(tid)
            skipped.discard(tid)
        elif cmd.get("action") == "skip":
            skipped.add(tid)
            approved.discard(tid)
    return approved, skipped


def is_night(au: dict, now: dt.datetime) -> bool:
    night = au.get("night") or {}
    start, end = str(night.get("start") or ""), str(night.get("end") or "")
    if not start or not end or start == end:
        return False
    cur = now.strftime("%H:%M")
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end


# --------------------------------------------------------------------------
# smx-team / notify 外壳
# --------------------------------------------------------------------------

class Shell:
    """所有外部命令走这里，--dry-run 时只记录不执行。"""

    def __init__(self, dry_run: bool, log: Log):
        self.dry_run, self.log = dry_run, log
        self.planned = []

    def run(self, argv: list[str], env_extra: dict | None = None) -> tuple[int, str]:
        shown = " ".join(shlex.quote(a) for a in argv)
        if self.dry_run:
            self.planned.append(shown)
            self.log("[dry-run] $ %s" % shown)
            return 0, ""
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=120,
                               env={**os.environ, **(env_extra or {})})
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.log("命令失败 %s：%s" % (shown, exc))
            return 127, ""
        if p.returncode != 0:
            self.log("命令退出 %d：%s · %s" % (p.returncode, shown, (p.stderr or "")[:300]))
        return p.returncode, p.stdout or ""


class Smx:
    """smx-team 封装（可用 --worker-cmd 换成 stub）。"""

    def __init__(self, shell: Shell, binary: str, from_pane: str):
        self.sh, self.bin, self.from_pane = shell, binary, from_pane

    def _env(self):
        return {"SEEDMUX_PANE_ID": self.from_pane} if self.from_pane else None

    def panes(self) -> list[dict] | None:
        """桥不可用时返回 None（调用方据此跳过依赖 pane 状态的检查，避免误判 pane 消失）。"""
        code, out = self.sh.run([self.bin, "panes", "--json"])
        if code != 0:
            return None
        try:
            return json.loads(out).get("panes", [])
        except ValueError:
            return None

    def spawn(self, task_file: str, cwd: str, acceptance: str, scope: str,
              agent: str, model: str) -> tuple[str, str]:
        argv = [self.bin, "spawn", "--agent", agent, "--cwd", cwd,
                "--task-file", task_file]
        if model:
            argv += ["--model", model]
        if acceptance:
            argv += ["--acceptance", acceptance]
        if scope:
            argv += ["--scope", scope]
        code, out = self.sh.run(argv, self._env())
        return self._parse(code, out)

    def assign(self, pane: str, task_file: str, acceptance: str, scope: str) -> tuple[str, str]:
        code, out = self.sh.run([self.bin, "assign", "--to", pane, "--task-file", task_file,
                                 "--acceptance", acceptance, "--scope", scope])
        return self._parse(code, out)

    def send(self, pane: str, text: str) -> bool:
        if "\n" in text:
            return False
        code, _ = self.sh.run([self.bin, "send", "--to", pane, "--text", text])
        return code == 0

    @staticmethod
    def _parse(code: int, out: str) -> tuple[str, str]:
        if code != 0:
            return "", ""
        m = re.search(r"task=(T-\w+)\s+pane=([0-9A-Fa-f-]+)", out)
        return (m.group(1), m.group(2)) if m else ("", "")


class Notifier:
    """notify.py 封装；调用失败只记日志不炸流程。"""

    def __init__(self, shell: Shell, cmd: str, instance: str):
        self.sh, self.cmd, self.instance = shell, cmd, instance

    def event(self, event: str, title: str = "", body: str = "", company: str = "",
              aid: str = "", url: str = "", date: str = "") -> bool:
        argv = shlex.split(self.cmd) + [event, "--instance", self.instance]
        if title:
            argv += ["--title", title]
        if body:
            argv += ["--body", body]
        if company:
            argv += ["--company", company]
        if aid:
            argv += ["--id", str(aid)]
        if url:
            argv += ["--url", url]
        if date:
            argv += ["--date", date]
        code, _ = self.sh.run(argv)
        return code == 0


# --------------------------------------------------------------------------
# 作业渲染与派单
# --------------------------------------------------------------------------

def submit_level(intent: dict, company: str, company_tier: str, fit_tier: str,
                 mode: str) -> tuple[str, str]:
    """按 redlines §1 + intent.permissions.submit 算提交档位。返回 (worker|user, 依据)。"""
    if mode == "manual":
        return "user", "mode=manual 一律停在提交前"
    perms = ((intent.get("permissions") or {}).get("submit")) or {}
    confirms = {norm(x) for x in perms.get("user_confirm_companies") or []}
    if norm(company) in confirms or reg.employer_key(company) in {reg.employer_key(x) for x in confirms}:
        return "user", "公司在 user_confirm_companies"
    if fit_tier == "练手" and perms.get("practice", "auto") == "auto":
        return "worker", "练手档 practice=auto"
    if company_tier in set(perms.get("user_confirm_tiers") or []):
        return "user", "层级 %s 在 user_confirm_tiers" % company_tier
    default = perms.get("default", "worker")
    return ("user" if default == "user" else "worker"), "default=%s" % default


def render_task(template: str, job: dict, ctx: dict) -> str:
    intent, night = ctx["intent"], ctx["night_now"]
    perms = intent.get("permissions") or {}
    reg_policy = perms.get("register_accounts", "user")
    attach = (intent.get("policies") or {}).get("attachments_required", "hold")
    attach_text = {"hold": "hold：看板标待投递，备注「待材料：需XX，门户截止YYYY-MM-DD」，报主持人",
                   "skip": "skip：标不合适并写原因"}.get(attach, attach)
    fields = {
        "COMPANY": job.get("company") or "",
        "ANNOUNCEMENT_ID": str(job["announcement_id"]),
        "TITLE": job.get("title") or "",
        "CLASS_TIME": job.get("class_time") or "见公告原文",
        "LINK": job.get("link") or "—",
        "APPLY_LINK": job.get("apply_link") or job.get("link") or "—",
        "CITIES": "、".join(job.get("cities") or []) or "见公告",
        "EXPIRED_AT": job.get("expired_at") or "看板未录，进门户核实",
        "TIER": job.get("tier") or "常规",
        "COMPANY_TIER": job.get("company_tier") or "中小",
        "FIT_ROLES": "、".join(job.get("fit_roles") or []) or "进门户自判",
        "FIT_RISKS": "、".join(job.get("fit_risks") or []) or "无",
        "SUBMIT_LEVEL": job.get("submit_level") or "user",
        "SUBMIT_BASIS": job.get("submit_basis") or "工单未注明→按最严 user 档",
        "REGISTER_POLICY": reg_policy,
        "ATTACH_POLICY": attach_text,
        "FOREIGN_CV": (intent.get("policies") or {}).get("foreign_cv", "en"),
        "INSTANCE": ctx["instance"],
        "REPO": ctx["repo"],
        "BATCH": job.get("batch") or "",
        "EXTRA": job.get("extra") or "",
    }
    if night:
        fields["EXTRA"] = (fields["EXTRA"] + "\n- 注意：本单在夜间时段派出，硬门槛按下面「夜间门槛」节处理").strip()
    out = template
    for k, v in fields.items():
        out = out.replace("{{%s}}" % k, str(v))
    return out


def new_job(aid: str, ann: dict | None, fit: dict | None, source: str, batch: str,
            intent: dict, tier_map: dict, default_tier: str, mode: str) -> dict:
    ann = ann or {}
    fit = fit or {}
    company = fit.get("company") or ann.get("company") or ""
    fit_tier = fit.get("tier") or "常规"
    company_tier = tier_map.get(reg.norm_text(company).lower()) or (
        "国企" if {"国企", "事业单位"} & set(ann.get("company_tags") or []) else default_tier)
    level, basis = submit_level(intent, company, company_tier, fit_tier, mode)
    return {
        "announcement_id": int(aid), "company": company, "tier": fit_tier,
        "company_tier": company_tier, "source": source, "status": None,
        "task_id": None, "pane": None, "portal": None,
        "title": ann.get("title") or "", "link": ann.get("link") or "",
        "apply_link": ann.get("from_url") or ann.get("link") or "",
        "class_time": ann.get("class_time") or "", "cities": ann.get("cities") or [],
        "expired_at": ann.get("expired_at") or "", "fit_roles": fit.get("roles") or [],
        "fit_risks": fit.get("risks") or [], "fit_score": fit.get("fit") or 0,
        "submit_level": level, "submit_basis": basis,
        "batch": batch, "gate": None, "field_table": None,
        "nudges": 0, "stuck_since": None, "prev_status": None,
        "parked": False, "parked_at": None, "deadline_reminded": False,
        "created_at": now_iso(), "history": [],
    }


# --------------------------------------------------------------------------
# 一轮调度
# --------------------------------------------------------------------------

class Dispatcher:
    def __init__(self, args):
        self.args = args
        self.instance = os.path.abspath(os.path.expanduser(args.instance))
        self.repo = os.path.abspath(args.repo or REPO)
        self.log = Log(state_dir(self.instance, "dispatcher.log"))
        self.shell = Shell(args.dry_run, self.log)
        self.smx = Smx(self.shell, args.worker_cmd, args.from_pane or "")
        self.notifier = Notifier(self.shell, args.notify_cmd, self.instance)
        self.tasks_dir = os.path.abspath(os.path.expanduser(args.tasks_dir))
        self.announcements = load_announcements(args.data)
        self.tier_map, self.default_tier = load_tiers(args.tiers)
        self.intent = read_json(os.path.join(self.instance, "intent.yaml"), None)
        if self.intent is None:
            self.intent = {}
            try:
                self.intent = yaml.safe_load(
                    open(os.path.join(self.instance, "intent.yaml"), encoding="utf-8")) or {}
            except OSError:
                self.log("WARN: 读不到 intent.yaml，权限按最严处理")
        self.template_path = os.path.join(self.repo, "templates", "worker-task.md")
        self.state_path = state_dir(self.instance, "dispatcher-state.json")

    # ---------------- 指令 ----------------

    @staticmethod
    def _is_fast(cmd: dict) -> bool:
        """§8.6 时效指令：reply/submit/refresh/takeover；带 handoff_id 的 resume 视同 refresh。"""
        a = cmd.get("action")
        return a in FAST_COMMANDS or (a == "resume" and bool((cmd.get("args") or {}).get("handoff_id")))

    def consume_commands(self, jobs: dict[str, dict], au: dict, now: dt.datetime,
                         only_fast: bool = False) -> int:
        pending_path = state_dir(self.instance, "commands.jsonl")
        done_path = state_dir(self.instance, "commands.done.jsonl")
        done_ids = {str(c.get("id")) for c in read_jsonl(done_path)}
        n = 0
        for cmd in read_jsonl(pending_path):
            cid = str(cmd.get("id") or "")
            if not cid or cid in done_ids:
                continue
            if only_fast and not self._is_fast(cmd):
                continue
            result = self.apply_command(cmd, jobs, au, now)
            rec = dict(cmd)
            rec.update({"done_at": now_iso(now), "result": result})
            append_jsonl(done_path, rec)
            self.log("cmd %s %s → %s" % (cid, cmd.get("action"), result))
            n += 1
        return n

    def fast_poll(self, now: dt.datetime):
        """1 秒级短轮询里只处理时效指令；其余留给 30 秒大循环。"""
        au = load_autonomy(self.instance)
        jobs = load_jobs(self.instance)
        self.consume_commands(jobs, au, now, only_fast=True)

    def apply_command(self, cmd: dict, jobs: dict[str, dict], au: dict, now: dt.datetime) -> str:
        action = cmd.get("action")
        target = str(cmd.get("target") or "")
        args = cmd.get("args") or {}
        job = jobs.get(target)
        if action == "resume" and args.get("handoff_id"):
            action = "refresh"                      # §8.8：兼容旧版「重新获取」写法
        if action not in COMMANDS:
            return "error:未知动作 %s" % action
        if action in ("approve", "skip", "hold", "resume", "takeover", "submit", "reply",
                      "refresh") and not target:
            return "error:缺 target"

        if action == "approve":
            if job and job.get("status") not in TERMINAL:
                return "ok:作业已存在 %s" % job["status"]
            if job and job.get("status") == "skipped":
                job_transition(self.instance, job, "queued", by="user", note="approve 撤销 skip",
                               now=now, log=self.log)
                return "ok:已撤销 skip 重新入队"
            if job:
                return "error:作业已终结 %s" % job["status"]
            aid = target
            fit = load_fit(self.instance).get(aid)
            ann = self.announcements.get(aid)
            if not ann and not fit and not args.get("company"):
                return "error:公告 %s 无数据，approve 需 args.company" % aid
            j = new_job(aid, ann, fit, "manual" if au.get("mode") == "manual" else "approved",
                        self.batch_name(now), self.intent, self.tier_map, self.default_tier,
                        au.get("mode", "supervised"))
            if args.get("company") and not j.get("company"):
                j["company"] = args["company"]
            job_transition(self.instance, j, "queued", by="user", note="approve", now=now,
                           log=self.log)
            jobs[aid] = j
            return "ok:已入队"

        if action == "skip":
            if not job:
                return "ok:无作业（已记入 skipped 名单）"
            if job.get("status") in TERMINAL:
                return "ok:作业已终结 %s" % job["status"]
            if job.get("pane") and job.get("task_id") and not job.get("parked"):
                self.send_to_worker(job, "[team-msg task=%s] 用户取消了本单：停止操作、关浏览器空间，回执 skipped"
                                    % job["task_id"])
            job_transition(self.instance, job, "skipped", by="user", note="用户 skip",
                           now=now, log=self.log)
            return "ok:已跳过"

        if action == "hold":
            if not job or job.get("status") not in ACTIVE:
                return "error:没有可暂停的活动作业"
            job["prev_status"] = job["status"]
            if not job.get("parked"):
                self.send_to_worker(job, "[team-msg task=%s] 用户 hold：停在当前安全点（不丢表单），等恢复通知"
                                    % job.get("task_id"))
            job_transition(self.instance, job, "held", by="user", note="hold", now=now, log=self.log)
            return "ok:已暂停"

        if action == "resume":
            if not job or job.get("status") != "held":
                return "error:作业不在 held"
            back = job.get("prev_status") or "filling"
            job["prev_status"] = None
            self.clear_portal_pause(self.portal_of(job))   # 门户暂停的单被恢复 = 用户要放行
            if job.get("pane") and job.get("task_id"):
                self.send_to_worker(job, "[team-msg task=%s] 用户 resume：继续按工单执行"
                                    % job.get("task_id"))
            job_transition(self.instance, job, back, by="user", note="resume", now=now, log=self.log)
            return "ok:恢复 %s" % back

        if action == "refresh":
            # §8.7/§8.8「重新获取」：在跑作业 → 转 worker 刷新二维码；parked → 重派
            if not job:
                return "error:无作业"
            if job.get("parked"):
                self.resolve_handoffs(job, note="refresh 重新获取，重派")
                job.update({"parked": False, "parked_at": None, "gate": None,
                            "task_id": None, "deadline_reminded": False})
                job_transition(self.instance, job, "queued", by="user",
                               note="[重新获取] parked 重派（优先回原 pane 保登录态）",
                               now=now, log=self.log)
                return "ok:parked 已重新入队"
            if job.get("status") in TERMINAL:
                return "error:作业已终结 %s" % job["status"]
            if job.get("status") in ("queued", "held"):
                return "ok:作业在 %s，无需刷新" % job["status"]
            hid = str(args.get("handoff_id") or "")
            ok = self.send_to_worker(
                job, "[team-msg task=%s] 用户点了「重新获取」：请刷新二维码并 "
                     "handoff-update --id %s --shot <新截图> 原地换图"
                     % (job.get("task_id"), hid or "<handoff_id>"))
            return "ok:已转发 worker 刷新" if ok else "error:无 pane 可转发"

        if action == "takeover":
            if not job or job.get("status") not in ACTIVE:
                return "error:没有可接管的活动作业"
            if job.get("parked"):
                # parked 的 worker 已交还浏览器；直接转 held + 关掉挂着的 handoff
                self.resolve_handoffs(job, note="takeover")
                job["prev_status"] = "gate_wait"
            else:
                self.send_to_worker(job, "[team-msg task=%s] 用户接管：handOff() 浏览器并停止操作，不要回执"
                                    % job.get("task_id"))
                job["prev_status"] = job["status"]
            job["parked"] = False
            job_transition(self.instance, job, "held", by="user", note="用户接管",
                           now=now, log=self.log)
            return "ok:已转用户接管"

        if action == "submit":
            if job and job.get("parked"):
                return "error:作业 parked 无 worker，请先点「重新获取」重派"
            if not job or job.get("status") not in {"review_wait", "gate_wait", "held"}:
                return "error:作业 %s 不在可提交状态" % (job.get("status") if job else "不存在")
            self.send_to_worker(job, "[team-msg task=%s] 用户已终审（或已本人提交）：执行阶段 3 门户回读核验；"
                                "若用户未点提交则按授权点最终提交。不要重复点提交" % job.get("task_id"))
            self.resolve_handoffs(job, note="submit 指令")
            job_transition(self.instance, job, "submitting", by="user", note="submit",
                           now=now, log=self.log)
            return "ok:已通知 worker 进入核验"

        if action == "reply":
            if not job:
                return "error:无作业"
            if job.get("parked"):
                # 用户在关卡消息上回了内容（如短信码）：重派并把话带给新 worker
                self.resolve_handoffs(job, note="reply 触发重派")
                job.update({"parked": False, "parked_at": None, "gate": None,
                            "task_id": None, "deadline_reminded": False,
                            "pending_reply": str(args.get("text") or "")})
                job_transition(self.instance, job, "queued", by="user",
                               note="parked 作业收到回复，重派并转达", now=now, log=self.log)
                return "ok:已重派并转达"
            if not job.get("pane"):
                return "error:作业无 pane"
            text = str(args.get("text") or "").strip()
            if not text:
                return "error:空消息"
            if len(text) > 200 or "\n" in text:
                inbox = state_dir(self.instance, "inbox")
                os.makedirs(inbox, exist_ok=True)
                fp = os.path.join(inbox, "%s-%s.txt" % (target, now.strftime("%H%M%S")))
                with open(fp, "w", encoding="utf-8") as fh:
                    fh.write(text)
                text = "请读文件 %s（用户给你的长消息）" % fp
            ok = self.send_to_worker(job, "[team-msg task=%s] 用户说：%s" % (job.get("task_id"), text))
            return "ok:已转发" if ok else "error:send 失败"

        if action == "mode":
            mode = str(args.get("mode") or "")
            if mode not in ("manual", "supervised", "auto"):
                return "error:mode 取值 manual|supervised|auto"
            tier = str(args.get("tier") or "")
            if tier:
                if tier not in TIER_ORDER:
                    return "error:tier 取值 冲|常规|练手"
                au.setdefault("tier_modes", {})[tier] = mode
            else:
                au["mode"] = mode
            au["updated_by"] = cmd.get("by") or "cli"
            au["updated_at"] = now_iso(now)
            atomic_write(state_dir(self.instance, "autonomy.json"), au)
            return "ok:%smode=%s" % (("tier_modes.%s " % tier) if tier else "", mode)

        if action in ("pause", "unpause"):
            au["paused"] = (action == "pause")
            au["updated_by"] = cmd.get("by") or "cli"
            au["updated_at"] = now_iso(now)
            atomic_write(state_dir(self.instance, "autonomy.json"), au)
            return "ok:paused=%s" % au["paused"]

        return "error:未处理"

    # ---------------- 入队 ----------------

    def batch_name(self, now: dt.datetime) -> str:
        return "auto-%s" % now.strftime("%Y%m%d")

    def enqueue(self, jobs: dict[str, dict], au: dict, approved: set[str], skipped: set[str],
                now: dt.datetime):
        mode = au.get("mode", "supervised")
        tier_modes = au.get("tier_modes") or {}
        fit_all = load_fit(self.instance)
        for aid, fit in fit_all.items():
            tier = fit.get("tier")
            if tier in (None, "不合适") or aid in jobs or aid in skipped:
                continue
            eff = tier_modes.get(tier) or mode
            if eff == "auto":
                source = "auto"
            elif aid in approved:
                source = "approved" if eff == "supervised" else "manual"
            else:
                continue
            j = new_job(aid, self.announcements.get(aid), fit, source, self.batch_name(now),
                        self.intent, self.tier_map, self.default_tier, mode)
            job_transition(self.instance, j, "queued", by="dispatcher",
                           note="入队（%s 档 %s）" % (eff, tier), now=now, log=self.log)
            jobs[aid] = j
        # manual/supervised 下，approve 的 id 没有 fit 记录时由 apply_command 建单，这里不重复。

    # ---------------- 推进（只看标记文件，不解析 pane 输出） ----------------

    def outcome_path(self, aid) -> str:
        return state_dir(self.instance, "outcomes", "%s.json" % aid)

    def fields_path(self, aid) -> str:
        return os.path.join(self.instance, "log", "fields", "%s.md" % aid)

    def task_meta(self, task_id: str) -> dict:
        if not task_id:
            return {}
        return read_json(os.path.join(self.tasks_dir, task_id, "meta.json"), {})

    def task_reply(self, task_id: str) -> str:
        if not task_id:
            return ""
        try:
            return open(os.path.join(self.tasks_dir, task_id, "reply.md"), encoding="utf-8").read()
        except OSError:
            return ""

    def open_handoffs(self, aid) -> list[dict]:
        d = state_dir(self.instance, "handoffs")
        out = []
        if not os.path.isdir(d):
            return out
        for fn in os.listdir(d):
            if not fn.endswith(".json") or fn.startswith("."):
                continue
            rec = read_json(os.path.join(d, fn), None)
            if isinstance(rec, dict) and str(rec.get("announcement_id")) == str(aid) \
                    and not rec.get("resolved_at"):
                out.append(rec)
        return out

    def resolve_handoffs(self, job: dict, note: str = ""):
        for rec in self.open_handoffs(job["announcement_id"]):
            rec["resolved_at"] = now_iso()
            if note:
                rec["resolved_by"] = "dispatcher:%s" % note
            atomic_write(state_dir(self.instance, "handoffs", "%s.json" % rec["id"]), rec)

    def send_to_worker(self, job: dict, text: str) -> bool:
        pane = job.get("pane")
        if not pane or "\n" in text:
            return False
        ok = self.smx.send(pane, text)
        self.log("→ pane %s：%s%s" % (str(pane)[:8], text[:80], "" if ok else "（发送失败）"))
        return ok

    def handoff_file(self, rec: dict) -> str:
        return state_dir(self.instance, "handoffs", "%s.json" % rec.get("id"))

    @staticmethod
    def _gate_exhausted(rec: dict) -> bool:
        """§8.7：二维码过期且自动刷新次数用尽 → 该关卡的兜底是 parked。"""
        return rec.get("status") == "expired" and \
            int(rec.get("refresh_count") or 0) >= int(rec.get("max_refresh") or 3)

    def portal_of(self, job: dict, rec: dict | None = None) -> str:
        """作业所属门户：job.portal > handoff.portal > 投递链接域名映射 > 域名本身。"""
        p = job.get("portal") or (rec or {}).get("portal") or ""
        if p:
            return reg.norm_portal(p)
        import urllib.parse
        for url in (job.get("apply_link") or "", job.get("link") or ""):
            url = str(url).strip()
            if not url or "@" in url and "://" not in url:   # 邮件类公告的 from_url 是文本
                continue
            host = urllib.parse.urlparse(url if "://" in url else "https://" + url).netloc.lower()
            if not host:
                continue
            for pat, name in PORTAL_DOMAINS:
                if pat in host:
                    return name
            return host                       # 未识别门户按域名归组
        return ""

    def park_job(self, job: dict, rec: dict, now: dt.datetime):
        """§8.7：刷新次数用尽 → gate_wait:parked，释放 pane 去下一家，通知一次。"""
        job["parked"] = True
        job["parked_at"] = now_iso(now)
        if not job.get("portal"):
            p = self.portal_of(job, rec)
            if p:
                job["portal"] = p
        job["gate"] = {"kind": "parked", "handoff_id": rec.get("id"), "since": job["parked_at"]}
        if job.get("status") != "gate_wait":
            job_transition(self.instance, job, "gate_wait",
                           note="关卡 expired 且刷新用尽 → parked，释放 pane", now=now, log=self.log)
        else:
            save_job(self.instance, job)
            self.log("job %s %s: gate_wait → parked（释放 pane）"
                     % (job["announcement_id"], job.get("company") or ""))
        self.notifier.event("blocked", company=job.get("company") or "", aid=str(job["announcement_id"]),
                            title="关卡搁置 · %s" % (job.get("company") or job["announcement_id"]),
                            body="%s 刷新 %s 次仍未处理，worker 已释放去下一家；方便时在该条消息点「重新获取」"
                                 % (handoffs.KIND_LABEL.get(rec.get("kind") or "", "扫码")
                                    if handoffs else "扫码", rec.get("max_refresh") or 3))
        self.record_park(job, now)

    def record_park(self, job: dict, now: dt.datetime):
        """同门户 24h parked ≥3 → 暂停该门户后续作业（state + 战报标出）。"""
        st = read_json(self.state_path, {})
        events = [e for e in (st.get("park_events") or [])
                  if parse_time(e.get("at")) and (now - parse_time(e["at"])).total_seconds() < 7 * 86400]
        portal = job.get("portal") or ""
        events.append({"aid": job["announcement_id"], "company": job.get("company") or "",
                       "portal": portal, "at": now_iso(now)})
        st["park_events"] = events
        if portal:
            recent = [e for e in events if e.get("portal") == portal
                      and (now - parse_time(e["at"])).total_seconds() < PORTAL_PARK_WINDOW]
            pauses = st.setdefault("portal_pauses", {})
            if len(recent) >= PORTAL_PARK_LIMIT and portal not in pauses:
                pauses[portal] = {"since": now_iso(now), "count": len(recent)}
                self.notifier.event("blocked", title="门户暂停 · %s" % portal,
                                    body="%d 小时内 %d 单在 %s 关卡 parked，该门户后续作业已暂停；"
                                         "看 /today 或战报决定是否恢复" % (PORTAL_PARK_WINDOW // 3600,
                                                                         len(recent), portal))
                self.log("门户 %s %dh 内 parked %d 次 → 暂停该门户" % (
                    portal, PORTAL_PARK_WINDOW // 3600, len(recent)))
        atomic_write(self.state_path, st)

    def portal_pauses(self) -> dict:
        return read_json(self.state_path, {}).get("portal_pauses") or {}

    def clear_portal_pause(self, portal: str):
        if not portal:
            return
        st = read_json(self.state_path, {})
        if portal in (st.get("portal_pauses") or {}):
            st["portal_pauses"].pop(portal)
            atomic_write(self.state_path, st)
            self.log("门户 %s 暂停解除" % portal)
            self.restore_portal_jobs(portal)

    def restore_portal_jobs(self, portal: str):
        for job in load_jobs(self.instance).values():
            if job.get("status") == "held" and job.get("hold_reason") == "portal_pause:%s" % portal:
                back = job.get("prev_status") or "queued"
                job["prev_status"], job["hold_reason"] = None, None
                job_transition(self.instance, job, back, by="dispatcher",
                               note="门户暂停解除，自动恢复", now=None, log=self.log)

    def park_watch(self, job: dict, now: dt.datetime):
        """parked 作业的后勤：48h → skipped:gate_timeout；截止前 2 天提醒一次。"""
        parked_at = parse_time(job.get("parked_at")) or parse_time((job.get("gate") or {}).get("since"))
        if not parked_at:
            return
        if (now - parked_at).total_seconds() > PARK_TIMEOUT_SEC:
            job["parked"] = False
            job_transition(self.instance, job, "skipped",
                           note="gate_timeout：关卡 parked 超 48 小时", now=now, log=self.log)
            self.notifier.event("blocked", company=job.get("company") or "",
                                aid=str(job["announcement_id"]),
                                title="关卡超时跳过 · %s" % (job.get("company") or ""),
                                body="parked 超 48 小时未处理，已跳过；要投可 approve 重派")
            return
        exp = parse_time(job.get("expired_at"))
        if exp and not job.get("deadline_reminded") \
                and (exp - now).total_seconds() <= PARK_REMIND_SEC:
            self.notifier.event("deadline", company=job.get("company") or "",
                                aid=str(job["announcement_id"]), date=job.get("expired_at"),
                                title="临截止仍卡关 · %s" % (job.get("company") or ""),
                                body="%s 截止，作业仍 parked 在关卡；不处理将在 parked 48h 后跳过"
                                     % job.get("expired_at"))
            job["deadline_reminded"] = True
            save_job(self.instance, job)

    def scanned_check(self, job: dict, opens: list[dict], now: dt.datetime):
        """§8.7：scanned 超 60 秒页面没跳转 → 叫 worker 刷新页面检测登录态（每条 handoff 一次）。"""
        for rec in opens:
            if rec.get("status") != "scanned" or rec.get("scanned_check_at"):
                continue
            base = parse_time(rec.get("interacted_at")) or parse_time(rec.get("scanned_at"))
            if base is None:
                try:
                    base = dt.datetime.fromtimestamp(
                        os.path.getmtime(self.handoff_file(rec))).astimezone()
                except OSError:
                    base = parse_time(rec.get("created_at"))
            if base is None or (now - base).total_seconds() <= SCANNED_CHECK_SEC:
                continue
            ok = self.send_to_worker(
                job, "[team-msg task=%s] 扫码已超 60 秒页面未跳转：刷新页面检测登录态——"
                     "已登录→继续填报并 handoff-update --id %s --status resolved；"
                     "未登录→按工单走过期分支（刷新二维码 + handoff-update --shot）"
                     % (job.get("task_id"), rec.get("id")))
            if ok or self.args.dry_run:
                rec["scanned_check_at"] = now_iso(now)
                atomic_write(self.handoff_file(rec), rec)

    def monitor(self, jobs: dict[str, dict], au: dict, now: dt.datetime, panes: list[dict] | None):
        pane_state = {p.get("paneId"): p for p in (panes or [])}
        night = is_night(au, now)
        queue = self.night_queue()
        for job in jobs.values():
            st = job.get("status")
            if st not in ACTIVE:
                continue
            aid = job["announcement_id"]

            # 1) 终局 outcome（worker 写 state/outcomes/<id>.json）
            outcome = read_json(self.outcome_path(aid), None)
            if outcome and st not in TERMINAL:
                self.apply_outcome(job, outcome, now)
                continue

            # parked：worker 已交还浏览器，只做超时/临截止后勤，不查 pane 不催
            if job.get("parked"):
                self.park_watch(job, now)
                continue

            # 2) handoff：未 resolved → gate_wait / review_wait；resolved → 回 filling 并放行
            opens = self.open_handoffs(aid)
            if opens:
                exhausted = [r for r in opens if self._gate_exhausted(r)]
                if exhausted:
                    self.park_job(job, exhausted[0], now)
                    continue
                kinds = {r.get("kind") for r in opens}
                if "submit_confirm" in kinds:
                    if st != "review_wait":
                        job["gate"] = {"kind": "submit_confirm",
                                       "handoff_id": opens[0].get("id"), "since": now_iso(now)}
                        job_transition(self.instance, job, "review_wait",
                                       note="submit_confirm handoff", now=now, log=self.log)
                elif st != "gate_wait":
                    job["gate"] = {"kind": sorted(kinds)[0],
                                   "handoff_id": opens[0].get("id"), "since": now_iso(now)}
                    job_transition(self.instance, job, "gate_wait",
                                   note="等用户：%s" % job["gate"]["kind"], now=now, log=self.log)
                    if night and (au.get("night") or {}).get("gate_policy") == "queue":
                        job["night_queued"] = True
                        queue.append({"id": aid, "company": job.get("company"),
                                      "kind": job["gate"]["kind"], "at": now_iso(now)})
                        self.save_night_queue(queue)
                st = job.get("status")
                self.scanned_check(job, opens, now)
            elif st == "gate_wait" and job.get("gate"):
                job["gate"] = None
                job_transition(self.instance, job, "filling", note="handoff 已处理",
                               now=now, log=self.log)
                self.send_to_worker(job, "[team-msg task=%s] 之前等用户的事已处理，继续按工单执行"
                                    % job.get("task_id"))
                st = "filling"

            # 3) 字段对照表出现 = worker 停在提交前（submitting 不回弹：用户已放行）
            if st in ("dispatched", "filling") and os.path.isfile(self.fields_path(aid)):
                job["field_table"] = "log/fields/%s.md" % aid
                job_transition(self.instance, job, "review_wait", note="字段表已写，停在提交前",
                               now=now, log=self.log)
                st = "review_wait"

            # 4) worker 回执（meta.json status / reply.md）
            meta = self.task_meta(job.get("task_id"))
            mst = str(meta.get("status") or "")
            if mst.startswith("replied:"):
                self.apply_reply(job, mst.split(":", 1)[1], now)
                continue

            # 5) pane 存活（桥不可用 panes=None 时跳过，不误判）
            pane = job.get("pane")
            if pane and panes is not None:
                p = pane_state.get(pane)
                if p is None or p.get("state") == "exited":
                    job_transition(self.instance, job, "failed", note="worker pane 消失",
                                   now=now, log=self.log)
                    self.notifier.event("blocked", company=job.get("company") or "",
                                        aid=str(aid), body="worker pane 消失，需人工核查后重派")
                    continue
                # dispatched 且 pane 在干活 → filling
                if st == "dispatched" and p.get("state") not in ("idle",):
                    job_transition(self.instance, job, "filling", note="worker 已接单",
                                   now=now, log=self.log)

    def apply_outcome(self, job: dict, outcome: dict, now: dt.datetime):
        aid = job["announcement_id"]
        result = outcome.get("result")
        # §9.2：blocked/skipped/failed 必填 reason；note 是旧写法的自由结论，作兜底
        note = outcome.get("reason") or outcome.get("note") or ""
        job["outcome"] = outcome
        if outcome.get("portal"):
            job["portal"] = outcome["portal"]
        job_applied = outcome.get("job_applied") or outcome.get("job") or ""
        if result in ("submitted", "verified"):
            to = "verified" if result == "verified" else "submitted"
            job_transition(self.instance, job, to, note=note or "worker 回执%s" % result,
                           now=now, log=self.log)
            try:
                reg.add(self.instance, job.get("company") or "", brand=job.get("company") or "",
                        portal=job.get("portal") or outcome.get("portal") or "",
                        account=outcome.get("account") or "",
                        announcement_ids=[aid], job=job_applied,
                        status="submitted" if to == "submitted" else "verified")
            except Exception as exc:  # 登记失败不能炸调度，但要留痕
                self.log("WARN: registry 写入失败 %s：%s" % (aid, exc))
            self.notifier.event("batch-done", title="已投递 · %s" % (job.get("company") or aid),
                                body="%s · %s" % (job_applied, note), company=job.get("company") or "",
                                aid=str(aid))
        elif result == "skipped":
            job_transition(self.instance, job, "skipped", note=note or "worker 跳过",
                           now=now, log=self.log)
        elif result == "failed":
            job_transition(self.instance, job, "failed", note=note or "worker 报失败",
                           now=now, log=self.log)
            self.notifier.event("blocked", company=job.get("company") or "", aid=str(aid),
                                body="失败：%s" % (note or "无原因"))
        elif result in ("blocked", "unfit", "need_materials"):
            # §9.2 枚举是 submitted|blocked|skipped|failed；
            # 旧的 unfit/need_materials 映射进 blocked 并保留原因
            prefix = {"unfit": "不合适", "need_materials": "待材料"}.get(result)
            body = ("%s：%s" % (prefix, note)) if prefix else (note or "worker blocked")
            job_transition(self.instance, job, "blocked", note=body, now=now, log=self.log)
            self.notifier.event("blocked", company=job.get("company") or "", aid=str(aid),
                                body=body)
        else:
            job_transition(self.instance, job, "blocked", note=note or "worker blocked",
                           now=now, log=self.log)
            self.notifier.event("blocked", company=job.get("company") or "", aid=str(aid),
                                body=note or "worker blocked")

    def apply_reply(self, job: dict, rstatus: str, now: dt.datetime):
        aid = job["announcement_id"]
        if rstatus == "done":
            outcome = read_json(self.outcome_path(aid), None)
            if outcome:
                self.apply_outcome(job, outcome, now)
                return
            first = (self.task_reply(job.get("task_id")).strip().splitlines() or [""])[0]
            if re.search(r"已投递|投递成功|提交成功", first):
                job_transition(self.instance, job, "submitted",
                               note="回执称已投递（无 outcome 文件，建议人工复核）", now=now, log=self.log)
            elif "不合适" in first:
                job_transition(self.instance, job, "skipped", note="回执：%s" % first[:60],
                               now=now, log=self.log)
            else:
                job_transition(self.instance, job, "blocked",
                               note="回执 done 但无 outcome，语义不明需人工核对", now=now, log=self.log)
                self.notifier.event("blocked", company=job.get("company") or "", aid=str(aid),
                                    body="回执 done 但无 outcome 文件")
        elif rstatus == "blocked":
            first = (self.task_reply(job.get("task_id")).strip().splitlines() or [""])[0]
            job_transition(self.instance, job, "blocked", note="worker blocked：%s" % first[:80],
                           now=now, log=self.log)
            self.notifier.event("blocked", company=job.get("company") or "", aid=str(aid),
                                body=first[:120] or "worker blocked")
        elif rstatus == "failed":
            job_transition(self.instance, job, "failed", note="worker failed", now=now, log=self.log)
            self.notifier.event("blocked", company=job.get("company") or "", aid=str(aid),
                                body="worker failed，需人工核查")

    # ---------------- 自停巡检 ----------------

    def nudge_check(self, jobs: dict[str, dict], now: dt.datetime, panes: list[dict] | None):
        if panes is None:
            return
        pane_state = {p.get("paneId"): p for p in panes}
        limit = self.args.max_nudges
        for job in jobs.values():
            if job.get("status") not in WORKING:
                continue
            pane = pane_state.get(job.get("pane") or "")
            if pane is None or pane.get("state") != "idle":
                if job.get("stuck_since") is not None:
                    job["stuck_since"] = None
                    save_job(self.instance, job)
                continue
            since = parse_time(job.get("stuck_since"))
            if since is None:
                job["stuck_since"] = now_iso(now)
                save_job(self.instance, job)
                continue
            idle_min = (now - since).total_seconds() / 60
            if idle_min < self.args.nudge_after_min:
                continue
            if job.get("nudges", 0) >= limit:
                job_transition(self.instance, job, "blocked",
                               note="nudge %d 次仍空转，转人工" % limit, now=now, log=self.log)
                self.notifier.event("blocked", company=job.get("company") or "",
                                    aid=str(job["announcement_id"]),
                                    body="worker 空转，nudge %d 次无响应" % limit)
                continue
            job["nudges"] = job.get("nudges", 0) + 1
            job["stuck_since"] = now_iso(now)
            save_job(self.instance, job)
            self.send_to_worker(job, "[team-msg task=%s] 继续按工单执行；先读 %s/log/ops/ 下本单留痕最后一节确认进度（第 %d/%d 次提醒）"
                                % (job.get("task_id"), self.instance, job["nudges"], limit))

    # ---------------- 派单 ----------------

    def dispatched_today(self, jobs: dict[str, dict], today: str) -> int:
        n = 0
        for job in jobs.values():
            for h in job.get("history") or []:
                if h.get("to") == "dispatched" and str(h.get("at", "")).startswith(today):
                    n += 1
                    break
        return n

    def dispatch(self, jobs: dict[str, dict], au: dict, registry_recs: list[dict],
                 tracker_keys: set[str], now: dt.datetime, panes: list[dict]):
        if au.get("paused"):
            return
        today = now.strftime("%Y-%m-%d")
        active = [j for j in jobs.values() if (j.get("status") in ACTIVE or
                  (j.get("status") == "held" and j.get("pane"))) and not j.get("parked")]
        max_par = int(au.get("max_parallel") or 5)
        cap = int(au.get("daily_cap") or 50)
        used_today = self.dispatched_today(jobs, today)
        pane_map = {p.get("paneId"): p for p in (panes or [])}
        busy_panes = {j.get("pane") for j in active}
        pool_panes = {j.get("pane") for j in jobs.values() if j.get("pane")}
        # 可复用 pane = 池内且不属于在途作业（终局/skipped/parked 的 worker 可直接接新单）；
        # smx 报 idle 但作业仍在途的 pane 不可复用（那是卡住，走 nudge 路径）
        free_panes = sorted(pool_panes - busy_panes)

        pauses = self.portal_pauses()
        queue = [j for j in jobs.values() if j.get("status") == "queued"]
        # 排序：截止近优先 → 档（冲→常规→练手）→ 分数高优先
        queue.sort(key=lambda j: (j.get("expired_at") or "9999",
                                  TIER_ORDER.get(j.get("tier"), 3),
                                  -(j.get("fit_score") or 0),
                                  str(j["announcement_id"])))
        for job in queue:
            if len(active) >= max_par or used_today >= cap:
                return
            if job.get("task_id"):          # 幂等：已有 task_id 的作业绝不重派
                continue
            portal = self.portal_of(job)
            if portal and portal in pauses:
                job["prev_status"] = "queued"
                job["hold_reason"] = "portal_pause:%s" % portal
                job_transition(self.instance, job, "held", by="dispatcher",
                               note="门户 %s %dh 内 parked≥%d，暂停该门户（resume 恢复）"
                                    % (portal, PORTAL_PARK_WINDOW // 3600, PORTAL_PARK_LIMIT),
                               now=now, log=self.log)
                continue
            # 防重投：registry 命中 / tracker 已投
            ekey = reg.employer_key(job.get("company") or "")
            hits = reg.hit(registry_recs, ekey, None) if ekey else []
            if hits:
                job_transition(self.instance, job, "skipped",
                               note="已投过（registry %s@%s）" % (hits[0].get("employer"),
                                                              hits[0].get("portal") or "任意门户"),
                               now=now, log=self.log)
                continue
            if ekey and ekey in tracker_keys:
                job_transition(self.instance, job, "skipped", note="tracker 已投",
                               now=now, log=self.log)
                continue
            if self.args.dry_run:
                self.log("[dry-run] 将派单 %s %s（%s档 submit=%s）" % (
                    job["announcement_id"], job.get("company"), job.get("tier"), job.get("submit_level")))
                continue
            tid, pane = self.dispatch_one(job, now, is_night(au, now), free_panes)
            if not tid:
                self.log("派单失败（smx 未返回 task）：%s %s" % (job["announcement_id"], job.get("company")))
                continue
            job["task_id"], job["pane"] = tid, pane
            job["dispatched_at"] = now_iso(now)
            job_transition(self.instance, job, "dispatched", note="task=%s pane=%s" % (tid, pane[:8]),
                           now=now, log=self.log)
            if job.get("pending_reply"):    # parked 期间用户回的话，转达给新 worker
                self.send_to_worker(job, "[team-msg task=%s] 用户说：%s"
                                    % (tid, job.pop("pending_reply")))
                save_job(self.instance, job)
            used_today += 1
            active.append(job)
            busy_panes.add(pane)
            free_panes = [p for p in free_panes if p != pane]

    def dispatch_one(self, job: dict, now: dt.datetime, night: bool,
                     free_panes: list[str]) -> tuple[str, str]:
        aid = job["announcement_id"]
        try:
            template = open(self.template_path, encoding="utf-8").read()
        except OSError as exc:
            self.log("ERROR: 读不到工单模板 %s：%s" % (self.template_path, exc))
            return "", ""
        ctx = {"intent": self.intent, "instance": self.instance, "repo": self.repo,
               "night_now": night}
        body = render_task(template, job, ctx)
        tf_dir = state_dir(self.instance, "task-files")
        os.makedirs(tf_dir, exist_ok=True)
        tf = os.path.join(tf_dir, "%s.md" % aid)
        with open(tf, "w", encoding="utf-8") as fh:
            fh.write(body)
        acceptance = "test -s '%s'" % self.outcome_path(aid)
        scope = "%s %s" % (os.path.join(self.instance, "log"), os.path.join(self.instance, "state"))
        if free_panes:
            # 重派单优先回原 pane（TaskSpace/登录态还在就接着填，§8.7）
            free_panes = sorted(free_panes, key=lambda p: (p != job.get("pane"), p))
            return self.smx.assign(free_panes[0], tf, acceptance, scope)
        return self.smx.spawn(tf, self.args.worker_cwd, acceptance, scope,
                              self.args.worker_agent, self.args.worker_model)

    # ---------------- 夜间 ----------------

    def night_queue(self) -> list[dict]:
        return read_json(state_dir(self.instance, "night-queue.json"), [])

    def save_night_queue(self, q: list[dict]):
        atomic_write(state_dir(self.instance, "night-queue.json"), q)

    def night_transition(self, au: dict, jobs: dict[str, dict], now: dt.datetime):
        st = read_json(self.state_path, {})
        was_night = bool(st.get("night_active"))
        night = is_night(au, now)
        if was_night and not night:
            q = self.night_queue()
            open_h = [j for j in jobs.values()
                      if j.get("status") in ("gate_wait", "review_wait")]
            if q or open_h:
                lines = ["夜间攒下的待处理："]
                for e in q:
                    lines.append("· %s（%s）等 %s" % (e.get("company"), e.get("id"), e.get("kind")))
                for j in open_h:
                    lines.append("· %s（%s）%s" % (j.get("company"), j["announcement_id"], j["status"]))
                today = now.strftime("%Y-%m-%d")
                done = [j for j in jobs.values() for h in j.get("history", [])
                        if h.get("to") in ("submitted", "verified") and str(h.get("at", "")).startswith(today)]
                lines.append("今日已投 %d 家；在跑 %d 家" % (len(done), len([
                    j for j in jobs.values() if j.get("status") in ACTIVE])))
                self.notifier.event("batch-done", title="夜间待处理汇总",
                                    body="；".join(lines)[:400])
            self.save_night_queue([])
            for job in jobs.values():
                if job.pop("night_queued", None):
                    save_job(self.instance, job)
        st["night_active"] = night
        atomic_write(self.state_path, st)

    # ---------------- 每日 batch-done ----------------

    def daily_wrap(self, jobs: dict[str, dict], now: dt.datetime):
        st = read_json(self.state_path, {})
        today = now.strftime("%Y-%m-%d")
        if st.get("date") != today:
            st.update({"date": today, "batch_done_sent": False})
            atomic_write(self.state_path, st)
        if st.get("batch_done_sent"):
            return
        if not any(j.get("status") in ACTIVE or j.get("status") == "queued" for j in jobs.values()):
            done = [j for j in jobs.values() for h in j.get("history", [])
                    if h.get("to") == "dispatched" and str(h.get("at", "")).startswith(today)]
            if done:
                ok = self.notifier.event("batch-done", title="今日队列清空",
                                         body="今天共派 %d 单，全部到终局" % len(done))
                if ok or self.args.dry_run:
                    st["batch_done_sent"] = True
                    atomic_write(self.state_path, st)

    # ---------------- tgbot 心跳（§8.7：bot 进程挂了） ----------------

    def tgbot_expected(self) -> bool:
        """该不该有 tgbot 在跑：notify.yaml 开了 telegram 通道，或心跳/日志文件已存在。"""
        if os.path.isfile(state_dir(self.instance, "tgbot.heartbeat")) or \
                os.path.isfile(state_dir(self.instance, "tgbot.log")):
            return True
        cfg = read_json(os.path.join(self.instance, "notify.yaml"), None)
        if cfg is None:
            try:
                cfg = yaml.safe_load(
                    open(os.path.join(self.instance, "notify.yaml"), encoding="utf-8")) or {}
            except OSError:
                cfg = {}
        return "telegram" in (cfg.get("channels") or [])

    def heartbeat_check(self):
        """state/tgbot.heartbeat mtime 超 2 分钟 → Bark 通知，每小时最多一次。
        5 分钟无响应的二次提醒是 tgbot 自己的职责，这里不重复发。"""
        hb = state_dir(self.instance, "tgbot.heartbeat")
        if os.path.isfile(hb):
            stale = (time.time() - os.path.getmtime(hb)) > TGBOT_HB_STALE_SEC
        else:
            stale = self.tgbot_expected()      # 该有 bot 却从没写过心跳 = 挂了/没起来
        if not stale:
            return
        st = read_json(self.state_path, {})
        last = float(st.get("tgbot_down_notified_ts") or 0)
        if time.time() - last < TGBOT_HB_NOTIFY_SEC:
            return
        self.notifier.event("blocked", title="Telegram bot 无心跳",
                            body="state/tgbot.heartbeat 超 %d 秒未更新，launchd KeepAlive 应会自动重启；"
                                 "关卡通知目前只剩 Bark/web 通道" % TGBOT_HB_STALE_SEC)
        st["tgbot_down_notified_ts"] = time.time()
        atomic_write(self.state_path, st)
        self.log("WARN: tgbot 心跳过期，已通知（1 小时内不再重复）")

    # ---------------- 一轮 ----------------

    def tick(self, now: dt.datetime):
        au = load_autonomy(self.instance)
        jobs = load_jobs(self.instance)
        approved, skipped = derive_user_marks(self.instance)
        self.consume_commands(jobs, au, now)
        self.enqueue(jobs, au, approved, skipped, now)
        panes = None if self.args.dry_run else self.smx.panes()
        if panes is None and not self.args.dry_run:
            self.log("WARN: smx panes 不可用，本轮跳过 pane 巡检")
        self.monitor(jobs, au, now, panes)
        self.nudge_check(jobs, now, panes)
        registry_recs = reg.load_registry(self.instance)
        tracker_keys = tracker_applied(os.path.join(self.instance, "tracker.md"))
        self.dispatch(jobs, au, registry_recs, tracker_keys, now, panes)
        self.night_transition(au, jobs, now)
        self.daily_wrap(jobs, now)
        self.heartbeat_check()

    def run(self) -> int:
        os.makedirs(state_dir(self.instance), exist_ok=True)
        lock_path = state_dir(self.instance, "dispatcher.lock")
        lock_fh = open(lock_path, "a+")
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("另一个 dispatcher 已在跑（%s），退出" % lock_path, file=sys.stderr)
            return 0
        try:
            while True:
                now = parse_time(self.args.now) or dt.datetime.now(CST)
                try:
                    self.tick(now)
                except Exception as exc:
                    self.log("ERROR: tick 异常 %r" % exc)
                if self.args.once:
                    return 0
                # §8.6：大循环间隔内 1 秒短轮询时效指令（reply/submit/refresh/takeover）
                deadline = time.time() + self.args.interval
                while time.time() < deadline:
                    time.sleep(min(self.args.cmd_poll, max(0.05, deadline - time.time())))
                    try:
                        fnow = parse_time(self.args.now) or dt.datetime.now(CST)
                        self.fast_poll(fnow)
                    except Exception as exc:
                        self.log("ERROR: 快轮询异常 %r" % exc)
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            lock_fh.close()


# --------------------------------------------------------------------------
# status / cmd / install-plist
# --------------------------------------------------------------------------

def cmd_status(args) -> int:
    instance = os.path.abspath(os.path.expanduser(args.instance))
    jobs = load_jobs(instance)
    if not jobs:
        print("(没有作业)")
        return 0
    rows = sorted(jobs.values(), key=lambda j: (str(j.get("created_at")), str(j["announcement_id"])))
    print("%-8s %-10s %-4s %-12s %-10s %-8s %s" % ("id", "公司", "档", "状态", "task", "nudge", "备注"))
    for j in rows:
        last = (j.get("history") or [{}])[-1]
        print("%-8s %-10s %-4s %-12s %-10s %-8s %s" % (
            j["announcement_id"], (j.get("company") or "")[:10], j.get("tier") or "-",
            j.get("status"), (j.get("task_id") or "-")[:10], j.get("nudges", 0),
            (last.get("note") or "")[:40]))
    return 0


def cmd_push(args) -> int:
    instance = os.path.abspath(os.path.expanduser(args.instance))
    rec = {"id": "cmd-%s" % dt.datetime.now().strftime("%Y%m%d%H%M%S%f"),
           "at": now_iso(), "by": "cli", "action": args.action,
           "target": args.target, "args": {}}
    for kv in args.arg or []:
        k, _, v = kv.partition("=")
        rec["args"][k] = v
    if args.action == "mode" and args.target and "mode" not in rec["args"]:
        rec["args"]["mode"] = args.target
        rec["target"] = ""
    append_jsonl(state_dir(instance, "commands.jsonl"), rec)
    print("已写入 %s" % state_dir(instance, "commands.jsonl"))
    return 0


PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.campus-apply.dispatcher</string>
    <key>ProgramArguments</key>
    <array>
        <string>{uv}</string>
        <string>run</string>
        <string>--script</string>
        <string>{script}</string>
        <string>--instance</string>
        <string>{instance}</string>
    </array>
    <key>WorkingDirectory</key><string>{repo}</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>30</integer>
    <key>StandardOutPath</key><string>{instance}/state/dispatcher.out.log</string>
    <key>StandardErrorPath</key><string>{instance}/state/dispatcher.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>CAMPUS_INSTANCE</key><string>{instance}</string>
        <key>CAMPUS_REPO</key><string>{repo}</string>
        <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
"""


def cmd_install_plist(args) -> int:
    import shutil
    instance = os.path.abspath(os.path.expanduser(args.instance))
    repo = os.path.abspath(args.repo or REPO)
    text = PLIST.format(uv=shutil.which("uv") or "/opt/homebrew/bin/uv",
                        script=os.path.join(repo, "host", "dispatcher.py"),
                        instance=instance, repo=repo)
    if args.dest:
        os.makedirs(args.dest, exist_ok=True)
        path = os.path.join(args.dest, "com.campus-apply.dispatcher.plist")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("已写出 %s（不会自动 load；要启用请手动 launchctl load）" % path)
    else:
        sys.stdout.write(text)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="校招投递常驻调度器")
    ap.add_argument("--instance", default=os.environ.get("CAMPUS_INSTANCE"),
                    help="实例目录（默认 $CAMPUS_INSTANCE）")
    ap.add_argument("--repo", default=None, help="仓库根（默认脚本所在仓库）")
    ap.add_argument("--interval", type=float, default=30, help="常驻模式每轮间隔秒（默认 30）")
    ap.add_argument("--cmd-poll", type=float, default=1.0,
                    help="时效指令短轮询间隔秒（默认 1，§8.6）")
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument("--dry-run", action="store_true", help="只打印将做的动作，不执行")
    ap.add_argument("--worker-cmd", default=os.environ.get("SMX_TEAM_BIN")
                    or os.path.expanduser("~/.seedmux/bin/smx-team"),
                    help="与 worker 通信的命令（默认 smx-team；测试可指向 stub）")
    ap.add_argument("--notify-cmd",
                    default="uv run --script %s" % os.path.join(REPO, "host", "notify.py"),
                    help="通知命令（默认 host/notify.py）")
    ap.add_argument("--tasks-dir", default=os.path.expanduser("~/.seedmux/team/tasks"))
    ap.add_argument("--data", default=os.path.join(REPO, "data", "paperball", "announcements.jsonl"))
    ap.add_argument("--tiers", default=os.path.join(REPO, "data", "tiers.yaml"))
    ap.add_argument("--from-pane", default=os.environ.get("DISPATCHER_PANE") or
                    os.environ.get("SEEDMUX_PANE_ID") or "",
                    help="dispatcher 所在 pane（决定 worker 回执发给谁）")
    ap.add_argument("--worker-agent", default="devin")
    ap.add_argument("--worker-model", default="swe-2-max")
    ap.add_argument("--worker-cwd", default=None,
                    help="worker 启动目录（默认实例目录的上一级）")
    ap.add_argument("--nudge-after-min", type=float, default=20,
                    help="pane idle 且作业在干活态超过该分钟数 → nudge（默认 20）")
    ap.add_argument("--max-nudges", type=int, default=3)
    ap.add_argument("--now", default=None, help="固定当前时间 ISO（测试用）")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("status", help="打印作业表")
    p_cmd = sub.add_parser("cmd", help="往 commands.jsonl 追加一条用户指令")
    p_cmd.add_argument("action", choices=sorted(COMMANDS))
    p_cmd.add_argument("--target", default="")
    p_cmd.add_argument("--arg", action="append", help="k=v，可重复")
    p_pl = sub.add_parser("install-plist", help="生成 launchd KeepAlive plist（不安装）")
    p_pl.add_argument("--dry-run", action="store_true", help="打印到 stdout（默认行为）")
    p_pl.add_argument("--dest", default=None, help="写到该目录（不 load）")

    args = ap.parse_args()
    if not args.instance or not os.path.isdir(args.instance):
        ap.error("需要 --instance 或环境变量 CAMPUS_INSTANCE")
    if not args.worker_cwd:
        args.worker_cwd = os.path.dirname(os.path.abspath(os.path.expanduser(args.instance)))
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "cmd":
        return cmd_push(args)
    if args.cmd == "install-plist":
        return cmd_install_plist(args)
    return Dispatcher(args).run()


if __name__ == "__main__":
    sys.exit(main())
