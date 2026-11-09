#!/bin/sh
# 맥에서: 배포하는 이미지(k8s/docs.yaml의 image: 전부)의 OS·파이썬 패키지 취약점을 trivy로 본다
# (https://github.com/reddol18/docflow/issues/25). 보고만 한다 — 취약점이 있어도 종료 코드 0.
#   sh scan_image.sh             이미지별 표 + 요약 (결과를 이슈·PR에 붙인다)
#   sh scan_image.sh --summary   이미지별 HIGH·CRITICAL 요약 한 줄만 (deploy.sh가 빌드·재시작 뒤 부른다, issues/46)
# 종료 코드 1은 도구 문제일 때만: trivy 해시 불일치·받기 실패, 디스크 여유 부족, 매니페스트에 image: 없음.
#
# Windows에서 (결과는 저장소 밖에 둔다 — 저장소 안에 두면 deploy.ps1이 '커밋 안 된 변경'으로 멈춘다):
#   [Console]::OutputEncoding = [Text.Encoding]::UTF8    # 한글·표 선이 깨지지 않게
#   ssh -o BatchMode=yes user@mac "sh ~/doc_platform/scripts/scan_image.sh" | Out-File -Encoding utf8 $env:TEMP\trivy_image.txt
# 맥에는 deploy.ps1이 scripts/mac/*.sh와 scripts/tool_pins.txt를 ~/doc_platform/scripts/로 복사한다.
#
# trivy 버전·해시는 scripts/tool_pins.txt 한 곳에 고정한다 (https://github.com/reddol18/docflow/issues/46).
# 처음에 GitHub 릴리스에서 받아 ~/.local/share/docflow-tools 에 두고 고정한 자산 해시로 확인한다(릴리스 체크섬 파일은 쓰지 않음).
# 받아 둔 실행 파일도 부를 때마다 고정한 실행 파일 해시로 확인하고, 다르면 실행하지 않고 멈춘다.
# 시험용으로 바꿀 수 있는 곳: DOCFLOW_TOOL_PINS(고정 파일), DOCFLOW_TOOLS_DIR(받아 두는 곳)
#
# image:가 '태그@sha256:…'(다이제스트 고정)이면 그 다이제스트로 찾고, 보고한 ID가 그 다이제스트다.
set -e
umask 077
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"  # colima (Intel·Apple Silicon Homebrew)
CURL=/usr/bin/curl; SHASUM=/usr/bin/shasum; TAR_BIN=/usr/bin/tar     # 검증에 쓰는 도구는 시스템 것만
HERE=$(cd "$(dirname "$0")" && pwd)
PINS=${DOCFLOW_TOOL_PINS:-$HERE/tool_pins.txt}
TOOLS=${DOCFLOW_TOOLS_DIR:-$HOME/.local/share/docflow-tools}
SUMMARY=0
case "${1:-}" in
  --summary) SUMMARY=1 ;;
  "") ;;
  *) echo "쓰는 법: sh scan_image.sh [--summary]"; exit 1 ;;
esac

case "$(uname -m)" in
  x86_64) PLAT=darwin-x86_64 ;;
  arm64)  PLAT=darwin-arm64 ;;
  *) echo "모르는 CPU: $(uname -m)"; exit 1 ;;
