#!/bin/sh
# 맥에서: k3s가 쓰는 containerd(k8s.io)에 이미지 둘을 빌드한다.
#   docflow:dev      ~/doc_platform (API + OCR)
#   docflow-web:dev  ~/doc_platform/frontend — 프런트 운영 이미지: 빌드 안에서 next build, 실행은 next start
#                    (frontend/Dockerfile, https://github.com/reddol18/docflow/issues/48). 개발 서버(next dev)는 들어가지 않는다
#   sh build_image.sh [all|api|web] [rebuild]
#     기본 all. api·web은 하나만. rebuild: deploy.sh가 롤아웃 직전 이미지가 없을 때(kubelet 이미지 GC) 빌드 캐시로 그 하나만
#     다시 만드는 경로 — 디스크 확인을 그 이미지 크기로 한다(아래) (https://github.com/reddol18/docflow/issues/119)
# 빌드한 이미지는 바로 고정(pin)한다: containerd 라벨 io.cri-containerd.pinned=pinned → CRI가 pinned: true로 알리고
# kubelet 이미지 GC는 고정 이미지를 지우지 않는다. 2026-11-13 이 클러스터(k3s v1.35, containerd v2.3.1)에서
# docflow-web:dev에 붙였다 떼어 crictl inspecti의 pinned가 true/false로 바뀌는 것을 확인했다.
# 태그가 새 빌드로 옮겨 가면 새 이미지에 다시 붙이고(라벨이 남든 안 남든 같게), 옛 판 ID에 남은 고정은 푼다 —
# 고정된 옛 판은 GC되지 않아 디스크를 계속 잡기 때문. 풀지 못하면 '!!'로 알리고 image_list.sh '고정된 이미지'에 보인다.
set -e
WHAT=${1:-all}
MODE=${2:-}
case "$WHAT" in all|api|web) ;; *) echo "build_image.sh: all|api|web 중 하나 (받은 값: $WHAT)" >&2; exit 1 ;; esac
case "$MODE" in ""|rebuild) ;; *) echo "build_image.sh: 두 번째 인자는 rebuild만 (받은 값: $MODE)" >&2; exit 1 ;; esac
[ "$MODE" != rebuild ] || [ "$WHAT" != all ] || { echo "build_image.sh: rebuild는 api·web 하나만" >&2; exit 1; }
export PATH="/usr/local/bin:$HOME/.local/bin:$PATH"
cd ~/doc_platform
# 빌드 전 디스크 확인 (반드시, 따로 불러도) — scripts/mac/disk_check.sh
if [ "$MODE" = rebuild ]; then
  # 캐시로 이미지 하나를 다시 쓰는 양 = 압축 층(blob) + 풀린 스냅숏. 2026-11-13 실측: docflow 618MiB + 648MB ≈ 1.3GB,
  # docflow-web 190MiB + 199MB ≈ 0.4GB → 올려 1400M·450M. GC가 방금 지웠다면 사용률이 low(80%) 근처라 여유(-3%p)를 두면
  # 늘 막히므로, 양을 아는 이 경로는 GC high 문턱 자체로 잰다(disk_check.sh rebuild)
  if [ "$WHAT" = api ]; then sh ~/doc_platform/scripts/disk_check.sh 1400M rebuild </dev/null
  else sh ~/doc_platform/scripts/disk_check.sh 450M rebuild </dev/null; fi
else
  # 2GB 더: 프런트 이미지(node 기반 약 60MB 받기 + npm ci·next build 층, 이미지 약 0.4GB)와 빌드 캐시 몫
  sh ~/doc_platform/scripts/disk_check.sh 2 </dev/null
fi

