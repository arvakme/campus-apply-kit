#!/usr/bin/env bash
# backup-instance.sh · 实例目录本地备份（永不 push）
#
# 两层：
#   1. 实例目录本身是一个只在本地的 git 仓库，每次运行有变化就自动提交一次（可回到任意历史版本）
#   2. 实例目录 + 额外文件或目录（简历 PDF、证件照等，写在 $CAMPUS_INSTANCE/backup.extra，一行一个绝对路径；别放大归档目录）
#      rsync 到 $CAMPUS_BACKUP_DIR/<日期>/，保留最近 14 天
#
# 用法：CAMPUS_INSTANCE=~/campus-apply-instance host/backup-instance.sh
# 恢复：cd $CAMPUS_INSTANCE && git log --stat -- <文件>  →  git show <提交>:<文件> > <文件>
set -euo pipefail

INSTANCE="${CAMPUS_INSTANCE:?先 export CAMPUS_INSTANCE=<实例目录>}"
BACKUP_DIR="${CAMPUS_BACKUP_DIR:-$HOME/Backups/campus-apply}"
KEEP_DAYS="${CAMPUS_BACKUP_KEEP_DAYS:-14}"
LOG="${INSTANCE}/state/backup.log"
mkdir -p "${INSTANCE}/state" "${BACKUP_DIR}"

log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >> "${LOG}"; }

# 防重入
LOCK="${INSTANCE}/state/backup.lockdir"
if ! mkdir "${LOCK}" 2>/dev/null; then
  log "上一次备份还在跑，跳过"; exit 0
fi
trap 'rmdir "${LOCK}"' EXIT

cd "${INSTANCE}"

# 1. 本地 git 快照
if [ ! -d .git ]; then
  git init -q -b main
  cat > .gitignore <<'EOF'
# 生成物与运行锁，不进快照
dashboard.html
*.lockdir
__pycache__/
state/*.log
EOF
  log "初始化本地 git 仓库"
fi
if git remote | grep -q .; then
  log "实例仓库出现了远端 $(git remote | tr '\n' ' ')，拒绝继续：实例永不 push"; exit 1
fi
git add -A
if ! git diff --cached --quiet; then
  n=$(git diff --cached --name-only | wc -l | tr -d ' ')
  git -c user.name=campus-backup -c user.email=backup@localhost \
    commit -q --no-verify -m "auto snapshot $(date '+%F %T') · ${n} files"
  log "git 快照：${n} 个文件变化"
fi

# 2. 每日目录快照（同一天重复运行只覆盖当天；未变化的文件硬链接到前一天，几乎不占额外空间）
DAY_DIR="${BACKUP_DIR}/$(date +%F)"
PREV="$(find "${BACKUP_DIR}" -mindepth 1 -maxdepth 1 -type d ! -name "$(date +%F)" | sort | tail -1)"
LINK=()
[ -n "${PREV}" ] && LINK=(--link-dest="${PREV}/instance")
mkdir -p "${DAY_DIR}"
rsync -a --delete --exclude '*.lockdir' "${LINK[@]+"${LINK[@]}"}" "${INSTANCE}/" "${DAY_DIR}/instance/"
if [ -f "${INSTANCE}/backup.extra" ]; then
  while IFS= read -r src; do
    [ -z "${src}" ] || [ "${src#\#}" != "${src}" ] && continue
    [ -e "${src}" ] || { log "额外目录不存在：${src}"; continue; }
    rsync -a --delete "${src%/}" "${DAY_DIR}/extra/"
  done < "${INSTANCE}/backup.extra"
fi
find "${BACKUP_DIR}" -mindepth 1 -maxdepth 1 -type d -mtime +"${KEEP_DAYS}" -exec rm -rf {} +
log "目录快照完成：${DAY_DIR}"
