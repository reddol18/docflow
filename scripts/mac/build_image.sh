#!/bin/sh
# 맥에서: ~/doc_platform 을 k3s가 쓰는 containerd(k8s.io)에 docflow:dev 이미지로 빌드한다.
set -e
export PATH="/usr/local/bin:$HOME/.local/bin:$PATH"
cd ~/doc_platform
colima nerdctl -- --namespace k8s.io build -t docflow:dev . > ~/docflow_build.log 2>&1 || { tail -40 ~/docflow_build.log; exit 1; }
tail -5 ~/docflow_build.log
colima nerdctl -- --namespace k8s.io images | grep docflow
