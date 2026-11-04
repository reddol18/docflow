#!/bin/sh
# 맥에서: ~/doc_platform 을 k3s가 쓰는 containerd(k8s.io)에 docflow:dev 이미지로 빌드한다.
set -e
export PATH="/usr/local/bin:$HOME/.local/bin:$PATH"
cd ~/doc_platform
# --pull: 기반 이미지 태그를 다시 받아 그 사이 나온 갱신을 쓴다.
# APT_REFRESH(ISO 주): apt 갱신 층의 캐시를 주 1회 깬다 — 그대로 두면 처음 빌드한 층이 계속 재사용돼
# 그 뒤 나온 Debian 보안 갱신이 들어오지 않는다 (https://github.com/reddol18/docflow/issues/44)
colima nerdctl -- --namespace k8s.io build --pull --build-arg "APT_REFRESH=$(date +%G-W%V)" -t docflow:dev . \
    > ~/docflow_build.log 2>&1 || { tail -40 ~/docflow_build.log; exit 1; }
tail -5 ~/docflow_build.log
colima nerdctl -- --namespace k8s.io images | grep docflow
