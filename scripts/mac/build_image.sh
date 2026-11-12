#!/bin/sh
# 맥에서: k3s가 쓰는 containerd(k8s.io)에 이미지 둘을 빌드한다.
#   docflow:dev      ~/doc_platform (API + OCR)
#   docflow-web:dev  ~/doc_platform/frontend — 프런트 운영 이미지: 빌드 안에서 next build, 실행은 next start
#                    (frontend/Dockerfile, https://github.com/reddol18/docflow/issues/48). 개발 서버(next dev)는 들어가지 않는다
#   sh build_image.sh [all|api|web]   기본 all. api·web은 하나만 — deploy.sh가 롤아웃 직전 이미지가 없을 때(kubelet 이미지 GC)
#                                     빌드 캐시로 다시 만드는 데 쓴다 (https://github.com/reddol18/docflow/issues/119)
set -e
WHAT=${1:-all}
case "$WHAT" in all|api|web) ;; *) echo "build_image.sh: all|api|web 중 하나 (받은 값: $WHAT)" >&2; exit 1 ;; esac
export PATH="/usr/local/bin:$HOME/.local/bin:$PATH"
cd ~/doc_platform
# 빌드 전 디스크 여유 확인 (반드시, 따로 불러도) — scripts/mac/disk_check.sh
# 2GB 더: 프런트 이미지(node 기반 약 60MB 받기 + npm ci·next build 층, 이미지 약 0.4GB)와 빌드 캐시 몫
sh ~/doc_platform/scripts/disk_check.sh 2 </dev/null
# --pull 없음: Dockerfile FROM이 다이제스트 고정이라(issues/46) 다시 확인해도 같은 이미지다 — 레지스트리 장애에 빌드가 막히지 않게 뺀다.
# 기반 이미지 갱신은 Dockerfile의 다이제스트를 바꿀 때만 들어온다 (태그와 다이제스트를 함께, scripts/mac/image_tags.sh로 확인).
# APT_REFRESH(ISO 주): apt 갱신 층의 캐시를 주 1회 깬다 — 그대로 두면 처음 빌드한 층이 계속 재사용돼
# 그 뒤 나온 Debian 보안 갱신이 들어오지 않는다 (https://github.com/reddol18/docflow/issues/44)
if [ "$WHAT" != web ]; then
colima nerdctl -- --namespace k8s.io build --build-arg "APT_REFRESH=$(date +%G-W%V)" -t docflow:dev . \
    > ~/docflow_build.log 2>&1 </dev/null || { tail -40 ~/docflow_build.log; exit 1; }
tail -5 ~/docflow_build.log
# 패키지가 requirements.lock 그대로인지 (Dockerfile이 설치 뒤 diff, https://github.com/reddol18/docflow/issues/33). 층을 캐시로 쓰면 줄이 안 나온다
# 개수가 숫자인 줄만 (캐시면 로그에 RUN 명령 글자 "$(wc -l < /tmp/want)개 ..."가 찍혀 그것이 잡혔다). 파이프 끝 tail은 늘 성공하므로 빈 값으로 판단
lock_line=$(grep -oE 'lock 일치: 이미지 패키지 [0-9]+개 = requirements\.lock' ~/docflow_build.log | tail -1)
echo "${lock_line:-lock 일치: 설치 층 캐시 사용 (그 층을 만든 빌드에서 확인됨)}"
fi
if [ "$WHAT" != api ]; then
# 프런트: npm ci(package-lock.json 그대로) → next build → 개발 의존성 빼기 → 실행 단계. rewrite 주소(DOCFLOW_URL)는 빌드 때 정해진다
[ -f ~/doc_platform/frontend/Dockerfile ] || { echo "frontend/Dockerfile 없음 — scripts/deploy.ps1이 frontend를 복사한다" >&2; exit 1; }
colima nerdctl -- --namespace k8s.io build -t docflow-web:dev frontend \
    > ~/docflow_web_build.log 2>&1 </dev/null || { tail -40 ~/docflow_web_build.log; exit 1; }
grep -E 'Compiled successfully|next build|Route \(app\)' ~/docflow_web_build.log | tail -3 || true
fi
colima nerdctl -- --namespace k8s.io images </dev/null | grep docflow
