#!/bin/sh
# 맥에서: 배포하는 이미지(k8s/docs.yaml·k8s/keycloak.yaml의 image: 전부)의 OS·파이썬 패키지 취약점을 trivy로 본다
# (https://github.com/reddol18/docflow/issues/25). 보고만 한다 — 취약점이 있어도 그것으로 실패하지 않는다.
#   sh scan_image.sh             이미지별 요약 + 표 (결과를 이슈·PR에 붙인다)
#   sh scan_image.sh --summary   이미지별 HIGH·CRITICAL 요약 한 줄만 (deploy.sh가 롤아웃 뒤 부른다, issues/46)
# 종료 코드
#   0  모든 이미지 요약을 냈다 (취약점 수와 상관없이)
#   1  일반 실패: 받기 실패, 디스크 여유 부족, 매니페스트에 image: 없음, trivy DB 갱신 실패(있는 DB로 요약은 냄),
#      이미지 하나라도 목록·내보내기·스캔·요약·표 실패 또는 k3s에 없음, 끊김(INT·TERM), 그 밖 예상 못 한 실패
#      (이미지 하나가 실패해도 '== 이미지 …: … 실패'를 찍고 다음 이미지로 간다). 요약 줄마다 DB 갱신 시각을 붙인다
#   2  검사 도구 무결성 실패 전용: trivy 자산·실행 파일 해시가 고정값과 다름, 고정 파일이 없거나 형식이 틀림 — 아무것도 실행하지 않는다
#
# Windows에서 (결과는 저장소 밖에 둔다 — 저장소 안에 두면 deploy.ps1이 '커밋 안 된 변경'으로 멈춘다):
#   [Console]::OutputEncoding = [Text.Encoding]::UTF8    # 한글·표 선이 깨지지 않게
#   ssh -o BatchMode=yes user@mac "sh ~/doc_platform/scripts/scan_image.sh" | Out-File -Encoding utf8 $env:TEMP\trivy_image.txt
# 맥에는 deploy.ps1이 scripts/mac/*.sh와 scripts/tool_pins.txt를 ~/doc_platform/scripts/로 복사한다.
#
# trivy 버전·해시는 scripts/tool_pins.txt 한 곳에 고정한다 (https://github.com/reddol18/docflow/issues/46).
# 처음에 GitHub 릴리스에서 받아 ~/.local/share/docflow-tools 에 두고 고정한 자산 해시로 확인한다(릴리스 체크섬 파일은 쓰지 않음).
# 임시 폴더에 받아 풀고, 실행 파일 해시까지 맞으면 제자리로 옮긴다(중간에 끊기면 다음 실행에 다시 받는다).
# 받아 둔 실행 파일도 부를 때마다 고정한 실행 파일 해시로 확인하고, 다르면 실행하지 않고 멈춘다.
# 시험용으로 바꿀 수 있는 곳: DOCFLOW_TOOL_PINS(고정 파일), DOCFLOW_TOOLS_DIR(받아 두는 곳) — 기본값과 다르면 경고를 찍는다.
# deploy.sh는 이 둘을 지우고 부른다.
#
# image:가 '태그@sha256:…'(다이제스트 고정)이면 그 다이제스트로 찾고, 보고한 ID가 그 다이제스트다.
# 보고서 캐시: 이미지 ID·trivy 판·trivy DB 갱신 시각이 같으면 전에 만든 JSON 보고서를 다시 쓴다(내보내기·스캔 생략,
# ~/.cache/docflow-scan). DB가 바뀌면 지난 캐시는 지운다. 표시 '(캐시)'.
set -e
set -f   # 고정 파일 칸을 나눌 때 글로브가 펼쳐지지 않게
umask 077
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"  # colima (Intel·Apple Silicon Homebrew)
CURL=/usr/bin/curl; SHASUM=/usr/bin/shasum; TAR_BIN=/usr/bin/tar     # 검증에 쓰는 도구는 시스템 것만
# 종료 코드 2는 무결성 실패(bad) 전용 — set -e로 새는 다른 실패가 우연히 2로 끝나면 1로 바꾼다. 끊기면(INT·TERM) 1
BAD=0; TAR=""; REP=""; CNT=""; NEW=""
on_exit() {
  rc=$?
  rm -rf "$NEW"; rm -f "$TAR" "$REP" "$CNT"
  if [ "$rc" = 2 ] && [ "$BAD" != 1 ]; then rc=1; fi
  exit "$rc"
}
trap on_exit EXIT
trap 'exit 1' INT TERM
bad() { BAD=1; echo "!! 검사 도구 무결성 실패: $*"; exit 2; }
HERE=$(cd "$(dirname "$0")" && pwd)
DEF_PINS="$HERE/tool_pins.txt"
DEF_TOOLS="$HOME/.local/share/docflow-tools"
PINS=${DOCFLOW_TOOL_PINS:-$DEF_PINS}
TOOLS=${DOCFLOW_TOOLS_DIR:-$DEF_TOOLS}
[ "$PINS" = "$DEF_PINS" ] || echo "경고: 고정 파일을 기본값이 아닌 곳에서 읽음 (DOCFLOW_TOOL_PINS=$PINS)" >&2
[ "$TOOLS" = "$DEF_TOOLS" ] || echo "경고: 검사 도구를 기본값이 아닌 곳에 둠 (DOCFLOW_TOOLS_DIR=$TOOLS)" >&2
CACHE="$HOME/.cache/docflow-scan"
TCACHE="$HOME/.cache/trivy"
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
[ -f "$PINS" ] || bad "고정 파일 없음: $PINS"
# 이름 버전 플랫폼 저장소 자산 자산_sha256 실행파일 실행파일_sha256 — trivy·이 플랫폼 줄이 정확히 하나
PIN=$(tr -d '\r' < "$PINS" | awk -v p="$PLAT" '$1 == "trivy" && $3 == p') || bad "$PINS 를 읽지 못함"
[ "$(printf '%s\n' "$PIN" | grep -c .)" = 1 ] || bad "$PINS 에 trivy $PLAT 줄이 하나여야 함"
set -- $PIN
[ $# = 8 ] || bad "$PINS trivy $PLAT 줄이 8칸이 아님"
VER=$2; REPO=$4; ASSET=$5; ASSET_SHA=$6; EXE=$7; EXE_SHA=$8
# 칸 형식: 경로·URL에 들어가는 칸은 영숫자로 시작하는 [A-Za-z0-9._-]만 (옵션·경로 이탈 막기), 해시는 소문자 64자리
is() { printf '%s' "$2" | grep -Eq "$1"; }
NAME_RE='^[A-Za-z0-9][A-Za-z0-9._-]*$'
HASH_RE='^[0-9a-f]{64}$'
for v in "$VER" "$ASSET" "$EXE"; do is "$NAME_RE" "$v" || bad "$PINS trivy $PLAT 칸 형식이 틀림: $v"; done
is '^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$' "$REPO" || bad "$PINS trivy $PLAT 저장소 형식이 틀림: $REPO"
is "$HASH_RE" "$ASSET_SHA" || bad "$PINS trivy $PLAT 자산 해시 형식이 틀림"
is "$HASH_RE" "$EXE_SHA" || bad "$PINS trivy $PLAT 실행 파일 해시 형식이 틀림"
set +f
DIR="$TOOLS/trivy-$VER"
TRIVY="$DIR/$EXE"
sha() { $SHASUM -a 256 "$1" | cut -d' ' -f1; }
# 지난번에 끊겨 남은 임시 것 정리 (받다 만 폴더, 쓰다 만 캐시)
rm -rf "$TOOLS"/.trivy-new.* 2>/dev/null || true
rm -f "$CACHE"/.*.tmp 2>/dev/null || true

if [ ! -f "$TRIVY" ]; then
  mkdir -p "$TOOLS"
  NEW=$(mktemp -d "$TOOLS/.trivy-new.XXXXXX")  # 여기서 받아 풀고 확인한 뒤 제자리로
  $CURL -fsSL --proto '=https' --proto-redir '=https' --tlsv1.2 -o "$NEW/$ASSET" \
    "https://github.com/$REPO/releases/download/v$VER/$ASSET" || { echo "$ASSET 받기 실패"; exit 1; }
  GOT=$(sha "$NEW/$ASSET")
  [ "$GOT" = "$ASSET_SHA" ] || bad "$ASSET 해시가 tool_pins.txt와 다름 (받은 $GOT) — 받지 않고 멈춤"
  $TAR_BIN -xzf "$NEW/$ASSET" -C "$NEW" "$EXE" || { echo "$ASSET 풀기 실패"; exit 1; }
  GOT=$(sha "$NEW/$EXE")
  [ "$GOT" = "$EXE_SHA" ] || bad "$ASSET 안 $EXE 해시가 tool_pins.txt와 다름 ($GOT) — 받지 않고 멈춤"
  mkdir -p "$DIR"
  mv "$NEW/$EXE" "$TRIVY"   # 같은 파일 시스템 안 이름 바꾸기 — 반쯤 쓴 실행 파일이 제자리에 남지 않는다
fi
# 받아 둔 실행 파일도 매번 (바뀌었으면 지우지 않고 멈춘다 — 무엇이 바뀌었는지 볼 수 있게)
GOT=$(sha "$TRIVY")
[ "$GOT" = "$EXE_SHA" ] || bad "$TRIVY 해시가 tool_pins.txt와 다름 ($GOT) — 실행하지 않음. 확인 뒤 $DIR 를 지우면 다시 받는다"

img_of() { [ -f "$1" ] && sed -n 's/^[[:space:]]*image:[[:space:]]*\([^[:space:]]*\).*/\1/p' "$1"; }
# 데모 Keycloak(k8s/keycloak.yaml, https://github.com/reddol18/docflow/issues/39)은 따로 올리는 것이라 k3s에 없으면 건너뛰기만 (실패 아님)
OPTIONAL=$(img_of "$HERE/../k8s/keycloak.yaml" | sort -u)
IMAGES=$( { img_of "$HERE/../k8s/docs.yaml"; printf '%s\n' "$OPTIONAL"; } | sed '/^$/d' | sort -u)  # 맥: ~/doc_platform/k8s
[ -n "$IMAGES" ] || { echo "k8s/docs.yaml에서 image:를 찾지 못함"; exit 1; }
# 이미지 tar(가장 큰 것 약 2GB)·trivy DB를 맥 디스크에 쓰므로 먼저 여유 확인 (반드시) — scripts/mac/disk_check.sh
sh "$HERE/disk_check.sh" </dev/null || exit 1
FAIL=0
# DB를 먼저 맞춘다 (이미지마다 하지 않게, 캐시 키에 갱신 시각을 쓰려고). 실패하면 있는 DB로 요약은 내되 실패로 끝낸다
if ! "$TRIVY" image --quiet --download-db-only --cache-dir "$TCACHE" </dev/null; then
  echo "== trivy DB 갱신 실패 — 있는 DB로 요약 (아래 줄의 DB 시각을 볼 것)"; FAIL=1
fi
DBAT=$(sed -n 's/.*"UpdatedAt":"\([^"]*\)".*/\1/p' "$TCACHE/db/metadata.json" 2>/dev/null | cut -c1-16) || DBAT=""
DBSTAMP=$(printf '%s' "$DBAT" | tr -dc '0-9') || DBSTAMP=""
DBSHOW=", DB ${DBAT:-없음}"  # 요약 줄마다 붙인다 — 묵은 DB가 보이게
mkdir -p "$CACHE"
if [ -n "$DBSTAMP" ]; then
  for f in "$CACHE"/*.json; do  # trivy 판이나 DB가 바뀐 지난 보고서는 지운다
    [ -e "$f" ] || continue
    case "$f" in *."$VER.$DBSTAMP".json) ;; *) rm -f "$f" || true ;; esac
  done
fi
TAR=$(mktemp "$HOME/docflow_image.XXXXXX")  # 이미지마다 덮어쓰고, 끝나면 지운다
REP=$(mktemp "$HOME/docflow_trivy.XXXXXX")  # trivy JSON 보고서 (한 번 스캔해 표·요약을 만든다)
CNT=$(mktemp "$HOME/docflow_count.XXXXXX")
# 요약: 발견(패키지×취약점) 수, 괄호 안은 고친 판이 있는 것
TPL='{{- range . }}{{- range .Vulnerabilities }}{{ .Severity }} {{ if .FixedVersion }}fix{{ else }}nofix{{ end }}
{{ end }}{{- end }}'
# 반복문 안 명령은 모두 실패를 잡아 '이미지 …: … 실패'를 찍고 다음 이미지로 (FAIL=1)
for IMG in $IMAGES; do
  case "$IMG" in
    *@sha256:*)  # repo:tag@d → repo@d (nerdctl은 태그+다이제스트 이름을 못 찾는다). 태그는 마지막 / 뒤에서만 (host:port/repo)
      NAME="${IMG%@*}"
      case "${NAME##*/}" in *:*) NAME="${NAME%:*}" ;; esac
      REF="$NAME@${IMG#*@}" ;;
    *) REF="$IMG" ;;
  esac
  # ID를 먼저 얻고 그 ID로 내보낸다 → 그 사이에 다시 빌드돼도 보고한 ID = 스캔한 이미지
  LIST=$(colima nerdctl -- --namespace k8s.io images --no-trunc --format '{{.ID}}' "$REF" </dev/null) \
    || { echo "== 이미지 $IMG: 이미지 목록 실패"; FAIL=1; continue; }
  ID=$(printf '%s\n' "$LIST" | sed -n 1p)
  if [ -z "$ID" ]; then
    case " $(echo $OPTIONAL) " in *" $IMG "*) echo "== 이미지 $IMG: k3s에 없음 (따로 올리는 데모 — 건너뜀)"; continue ;; esac
    echo "== 이미지 $IMG: k3s에 없음 (건너뜀)"; FAIL=1; continue
  fi
  KEY=$(printf '%s' "${ID#sha256:}" | tr -dc '0-9a-f') || { echo "== 이미지 $IMG ($ID): 요약 실패 (키)"; FAIL=1; continue; }
  CF="$CACHE/$KEY.$VER.$DBSTAMP.json"  # 캐시 키: 이미지 ID + trivy 판 + DB 갱신 시각
  HIT=""
  if [ -n "$DBSTAMP" ] && [ -s "$CF" ] && cp "$CF" "$REP"; then
    HIT=" (캐시)"
  else
    if ! colima nerdctl -- --namespace k8s.io save "$ID" </dev/null > "$TAR"; then
      echo "== 이미지 $IMG ($ID): 스캔 실패 (내보내기)"; FAIL=1; continue
    fi
    if ! "$TRIVY" image --quiet --input "$TAR" --scanners vuln --severity HIGH,CRITICAL --exit-code 0 --skip-db-update \
        --format json --output "$REP" --cache-dir "$TCACHE" </dev/null; then
      echo "== 이미지 $IMG ($ID): 스캔 실패"; FAIL=1; continue
    fi
    : > "$TAR" || true  # 다음 이미지 전에 디스크를 돌려준다
  fi
  # convert 실패를 놓치지 않게 파일로 받고 종료 코드를 본다 (파이프면 awk가 0개로 '요약'해 버린다)
  if ! "$TRIVY" convert --quiet --format template --template "$TPL" --output "$CNT" "$REP" </dev/null; then
    echo "== 이미지 $IMG ($ID): 요약 실패$HIT"; FAIL=1
    [ -z "$HIT" ] || rm -f "$CF" || true  # 깨진 캐시는 지워 다음에 다시 스캔
    continue
  fi
  COUNTS=$(awk '
    NF { n[$1]++; if ($2 == "fix") f[$1]++ }
    END { printf "HIGH %d (고친 판 %d), CRITICAL %d (고친 판 %d)", n["HIGH"], f["HIGH"], n["CRITICAL"], f["CRITICAL"] }' "$CNT") \
    || { echo "== 이미지 $IMG ($ID): 요약 실패 (세기)"; FAIL=1; continue; }
  echo "== 이미지 $IMG ($ID): $COUNTS$DBSHOW$HIT"
  if [ -z "$HIT" ] && [ -n "$DBSTAMP" ]; then  # 캐시 쓰기 실패는 다음에 다시 스캔할 뿐 — 실패로 치지 않는다
    { cp "$REP" "$CACHE/.$KEY.tmp" && mv "$CACHE/.$KEY.tmp" "$CF"; } || { rm -f "$CACHE/.$KEY.tmp"; echo "경고: 보고서 캐시 쓰기 실패" >&2; }
  fi
  if [ "$SUMMARY" != 1 ] && ! "$TRIVY" convert --quiet --format table "$REP" </dev/null; then
    echo "== 이미지 $IMG ($ID): 표 출력 실패"; FAIL=1
  fi
done
exit $FAIL
