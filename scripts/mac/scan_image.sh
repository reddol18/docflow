#!/bin/sh
# 맥에서: 배포하는 이미지(k8s/docs.yaml의 image: 전부)의 OS·파이썬 패키지 취약점을 trivy로 본다
# (https://github.com/reddol18/docflow/issues/25). 보고만 한다(종료 코드 0). 결과 요약을 이슈·PR에 붙인다.
#
# Windows에서 (결과는 저장소 밖에 둔다 — 저장소 안에 두면 deploy.ps1이 '커밋 안 된 변경'으로 멈춘다):
#   [Console]::OutputEncoding = [Text.Encoding]::UTF8    # 한글·표 선이 깨지지 않게
#   ssh -o BatchMode=yes user@mac "sh ~/doc_platform/scripts/scan_image.sh" | Out-File -Encoding utf8 $env:TEMP\trivy_image.txt
# 맥에는 deploy.ps1이 scripts/mac/*.sh를 ~/doc_platform/scripts/로 복사한다.
#
# trivy는 처음에 GitHub 릴리스에서 받아 ~/.local/share/docflow-tools 에 두고 같은 릴리스의 체크섬으로 확인한다
# (버전은 scripts/check.ps1의 $V.trivy와 같게). 체크섬이 같은 출처라 전송 손상만 막는다 — 고정 해시·서명 확인은 후속 이슈.
set -e
umask 077
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"  # colima (Intel·Apple Silicon Homebrew)
CURL=/usr/bin/curl; SHASUM=/usr/bin/shasum; TAR_BIN=/usr/bin/tar     # 검증에 쓰는 도구는 시스템 것만
VER=0.75.0
DIR="$HOME/.local/share/docflow-tools/trivy-$VER"
TRIVY="$DIR/trivy"
case "$(uname -m)" in
  x86_64) ASSET="trivy_${VER}_macOS-64bit.tar.gz" ;;
  arm64)  ASSET="trivy_${VER}_macOS-ARM64.tar.gz" ;;
  *) echo "모르는 CPU: $(uname -m)"; exit 1 ;;
esac
if [ ! -x "$TRIVY" ]; then
  mkdir -p "$DIR"
  BASE="https://github.com/aquasecurity/trivy/releases/download/v$VER"
  $CURL -fsSL --proto '=https' --proto-redir '=https' --tlsv1.2 -o "$DIR/$ASSET" "$BASE/$ASSET"
  $CURL -fsSL --proto '=https' --proto-redir '=https' --tlsv1.2 -o "$DIR/sums.txt" "$BASE/trivy_${VER}_checksums.txt"
  WANT=$(grep " $ASSET\$" "$DIR/sums.txt" | cut -d' ' -f1)
  GOT=$($SHASUM -a 256 "$DIR/$ASSET" | cut -d' ' -f1)
  if [ -z "$WANT" ] || [ "$WANT" != "$GOT" ]; then rm -rf "$DIR"; echo "$ASSET 체크섬 불일치"; exit 1; fi
  $TAR_BIN -xzf "$DIR/$ASSET" -C "$DIR" trivy
  rm -f "$DIR/$ASSET" "$DIR/sums.txt"
fi

IMAGES=$(sed -n 's/^[[:space:]]*image:[[:space:]]*\([^[:space:]]*\).*/\1/p' ~/doc_platform/k8s/docs.yaml | sort -u)
[ -n "$IMAGES" ] || { echo "k8s/docs.yaml에서 image:를 찾지 못함"; exit 1; }
TAR=$(mktemp "$HOME/docflow_image.XXXXXX")  # 이미지마다 덮어쓰고, 끝나면 지운다 (가장 큰 것 약 2GB)
trap 'rm -f "$TAR"' EXIT
for IMG in $IMAGES; do
  # ID를 먼저 얻고 그 ID로 내보낸다 → 그 사이에 다시 빌드돼도 보고한 ID = 스캔한 이미지
  ID=$(colima nerdctl -- --namespace k8s.io images --no-trunc --format '{{.ID}}' "$IMG" | head -1)
  [ -n "$ID" ] || { echo "== $IMG: k3s에 없음 (건너뜀)"; continue; }
  colima nerdctl -- --namespace k8s.io save "$ID" > "$TAR"
  echo "== 이미지 $IMG ($ID)"
  "$TRIVY" image --quiet --input "$TAR" --scanners vuln --severity HIGH,CRITICAL --exit-code 0 \
    --format table --cache-dir "$HOME/.cache/trivy"
done
