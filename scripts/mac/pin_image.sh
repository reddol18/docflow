# 우리 이미지 고정(pin) — build_image.sh·deploy.sh가 불러 쓴다 (. ~/doc_platform/scripts/pin_image.sh). 혼자 실행하지 않는다.
# https://github.com/reddol18/docflow/issues/119
# containerd 라벨 io.cri-containerd.pinned=pinned를 이름 ref(docker.io/library/<이름>)에 붙이면 CRI가 그 이미지를 pinned: true로
# 알리고 kubelet 이미지 GC는 지우지 않는다. 2026-11-13 이 클러스터(k3s v1.35, containerd v2.3.1)에서 docflow-web:dev에
# 붙였다 떼어 crictl inspecti의 pinned가 true/false로 바뀌는 것을 확인했다.
# 시험: tests/test_disk_check.py가 PATH 앞의 가짜 colima(tests/fixtures/fakebin)로 분기를 돌린다.
PIN_TRIES=${PIN_TRIES:-10}   # CRI 확인 횟수 (1초 간격) — 빌드 직후 CRI 캐시에 늦게 보일 수 있다
PIN_SLEEP=${PIN_SLEEP:-1}

vm() { colima ssh -- "$@" </dev/null; }
# crictl images -q <이름>은 이름을 거르지 않고 모든 ID를 낸다(2026-11-13 확인) → inspecti로 하나만. 없으면 빈 값
image_id() { vm sudo crictl inspecti -o go-template --template '{{.status.id}}' "docker.io/library/$1" 2>/dev/null || true; }
is_pinned() { vm sudo crictl inspecti "$1" 2>/dev/null | grep -q '"pinned": true'; }

# 라벨을 붙이고(CRI에 아직 안 보여도 ctr 이름 ref에 먼저) CRI의 id·pinned: true를 PIN_TRIES번까지 다시 본다. 되면 0
pin_once() {  # $1 이름
  ref="docker.io/library/$1"
  vm sudo ctr -n k8s.io images label "$ref" io.cri-containerd.pinned=pinned >/dev/null 2>&1 || return 1
  i=0
  while [ "$i" -lt "$PIN_TRIES" ]; do
    pin_id=$(image_id "$1")
    if [ -n "$pin_id" ] && is_pinned "$ref"; then return 0; fi
    i=$((i + 1)); [ "$i" -ge "$PIN_TRIES" ] || sleep "$PIN_SLEEP"
  done
  return 1
}

# $1 이름(docflow:dev), $2 빌드 전 ID(없으면 빈 값), $3 strict면 실패 때 0이 아닌 값(배포 경로 — 롤아웃하지 않게)
pin_image() {
  pin_id=""
  if pin_once "$1" || { echo "!! 이미지 고정: $1 첫 시도 실패 — 한 번 더" >&2; pin_once "$1"; }; then
    echo "== 이미지 고정: $1 ${pin_id#sha256:} (pinned: true — kubelet 이미지 GC 대상 아님)"
  else
    if [ "${3:-}" = strict ]; then
      echo "!! 이미지 고정 실패: $1 — 두 번 시도해도 pinned: true가 아님. 배포 경로라 멈춘다 (롤아웃하지 않음)" >&2
      return 1
    fi
    echo "!! 이미지 고정 실패: $1 — GC에서 빠지지 않는다 (수동 빌드라 경고만. 배포는 deploy.sh가 다시 확인하고 멈춘다)" >&2
  fi
  # 옛 판: 라벨은 이름 ref에만 붙이므로, 새 빌드가 태그를 옮기면 옛 ID에는 고정 ref가 남지 않아 CRI가 고정을 푼다.
  # 여기서는 풀렸는지 확인만 한다 — 남아 있으면 GC되지 않아 디스크를 잡는다
  if [ -z "${2:-}" ]; then
    echo "== 옛 판 확인 생략: $1 빌드 전 ID 없음 — 고정이 남은 옛 판은 sh ~/doc_platform/scripts/image_list.sh '고정된 이미지'에서 본다"
  elif [ "$2" != "$pin_id" ]; then
    if is_pinned "$2"; then
      echo "!! 옛 판 고정이 남음: $1 ${2#sha256:} — GC되지 않아 디스크를 잡는다. image_list.sh '고정된 이미지'에서 보고 사용자가 정한다" >&2
    else
      echo "== 옛 판 고정 없음: $1 ${2#sha256:} (안 쓰이면 GC 대상)"
    fi
  fi
  return 0
}
