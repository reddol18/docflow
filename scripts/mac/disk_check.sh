#!/bin/sh
# 디스크 확인 — 배포·이미지 빌드/받기·파드 늘리기 같은, 디스크를 쓰는 일 전에 반드시 부른다. 모자라면 종료 코드 1.
#   sh disk_check.sh            지금 상태로
#   sh disk_check.sh 8          이번 일에 colima에서 8GB를 더 쓸 예정
# 보는 것 세 가지 (하나라도 걸리면 멈춤):
#   1) 이미지 디스크 사용률(%) — '지금 쓴 양 + 이번에 더 쓸 GB'의 사용률이 kubelet 이미지 GC 문턱 - 여유 미만이어야 한다
#   2) colima 데이터 디스크 여유 GB (MIN_VM + 더 쓸 GB)
#   3) 맥 호스트 여유 GB
# 왜:
#   2026-11-07 Ollama 이미지(3.4GB)를 받다가 colima 디스크가 모자라 kubelet이 운영 docflow·postgres·qdrant를 내쫓았다(축출).
#   2026-11-10 이미지 디스크가 GC 문턱(85%)을 넘은 채 배포하다, Recreate로 옛 파드가 내려간 사이 kubelet이 쓰이지 않던
#   docflow:dev를 지워 새 파드가 ErrImageNeverPull로 약 10분 중단 (https://github.com/reddol18/docflow/issues/119).
#   kubelet은 이미지 디스크 사용률이 high 문턱 이상이면 low 문턱까지, 돌고 있는 컨테이너가 쓰지 않는 이미지를 오래된 순으로 지운다.
#   여유 GB만 보던 예전 확인은 통과했다(문턱은 비율). 그래서 비율로 본다.
# 정리 기준·목록: README 'colima 디스크 정리 기준', sh scripts/mac/image_list.sh (읽기만)
set -eu
export PATH="/usr/local/bin:/opt/homebrew/bin:$HOME/.local/bin:$PATH"

# ---- 값은 여기 한 곳 (환경 변수로 바꿀 수 있다) ----
MIN_VM=${DISK_MIN_VM_GB:-4}              # colima 데이터 디스크 최소 여유 GB (+ 더 쓸 GB)
MIN_HOST=${DISK_MIN_HOST_GB:-10}         # 맥 호스트 최소 여유 GB
GC_MARGIN=${DISK_GC_MARGIN_PCT:-3}       # high 문턱보다 이만큼(%p) 아래에서 멈춘다 — 빌드 추정 오차·롤아웃 중 로그 몫
GC_HIGH_DEFAULT=85                       # kubelet imageGCHighThresholdPercent 기본값 (실제 값을 못 읽을 때)
GC_LOW_DEFAULT=80                        # kubelet imageGCLowThresholdPercent 기본값
IMAGE_FS=${DISK_IMAGE_FS:-/var/lib/containerd}   # colima VM 안 이미지 디스크(containerd 루트, /var/lib/rancher와 같은 /dev/vdb1)
VM_FS=/mnt/lima-colima
# 시험 값 (설정하면 그 측정 대신 쓴다 — 출력에 늘 '시험 값'이 찍힌다. deploy.sh는 지우고 부른다):
#   DISK_TEST_IMG_PCT        이미지 디스크 사용률 %
#   DISK_TEST_IMG_SIZE_GB    이미지 디스크 크기 GB (없으면 실제 측정, 시험 사용률과 함께 씀)
#   DISK_TEST_VM_AVAIL_GB    colima 데이터 디스크 여유 GB
#   DISK_TEST_HOST_AVAIL_GB  맥 호스트 여유 GB
#   DISK_GC_HIGH_PCT / DISK_GC_LOW_PCT   GC 문턱 (설정하면 클러스터에서 읽지 않는다)
#   DISK_GC_QUERY=0          클러스터에서 문턱을 읽지 않고 기본값(85/80)을 쓴다

NEED_MORE=${1:-0}
num() { case "$2" in ''|*[!0-9]*) echo "disk_check: $1 값이 0 이상의 정수가 아님: '$2'" >&2; exit 1 ;; esac; }
num "더 쓸 GB" "$NEED_MORE"; num DISK_MIN_VM_GB "$MIN_VM"; num DISK_MIN_HOST_GB "$MIN_HOST"; num DISK_GC_MARGIN_PCT "$GC_MARGIN"
test_mode=""
for v in DISK_TEST_IMG_PCT DISK_TEST_IMG_SIZE_GB DISK_TEST_VM_AVAIL_GB DISK_TEST_HOST_AVAIL_GB; do
  eval "x=\${$v:-}"
  [ -z "$x" ] || { num "$v" "$x"; test_mode="$test_mode $v=$x"; }
done
[ -z "$test_mode" ] || echo "!! 시험 값 사용:$test_mode — 실제 측정이 아님"

# ---- 측정 ----
# colima VM 안 df 한 번 (시험 값이 다 있으면 부르지 않는다)
if [ -z "${DISK_TEST_IMG_PCT:-}" ] || [ -z "${DISK_TEST_IMG_SIZE_GB:-}" ] || [ -z "${DISK_TEST_VM_AVAIL_GB:-}" ]; then
  vmdf=$(colima ssh -- df -Pk "$VM_FS" "$IMAGE_FS" </dev/null) || { echo "colima 디스크를 읽지 못함 (colima가 꺼져 있나?) — 멈춤" >&2; exit 1; }
