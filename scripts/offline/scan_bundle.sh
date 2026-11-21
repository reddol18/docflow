#!/bin/bash
# 묶음 안 이미지 tar를 그대로 trivy로 스캔한다 (https://github.com/reddol18/docflow/issues/81 — 보안 리뷰 #6, 버그 리뷰 2차 #4).
# 레지스트리에서 태그로 다시 받으면 묶음에 든 것과 다를 수 있어, 묶음의 tar만 본다: images/*.tar.zst(앱·레지스트리 이미지)와
# k3s/k3s-airgap-images-amd64.tar.zst(k3s 시스템 이미지 여러 개).
#   sudo bash scripts/offline/scan_bundle.sh <묶음 폴더> <저장소 폴더(tool_pins.txt)> [결과 폴더]
# 필요한 것: 인터넷(trivy 취약점 DB 받기), k3s(돌고 있는 containerd — 여러 이미지가 든 airgap tar를 이미지 하나씩 나누는 데),
# zstd, jq. 이미지는 따로 만든 containerd 네임스페이스(docflow-bundle-scan)에 잠깐 넣었다 지운다 (k8s.io는 건드리지 않음).
# 결과(묶음 밖): <결과 폴더>/<이미지>.json, <결과 폴더>/summary.tsv — 이미지·HIGH·CRITICAL·그중 고친 판 있음(FixedVersion).
# HIGH·CRITICAL이 있어도 멈추지 않는다 (수를 보고). 묶음은 읽기만 한다.
set -euo pipefail
B=${1:?묶음 폴더}
REPO=${2:?저장소 폴더}
OUT=${3:-/root/docflow-trivy-$(basename "$B")}
NS=docflow-bundle-scan
say() { printf '[scan %s] %s\n' "$(date +%H:%M:%S)" "$*"; }
[ "$(id -u)" = 0 ] || { echo "root로 돌린다" >&2; exit 1; }
for c in k3s zstd jq sha256sum curl; do command -v $c >/dev/null || { echo "$c 없음" >&2; exit 1; }; done

# trivy: scripts/tool_pins.txt의 linux-x86_64 줄(판·자산·실행 파일 해시)로 받고 확인한다
trivy_bin() {
  local pin ver asset asha exe esha dir
  pin=$(tr -d '\r' < "$REPO/scripts/tool_pins.txt" | awk '$1 == "trivy" && $3 == "linux-x86_64"')
  [ "$(printf '%s\n' "$pin" | grep -c .)" = 1 ] || { echo "tool_pins.txt에 trivy linux-x86_64 줄이 하나여야 함" >&2; return 1; }
  read -r _ ver _ _ asset asha exe esha <<< "$pin"
  dir=/root/.cache/docflow-tools/trivy-$ver; mkdir -p "$dir"
  if ! echo "$esha  $dir/$exe" | sha256sum -c - >/dev/null 2>&1; then
    curl -fsSL -o "$dir/$asset" "https://github.com/aquasecurity/trivy/releases/download/v$ver/$asset"
    echo "$asha  $dir/$asset" | sha256sum -c - >/dev/null || { echo "trivy 자산 해시 다름 — 실행하지 않음" >&2; return 1; }
    tar -xzf "$dir/$asset" -C "$dir" "$exe"
  fi
  echo "$esha  $dir/$exe" | sha256sum -c - >/dev/null || { echo "trivy 실행 파일 해시 다름 — 실행하지 않음" >&2; return 1; }
  echo "$dir/$exe"
}
TRIVY=$(trivy_bin)
k3s ctr version >/dev/null 2>&1 || { echo "k3s containerd에 붙지 못함 — k3s가 돌고 있어야 한다 (systemctl start k3s)" >&2; exit 1; }
CTR="k3s ctr -n $NS"
W=$(mktemp -d /root/docflow-scan.XXXXXX)
cleanup() {
  for r in $($CTR images ls -q 2>/dev/null); do $CTR images rm "$r" >/dev/null 2>&1 || true; done
  k3s ctr namespaces rm "$NS" >/dev/null 2>&1 || true
  rm -rf "$W"
}
trap cleanup EXIT
mkdir -p "$OUT"
SUM="$OUT/summary.tsv"
{ echo "# 묶음 $(basename "$B") tar 스캔, $(date -Iseconds), $("$TRIVY" --version | head -1)"
  printf '이미지\t파일\tHIGH\tCRITICAL\t고친 판 있음(HIGH+CRITICAL)\n'; } > "$SUM"

# 1) 묶음 tar를 하나씩 풀어 따로 만든 네임스페이스에 넣는다 (넣을 때 어느 파일에서 왔는지 기록)
declare -A FROM
for f in "$B"/images/*.tar.zst "$B"/k3s/k3s-airgap-images-amd64.tar.zst; do
  say "가져오기 $(basename "$f")"
  zstd -d -q -c "$f" > "$W/x.tar"
  before=$($CTR images ls -q | sort)
  $CTR images import --platform linux/amd64 "$W/x.tar" >/dev/null
  rm -f "$W/x.tar"
  for r in $(comm -13 <(echo "$before") <($CTR images ls -q | sort)); do FROM[$r]=$(basename "$f"); done
done

# 2) 같은 내용(다이제스트)은 한 번만: 이름 하나씩 내보내 trivy --input으로 본다
declare -A SEEN
for r in $($CTR images ls -q | sort); do
  dg=$($CTR images ls | awk -v r="$r" '$1 == r {print $3}')
  [ -z "${SEEN[$dg]:-}" ] || continue
  SEEN[$dg]=1
  name=$(echo "${r%%@*}" | sed 's#^docker.io/##; s#^library/##')
  out="$OUT/$(echo "$name" | tr '/:' '__').json"
  say "스캔 $name (${FROM[$r]:-?})"
  $CTR images export --platform linux/amd64 "$W/one.tar" "$r"
  if "$TRIVY" image --quiet --timeout 45m --input "$W/one.tar" --scanners vuln --severity HIGH,CRITICAL --format json -o "$out" >> "$OUT/trivy.log" 2>&1; then
    printf '%s\t%s\t%s\t%s\t%s\n' "$name" "${FROM[$r]:-?}" \
      "$(jq '[.Results[]?.Vulnerabilities[]? | select(.Severity=="HIGH")] | length' "$out")" \
      "$(jq '[.Results[]?.Vulnerabilities[]? | select(.Severity=="CRITICAL")] | length' "$out")" \
      "$(jq '[.Results[]?.Vulnerabilities[]? | select((.FixedVersion // "") != "")] | length' "$out")" >> "$SUM"
  else
    printf '%s\t%s\t스캔 실패\t-\t(%s/trivy.log)\n' "$name" "${FROM[$r]:-?}" "$OUT" >> "$SUM"
  fi
  rm -f "$W/one.tar"
done
say "요약: $SUM"; cat "$SUM"
