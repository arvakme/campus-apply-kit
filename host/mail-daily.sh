#!/usr/bin/env bash
# mail-daily.sh · 每日邮件投递：draft = 派 3 个 Devin 起草；send = 校验后发出（受 intent 每日上限约束）
#
#   bash host/mail-daily.sh draft <批次>     # 读 $CAMPUS_INSTANCE/state/mail-tasks/<批次>-mail-{1,2,3}.md
#   bash host/mail-daily.sh send  <批次>     # mail/<批次>/check.py 通过的草稿才发；已发的跳过
#
# 发件人取环境变量 CAMPUS_MAIL_FROM。由 launchd（dev.campus-apply.mail-daily-draft / -send）按时调用。
set -euo pipefail
REPO="${CAMPUS_REPO:?}"; INSTANCE="${CAMPUS_INSTANCE:?}"
cmd="${1:-}"; batch="${2:?需要批次名，如 B-10}"
LOG="$INSTANCE/state/mail-daily.log"
log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*" >> "$LOG"; }

case "$cmd" in
  draft)
    for n in 1 2 3; do
      f="$INSTANCE/state/mail-tasks/${batch}-mail-${n}.md"
      [[ -f "$f" ]] || { log "缺工单 $f，跳过"; continue; }
      out=$("$HOME/.seedmux/bin/smx-team" spawn --agent devin --model swe-2-max --cwd "$INSTANCE" \
            --task-file "$f" --direction down --scope "mail/${batch}" \
            --acceptance "cd mail/${batch} && python3 check.py" 2>&1 | tail -1)
      log "派起草 ${batch}-${n}：$out"
      sleep 90   # 错开，避免 Devin 限流
    done
    ;;
  send)
    cd "$INSTANCE/mail/$batch"
    if ! python3 check.py >> "$LOG" 2>&1; then
      log "check.py 有错误草稿，只发校验通过的（mail-send 会再次校验必填项）"
    fi
    uv run --script "$REPO/host/mail-send.py" "$INSTANCE/mail/$batch" --instance "$INSTANCE" \
      --from "${CAMPUS_MAIL_FROM:?需要 CAMPUS_MAIL_FROM}" >> "$INSTANCE/mail/$batch/send.log" 2>&1 \
      && log "发送完成 $batch" || log "发送出错 $batch，见 mail/$batch/send.log"
    ;;
  *) echo "用法: mail-daily.sh draft|send <批次>" >&2; exit 2 ;;
esac
