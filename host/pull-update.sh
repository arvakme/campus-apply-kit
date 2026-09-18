#!/usr/bin/env bash
# pull-update.sh · 同学侧定时拉取：fetch → 有新提交才 pull --ff-only → 重扫候选 → 通知
#
# 由 launchd 每小时 :07 + 登录时触发
# （uv run host/install-schedule.py install --role member），也可手动执行：
#   CAMPUS_REPO=... CAMPUS_INSTANCE=... bash host/pull-update.sh
#
# 幂等：没有新提交就静默退出；工作区脏或分支分叉时不动仓库，同一问题 24 小时内只通知一次。
#
# 环境变量：
#   CAMPUS_REPO        仓库根（默认取本脚本上一级目录）
#   CAMPUS_INSTANCE    实例目录（必填）
#   CAMPUS_SCAN_PY     覆盖 scan 脚本路径（测试替身用）
#   CAMPUS_JUDGE_PY    覆盖 judge 脚本路径（测试替身用；文件不存在则跳过）
#   CAMPUS_NOTIFY_PY   覆盖 notify 脚本路径（测试替身用）
#
# 退出码：0 成功 / 无新提交；1 有阻塞（脏区、分叉、fetch/pull/scan 失败）。
set -euo pipefail

REPO="${CAMPUS_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
INSTANCE="${CAMPUS_INSTANCE:?需要 CAMPUS_INSTANCE 环境变量（plist 里已写死）}"
STATE="$INSTANCE/state"
LOG="$STATE/pull-update.log"
LOCKDIR="$STATE/pull-update.lock"
RETAIN_DAYS=30
WARN_INTERVAL=86400   # 同一问题 24 小时内只通知一次

SCAN_PY="${CAMPUS_SCAN_PY:-$REPO/host/scan.py}"
JUDGE_PY="${CAMPUS_JUDGE_PY:-$REPO/host/judge.py}"
NOTIFY_PY="${CAMPUS_NOTIFY_PY:-$REPO/host/notify.py}"
BATTLE_MAP="$INSTANCE/log/battle-map.md"

mkdir -p "$STATE"

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" | tee -a "$LOG"; }

notify() {
  if uv run "$NOTIFY_PY" --instance "$INSTANCE" "$@" >>"$LOG" 2>&1; then
    log "已通知：$*"
  else
    log "通知失败（$*），降级为仅日志"
  fi
}

# 同一问题 24h 只通知一次：state/.pull-update-warn-<key> 的 mtime 即上次通知时间
notify_once() {
  local key="$1"; shift
  local mark="$STATE/.pull-update-warn-${key}"
  if [[ -f "$mark" ]] && (( $(date +%s) - $(stat -f %m "$mark") < WARN_INTERVAL )); then
    log "问题 ${key} 24h 内已通知过，本次只记日志"
    return 0
  fi
  notify "$@"
  touch "$mark"
}

clear_warns() { rm -f "$STATE"/.pull-update-warn-* 2>/dev/null || true; }

LOCK_HELD=0
acquire_lock() {
  if mkdir "$LOCKDIR" 2>/dev/null; then
    echo $$ >"$LOCKDIR/pid"; LOCK_HELD=1; return 0
  fi
  local pid=""
  pid="$(cat "$LOCKDIR/pid" 2>/dev/null || true)"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    return 1
  fi
  rm -rf "$LOCKDIR"
  if mkdir "$LOCKDIR" 2>/dev/null; then
    echo $$ >"$LOCKDIR/pid"; LOCK_HELD=1; return 0
  fi
  return 1
}
trap '[[ "$LOCK_HELD" == 1 ]] && rm -rf "$LOCKDIR"' EXIT

prune_log() {
  [[ -f "$LOG" ]] || return 0
  local cutoff tmp
  cutoff="$(date -v-"${RETAIN_DAYS}"d '+%Y-%m-%d')"
  tmp="$(mktemp "$STATE/.pull-update-log.XXXXXX")"
  awk -v c="$cutoff" '/^\[[0-9]{4}-[0-9]{2}-[0-9]{2}/ { if (substr($0,2,10) < c) next } { print }' \
    "$LOG" >"$tmp" && mv "$tmp" "$LOG"
}