else
  vmdf=""
fi
# df -Pk 줄: 장치 크기KB 사용KB 여유KB 사용% 마운트
vm_avail_gb=${DISK_TEST_VM_AVAIL_GB:-$(printf '%s\n' "$vmdf" | awk 'NR==2 {print int($4/1048576)}')}
img_size_kb=$(printf '%s\n' "$vmdf" | awk 'NR==3 {print $2}')
img_used_kb=$(printf '%s\n' "$vmdf" | awk 'NR==3 {print $3}')
[ -z "${DISK_TEST_IMG_SIZE_GB:-}" ] || img_size_kb=$((DISK_TEST_IMG_SIZE_GB * 1048576))
[ -z "${DISK_TEST_IMG_PCT:-}" ] || img_used_kb=$((img_size_kb * DISK_TEST_IMG_PCT / 100))
num "이미지 디스크 크기(KB)" "${img_size_kb:-}"; num "이미지 디스크 사용(KB)" "${img_used_kb:-}"
[ "$img_size_kb" -gt 0 ] || { echo "이미지 디스크 크기가 0 — 멈춤" >&2; exit 1; }
if [ -n "${DISK_TEST_HOST_AVAIL_GB:-}" ]; then host=$DISK_TEST_HOST_AVAIL_GB; else host=$(df -g /Users | awk 'NR==2 {print $4}'); fi

# kubelet GC 문턱: 환경 변수 > 노드의 실제 kubelet 설정(configz) > 기본값
gc_high=${DISK_GC_HIGH_PCT:-}; gc_low=${DISK_GC_LOW_PCT:-}; gc_src="환경 변수"
if [ -z "$gc_high" ] && [ "${DISK_GC_QUERY:-1}" != 0 ] && command -v kubectl >/dev/null 2>&1; then
  node=$(kubectl get nodes -o name </dev/null 2>/dev/null | head -1 | cut -d/ -f2) || node=""
  if [ -n "$node" ]; then
    cz=$(kubectl get --raw "/api/v1/nodes/$node/proxy/configz" </dev/null 2>/dev/null) || cz=""
    gc_high=$(printf '%s' "$cz" | sed -n 's/.*"imageGCHighThresholdPercent":\([0-9][0-9]*\).*/\1/p')
    gc_low=$(printf '%s' "$cz" | sed -n 's/.*"imageGCLowThresholdPercent":\([0-9][0-9]*\).*/\1/p')
    [ -z "$gc_high" ] || gc_src="노드 $node kubelet 설정(configz)"
  fi
fi
if [ -z "$gc_high" ]; then gc_high=$GC_HIGH_DEFAULT; gc_src="kubelet 기본값 (실제 설정을 읽지 못함)"; fi
gc_low=${gc_low:-$GC_LOW_DEFAULT}
num "GC high 문턱" "$gc_high"; num "GC low 문턱" "$gc_low"

# ---- 판단 ----
# 사용률은 올림: (지금 사용 + 더 쓸 양) / 크기
need_kb=$((NEED_MORE * 1048576))
now_pct=$(( (img_used_kb * 100 + img_size_kb - 1) / img_size_kb ))
exp_pct=$(( ((img_used_kb + need_kb) * 100 + img_size_kb - 1) / img_size_kb ))
limit=$((gc_high - GC_MARGIN))
need_vm=$((MIN_VM + NEED_MORE))
echo "이미지 디스크($IMAGE_FS): 지금 ${now_pct}%, 이번에 ${NEED_MORE}GB 더 쓰면 ${exp_pct}% — 멈춤 기준 ${limit}% 이상이면 멈춤 (GC high ${gc_high}% - 여유 ${GC_MARGIN}%p, low ${gc_low}%; 문턱 출처: $gc_src)"
echo "디스크 여유: colima ${vm_avail_gb}GB (필요 ${need_vm}GB), 맥 ${host}GB (필요 ${MIN_HOST}GB)"
ok=1
if [ "$exp_pct" -ge "$limit" ]; then
  echo "!! 이미지 디스크 사용률 ${exp_pct}%가 멈춤 기준 ${limit}% 이상 — kubelet 이미지 GC(${gc_high}%)가 배포 중 docflow 이미지를 지울 수 있다 (issues/119)." >&2
  echo "   목록: sh ~/doc_platform/scripts/image_list.sh (읽기만). 지울 이미지는 사용자가 정한다 — README 'colima 디스크 정리 기준'" >&2
  ok=0
fi
[ "${vm_avail_gb:-0}" -ge "$need_vm" ] || { echo "colima 디스크 여유가 모자람 — 쓰지 않는 이미지·데이터를 먼저 정리 (README 'colima 디스크 정리 기준')" >&2; ok=0; }
[ "${host:-0}" -ge "$MIN_HOST" ] || { echo "맥 디스크 여유가 모자람" >&2; ok=0; }
[ "$ok" = 1 ]
