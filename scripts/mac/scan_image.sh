#!/bin/sh
# 맥에서: docflow:dev 이미지의 OS 패키지·파이썬 패키지 취약점을 trivy로 본다 (https://github.com/reddol18/docflow/issues/25).
# Windows에서:  ssh user@mac "sh ~/doc_platform/scripts/scan_image.sh" > scratch/trivy_image.txt
# 보고만 한다(종료 코드 0). 결과 요약을 이슈·PR에 붙인다.
# trivy는 처음에 받아 ~/.local/share/docflow-tools 에 두고 체크섬을 확인한다 (버전은 scripts/check.ps1과 같게).
set -e
export PATH="/usr/local/bin:$HOME/.local/bin:$PATH"
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
  curl -fsSL -o "$DIR/$ASSET" "$BASE/$ASSET"
  curl -fsSL -o "$DIR/sums.txt" "$BASE/trivy_${VER}_checksums.txt"
  WANT=$(grep " $ASSET\$" "$DIR/sums.txt" | cut -d' ' -f1)
  GOT=$(shasum -a 256 "$DIR/$ASSET" | cut -d' ' -f1)
  if [ -z "$WANT" ] || [ "$WANT" != "$GOT" ]; then rm -rf "$DIR"; echo "$ASSET 체크섬 불일치"; exit 1; fi
  tar -xzf "$DIR/$ASSET" -C "$DIR" trivy
fi

TAR="$HOME/docflow_image.tar"
trap 'rm -f "$TAR"' EXIT
colima nerdctl -- --namespace k8s.io save docflow:dev > "$TAR"  # VM 안 이미지를 맥으로 (약 2GB, 끝나면 지운다)
ID=$(colima nerdctl -- --namespace k8s.io images --format '{{.ID}} {{.CreatedSince}}' docflow:dev | head -1)
echo "== 이미지 docflow:dev ($ID)"
"$TRIVY" image --quiet --input "$TAR" --scanners vuln --severity HIGH,CRITICAL --exit-code 0 \
  --format table --cache-dir "$HOME/.cache/trivy"
