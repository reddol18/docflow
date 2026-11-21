#!/bin/bash
# 폐쇄망 설치 묶음 만들기 (https://github.com/reddol18/docflow/issues/81). 인터넷이 되는 리눅스(이 PC는 WSL 배포판 docflow81)에서
# root로 돌린다. 필요한 것: docker + buildx(앱 이미지 빌드), k3s(같은 판 — 레지스트리 이미지를 다이제스트 그대로 받아 내보낸다), git, zstd, jq, python3.
#
#   sudo bash scripts/offline/build_bundle.sh <저장소 폴더> <Ollama 모델 폴더(읽기만)> <묶음 만들 폴더>
#   예) bash scripts/offline/build_bundle.sh /mnt/e/claude-dev/base/records_poc/doc_platform /mnt/e/dev/models /mnt/f/docflow81/bundle
#   sudo bash scripts/offline/build_bundle.sh --refresh <저장소> <있는 묶음 폴더>   스크립트·매니페스트만 지금 커밋으로 (아래 설명)
#
# 묶음 = <묶음 폴더>/docflow-offline-<커밋>/ (설치 절차: docs/offline_install.md)
#   k3s/       k3s 바이너리·airgap 이미지·설치 스크립트 (K3S_VERSION, 내려받은 sha256sum 파일로 확인)
#   images/    앱 이미지 docflow:dev·docflow-web:dev(이 커밋을 git archive로 꺼내 빌드) + 레지스트리 이미지
#              postgres·qdrant·redis·ollama — k8s 매니페스트의 다이제스트 그대로(k3s ctr pull → export, 다이제스트가 바뀌지 않게)
#   models/    Ollama 모델 3개(qwen2.5:7b·qwen2.5vl:7b·bge-m3) manifest + blob. 모델 폴더는 읽기만 한다
#   debs/      설치·증거 도구 (iptables·conntrack·tcpdump와 그 의존 .deb) — 설치할 곳에 없을 때만 dpkg로 넣는다
#   manifests/ docflow-offline.yaml(k8s/offline 덧씌움을 푼 것)·Corefile
#   scripts/   install.sh·egress_guard.sh·evidence.sh·smoke_test.sh (이 폴더 그대로)
#   CONTENTS.txt(내용·크기), SHA256SUMS(설치할 때 sha256sum -c)
#   끝에 묶음 tar를 trivy로 스캔한다 (scripts/offline/scan_bundle.sh, 결과는 묶음 밖)
# 디스크: 시작 전에 묶음 폴더와 / 여유를 확인하고 모자라면 멈춘다 (records_poc/CLAUDE.md '디스크 여유 확인').
set -euo pipefail
REPO=${1:?저장소 폴더}
MODELS=${2:?Ollama 모델 폴더}
OUTROOT=${3:?묶음 만들 폴더}
K3S_VERSION=${K3S_VERSION:-v1.37.2+k3s1}
OLLAMA_MODELS_WANT=${OLLAMA_MODELS_WANT:-"library/qwen2.5:7b library/qwen2.5vl:7b library/bge-m3:latest"}
NEED_OUT_GB=${NEED_OUT_GB:-30}   # 묶음 약 20GB + 여유
NEED_ROOT_GB=${NEED_ROOT_GB:-25} # docker 빌드 층·k3s containerd 내용·임시 파일
DEB_PKGS=${DEB_PKGS:-"iptables conntrack tcpdump"}
# k3s 설치 스크립트: get.k3s.io(받을 때마다 바뀜)가 아니라 그 판의 태그에 있는 install.sh를 받아 고정 해시와 견준다.
# K3S_VERSION을 바꾸면 이 해시도 함께 바꾼다 (raw.githubusercontent.com/k3s-io/k3s/<판>/install.sh를 받아 sha256)
K3S_INSTALL_SHA256_DEFAULT_FOR="v1.37.2+k3s1"
K3S_INSTALL_SHA256=${K3S_INSTALL_SHA256:-$([ "$K3S_VERSION" = "$K3S_INSTALL_SHA256_DEFAULT_FOR" ] && echo ed01f89fd977bf20ac1516bbebf8370bf3ddbaa55dac8aba610956a4c78cc00b || true)}

