#!/usr/bin/env bash
# pool-window.sh · 投递时间窗：open = 恢复派单 + 关勿扰；close = 暂停派新单 + 开勿扰到次日开窗时间
#
#   bash host/pool-window.sh open
#   bash host/pool-window.sh close [HH:MM]   # 勿扰到次日 HH:MM，默认 12:00
#
# 由 launchd（dev.campus-apply.pool-window-open / -close）按用户定的时间调用；在跑的 worker 做完当前公司再停。
set -euo pipefail
INSTANCE="${CAMPUS_INSTANCE:?需要 CAMPUS_INSTANCE}"
STATE="$INSTANCE/state"
mkdir -p "$STATE"
case "${1:-}" in
  open)
    rm -f "$STATE/slot-pool.pause" "$STATE/dnd.json"
    echo "[$(date '+%F %T')] 开窗：恢复派单，关闭勿扰" >> "$STATE/slot-pool.log"
    ;;
  close)
    at="${2:-12:00}"
    touch "$STATE/slot-pool.pause"
    until=$(/usr/bin/python3 -c "
import datetime as dt,sys
h,m=map(int,sys.argv[1].split(':'))
n=dt.datetime.now().astimezone()
t=n.replace(hour=h,minute=m,second=0,microsecond=0)
t = t if t>n else t+dt.timedelta(days=1)
print(t.isoformat(timespec='seconds'))" "$at")
    printf '{"until": "%s", "reason": "投递时间窗关闭（每天 %s 开始）"}' "$until" "$at" > "$STATE/dnd.json"
    echo "[$(date '+%F %T')] 关窗：暂停派新单，勿扰到 $until" >> "$STATE/slot-pool.log"
    ;;
  *) echo "用法: pool-window.sh open|close [HH:MM]" >&2; exit 2 ;;
esac
