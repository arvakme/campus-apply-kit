#!/usr/bin/env bash
# daily-sync.sh · 维护者每日同步：拉仓库 → sync-source → fetch-jd → 提交 data/ → push
#
# 由 launchd 每天 01:00 触发（uv run host/install-schedule.py install --role maintainer），
# 也可手动执行：CAMPUS_REPO=... CAMPUS_INSTANCE=... bash host/daily-sync.sh
#
# 环境变量：
#   CAMPUS_REPO        仓库根（默认取本脚本上一级目录）
#   CAMPUS_INSTANCE    实例目录（必填；日志与锁都放它的 state/ 下）
#   CAMPUS_SYNC_SOURCE_PY  覆盖 sync-source 脚本路径（测试替身用）
#   CAMPUS_FETCH_JD_PY     覆盖 fetch-jd 脚本路径（测试替身用；文件不存在则跳过）
#   CAMPUS_NOTIFY_PY       覆盖 notify 脚本路径（测试替身用）
#
# 退出码：0 成功（含"无变化"）；2/3 透传 sync-source 的登录失效/限流；1 其他失败。
set -euo pipefail

REPO="${CAMPUS_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
INSTANCE="${CAMPUS_INSTANCE:?需要 CAMPUS_INSTANCE 环境变量（plist 里已写死）}"
STATE="$INSTANCE/state"
LOG="$STATE/daily-sync.log"
LOCKDIR="$STATE/daily-sync.lock"
RETAIN_DAYS=30
PUSH_RETRIES=3

SYNC_SOURCE_PY="${CAMPUS_SYNC_SOURCE_PY:-$REPO/host/sync-source.py}"
FETCH_JD_PY="${CAMPUS_FETCH_JD_PY:-$REPO/host/fetch-jd.py}"
NOTIFY_PY="${CAMPUS_NOTIFY_PY:-$REPO/host/notify.py}"

mkdir -p "$STATE"

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" | tee -a "$LOG"; }

# 通知一律走 notify.py；没配 notify.yaml 或推送失败时只记日志，不中断主流程
notify() {
  if uv run "$NOTIFY_PY" --instance "$INSTANCE" "$@" >>"$LOG" 2>&1; then
    log "已通知：$*"
  else
    log "通知失败（$*），降级为仅日志"
  fi
}

# mkdir 原子加锁；持锁进程已死则接管（防上次崩溃留下的死锁）
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

# 只保留最近 RETAIN_DAYS 天的日志（按行首 [YYYY-MM-DD 时间戳过滤，无时间戳的行保留）
prune_log() {
  [[ -f "$LOG" ]] || return 0
  local cutoff tmp
  cutoff="$(date -v-"${RETAIN_DAYS}"d '+%Y-%m-%d')"
  tmp="$(mktemp "$STATE/.daily-sync-log.XXXXXX")"
  awk -v c="$cutoff" '/^\[[0-9]{4}-[0-9]{2}-[0-9]{2}/ { if (substr($0,2,10) < c) next } { print }' \
    "$LOG" >"$tmp" && mv "$tmp" "$LOG"
}