say() { printf '[build %s] %s\n' "$(date +%H:%M:%S)" "$*"; }
free_gb() { df -P -BG "$1" | awk 'NR==2 {gsub("G","",$4); print $4}'; }
need() { local d=$1 want=$2 have; have=$(free_gb "$d"); say "디스크 $d 여유 ${have}GB (필요 ${want}GB)";
         [ "$have" -ge "$want" ] || { echo "디스크 모자람: $d 여유 ${have}GB < ${want}GB — 멈춤" >&2; exit 1; }; }

put_scripts() { # 소스 폴더 → 묶음: 풀어 둔 매니페스트·Corefile·스크립트·설치 문서
  k3s kubectl kustomize --load-restrictor LoadRestrictionsNone "$1/k8s/offline" > "$2/manifests/docflow-offline.yaml"
  tr -d '\r' < "$1/k8s/offline/Corefile" > "$2/manifests/Corefile"
  for s in install.sh egress_guard.sh evidence.sh smoke_test.sh; do tr -d '\r' < "$1/scripts/offline/$s" > "$2/scripts/$s"; chmod +x "$2/scripts/$s"; done
  cp "$1/docs/offline_install.md" "$2/"
}
write_index() { # 묶음 폴더, 머리 글(표준 입력) → CONTENTS.txt·SHA256SUMS
  # 목록은 묶음 밖 임시 파일에 만든 뒤 옮긴다 (묶음 안에 만들면 그 임시 파일 자신이 목록·합계에 들어간다)
  local tmp; tmp=$(mktemp)
  { cat; echo; echo "파일 (바이트):"
    (cd "$1" && find . -type f ! -name SHA256SUMS ! -name CONTENTS.txt -printf '%s\t%P\n' | sort -k2)
    echo; echo "합계(CONTENTS.txt·SHA256SUMS 빼고): $(cd "$1" && find . -type f ! -name SHA256SUMS ! -name CONTENTS.txt -printf '%s\n' | awk '{s+=$1} END {print s}') 바이트"; } > "$tmp"
  mv "$tmp" "$1/CONTENTS.txt"; chmod 0644 "$1/CONTENTS.txt"
  (cd "$1" && find . -type f ! -name SHA256SUMS -printf '%P\n' | sort | xargs -d '\n' sha256sum > SHA256SUMS)
}

[ "$(id -u)" = 0 ] || { echo "root로 돌린다" >&2; exit 1; }

# --refresh <저장소> <있는 묶음 폴더>: 스크립트·매니페스트·문서만 지금 커밋으로 바꾸고 해시를 다시 만든다 (이미지·모델은 그대로).
# 묶음을 만든 커밋과 지금 커밋 사이에 이미지에 드는 파일(app·Dockerfile·frontend·requirements·이미지 다이제스트가 든 매니페스트)이
# 바뀌었으면 거절한다 — 그때는 처음부터 다시 만든다. 폴더 이름은 docflow-offline-<지금 커밋>으로 바뀐다
if [ "$REPO" = --refresh ]; then
  REPO=$MODELS; OLD_B=${OUTROOT%/}
  OLD=$(basename "$OLD_B" | sed 's/^docflow-offline-//')
  NEW=$(git -C "$REPO" rev-parse --short=12 HEAD)
  git -C "$REPO" diff --quiet "$OLD" HEAD -- app Dockerfile .dockerignore frontend requirements.txt requirements.lock k8s/docs.yaml k8s/offline/llm.yaml \
    || { echo "커밋 $OLD → $NEW 사이에 이미지에 드는 파일이 바뀜 — --refresh 말고 처음부터 다시 만든다" >&2; exit 1; }
  SRC=$(mktemp -d /root/docflow-src.XXXXXX); trap 'rm -rf "$SRC"' EXIT
  git -C "$REPO" archive --format=tar HEAD | tar -x -C "$SRC"
  put_scripts "$SRC" "$OLD_B"
  NB="$(dirname "$OLD_B")/docflow-offline-$NEW"
  [ "$OLD_B" = "$NB" ] || mv "$OLD_B" "$NB"
  { sed -n '1,/^파일 (바이트):/p' "$NB/CONTENTS.txt" | sed '$d' | sed '/^$/d' | grep -v '^스크립트·매니페스트:'
    echo "스크립트·매니페스트: 커밋 $NEW (이미지·모델은 커밋 $OLD — 그 사이 이미지에 드는 파일 변경 없음, $(date -Iseconds) 갱신)"; } > "$SRC/head.txt"
  write_index "$NB" < "$SRC/head.txt"
  say "갱신 끝: $NB"; exit 0
