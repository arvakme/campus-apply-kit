#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""install-schedule.py · 生成并安装 launchd 定时同步（维护者 daily-sync / 同学 pull-update）

    uv run --script host/install-schedule.py install   --role member
    uv run --script host/install-schedule.py uninstall --role member
    uv run --script host/install-schedule.py status [--role member]
    uv run --script host/install-schedule.py install --role member --dry-run   # 只打印 plist
    uv run --script host/install-schedule.py install --role member --dest DIR  # 写到指定目录（测试用，不碰 launchctl）

说明：
- launchd 环境变量极少，plist 里写死 CAMPUS_REPO / CAMPUS_INSTANCE / PATH；
  stdout/stderr 落到 $CAMPUS_INSTANCE/state/ 下（脚本自身的 daily-sync.log / pull-update.log 也在那里）。
- install 默认装到 ~/Library/LaunchAgents 并 launchctl bootstrap；--dest 指向别处时只写文件不注册。
- 维护者注意：MacBook 合盖睡眠会错过 01:00，launchd 会在唤醒后补跑；可选
  `sudo pmset repeat wake MTWRFSU 00:55:00` 定时唤醒（只打印建议，本脚本不执行 sudo）。
  公开版的 host/sync-source.py 是占位文件：先按 adapters/sources/README.md 写好你自己的适配器，再装 maintainer 角色。
