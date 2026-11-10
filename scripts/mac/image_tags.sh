#!/bin/sh
# Docker Hub에서 최근 태그를 보고, 고정한 이미지(Dockerfile FROM, k8s/docs.yaml image:의 '태그@sha256:')의
# 태그가 지금 레지스트리에서 가리키는 다이제스트를 고정값과 맞대 본다 (https://github.com/reddol18/docflow/issues/46).
# 판을 올릴 때: 태그와 다이제스트를 함께 바꾼다 — 태그만 바꾸면 다이제스트가 옛 이미지를 계속 가리킨다(아래 '다름'으로 보인다).
# 읽기만 한다 (매니페스트 HEAD, 이미지를 받지 않음). 맥이나 저장소 폴더 어디서든: sh scripts/mac/image_tags.sh
# 종료 코드 1: 고정값과 다른 것이 있거나 확인하지 못함.
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd); [ -f "$ROOT/Dockerfile" ] || ROOT=$(cd "$HERE/../.." && pwd)  # 맥 배치 / 저장소
CURL="/usr/bin/curl -fsS --proto =https --tlsv1.2"

rc=0
# 태그 목록: 받기 실패면 '확인 못 함' (빈 목록을 '새 태그 없음'으로 읽지 않게)
tags() {  # 저장소 질의 grep식 개수
  T=$($CURL "https://hub.docker.com/v2/repositories/$1/tags?$2") || { echo "$1: 태그 확인 못 함"; rc=1; return; }
  printf '%s' "$T" | grep -o "$3" | head -"$4"
}
echo "-- 최근 태그"
tags qdrant/qdrant "page_size=40&ordering=last_updated" '"name":"v[0-9.]*"' 6
tags library/postgres "page_size=60&name=17" '"name":"17\.[0-9]*-alpine"' 4
tags library/python "page_size=60&name=3.11" '"name":"3\.11\.[0-9]*-slim-bookworm"' 4
tags library/node "page_size=60&name=-alpine3&ordering=last_updated" '"name":"24\.[0-9.]*-alpine3\.[0-9]*"' 4  # 프런트 (frontend/Dockerfile, issues/48)

echo "-- 고정한 태그의 지금 다이제스트"
PINNED=$( { sed -n 's/^FROM[[:space:]]*\([^[:space:]]*@sha256:[0-9a-f]*\).*/\1/p' "$ROOT/Dockerfile" "$ROOT/frontend/Dockerfile"
            sed -n 's/^[[:space:]]*image:[[:space:]]*\([^[:space:]]*@sha256:[0-9a-f]*\).*/\1/p' "$ROOT/k8s/docs.yaml"; } | sort -u)
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
