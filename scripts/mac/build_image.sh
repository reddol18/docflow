#!/bin/sh
# 맥에서: k3s가 쓰는 containerd(k8s.io)에 이미지 둘을 빌드한다.
#   docflow:dev      ~/doc_platform (API + OCR)
#   docflow-web:dev  ~/doc_platform/frontend — 프런트 운영 이미지: 빌드 안에서 next build, 실행은 next start
#                    (frontend/Dockerfile, https://github.com/reddol18/docflow/issues/48). 개발 서버(next dev)는 들어가지 않는다
#   sh build_image.sh [all|api|web] [deploy|rebuild]
#     기본 all. api·web은 하나만. 두 번째 인자는 배포 경로 표시(deploy.sh만 준다):
#       deploy   보통 배포 빌드 — 고정(pin)이 두 번 시도해도 안 되면 멈춘다(그 뒤 apply·롤아웃을 하지 않게)
#       rebuild  롤아웃 직전 이미지가 없을 때(kubelet 이미지 GC) 빌드 캐시로 그 하나만 다시 만드는 경로 — 디스크 확인을
#                그 이미지 크기로 하고(아래), 고정이 안 되면 멈춘다 (https://github.com/reddol18/docflow/issues/119)
#     인자 없이(손으로) 빌드하면 고정 실패는 경고만 한다.
# 빌드한 이미지는 바로 고정한다 — scripts/mac/pin_image.sh (라벨 io.cri-containerd.pinned=pinned, kubelet GC가 지우지 않음)
set -e
WHAT=${1:-all}
MODE=${2:-}
case "$WHAT" in all|api|web) ;; *) echo "build_image.sh: all|api|web 중 하나 (받은 값: $WHAT)" >&2; exit 1 ;; esac
case "$MODE" in ""|deploy|rebuild) ;; *) echo "build_image.sh: 두 번째 인자는 deploy·rebuild만 (받은 값: $MODE)" >&2; exit 1 ;; esac
[ "$MODE" != rebuild ] || [ "$WHAT" != all ] || { echo "build_image.sh: rebuild는 api·web 하나만" >&2; exit 1; }
PIN_MODE=""; [ -z "$MODE" ] || PIN_MODE=strict
export PATH="/usr/local/bin:$HOME/.local/bin:$PATH"
# 시간 제한·중단 (Windows scripts/lib/remote.ps1이 이 프로세스 그룹에 TERM, https://github.com/reddol18/docflow/issues/117):
# 호스트의 colima 클라이언트가 죽어도 VM 안 nerdctl build·buildctl은 계속 돈다 → 우리 빌드(docflow:dev·docflow-web:dev)만 멈춘다.
# '[n]erdctl'은 pkill이 자기를 감싼 셸 명령줄을 잡지 않게 (remote.ps1 $RemoteVmBuildPattern과 같은 무늬 — 시험이 본다)
VM_BUILD='([n]erdctl|[b]uildctl).*docflow(-web)?:dev'
stop_vm_build() {
  trap - TERM INT HUP
  echo "!! 중단 신호 — colima VM 안 docflow 빌드를 멈춘다" >&2
  colima ssh -- sudo pkill -TERM -f -- "$VM_BUILD" </dev/null >/dev/null 2>&1 || true
  exit 143
}
trap stop_vm_build TERM INT HUP
cd ~/doc_platform
. ~/doc_platform/scripts/pin_image.sh
# 빌드 전 디스크 확인 (반드시, 따로 불러도) — scripts/mac/disk_check.sh
if [ "$MODE" = rebuild ]; then
  # 캐시로 이미지 하나를 다시 쓰는 양 = 압축 층(blob) + 풀린 스냅숏. 2026-11-13 실측: docflow 618MiB + 648MB ≈ 1.3GB,
  # docflow-web 190MiB + 199MB ≈ 0.4GB → 올려 1400M·450M. GC가 방금 지웠다면 사용률이 low(80%) 근처라 보통 여유(3%p)를
  # 두면 늘 막히므로, 양을 아는 이 경로는 여유 1%p로 잰다(disk_check.sh rebuild — 근거는 README 'colima 디스크 정리 기준')
  size=450M; [ "$WHAT" != api ] || size=1400M
  sh ~/doc_platform/scripts/disk_check.sh "$size" rebuild </dev/null || {
    echo "!! 다시 빌드할 디스크가 모자람 — 정리(사용자 판단, README 'colima 디스크 정리 기준') 뒤: sh ~/doc_platform/scripts/build_image.sh $WHAT rebuild" >&2
    exit 1; }
else
  # 2GB 더: 프런트 이미지(node 기반 약 60MB 받기 + npm ci·next build 층, 이미지 약 0.4GB)와 빌드 캐시 몫
  sh ~/doc_platform/scripts/disk_check.sh 2 </dev/null
fi
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
pin_image docflow:dev "$old_api" $PIN_MODE
fi
if [ "$WHAT" != api ]; then
# 프런트: npm ci(package-lock.json 그대로) → next build → 개발 의존성 빼기 → 실행 단계. rewrite 주소(DOCFLOW_URL)는 빌드 때 정해진다
[ -f ~/doc_platform/frontend/Dockerfile ] || { echo "frontend/Dockerfile 없음 — scripts/deploy.ps1이 frontend를 복사한다" >&2; exit 1; }
old_web=$(image_id docflow-web:dev)
colima nerdctl -- --namespace k8s.io build -t docflow-web:dev frontend \
    > ~/docflow_web_build.log 2>&1 </dev/null || { tail -40 ~/docflow_web_build.log; exit 1; }
grep -E 'Compiled successfully|next build|Route \(app\)' ~/docflow_web_build.log | tail -3 || true
pin_image docflow-web:dev "$old_web" $PIN_MODE
fi
colima nerdctl -- --namespace k8s.io images </dev/null | grep docflow
