#!/bin/sh
# 디스크 확인 — 배포·이미지 빌드/받기·파드 늘리기 같은, 디스크를 쓰는 일 전에 반드시 부른다. 걸리면 종료 코드 1.
#   sh disk_check.sh                 지금 상태로
#   sh disk_check.sh 8               이번 일에 colima에서 8GB를 더 쓸 예정 (정수 GB)
#   sh disk_check.sh 1400M rebuild   이미지 하나를 빌드 캐시로 다시 만들 때 (MB 단위, 여유 1%p로 판단 — 아래)
# 보는 것 세 가지 (하나라도 걸리면 멈춤):
#   1) 이미지 디스크 사용률(%) — 이번에 더 쓴 뒤의 사용률이 kubelet 이미지 GC 문턱 - 여유 미만이어야 한다
#   2) colima 데이터 디스크 여유 GB (MIN_VM + 더 쓸 양)
#   3) 맥 호스트 여유 GB
# 사용률은 kubelet과 같은 식: 100 - 내림(여유 * 100 / 크기) (df -Pk의 크기·여유. 예약 블록 때문에 '사용/크기'보다 높게 나온다)
# 왜:
#   2026-11-07 Ollama 이미지(3.4GB)를 받다가 colima 디스크가 모자라 kubelet이 운영 docflow·postgres·qdrant를 내쫓았다(축출).
#   2026-11-10 이미지 디스크가 GC 문턱(85%)을 넘은 채 배포하다, Recreate로 옛 파드가 내려간 사이 kubelet이 쓰이지 않던
#   docflow:dev를 지워 새 파드가 ErrImageNeverPull로 약 10분 중단 (https://github.com/reddol18/docflow/issues/119).
#   kubelet은 이미지 디스크 사용률이 high 문턱 이상이면 low 문턱까지, 돌고 있는 컨테이너가 쓰지 않는 이미지를 오래된 순으로 지운다.
#   여유 GB만 보던 예전 확인은 통과했다(문턱은 비율). 그래서 비율로 본다.
#   우리 이미지는 빌드 직후 고정(pin)해 GC 대상에서 빠지지만(build_image.sh), 다른 이미지가 지워지고 문턱 근처에서는
#   축출까지 가까워지므로 이 확인은 그대로 둔다.
# 정리 기준·목록: README 'colima 디스크 정리 기준', sh scripts/mac/image_list.sh (읽기만)
set -eu
export PATH="/usr/local/bin:/opt/homebrew/bin:$HOME/.local/bin:$PATH"
URL119=https://github.com/reddol18/docflow/issues/119

# ---- 값은 여기 한 곳 (환경 변수로 바꿀 수 있다. 배포 — scripts/deploy.ps1·scripts/mac/deploy.sh — 는 모두 지우고 부른다) ----
MIN_VM=${DISK_MIN_VM_GB:-4}              # colima 데이터 디스크 최소 여유 GB (+ 더 쓸 양)
MIN_HOST=${DISK_MIN_HOST_GB:-10}         # 맥 호스트 최소 여유 GB
GC_MARGIN=${DISK_GC_MARGIN_PCT:-3}       # high 문턱보다 이만큼(%p) 아래에서 멈춘다 — 빌드 추정 오차·롤아웃 중 로그 몫
GC_REBUILD_MARGIN=${DISK_GC_REBUILD_MARGIN_PCT:-1}   # rebuild 경로의 여유(%p) — 아래 인자 설명
GC_HIGH_DEFAULT=85                       # kubelet imageGCHighThresholdPercent 기본값 (2026-11-13 이 노드 configz 실측도 85)
GC_LOW_DEFAULT=80                        # kubelet imageGCLowThresholdPercent 기본값 (실측 80)
GC_HIGH_UNKNOWN=80                       # configz를 못 읽었을 때 쓰는 보수적 high 문턱
IMAGE_FS=${DISK_IMAGE_FS:-/var/lib/containerd}   # colima VM 안 이미지 디스크(containerd 루트, /var/lib/rancher와 같은 /dev/vdb1)
VM_FS=/mnt/lima-colima
# 시험 값 — 넷을 모두 함께 줄 때만 받는다(측정과 섞지 않음). 출력에 늘 '시험 값'이 찍힌다:
#   DISK_TEST_IMG_PCT  DISK_TEST_IMG_SIZE_GB  DISK_TEST_VM_AVAIL_GB  DISK_TEST_HOST_AVAIL_GB
# 문턱: DISK_GC_HIGH_PCT(+ DISK_GC_LOW_PCT)를 주면 클러스터에서 읽지 않는다. DISK_GC_QUERY=0이면 읽지 않고 기본값 85/80 (시험용)

die() { echo "disk_check: $*" >&2; exit 1; }
num() {  # 0 이상 정수, 6자리까지
  case "$2" in ''|*[!0-9]*) die "$1 값이 0 이상의 정수가 아님: '$2'" ;; esac
  [ "${#2}" -le 6 ] || die "$1 값이 너무 큼(6자리까지): '$2'"
}