# battle-map.md 主表（11 列，id 在第 5 列）里的公告 id 集合；文件不存在输出空
bm_ids() {
  [[ -f "$1" ]] || return 0
  awk -F'|' 'NF >= 13 { gsub(/[[:space:]]/, "", $6); if ($6 ~ /^[0-9]+$/) print $6 }' "$1" | sort -u
}

main() {
  cd "$REPO"
  prune_log
  log "===== pull-update 开始 ====="

  if ! acquire_lock; then
    log "已有实例在跑（锁 ${LOCKDIR}），本次退出"
    exit 0
  fi

  # 0. 旧版定时（一天 5 次）自动升级成每小时一次，同学不用手动重装
  local plist="$HOME/Library/LaunchAgents/dev.campus-apply.pull-update.plist"
  if [[ -f "$plist" ]] && plutil -extract StartCalendarInterval.0.Hour raw "$plist" >/dev/null 2>&1; then
    if uv run "$REPO/host/install-schedule.py" install --role member --instance "$INSTANCE" >>"$LOG" 2>&1; then
      log "定时任务已升级为每小时拉取"
    fi
  fi

  # 1. 工作区必须完全干净（同学仓库只读框架+数据，任何本地改动都先停手）
  local dirty
  dirty="$(git status --porcelain || true)"
  if [[ -n "$dirty" ]]; then
    log "工作区不干净，未动仓库："
    printf '%s\n' "$dirty" | sed 's/^/    /' | tee -a "$LOG"
    notify_once dirty blocked --title "campus-apply 仓库有本地改动" \
      --body "pull-update 已停手：工作区不干净，请手动处理（勿 stash/reset 他人物品）"
    exit 1
  fi

  # 2. fetch（失败多为网络问题，只记日志不打扰）
  if ! git fetch >>"$LOG" 2>&1; then
    log "git fetch 失败（网络/远端不可达），本次退出"
    exit 1
  fi

  # 3. 分叉检查与新旧判断
  local ahead behind counts
  if ! counts="$(git rev-list --left-right --count 'HEAD...@{upstream}' 2>/dev/null)" || [[ -z "$counts" ]]; then
    log "无法比较上游分支（@{upstream} 不存在？）"
    notify_once upstream blocked --title "campus-apply 上游分支异常" --body "git rev-list HEAD...@{upstream} 失败，请检查分支设置"
    exit 1
  fi
  read -r ahead behind <<<"$counts"
  if (( ahead > 0 )); then
    log "本地领先上游 ${ahead} 个提交（分叉），未动仓库"
    notify_once diverged blocked --title "campus-apply 分支分叉" \
      --body "本地有 ${ahead} 个上游没有的提交，pull-update 已停手，请手动处理"
    exit 1
  fi
  if (( behind == 0 )); then
    log "已是最新，无新提交，静默退出"
    clear_warns
    exit 0
  fi

  # 4. 有新提交：记下拉前状态再 ff-only
  local old_head before_map
  old_head="$(git rev-parse HEAD)"
  before_map="$(mktemp "$STATE/.battle-map-before.XXXXXX")"
  [[ -f "$BATTLE_MAP" ]] && cp "$BATTLE_MAP" "$before_map" || true

  if ! git pull --ff-only >>"$LOG" 2>&1; then
    rm -f "$before_map"
    log "git pull --ff-only 失败"
    notify_once pull-failed blocked --title "campus-apply 拉取失败" --body "git pull --ff-only 失败，请手动处理"
    exit 1
  fi
  local changed
  changed="$(git diff --name-only "${old_head}..HEAD")"
  log "已拉取 ${old_head:0:7}..$(git rev-parse --short HEAD)，变化文件：$(printf '%s\n' "$changed" | grep -c .) 个"

  # 5. data/ 有变化 → 重扫候选，按新增候选数推送
  if printf '%s\n' "$changed" | grep -q '^data/'; then
    local scan_rc=0
    uv run "$SCAN_PY" >>"$LOG" 2>&1 || scan_rc=$?
    if [[ "$scan_rc" != 0 ]]; then
      rm -f "$before_map"
      log "scan.py 失败（exit=${scan_rc}）"
      notify_once scan-failed blocked --title "campus-apply 候选扫描失败" --body "scan.py 退出码 ${scan_rc}，详见 state/pull-update.log"
      exit 1
    fi
    local new_n
    new_n="$(comm -13 <(bm_ids "$before_map") <(bm_ids "$BATTLE_MAP") | wc -l | tr -d ' ')"
    rm -f "$before_map"
    log "data/ 有更新：battle-map 新增候选 ${new_n} 家"
    if [[ -f "$JUDGE_PY" ]]; then
      if uv run "$JUDGE_PY" --incremental >>"$LOG" 2>&1; then
        log "judge 增量分档完成"
      else
        log "judge 失败（exit=$?），仅记日志不中断"
      fi
    fi
    if (( new_n > 0 )); then
      notify batch-done --title "校招新增 ${new_n} 家" --body "battle-map 新增 ${new_n} 家候选公司，打开看板/候选人查看"
    fi
  else
    rm -f "$before_map"
    log "data/ 无变化，跳过 scan"
  fi

  # 6. 热更新：代码变了就重启对应的常驻服务（脚本类每次现起进程，拉下来即生效）
  local applied=()
  if printf '%s\n' "$changed" | grep -qE '^host/(tgbot\.py|channels/|notify\.py)|^web/app/handoffs\.py'; then
    if launchctl print "gui/$(id -u)/dev.campus-apply.tgbot" >/dev/null 2>&1; then
      launchctl kickstart -k "gui/$(id -u)/dev.campus-apply.tgbot" >>"$LOG" 2>&1 && applied+=("bot 已重启") \
        || log "tgbot 重启失败"
    fi
  fi
  if printf '%s\n' "$changed" | grep -qE '^web/'; then
    if command -v container >/dev/null 2>&1 && container list 2>/dev/null | grep -q campus; then
      if CAMPUS_REPO="$REPO" CAMPUS_INSTANCE="$INSTANCE" bash "$REPO/web/run.sh" >>"$LOG" 2>&1; then
        applied+=("看板已重建")
      else
        log "web 容器重建失败"
      fi
    fi
  fi
  if printf '%s\n' "$changed" | grep -qE '^host/install-schedule\.py'; then
    uv run "$REPO/host/install-schedule.py" install --role member --instance "$INSTANCE" >>"$LOG" 2>&1 \
      && applied+=("定时任务已重装") || log "install-schedule 重装失败"
  fi
  (( ${#applied[@]} )) && log "热更新：${applied[*]}"

  # 7. CHANGELOG 有变化 → 推送最新一节的标题与"你需要做什么"（没有手动步骤就只是告知）
  if printf '%s\n' "$changed" | grep -q '^CHANGELOG\.md$'; then
    local ver todo
    ver="$(grep -m1 '^## ' CHANGELOG.md | sed 's/^## //')"
    todo="$(awk '/^## /{n++} n==1 && /^### 你需要做什么/{f=1;next} n==1 && f && /^### /{f=0} n==1 && f' CHANGELOG.md | grep -v '^\s*$' | head -6)"
    [[ -z "$todo" ]] && todo="已自动更新，无需操作"
    notify batch-done --title "campus-apply 已更新到 ${ver}" --body "${applied[*]:-}${applied[*]:+；}${todo}"
  fi

  clear_warns
  log "===== pull-update 结束 ====="
  exit 0
}

main "$@"; exit $?   # 同一行读入：pull 改写本文件后 bash 不会续读新内容