main() {
  cd "$REPO"
  prune_log
  log "===== daily-sync 开始 ====="

  if ! acquire_lock; then
    log "已有实例在跑（锁 ${LOCKDIR}），本次退出"
    exit 0
  fi

  # 1. 前置检查：data/ 以外不允许有未提交改动
  local dirty
  dirty="$(git status --porcelain | grep -vE '^.. "?data/' || true)"
  if [[ -n "$dirty" ]]; then
    log "工作区有 data/ 以外的未提交改动，未执行同步："
    printf '%s\n' "$dirty" | sed 's/^/    /' | tee -a "$LOG"
    notify blocked --title "daily-sync 未执行" --body "仓库工作区不干净（data/ 之外有改动），请手动处理后明早会自动重试"
    exit 1
  fi

  # 2. 先拉框架更新
  if ! git pull --ff-only >>"$LOG" 2>&1; then
    log "git pull --ff-only 失败（可能分支分叉）"
    notify blocked --title "daily-sync 拉取失败" --body "git pull --ff-only 失败，可能存在分叉，请手动处理"
    exit 1
  fi

  # 3. 同步数据源公告
  local rc=0
  uv run "$SYNC_SOURCE_PY" >>"$LOG" 2>&1 || rc=$?
  if [[ "$rc" != 0 ]]; then
    case "$rc" in
      2) log "sync-source 登录态失效"
         notify blocked --title "数据源登录失效" --body "你的数据源适配器报告登录态失效（退出码 2），明早会自动重试" ;;
      3) log "sync-source 疑似限流/风控"
         notify blocked --title "数据源限流" --body "sync-source 被限流，今天数据未更新；不要连续重试" ;;
      *) log "sync-source 失败（exit=${rc}）"
         notify blocked --title "daily-sync 同步失败" --body "sync-source 退出码 ${rc}，详见实例 state/daily-sync.log" ;;
    esac
    exit "$rc"
  fi

  # 4. 可选：抓 JD 增量（计划项 B 的产物，存在才跑；失败不中断）
  if [[ -f "$FETCH_JD_PY" ]]; then
    if uv run "$FETCH_JD_PY" --incremental >>"$LOG" 2>&1; then
      log "fetch-jd 增量抓取完成"
    else
      log "fetch-jd 失败（exit=$?），仅记日志不中断"
    fi
  fi

  # 5. 只提交 data/；无变化则结束
  git add data/
  if git diff --cached --quiet -- data/; then
    log "data/ 无变化，不提交"
    log "===== daily-sync 结束 ====="
    exit 0
  fi

  # 提交前显式跑脱敏检查（pre-commit 钩子只存在于本机 clone，不能依赖）
  if ! python3 scripts/check_secrets.py --instance "$INSTANCE" >>"$LOG" 2>&1; then
    log "check_secrets 命中，放弃提交（已 add 未 commit）"
    notify blocked --title "daily-sync 脱敏命中" --body "check_secrets.py 有命中，data/ 未提交，请检查"
    exit 1
  fi

  # 提交信息：日期与总条数取自 meta.json；新增/删除行数取 announcements.jsonl 的 staged diff
  local meta synced count added deleted
  meta="$(python3 -c 'import json,sys; m=json.load(open(sys.argv[1])); print(m.get("synced_at","")[:10], m.get("count",0))' \
          data/paperball/meta.json 2>/dev/null || echo "? 0")"
  synced="${meta%% *}"; count="${meta##* }"
  read -r added deleted < <(git diff --cached --numstat -- data/paperball/announcements.jsonl |
                            awk '{a+=$1; d+=$2} END{print a+0, d+0}')
  if ! git commit -m "data: 同步 ${synced} · ${count} 条公告（+${added}/-${deleted}）" >>"$LOG" 2>&1; then
    log "git commit 失败（含 pre-commit 钩子）"
    notify blocked --title "daily-sync 提交失败" --body "git commit 未成功，详见实例 state/daily-sync.log"
    exit 1
  fi
  log "已提交：synced=${synced} count=${count} +${added}/-${deleted}"

  # 6. push，失败重试
  local attempt
  for attempt in $(seq 1 "$PUSH_RETRIES"); do
    if git push >>"$LOG" 2>&1; then
      log "push 成功（第 ${attempt} 次）"
      log "===== daily-sync 结束 ====="
      exit 0
    fi
    log "push 失败（第 ${attempt}/${PUSH_RETRIES} 次），10s 后重试"
    sleep 10
  done
  notify blocked --title "daily-sync push 失败" --body "git push 连续 ${PUSH_RETRIES} 次失败，提交留在本地，请检查网络/远端"
  exit 1
}

main "$@"
