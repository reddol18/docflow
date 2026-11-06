#!/bin/sh
# 디스크 여유 확인 — 배포·이미지 빌드/받기·파드 늘리기 같은, 디스크를 쓰는 일 전에 반드시 부른다. 모자라면 종료 코드 1.
#   sh disk_check.sh            colima 데이터 디스크 4GB, 맥 호스트 10GB 이상인지
#   sh disk_check.sh 8          이번 일에 colima에서 8GB를 더 쓸 예정 → 8 + 4GB 필요
# 왜: 2026-11-07 Ollama 이미지(3.4GB)를 받다가 colima 디스크가 모자라 kubelet이 운영 docflow·postgres·qdrant를
#     내쫓고(디스크 1.6GB 아래에서 축출), 이미지 정리가 docflow:dev까지 지워 다시 빌드했다.
#     colima 디스크 사용률 85%를 넘으면 kubelet이 쓰지 않는 이미지를 지운다(지금 쓰는 이미지만 남음).
set -eu
export PATH="/usr/local/bin:/opt/homebrew/bin:$HOME/.local/bin:$PATH"
NEED_MORE=${1:-0}
MIN_VM=${DISK_MIN_VM_GB:-4}
MIN_HOST=${DISK_MIN_HOST_GB:-10}
vm=$(colima ssh -- df -BG --output=avail /mnt/lima-colima | tail -1 | tr -dc '0-9')
host=$(df -g /Users | awk 'NR==2 {print $4}')
need_vm=$((MIN_VM + NEED_MORE))
echo "디스크 여유: colima ${vm}GB (필요 ${need_vm}GB), 맥 ${host}GB (필요 ${MIN_HOST}GB)"
ok=1
[ "${vm:-0}" -ge "$need_vm" ] || { echo "colima 디스크 여유가 모자람 — 쓰지 않는 이미지·데이터를 먼저 정리" >&2; ok=0; }
[ "${host:-0}" -ge "$MIN_HOST" ] || { echo "맥 디스크 여유가 모자람" >&2; ok=0; }
[ "$ok" = 1 ]
