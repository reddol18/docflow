#!/bin/sh
# Docker Hub에서 최근 태그를 보고, 고정한 이미지(Dockerfile FROM, k8s/docs.yaml image:의 '태그@sha256:')의
# 태그가 지금 레지스트리에서 가리키는 다이제스트를 고정값과 맞대 본다 (https://github.com/reddol18/docflow/issues/46).
# 판을 올릴 때: 태그와 다이제스트를 함께 바꾼다 — 태그만 바꾸면 다이제스트가 옛 이미지를 계속 가리킨다(아래 '다름'으로 보인다).
# 읽기만 한다 (매니페스트 HEAD, 이미지를 받지 않음). 맥이나 저장소 폴더 어디서든: sh scripts/mac/image_tags.sh
# 종료 코드 1: 고정값과 다른 것이 있거나 확인하지 못함.
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd); [ -f "$ROOT/Dockerfile" ] || ROOT=$(cd "$HERE/../.." && pwd)  # 맥 배치 / 저장소
CURL="/usr/bin/curl -fsS --proto =https --tlsv1.2"

echo "-- 최근 태그"
$CURL "https://hub.docker.com/v2/repositories/qdrant/qdrant/tags?page_size=40&ordering=last_updated" | grep -o '"name":"v[0-9.]*"' | head -6
$CURL "https://hub.docker.com/v2/repositories/library/postgres/tags?page_size=60&name=17" | grep -o '"name":"17\.[0-9]*-alpine"' | head -4
$CURL "https://hub.docker.com/v2/repositories/library/python/tags?page_size=60&name=3.11" | grep -o '"name":"3\.11\.[0-9]*-slim-bookworm"' | head -4

echo "-- 고정한 태그의 지금 다이제스트"
PINNED=$( { sed -n 's/^FROM[[:space:]]*\([^[:space:]]*@sha256:[0-9a-f]*\).*/\1/p' "$ROOT/Dockerfile"
            sed -n 's/^[[:space:]]*image:[[:space:]]*\([^[:space:]]*@sha256:[0-9a-f]*\).*/\1/p' "$ROOT/k8s/docs.yaml"; } | sort -u)
rc=0
for IMG in $PINNED; do
  NAME=${IMG%@*}; WANT=${IMG#*@}
  REPO=${NAME%:*}; TAG=${NAME##*:}
  case "$REPO" in */*/*|*.*/*|*:*/*) echo "$IMG: Docker Hub가 아닌 레지스트리는 보지 않음"; rc=1; continue ;; esac
  case "$REPO" in */*) ;; *) REPO="library/$REPO" ;; esac
  TOKEN=$($CURL "https://auth.docker.io/token?service=registry.docker.io&scope=repository:$REPO:pull" | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')
  GOT=$($CURL -I -H "Authorization: Bearer $TOKEN" \
    -H "Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json" \
    "https://registry-1.docker.io/v2/$REPO/manifests/$TAG" | tr -d '\r' | awk 'tolower($1) == "docker-content-digest:" { print $2 }')
  if [ -z "$GOT" ]; then echo "$NAME: 확인 못 함"; rc=1
  elif [ "$GOT" = "$WANT" ]; then echo "$NAME: 같음 ($WANT)"
  else echo "$NAME: **다름** — 고정 $WANT, 레지스트리 $GOT (태그가 다시 밀렸거나 태그만 바꿈)"; rc=1
  fi
done
exit $rc