# ---- 인자 ----
NEED_RAW=${1:-0}
MODE=${2:-}
case "$NEED_RAW" in
  *M) num "더 쓸 양(MB)" "${NEED_RAW%M}"; need_kb=$(( ${NEED_RAW%M} * 1024 )) ;;
  *)  num "더 쓸 양(GB)" "$NEED_RAW"; need_kb=$(( NEED_RAW * 1048576 )) ;;
esac
case "$MODE" in
  "") ;;
  # 다시 빌드: 캐시로 이미지 하나의 층(압축 blob + 풀린 스냅숏)만 다시 쓴다 — 양을 실측으로 알고(올려 잡음) 있으므로 여유를 1%p로.
  # 보통 여유(3%p, 82%)로 재면 GC 직후(low 80% 근처) 늘 막혀 되살릴 수 없다. 0%p면 high 바로 아래(84%)까지 채워 롤아웃 중
  # 로그·작은 쓰기로 high에 닿아 GC가 남의 이미지를 지울 수 있어 1%p(이 디스크 약 300MB)를 둔다. 여유 값은 DISK_GC_REBUILD_MARGIN_PCT
  rebuild) GC_MARGIN=$GC_REBUILD_MARGIN ;;
  *) die "두 번째 인자는 rebuild만 받는다: '$MODE'" ;;
esac
num DISK_MIN_VM_GB "$MIN_VM"; num DISK_MIN_HOST_GB "$MIN_HOST"; num DISK_GC_MARGIN_PCT "$GC_MARGIN"; num DISK_GC_REBUILD_MARGIN_PCT "$GC_REBUILD_MARGIN"
printf '%s\n' "$IMAGE_FS" | grep -Eq '^/[A-Za-z0-9/_.-]+$' || die "DISK_IMAGE_FS 경로 형식이 틀림: '$IMAGE_FS'"

n_test=0
for v in DISK_TEST_IMG_PCT DISK_TEST_IMG_SIZE_GB DISK_TEST_VM_AVAIL_GB DISK_TEST_HOST_AVAIL_GB; do
  eval "x=\${$v:-}"
  [ -z "$x" ] || { num "$v" "$x"; n_test=$((n_test + 1)); }
done
case "$n_test" in
  0) test_mode=0 ;;
  4) test_mode=1
     [ "$DISK_TEST_IMG_PCT" -le 100 ] || die "DISK_TEST_IMG_PCT는 100 이하"
     [ "$DISK_TEST_IMG_SIZE_GB" -gt 0 ] || die "DISK_TEST_IMG_SIZE_GB는 0보다 커야 함"
     echo "!! 시험 값 사용: 이미지 ${DISK_TEST_IMG_PCT}%/${DISK_TEST_IMG_SIZE_GB}GB, colima 여유 ${DISK_TEST_VM_AVAIL_GB}GB, 맥 여유 ${DISK_TEST_HOST_AVAIL_GB}GB — 실제 측정이 아님" ;;
  *) die "시험 값(DISK_TEST_IMG_PCT·DISK_TEST_IMG_SIZE_GB·DISK_TEST_VM_AVAIL_GB·DISK_TEST_HOST_AVAIL_GB)은 넷을 함께 줘야 한다 (${n_test}개만 있음)" ;;
esac

# ---- 측정 ----
dfk() {  # colima VM 안 df -Pk 한 경로 → "크기KB 여유KB"
  out=$(colima ssh -- df -Pk "$1" </dev/null 2>/dev/null) || die "colima VM 안 df 실패: $1 (경로가 없나? 'colima ssh -- df -Pk $1'로 확인)"
  printf '%s\n' "$out" | awk 'NR==2 {print $2, $4}'
}
if [ "$test_mode" = 1 ]; then
  img_size_kb=$((DISK_TEST_IMG_SIZE_GB * 1048576))
  img_avail_kb=$(( (img_size_kb * (100 - DISK_TEST_IMG_PCT) + 99) / 100 ))   # 올림 — kubelet 식으로 되돌렸을 때 정확히 그 %
  vm_avail_kb=$((DISK_TEST_VM_AVAIL_GB * 1048576))
  host=$DISK_TEST_HOST_AVAIL_GB