fi
for c in docker k3s git zstd jq python3 sha256sum; do command -v $c >/dev/null || { echo "$c 없음" >&2; exit 1; }; done
mkdir -p "$OUTROOT"
need "$OUTROOT" "$NEED_OUT_GB"
need / "$NEED_ROOT_GB"

COMMIT=$(git -C "$REPO" rev-parse --short=12 HEAD)
[ -z "$(git -C "$REPO" status --porcelain --untracked-files=no)" ] || say "주의: 커밋 안 된 변경이 있다 — 묶음은 커밋 $COMMIT 그대로(git archive)"
B="$OUTROOT/docflow-offline-$COMMIT"
[ ! -e "$B" ] || { echo "$B 이미 있음 — 지우고 다시" >&2; exit 1; }
mkdir -p "$B"/{k3s,images,models,debs,manifests,scripts}
SRC=$(mktemp -d /root/docflow-src.XXXXXX)
trap 'rm -rf "$SRC"' EXIT
git -C "$REPO" archive --format=tar HEAD | tar -x -C "$SRC"
say "소스: 커밋 $COMMIT → $SRC"

# ---- k3s (airgap) ----
say "k3s $K3S_VERSION 받기"
u="https://github.com/k3s-io/k3s/releases/download/${K3S_VERSION//+/%2B}"
for f in k3s k3s-airgap-images-amd64.tar.zst sha256sum-amd64.txt; do curl -fsSL -o "$B/k3s/$f" "$u/$f"; done
( cd "$B/k3s" && grep -E ' (k3s|k3s-airgap-images-amd64\.tar\.zst)$' sha256sum-amd64.txt | sha256sum -c - )
[ -n "$K3S_INSTALL_SHA256" ] || { echo "K3S_VERSION=$K3S_VERSION의 install.sh 해시(K3S_INSTALL_SHA256)를 준다" >&2; exit 1; }
curl -fsSL -o "$B/k3s/install.sh" "https://raw.githubusercontent.com/k3s-io/k3s/${K3S_VERSION//+/%2B}/install.sh"
echo "$K3S_INSTALL_SHA256  $B/k3s/install.sh" | sha256sum -c - >/dev/null \
  || { echo "k3s install.sh 해시가 고정값과 다름 — 멈춤" >&2; exit 1; }
say "k3s install.sh: 태그 $K3S_VERSION, sha256 일치"
chmod +x "$B/k3s/k3s" "$B/k3s/install.sh"

# ---- 앱 이미지 (docker 빌드 → save) ----
# BuildKit(docker buildx) 필요 — 예전 빌더는 쓰지 않는 단계(lock-resolve)까지 빌드한다. 빌드 기록은 /root/docflow-build-*.log
docker buildx version >/dev/null 2>&1 || { echo "docker buildx 없음 (apt install docker-buildx)" >&2; exit 1; }
say "docflow:dev 빌드 (/root/docflow-build-api.log)"
DOCKER_BUILDKIT=1 docker build --progress=plain --build-arg "APT_REFRESH=$(date +%G-W%V)" -t docflow:dev "$SRC" \
  > /root/docflow-build-api.log 2>&1 || { tail -40 /root/docflow-build-api.log; exit 1; }
grep -oE 'lock 일치: 이미지 패키지 [0-9]+개 = requirements\.lock' /root/docflow-build-api.log | tail -1 || true
say "docflow-web:dev 빌드 (/root/docflow-build-web.log)"
DOCKER_BUILDKIT=1 docker build --progress=plain -t docflow-web:dev "$SRC/frontend" \
  > /root/docflow-build-web.log 2>&1 || { tail -40 /root/docflow-build-web.log; exit 1; }
for img in docflow:dev docflow-web:dev; do
  n=${img%%:*}
  docker save "$img" | zstd -q -T0 -10 -o "$B/images/$n.tar.zst"
done

