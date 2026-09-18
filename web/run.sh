#!/usr/bin/env bash
# 构建并（重新）启动 web 容器 · Apple 原生 container CLI 1.0（不是 docker）
#
#   CAMPUS_INSTANCE=~/campus-apply-instance web/run.sh          # 构建 + 起容器
#   web/run.sh --no-build                                       # 只重启，不重新构建
#   web/run.sh stop                                             # 停容器（保留镜像）
#   web/run.sh bark [stop]                                      # 自建 bark-server，见 web/run-bark.sh
#
# 端口只绑 127.0.0.1:8787；手机访问用 tailscale serve 暴露到 tailnet（见 web/README.md）。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${CAMPUS_WEB_IMAGE:-campus-apply-web}"
NAME="${CAMPUS_WEB_NAME:-campus-web}"
PORT="${CAMPUS_WEB_PORT:-8787}"

die() { echo "ERROR: $*" >&2; exit 1; }
command -v container >/dev/null || die "没有 container CLI：从 https://github.com/apple/container/releases 安装（需 macOS 26 + Apple 芯片）"

case "${1:-}" in
  bark) shift; exec "$REPO/web/run-bark.sh" "$@" ;;
  stop) container stop "$NAME" >/dev/null 2>&1 && echo "已停止 ${NAME}（镜像保留）" || echo "$NAME 没在运行"; exit 0 ;;
esac

BUILD=1
[[ "${1:-}" == "--no-build" ]] && BUILD=0

[[ -n "${CAMPUS_INSTANCE:-}" ]] || die "先 export CAMPUS_INSTANCE=<实例目录>"
INSTANCE="$(cd "$CAMPUS_INSTANCE" 2>/dev/null && pwd -P)" || die "实例目录不存在：$CAMPUS_INSTANCE"

# 端口被非本容器占用时 container run 只报 "Address already in use"，提前说清楚
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 && ! container inspect "$NAME" >/dev/null 2>&1; then
  die "127.0.0.1:$PORT 已被占用（lsof -nP -iTCP:$PORT -sTCP:LISTEN 查是谁），换端口：CAMPUS_WEB_PORT=<端口> $0" 
fi

# 1. 系统服务
if ! container system status >/dev/null 2>&1; then
  echo "== container system start"
  container system start --enable-kernel-install
fi

# 2. 构建（上下文是仓库根，.containerignore 只放行 web/app）
if (( BUILD )); then
  echo "== container build -t $IMAGE"
  container build -t "$IMAGE" -f "$REPO/web/Containerfile" "$REPO"
fi

# 3. 停掉并删除旧容器（同名容器存在时 run 会失败）
if container inspect "$NAME" >/dev/null 2>&1; then
  echo "== 替换旧容器 $NAME"
  container stop "$NAME" >/dev/null 2>&1 || true
  container delete "$NAME" >/dev/null 2>&1 || true
fi

# 4. 启动
echo "== container run $NAME  (/instance ← ${INSTANCE})"
# 仓库 data/ 只读挂进容器，榜单实时读公告与分级，不依赖快照
REPO_DATA="$(cd "$(dirname "$0")/.." && pwd -P)/data"
container run -d --name "$NAME" \
  -p "127.0.0.1:${PORT}:8787" \
  -v "$INSTANCE":/instance \
  --mount "type=bind,source=${REPO_DATA},target=/repo/data,readonly" \
  -e CAMPUS_REPO=/repo \
  "$IMAGE" >/dev/null

# 5. 等健康检查
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
    echo "OK  http://127.0.0.1:${PORT}/   （tailnet：tailscale serve --bg ${PORT}，由你决定是否执行）"
    exit 0
  fi
  sleep 1
done
container logs "$NAME" 2>&1 | tail -20 >&2 || true
die "容器 30 秒内没有通过 /healthz"
