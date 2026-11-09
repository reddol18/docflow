#!/bin/sh
# 맥에서: requirements.txt를 이미지 플랫폼(linux/amd64, Dockerfile의 python)에서 풀어 pip freeze --all을 남긴다 (https://github.com/reddol18/docflow/issues/33).
# 보통은 Windows에서 scripts/lock_refresh.ps1로 부른다 (복사 + 이 스크립트 + 해시 붙이기). 배포·재시작은 하지 않는다.
# 결과: ~/doc_platform/requirements.freeze. Dockerfile의 lock-resolve 단계만 빌드하고, 잠깐 만든 이미지는 지운다.
set -e
export PATH="/usr/local/bin:$HOME/.local/bin:$PATH"
cd ~/doc_platform
# 빌드 전 디스크 여유 확인 (반드시). 패키지 설치 층(약 2~3GB)이 더 생긴다 → 4GB 더 — scripts/mac/disk_check.sh
sh ~/doc_platform/scripts/disk_check.sh 4
TAG=docflow:lock-refresh
# APT_REFRESH: build_image.sh와 같은 값이라 기반·apt 층 캐시를 같이 쓴다
colima nerdctl -- --namespace k8s.io build --pull --build-arg "APT_REFRESH=$(date +%G-W%V)" --target lock-resolve -t "$TAG" . \
    > ~/docflow_lock_build.log 2>&1 || { tail -40 ~/docflow_lock_build.log; exit 1; }
colima nerdctl -- --namespace k8s.io run --rm "$TAG" cat /requirements.freeze > ~/doc_platform/requirements.freeze
colima nerdctl -- --namespace k8s.io rmi "$TAG" >/dev/null
wc -l ~/doc_platform/requirements.freeze