else
  colima ssh -- true </dev/null >/dev/null 2>&1 || die "colima VM에 접속하지 못함 (꺼져 있나? 'colima list'로 확인) — 멈춤"
  s=$(dfk "$IMAGE_FS") || exit 1
  img_size_kb=${s% *}; img_avail_kb=${s#* }
  s=$(dfk "$VM_FS") || exit 1
  vm_avail_kb=${s#* }
  host=$(df -g /Users | awk 'NR==2 {print $4}')
fi
for x in "$img_size_kb" "$img_avail_kb" "$vm_avail_kb" "$host"; do
  case "$x" in ''|*[!0-9]*) die "디스크 크기·여유를 읽지 못함 ($IMAGE_FS, $VM_FS, 맥 /Users): '$x'" ;; esac
done
vm_avail_gb=$((vm_avail_kb / 1048576))
[ "$img_size_kb" -gt 0 ] || die "이미지 디스크 크기가 0 ($IMAGE_FS)"

# kubelet GC 문턱: 환경 변수 > 노드의 실제 kubelet 설정(configz) > 못 읽으면 보수적으로 80%
gc_high=${DISK_GC_HIGH_PCT:-}; gc_low=${DISK_GC_LOW_PCT:-}; gc_src="환경 변수"
if [ -z "$gc_high" ]; then
  if [ "${DISK_GC_QUERY:-1}" = 0 ]; then
    gc_high=$GC_HIGH_DEFAULT; gc_low=$GC_LOW_DEFAULT; gc_src="kubelet 기본값 (DISK_GC_QUERY=0, 조회 안 함)"
  else
    node=""; cz=""
    if command -v kubectl >/dev/null 2>&1; then
      node=$(kubectl get nodes -o name </dev/null 2>/dev/null | head -1 | cut -d/ -f2) || node=""
      [ -z "$node" ] || cz=$(kubectl get --raw "/api/v1/nodes/$node/proxy/configz" </dev/null 2>/dev/null) || cz=""
    fi
    gc_high=$(printf '%s' "$cz" | sed -n 's/.*"imageGCHighThresholdPercent":\([0-9][0-9]*\).*/\1/p')
    gc_low=$(printf '%s' "$cz" | sed -n 's/.*"imageGCLowThresholdPercent":\([0-9][0-9]*\).*/\1/p')
    if [ -n "$gc_high" ]; then
      gc_src="노드 $node kubelet 설정(configz)"
    else
      # 멈추지 않고 보수적으로: 클러스터를 못 읽으면 뒤의 apply가 어차피 실패하고, 읽기 권한만 없을 때는 85보다 낮은 80으로 재면
      # GC 문턱 아래를 지킨다(기본 85 기준 여유 8%p). 실제 문턱이 80 아래로 설정된 클러스터는 아니다(colima k3sArgs에 kubelet 인자 없음)
      gc_high=$GC_HIGH_UNKNOWN; gc_low="?"
      gc_src="!! 실제 설정을 읽지 못해 보수적 값"
      echo "!! kubelet GC 문턱을 노드 configz에서 읽지 못함 — 보수적으로 high ${GC_HIGH_UNKNOWN}%로 판단 ($URL119)" >&2
    fi
  fi
fi
num "GC high 문턱" "$gc_high"; [ "$gc_low" = "?" ] || num "GC low 문턱" "${gc_low:-$GC_LOW_DEFAULT}"
gc_low=${gc_low:-$GC_LOW_DEFAULT}

# ---- 판단 ----
pct() {  # 여유KB → kubelet 사용률(%)
  if [ "$1" -le 0 ]; then echo 100; else echo $(( 100 - ($1 * 100 / img_size_kb) )); fi
}
now_pct=$(pct "$img_avail_kb")
exp_pct=$(pct $((img_avail_kb - need_kb)))
limit=$((gc_high - GC_MARGIN))
need_vm_kb=$((MIN_VM * 1048576 + need_kb))
need_vm_gb=$(( (need_vm_kb + 1048575) / 1048576 ))
unit=GB; case "$NEED_RAW" in *M) unit="" ;; esac   # $( ) 안의 case는 맥 /bin/sh(bash 3.2)가 못 읽는다(2026-11-13 확인)
echo "이미지 디스크($IMAGE_FS): 지금 ${now_pct}%, 이번에 ${NEED_RAW}${unit} 더 쓰면 ${exp_pct}% — ${limit}% 이상이면 멈춤 (GC high ${gc_high}% - 여유 ${GC_MARGIN}%p${MODE:+ [$MODE]}, low ${gc_low}%; 문턱 출처: $gc_src)"
echo "디스크 여유: colima ${vm_avail_gb}GB (필요 ${need_vm_gb}GB), 맥 ${host}GB (필요 ${MIN_HOST}GB)"
ok=1
if [ "$exp_pct" -ge "$limit" ]; then
  echo "!! 이미지 디스크 사용률 ${exp_pct}%가 멈춤 기준 ${limit}% 이상 — kubelet 이미지 GC(${gc_high}%)가 배포 중 이미지를 지울 수 있다 ($URL119)." >&2
  echo "   목록: sh ~/doc_platform/scripts/image_list.sh (읽기만). 지울 것은 사용자가 정한다 — README 'colima 디스크 정리 기준'" >&2
  ok=0
fi
[ "$vm_avail_kb" -ge "$need_vm_kb" ] || { echo "colima 디스크 여유가 모자람 — 쓰지 않는 이미지·빌드 캐시를 먼저 정리 (README 'colima 디스크 정리 기준')" >&2; ok=0; }
[ "$host" -ge "$MIN_HOST" ] || { echo "맥 디스크 여유가 모자람" >&2; ok=0; }
[ "$ok" = 1 ]