esac
[ -f "$PINS" ] || { echo "고정 파일 없음: $PINS"; exit 1; }
# 이름 버전 플랫폼 저장소 자산 자산_sha256 실행파일 실행파일_sha256 — trivy·이 플랫폼 줄이 정확히 하나
PIN=$(tr -d '\r' < "$PINS" | awk -v p="$PLAT" '$1 == "trivy" && $3 == p')
[ "$(printf '%s\n' "$PIN" | grep -c .)" = 1 ] || { echo "$PINS 에 trivy $PLAT 줄이 하나여야 함"; exit 1; }
set -- $PIN
[ $# = 8 ] || { echo "$PINS trivy $PLAT 줄이 8칸이 아님"; exit 1; }
VER=$2; REPO=$4; ASSET=$5; ASSET_SHA=$6; EXE=$7; EXE_SHA=$8
DIR="$TOOLS/trivy-$VER"
TRIVY="$DIR/$EXE"
sha() { $SHASUM -a 256 "$1" | cut -d' ' -f1; }

if [ ! -f "$TRIVY" ]; then
  mkdir -p "$DIR"
  $CURL -fsSL --proto '=https' --proto-redir '=https' --tlsv1.2 -o "$DIR/$ASSET" \
    "https://github.com/$REPO/releases/download/v$VER/$ASSET"
  GOT=$(sha "$DIR/$ASSET")
  if [ "$GOT" != "$ASSET_SHA" ]; then
    rm -rf "$DIR"; echo "$ASSET 해시가 tool_pins.txt와 다름 (받은 $GOT) — 받지 않고 멈춤"; exit 1
  fi
  $TAR_BIN -xzf "$DIR/$ASSET" -C "$DIR" "$EXE"
  rm -f "$DIR/$ASSET"
fi
# 받아 둔 실행 파일도 매번 (바뀌었으면 지우지 않고 멈춘다 — 무엇이 바뀌었는지 볼 수 있게)
GOT=$(sha "$TRIVY")
if [ "$GOT" != "$EXE_SHA" ]; then
  echo "$TRIVY 해시가 tool_pins.txt와 다름 ($GOT) — 실행하지 않음. 확인 뒤 $DIR 를 지우면 다시 받는다"; exit 1
fi

IMAGES=$(sed -n 's/^[[:space:]]*image:[[:space:]]*\([^[:space:]]*\).*/\1/p' "$HERE/../k8s/docs.yaml" | sort -u)  # 맥: ~/doc_platform/k8s
[ -n "$IMAGES" ] || { echo "k8s/docs.yaml에서 image:를 찾지 못함"; exit 1; }
# 이미지 tar(가장 큰 것 약 2GB)를 맥 디스크에 쓰므로 먼저 여유 확인 (반드시) — scripts/mac/disk_check.sh
sh "$HERE/disk_check.sh" </dev/null
TAR=$(mktemp "$HOME/docflow_image.XXXXXX")  # 이미지마다 덮어쓰고, 끝나면 지운다
REP=$(mktemp "$HOME/docflow_trivy.XXXXXX")  # trivy JSON 보고서 (한 번 스캔해 표·요약을 만든다)
trap 'rm -f "$TAR" "$REP"' EXIT
# 요약: 발견(패키지×취약점) 수, 괄호 안은 고친 판이 있는 것
TPL='{{- range . }}{{- range .Vulnerabilities }}{{ .Severity }} {{ if .FixedVersion }}fix{{ else }}nofix{{ end }}
{{ end }}{{- end }}'
for IMG in $IMAGES; do
  case "$IMG" in
    *@sha256:*) REF="${IMG%@*}"; REF="${REF%:*}@${IMG#*@}" ;;  # repo:tag@d → repo@d (nerdctl은 태그+다이제스트 이름을 못 찾는다)
    *) REF="$IMG" ;;
  esac
  # ID를 먼저 얻고 그 ID로 내보낸다 → 그 사이에 다시 빌드돼도 보고한 ID = 스캔한 이미지
  ID=$(colima nerdctl -- --namespace k8s.io images --no-trunc --format '{{.ID}}' "$REF" </dev/null | head -1)
  [ -n "$ID" ] || { echo "== $IMG: k3s에 없음 (건너뜀)"; continue; }
  colima nerdctl -- --namespace k8s.io save "$ID" </dev/null > "$TAR"
  if ! "$TRIVY" image --quiet --input "$TAR" --scanners vuln --severity HIGH,CRITICAL --exit-code 0 \
      --format json --output "$REP" --cache-dir "$HOME/.cache/trivy" </dev/null; then
    echo "== $IMG ($ID): 스캔 실패"; continue
  fi
  COUNTS=$("$TRIVY" convert --quiet --format template --template "$TPL" "$REP" </dev/null | awk '
    NF { n[$1]++; if ($2 == "fix") f[$1]++ }
    END { printf "HIGH %d (고친 판 %d), CRITICAL %d (고친 판 %d)", n["HIGH"], f["HIGH"], n["CRITICAL"], f["CRITICAL"] }')
  echo "== 이미지 $IMG ($ID): $COUNTS"
  [ "$SUMMARY" = 1 ] || "$TRIVY" convert --quiet --format table "$REP" </dev/null
done
