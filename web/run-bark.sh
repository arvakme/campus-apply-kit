#!/usr/bin/env bash
# 自建 bark-server（官方镜像 finab/bark-server，注意不是 finb）· Apple container CLI
#
#   CAMPUS_INSTANCE=~/campus-apply-instance web/run-bark.sh     # 拉镜像（首次）+ 起容器
#   web/run-bark.sh stop                                        # 停容器（保留镜像和数据）
#
# 数据（设备注册表 bark.db）挂到 $CAMPUS_INSTANCE/state/bark；端口只绑 127.0.0.1:8788 → 容器 8080（CAMPUS_BARK_PORT 可改）。
# 手机 Bark App 里把服务器设为 tailnet 地址，见 web/README.md。
set -euo pipefail

IMAGE="${CAMPUS_BARK_IMAGE:-docker.io/finab/bark-server:latest}"
NAME="${CAMPUS_BARK_NAME:-campus-bark}"
PORT="${CAMPUS_BARK_PORT:-8788}"   # 8090 常被别的服务占用，默认用 8788

die() { echo "ERROR: $*" >&2; exit 1; }
command -v container >/dev/null || die "没有 container CLI"

if [[ "${1:-}" == "stop" ]]; then
  container stop "$NAME" >/dev/null 2>&1 && echo "已停止 ${NAME}（镜像与数据保留）" || echo "$NAME 没在运行"
  exit 0
fi

[[ -n "${CAMPUS_INSTANCE:-}" ]] || die "先 export CAMPUS_INSTANCE=<实例目录>"
INSTANCE="$(cd "$CAMPUS_INSTANCE" 2>/dev/null && pwd -P)" || die "实例目录不存在：$CAMPUS_INSTANCE"
mkdir -p "$INSTANCE/state/bark"

# 端口被非本容器占用时 container run 只报 "Address already in use"，提前说清楚
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 && ! container inspect "$NAME" >/dev/null 2>&1; then
  die "127.0.0.1:$PORT 已被占用（lsof -nP -iTCP:$PORT -sTCP:LISTEN 查是谁），换端口：CAMPUS_BARK_PORT=<端口> $0" 
fi

container system status >/dev/null 2>&1 || container system start --enable-kernel-install

# 只拉 arm64，避免把多架构全部解包（python 这类镜像全平台解包要好几分钟）
if ! container image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "== container image pull $IMAGE"
  container image pull --platform linux/arm64 "$IMAGE"
fi

if container inspect "$NAME" >/dev/null 2>&1; then
  container stop "$NAME" >/dev/null 2>&1 || true
  container delete "$NAME" >/dev/null 2>&1 || true
fi

echo "== container run $NAME  (/data ← $INSTANCE/state/bark)"
container run -d --name "$NAME" \
  -p "127.0.0.1:${PORT}:8080" \
  -v "$INSTANCE/state/bark":/data \
  "$IMAGE" >/dev/null

for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${PORT}/ping" >/dev/null 2>&1; then
    echo "OK  http://127.0.0.1:${PORT}/ping   （notify.yaml 的 bark_server 可指向 tailnet 上的这个端口）"
    exit 0
  fi
  sleep 1
done
container logs "$NAME" 2>&1 | tail -20 >&2 || true
die "bark-server 30 秒内没有响应 /ping"
