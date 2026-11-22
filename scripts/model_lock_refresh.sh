#!/bin/bash
# OCR 모델 lock(app/models.lock)을 다시 만든다 — 모델 판(paddleocr·paddlex 또는 app/ocr.py 모델 설정)을 올릴 때만
# (https://github.com/reddol18/docflow/issues/102). 보통은 Windows에서 scripts/model_lock_refresh.ps1로 부른다(WSL 배포판 안 root).
#
#   bash scripts/model_lock_refresh.sh <저장소 폴더> <쓸 lock 파일>
#
# 필요한 것: docker + buildx. Dockerfile의 model-lock-out 단계만 빌드한다 — 앱 이미지와 같은 명령(python -m app.warm)으로
# 모델을 받아 그 폴더의 목록·sha256을 꺼낸다(--output type=local, 이미지 태그 없음). 배포·재시작은 하지 않는다.
# 빌드 맥락은 그 단계가 쓰는 파일만 임시 폴더에 모은다 (저장소 전체 — node_modules 등 — 를 보내지 않게).
# 디스크: 시작 전에 / 여유를 확인하고 모자라면 멈춘다 (records_poc/CLAUDE.md '디스크 여유 확인').
set -euo pipefail
REPO=${1:?저장소 폴더}
OUT=${2:?쓸 lock 파일}
NEED_ROOT_GB=${NEED_ROOT_GB:-10}  # 기반 이미지 + 패키지 층(약 2GB) + 모델 + 빌드 캐시 여유

free_gb() { df -P -BG "$1" | awk 'NR==2 {gsub("G","",$4); print $4}'; }
have=$(free_gb /)
echo "디스크 / 여유 ${have}GB (필요 ${NEED_ROOT_GB}GB)"
[ "$have" -ge "$NEED_ROOT_GB" ] || { echo "디스크 모자람: / 여유 ${have}GB < ${NEED_ROOT_GB}GB — 멈춤" >&2; exit 1; }
docker buildx version >/dev/null 2>&1 || { echo "docker buildx 없음" >&2; exit 1; }

CTX=$(mktemp -d); DEST=$(mktemp -d)
trap 'rm -rf "$CTX" "$DEST"' EXIT
mkdir -p "$CTX/app"
for f in Dockerfile requirements.txt requirements.lock; do tr -d '\r' < "$REPO/$f" > "$CTX/$f"; done
for f in __init__.py ocr.py warm.py model_lock.py; do tr -d '\r' < "$REPO/app/$f" > "$CTX/app/$f"; done

echo "== model-lock-out 빌드 (모델 받기 → 목록·sha256)"
# --no-cache-filter model-lock: 모델 받기는 늘 새로 한다 (캐시된 층이면 지금 배포처의 파일이 아니라 예전 목록이 나온다)
DOCKER_BUILDKIT=1 docker buildx build --progress=plain --no-cache-filter model-lock --target model-lock-out \
  --output "type=local,dest=$DEST" "$CTX"
[ -s "$DEST/models.lock" ] || { echo "models.lock이 나오지 않음" >&2; exit 1; }
cp "$DEST/models.lock" "$OUT"
echo "== 씀: $OUT ($(grep -vc '^#' "$OUT")개 파일)"