# ---- 레지스트리 이미지 (매니페스트의 다이제스트 그대로) ----
mapfile -t REFS < <(grep -hoE 'image: [^ ]+@sha256:[0-9a-f]{64}' "$SRC/k8s/docs.yaml" "$SRC/k8s/offline/llm.yaml" | awk '{print $2}' | sort -u)
for ref in "${REFS[@]}"; do
  full=$ref; case "$ref" in */*) full="docker.io/$ref" ;; *) full="docker.io/library/$ref" ;; esac
  name=$(basename "${ref%%@*}" | tr ':' '_')
  say "받기 $full"
  k3s ctr -n k8s.io images pull --platform linux/amd64 "$full" >/dev/null
  k3s ctr -n k8s.io images export --platform linux/amd64 - "$full" | zstd -q -T0 -10 -o "$B/images/$name.tar.zst"
done

# ---- Ollama 모델 (읽기만) ----
say "모델: $OLLAMA_MODELS_WANT"
LIST=$(mktemp)
for m in $OLLAMA_MODELS_WANT; do
  mf="manifests/registry.ollama.ai/${m%%:*}/${m##*:}"
  [ -f "$MODELS/$mf" ] || { echo "모델 manifest 없음: $MODELS/$mf" >&2; exit 1; }
  echo "$mf" >> "$LIST"
  jq -r '.config.digest, .layers[].digest' "$MODELS/$mf" | sed 's/^sha256:/blobs\/sha256-/' >> "$LIST"
done
sort -u "$LIST" -o "$LIST"
# blob 이름이 곧 sha256 — 묶기 전에 확인한다 (깨진 파일을 넣지 않게)
grep '^blobs/' "$LIST" | while read -r b; do
  want=${b#blobs/sha256-}; have=$(sha256sum "$MODELS/$b" | cut -d' ' -f1)
  [ "$want" = "$have" ] || { echo "blob 해시 다름: $b" >&2; exit 1; }
done
tar -C "$MODELS" -cf "$B/models/ollama-models.tar" -T "$LIST"
cp "$LIST" "$B/models/files.txt"
rm -f "$LIST"

# ---- 도구 .deb ----
say "도구 .deb: $DEB_PKGS (+ 의존)"
apt-get update -qq
mapfile -t DEBS < <(apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts --no-breaks --no-replaces --no-enhances $DEB_PKGS \
  | grep -E '^[a-z0-9]' | sort -u)
( cd "$B/debs" && for p in "${DEBS[@]}"; do apt-get download "$p" >/dev/null 2>&1 || say "  .deb 없음(가상 꾸러미?): $p"; done )

# ---- 매니페스트·스크립트 ----
put_scripts "$SRC" "$B"

# ---- 목록·해시 ----
{
  echo "docflow 폐쇄망 설치 묶음 — 커밋 $COMMIT, 만든 때 $(date -Iseconds), k3s $K3S_VERSION"
  echo "모델: $OLLAMA_MODELS_WANT"
  echo "레지스트리 이미지:"; printf '  %s\n' "${REFS[@]}"
  echo "앱 이미지: docflow:dev $(docker image inspect -f '{{.Id}}' docflow:dev), docflow-web:dev $(docker image inspect -f '{{.Id}}' docflow-web:dev)"
} | write_index "$B"
say "끝: $B ($(du -sh "$B" | cut -f1))"

# ---- 이미지 취약점 스캔: 묶음 안 tar 그대로 (scripts/offline/scan_bundle.sh) ----
# 레지스트리에서 태그로 다시 받지 않는다 — 묶음에 든 것(앱 이미지·레지스트리 이미지·k3s airgap 이미지)을 trivy --input으로 본다.
# 결과는 묶음 밖: /root/docflow-trivy-<커밋>/(JSON), <묶음 폴더>/../trivy-<커밋>.tsv(요약). HIGH·CRITICAL이 있어도 멈추지 않는다
say "trivy 스캔 (묶음 tar)"
if bash "$SRC/scripts/offline/scan_bundle.sh" "$B" "$SRC" "/root/docflow-trivy-$COMMIT" > /root/docflow-build-trivy.log 2>&1; then
  cp "/root/docflow-trivy-$COMMIT/summary.tsv" "$OUTROOT/trivy-$COMMIT.tsv"; cat "$OUTROOT/trivy-$COMMIT.tsv"
else
  say "!! trivy 스캔 실패 — /root/docflow-build-trivy.log (묶음은 만들어졌다)"
fi
