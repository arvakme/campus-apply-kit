#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6"]
# ///
"""board-snapshot.py · 把当天榜单写进 $CAMPUS_INSTANCE/state/board/<date>.json

    uv run --script host/board-snapshot.py [--instance DIR] [--repo DIR] [--date YYYY-MM-DD]

数据构建与 web /board 完全同一份逻辑（import web/app/board.py），快照供：
  - 容器里的 /board?date=YYYY-MM-DD 看历史（容器只挂 /instance，读不到仓库 data/）
  - /board/diff 对比两天：新增 / 下线 / 状态变化
  - 无仓库环境（容器）下 /board 默认回退到最新快照

建议接在 daily-sync / pull-update 之后（不改那两个脚本，自行追加一行）：
    uv run --script "$CAMPUS_REPO/host/board-snapshot.py" --instance "$CAMPUS_INSTANCE"

重复执行同一天会原子覆盖（tempfile + os.replace），可安全重跑。
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from zoneinfo import ZoneInfo

REPO = os.environ.get("CAMPUS_REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "web", "app"))

import board  # noqa: E402

CST = ZoneInfo("Asia/Shanghai")


def main() -> int:
    ap = argparse.ArgumentParser(description="写当日榜单快照 state/board/<date>.json")
    ap.add_argument("--instance", default=os.environ.get("CAMPUS_INSTANCE"),
                    help="实例目录（默认 $CAMPUS_INSTANCE）")
    ap.add_argument("--repo", default=REPO, help="仓库根（默认 $CAMPUS_REPO 或脚本所在仓库）")
    ap.add_argument("--date", default=None, help="快照日期 YYYY-MM-DD（默认北京时间今天）")
    args = ap.parse_args()
    if not args.instance:
        ap.error("需要 --instance 或环境变量 CAMPUS_INSTANCE")
    instance = os.path.abspath(os.path.expanduser(args.instance))
    repo = os.path.abspath(os.path.expanduser(args.repo))
    if not os.path.isdir(instance):
        print("ERROR: 实例目录不存在：%s" % instance, file=sys.stderr)
        return 2
    if not board.repo_available(repo):
        print("ERROR: 仓库里没有 data/paperball/announcements.jsonl：%s" % repo, file=sys.stderr)
        return 2
    try:
        date = args.date or dt.datetime.now(CST).date().isoformat()
        dt.date.fromisoformat(date)
    except ValueError:
        ap.error("--date 需为 YYYY-MM-DD")

    data, stats = board.build(repo, instance, today=date)
    path = board.write_snapshot(instance, date, data)
    labels = stats.get("labels") or {}
    print("写出 %s" % path, file=sys.stderr)
    print("榜单 %s：公司 %d（公告 %d 条）· 今日新增 %d · 已投 %d · 进行中 %d · 等我处理 %d · 今日截止 %d"
          % (date, stats["total"], stats["announcements"], stats["todayNew"], stats["applied"],
             stats["running"], stats["waiting"], stats["dueToday"]), file=sys.stderr)
    print("我的状态分布：" + " · ".join("%s %d" % (k, labels[k]) for k in board.STATUS_ORDER if k in labels),
          file=sys.stderr)
    if stats.get("trackerUnmatched"):
        print("WARN: tracker 有 %d 行既按 id 也按雇主主体匹配不上任何公告（可能已下线）" % stats["trackerUnmatched"],
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