"""
from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DEST = os.path.expanduser("~/Library/LaunchAgents")

ROLES = {
    # role: (label, 脚本, StartCalendarInterval, RunAtLoad)
    "maintainer": (
        "dev.campus-apply.daily-sync",
        "host/daily-sync.sh",
        {"Hour": 1, "Minute": 0},
        False,
    ),
    "member": (
        "dev.campus-apply.pull-update",
        "host/pull-update.sh",
        [{"Minute": 7}],                    # 每小时 :07 拉一次（热更新），登录时补拉
        True,
    ),
}

# launchd 下 PATH 极小；ego-browser / uv / himalaya 等常见安装位置都带上
PATH_ENV = "/opt/homebrew/bin:/usr/local/bin:{home}/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def gui_target(label: str) -> str:
    return "gui/%d/%s" % (os.getuid(), label)


def build_plist(role: str, repo: str, instance: str) -> dict:
    label, script, cal, run_at_load = ROLES[role]
    name = label.rsplit(".", 1)[-1]
    plist = {
        "Label": label,
        "ProgramArguments": ["/bin/bash", os.path.join(repo, script)],
        "EnvironmentVariables": {
            "CAMPUS_REPO": repo,
            "CAMPUS_INSTANCE": instance,
            "PATH": PATH_ENV.format(home=os.path.expanduser("~")),
        },
        "StartCalendarInterval": cal,
        "ProcessType": "Background",
        "StandardOutPath": os.path.join(instance, "state", "%s.launchd.log" % name),
        "StandardErrorPath": os.path.join(instance, "state", "%s.launchd.log" % name),
    }
    # launchd 读不到终端里的代理变量：安装时把当前 shell 的代理写进 plist（国内 push GitHub / 访问 Telegram 需要）
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY"):
        v = os.environ.get(k) or os.environ.get(k.lower())
        if v:
            plist["EnvironmentVariables"][k] = v
    if run_at_load:
        plist["RunAtLoad"] = True
    return plist


def plist_path(dest: str, label: str) -> str:
    return os.path.join(dest, label + ".plist")


def resolve_paths(args) -> tuple[str, str]:
    repo = os.path.abspath(os.path.expanduser(args.repo or os.environ.get("CAMPUS_REPO") or REPO))
    instance = args.instance or os.environ.get("CAMPUS_INSTANCE")
    if not instance:
        sys.exit("ERROR: 需要 CAMPUS_INSTANCE（或 --instance）——launchd 环境里必须写死进 plist")
    instance = os.path.abspath(os.path.expanduser(instance))
    return repo, instance


def roles_for(args) -> list[str]:
    return [args.role] if args.role else list(ROLES)


def cmd_install(args) -> int:
    repo, instance = resolve_paths(args)
    dest = os.path.abspath(os.path.expanduser(args.dest))
    register = dest == os.path.abspath(DEFAULT_DEST)

    for role in roles_for(args):
        label, script, _, _ = ROLES[role]
        script_path = os.path.join(repo, script)
        if not os.path.isfile(script_path):
            print("ERROR: 找不到 %s" % script_path, file=sys.stderr)
            return 1
        plist = build_plist(role, repo, instance)
        path = plist_path(dest, label)

        if args.dry_run:
            print("# ---- %s（将写入 %s）----" % (label, path))
            sys.stdout.write(plistlib.dumps(plist).decode("utf-8"))
            if register:
                print("# 将执行: launchctl bootout %s（忽略失败）" % gui_target(label))
                print("# 将执行: launchctl bootstrap gui/%d %s" % (os.getuid(), path))
            else:
                print("# --dest 非默认目录，跳过 launchctl 注册")
            continue

        os.makedirs(dest, exist_ok=True)
        os.makedirs(os.path.join(instance, "state"), exist_ok=True)
        with open(path, "wb") as fh:
            plistlib.dump(plist, fh)
        print("已写入 %s" % path)
        if register:
            subprocess.run(["launchctl", "bootout", gui_target(label)],
                           capture_output=True)  # 已存在则先卸，装新版本；未装忽略报错
            proc = subprocess.run(["launchctl", "bootstrap", "gui/%d" % os.getuid(), path],
                                  capture_output=True, text=True)
            if proc.returncode != 0:
                print("ERROR: launchctl bootstrap 失败：%s" % (proc.stderr or proc.stdout).strip(),
                      file=sys.stderr)
                return 1
            print("已注册 %s（launchctl bootstrap）" % label)
        else:
            print("--dest 非默认目录，跳过 launchctl 注册")

    if "maintainer" in roles_for(args):
        print()
        print("维护者提醒：")
        print("  · MacBook 合盖睡眠会错过 01:00，launchd 会在唤醒后补跑。")
        print("    想准点跑可执行（本脚本不代跑 sudo）：sudo pmset repeat wake MTWRFSU 00:55:00")
        print("  · host/sync-source.py 还是占位文件的话，每天只会收到一条同步失败的通知；先写好你自己的适配器。")
    return 0


def cmd_uninstall(args) -> int:
    dest = os.path.abspath(os.path.expanduser(args.dest))
    register = dest == os.path.abspath(DEFAULT_DEST)
    for role in roles_for(args):
        label = ROLES[role][0]
        path = plist_path(dest, label)
        if args.dry_run:
            print("# 将执行: launchctl bootout %s（忽略失败）" % gui_target(label))
            print("# 将删除: %s" % path)
            continue
        if register:
            subprocess.run(["launchctl", "bootout", gui_target(label)], capture_output=True)
        if os.path.isfile(path):
            os.remove(path)
            print("已删除 %s" % path)
        else:
            print("%s 不存在，跳过" % path)
    return 0


def cmd_status(args) -> int:
    dest = os.path.abspath(os.path.expanduser(args.dest))
    rc = 0
    for role in roles_for(args):
        label, script, cal, run_at_load = ROLES[role]
        path = plist_path(dest, label)
        print("== %s（%s）==" % (role, label))
        if os.path.isfile(path):
            print("  plist: %s" % path)
            times = cal if isinstance(cal, list) else [cal]
            print("  计划: 每天 %s%s" % (" / ".join("%02d:%02d" % (t["Hour"], t["Minute"]) for t in times),
                                            "，登录时补跑" if run_at_load else ""))
        else:
            print("  plist: 未安装（%s 不存在）" % path)
            rc = 1
        proc = subprocess.run(["launchctl", "print", gui_target(label)], capture_output=True, text=True)
        if proc.returncode != 0:
            print("  launchd: 未加载")
            rc = 1
        else:
            shown = False
            for line in proc.stdout.splitlines():
                s = line.strip()
                if s.startswith(("state =", "last exit code =", "pid =", "program =")):
                    print("  launchd: %s" % s)
                    shown = True
            if not shown:
                print("  launchd: 已加载（无 state/exit 信息）")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description="安装/查看 campus-apply launchd 定时同步")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, help_ in (("install", "生成 plist 并 launchctl bootstrap"),
                        ("uninstall", "bootout 并删除 plist"),
                        ("status", "解析 launchctl print 看运行状态")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--role", choices=sorted(ROLES),
                       help="maintainer=daily-sync 01:00；member=pull-update 每小时+登录补拉。status 可省略=全部")
        p.add_argument("--repo", help="默认 $CAMPUS_REPO 或本脚本所在仓库")
        p.add_argument("--instance", help="默认 $CAMPUS_INSTANCE")
        p.add_argument("--dest", default=DEFAULT_DEST, help="plist 写入目录（默认 ~/Library/LaunchAgents；"
                                                          "写别处时跳过 launchctl，测试用）")
        p.add_argument("--dry-run", action="store_true", help="只打印 plist 和将执行的命令")
        p.set_defaults(func={"install": cmd_install, "uninstall": cmd_uninstall, "status": cmd_status}[name])
    args = ap.parse_args()
    if args.cmd in ("install", "uninstall") and not args.role:
        ap.error("%s 需要 --role maintainer|member" % args.cmd)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