vm() { colima ssh -- "$@" </dev/null; }
# crictl images -q <이름>은 이름을 거르지 않고 모든 ID를 낸다(2026-11-13 확인) → inspecti로 하나만. 없으면 빈 값
image_id() { vm sudo crictl inspecti -o go-template --template '{{.status.id}}' "docker.io/library/$1" 2>/dev/null || true; }
pin_image() {  # $1 이름(docflow:dev), $2 빌드 전 ID(없으면 빈 값). 실패는 '!!'로 알리고 빌드는 그대로 (deploy.sh의 이미지 확인이 보조)
  ref="docker.io/library/$1"
  new=$(image_id "$1")
  [ -n "$new" ] || { echo "!! 이미지 고정: $1 이(가) 빌드 뒤 CRI에 보이지 않음" >&2; return 0; }
  if vm sudo ctr -n k8s.io images label "$ref" io.cri-containerd.pinned=pinned >/dev/null \
     && vm sudo crictl inspecti "$ref" | grep -q '"pinned": true'; then
    echo "== 이미지 고정: $1 ${new#sha256:} (pinned: true — kubelet 이미지 GC 대상 아님)"
  else
    echo "!! 이미지 고정 실패: $1 — GC에서 빠지지 않는다. deploy.sh의 롤아웃 전 이미지 확인만 남는다" >&2
  fi
  # 옛 판: 태그가 옮겨 가 ID가 바뀌었으면 그 ID에 남은 고정을 푼다. 옛 ID의 ref는 CRI가 만든 sha256:<ID> 뿐(없으면 할 일 없음)
  if [ -n "$2" ] && [ "$2" != "$new" ]; then
    if vm sudo ctr -n k8s.io images ls -q "name==$2" | grep -q .; then
      vm sudo ctr -n k8s.io images label "$2" io.cri-containerd.pinned= >/dev/null || true
    fi
    if vm sudo crictl inspecti "$2" 2>/dev/null | grep -q '"pinned": true'; then
      echo "!! 옛 판 고정이 남음: $1 ${2#sha256:} — GC되지 않아 디스크를 잡는다. image_list.sh '고정된 이미지'에서 보고 사용자가 정한다" >&2
    else
      echo "== 옛 판 고정 없음: $1 ${2#sha256:} (안 쓰이면 GC 대상)"
    fi
  fi
}
# --pull 없음: Dockerfile FROM이 다이제스트 고정이라(https://github.com/reddol18/docflow/issues/46) 다시 확인해도 같은 이미지다 — 레지스트리 장애에 빌드가 막히지 않게 뺀다.
# 기반 이미지 갱신은 Dockerfile의 다이제스트를 바꿀 때만 들어온다 (태그와 다이제스트를 함께, scripts/mac/image_tags.sh로 확인).
# APT_REFRESH(ISO 주): apt 갱신 층의 캐시를 주 1회 깬다 — 그대로 두면 처음 빌드한 층이 계속 재사용돼
# 그 뒤 나온 Debian 보안 갱신이 들어오지 않는다 (https://github.com/reddol18/docflow/issues/44)
if [ "$WHAT" != web ]; then
old_api=$(image_id docflow:dev)
colima nerdctl -- --namespace k8s.io build --build-arg "APT_REFRESH=$(date +%G-W%V)" -t docflow:dev . \
    > ~/docflow_build.log 2>&1 </dev/null || { tail -40 ~/docflow_build.log; exit 1; }
tail -5 ~/docflow_build.log
# 패키지가 requirements.lock 그대로인지 (Dockerfile이 설치 뒤 diff, https://github.com/reddol18/docflow/issues/33). 층을 캐시로 쓰면 줄이 안 나온다
# 개수가 숫자인 줄만 (캐시면 로그에 RUN 명령 글자 "$(wc -l < /tmp/want)개 ..."가 찍혀 그것이 잡혔다). 파이프 끝 tail은 늘 성공하므로 빈 값으로 판단
lock_line=$(grep -oE 'lock 일치: 이미지 패키지 [0-9]+개 = requirements\.lock' ~/docflow_build.log | tail -1)
echo "${lock_line:-lock 일치: 설치 층 캐시 사용 (그 층을 만든 빌드에서 확인됨)}"
pin_image docflow:dev "$old_api"
fi
if [ "$WHAT" != api ]; then
# 프런트: npm ci(package-lock.json 그대로) → next build → 개발 의존성 빼기 → 실행 단계. rewrite 주소(DOCFLOW_URL)는 빌드 때 정해진다
[ -f ~/doc_platform/frontend/Dockerfile ] || { echo "frontend/Dockerfile 없음 — scripts/deploy.ps1이 frontend를 복사한다" >&2; exit 1; }
old_web=$(image_id docflow-web:dev)
colima nerdctl -- --namespace k8s.io build -t docflow-web:dev frontend \
    > ~/docflow_web_build.log 2>&1 </dev/null || { tail -40 ~/docflow_web_build.log; exit 1; }
grep -E 'Compiled successfully|next build|Route \(app\)' ~/docflow_web_build.log | tail -3 || true
pin_image docflow-web:dev "$old_web"
fi
colima nerdctl -- --namespace k8s.io images </dev/null | grep docflow
