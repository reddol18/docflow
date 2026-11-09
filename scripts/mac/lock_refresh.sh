#!/bin/sh
# 맥에서: requirements.txt를 이미지 플랫폼(linux/amd64, Dockerfile의 python)에서 풀어 pip freeze --all을 남긴다 (https://github.com/reddol18/docflow/issues/33).
# 보통은 Windows에서 scripts/lock_refresh.ps1로 부른다 (복사 + 이 스크립트 + 해시 붙이기). 배포·재시작은 하지 않는다.
# 결과: ~/doc_platform/requirements.freeze.
# 이미지를 만들지 않는다: Dockerfile의 lock-out 단계(freeze 파일 하나)를 --output type=local로 폴더에 꺼낸다 — 지울 태그가 남지 않는다.
# 빌드 캐시(lock-resolve 설치 층, 약 2~3GB)는 남는다. 이 빌드 것만 골라 지우는 안전한 방법이 없어(builder prune은 운영 이미지 캐시까지 지움)
# 그대로 두고 buildkit GC에 맡긴다 — 그만큼을 아래 디스크 확인에서 미리 본다.
set -e
export PATH="/usr/local/bin:/opt/homebrew/bin:$HOME/.local/bin:$PATH"
cd ~/doc_platform
# 빌드 전 디스크 여유 확인 (반드시). 설치 층이 캐시로 더 생긴다 → 4GB 더 — scripts/mac/disk_check.sh
sh ~/doc_platform/scripts/disk_check.sh 4
OUT="$HOME/doc_platform/.lock_out"
trap 'rm -rf "$OUT"' EXIT
rm -rf "$OUT" ~/doc_platform/requirements.freeze
# --platform: 운영 이미지와 같은 linux/amd64로 푼다. APT_REFRESH: build_image.sh와 같은 값이라 기반·apt 층 캐시를 같이 쓴다
colima nerdctl -- --namespace k8s.io build --pull --platform linux/amd64 --build-arg "APT_REFRESH=$(date +%G-W%V)" \
    --target lock-out --output "type=local,dest=$OUT" . \
    > ~/docflow_lock_build.log 2>&1 || { tail -40 ~/docflow_lock_build.log; exit 1; }
[ -s "$OUT/requirements.freeze" ] || { echo "freeze 파일이 없다 ($OUT)" >&2; tail -20 ~/docflow_lock_build.log; exit 1; }
cp "$OUT/requirements.freeze" ~/doc_platform/requirements.freeze
wc -l ~/doc_platform/requirements.freeze
