#!/bin/bash
# 묶음 안 이미지 tar를 그대로 trivy로 스캔한다 (https://github.com/reddol18/docflow/issues/81 — 보안 리뷰 #6, 버그 리뷰 2차 #4).
# 레지스트리에서 태그로 다시 받으면 묶음에 든 것과 다를 수 있어, 묶음의 tar만 본다: images/*.tar.zst(앱·레지스트리 이미지)와
# k3s/k3s-airgap-images-amd64.tar.zst(k3s 시스템 이미지 여러 개).
#   sudo bash scripts/offline/scan_bundle.sh <묶음 폴더> <저장소 폴더(tool_pins.txt)> [결과 폴더]
# 필요한 것: 인터넷(trivy 취약점 DB 받기), k3s(돌고 있는 containerd — 여러 이미지가 든 airgap tar를 이미지 하나씩 나누는 데),
# zstd, jq. 이미지는 따로 만든 containerd 네임스페이스(docflow-bundle-scan)에 잠깐 넣었다 지운다 (k8s.io는 건드리지 않음).
# 결과(묶음 밖): <결과 폴더>/<이미지>.json, <결과 폴더>/summary.tsv — 이미지·HIGH·CRITICAL·그중 고친 판 있음(FixedVersion).
# HIGH·CRITICAL이 있어도 멈추지 않는다 (수를 보고). 스캔 자체가 실패한 이미지가 하나라도 있으면(또는 trivy 해시 불일치·디스크
# 모자람이면) 0이 아닌 값으로 끝난다. 층은 풀지 않고(--no-unpack) 넣으며, 끝나면 이미지·내용·스냅숏·임시 tar가 남지 않았는지 적는다.
# 묶음은 읽기만 한다.
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
TARS=("$B"/images/*.tar.zst "$B"/k3s/k3s-airgap-images-amd64.tar.zst)
for f in "${TARS[@]}"; do [ -f "$f" ] && [ ! -L "$f" ] || { echo "묶음 tar 없음(또는 링크): $f" >&2; exit 1; }; done

# 디스크 어림: containerd 내용 저장소에 압축 층 그대로(--no-unpack, 풀지 않음) ≈ tar 합계 + 동시에 있는 임시 tar 둘
# (풀어 둔 x.tar와 내보낸 one.tar — 큰 것 기준, 압축 전 크기는 압축본의 3배로 어림) + 여유 2GB. 모자라면 시작하지 않는다
sum=0; max=0
for f in "${TARS[@]}"; do s=$(stat -c %s "$f"); sum=$((sum + s)); [ "$s" -le "$max" ] || max=$s; done
need_gb=$(( (sum + 2 * 3 * max) / 1024 / 1024 / 1024 + 2 ))
free_gb=$(df -P -BG /root | awk 'NR==2 {gsub("G","",$4); print $4}')
say "디스크 /root 여유 ${free_gb}GB, 어림 필요 ${need_gb}GB (tar 합계 $((sum/1024/1024))MB, 가장 큰 tar $((max/1024/1024))MB)"
[ "$free_gb" -ge "$need_gb" ] || { echo "디스크 모자람 — 시작하지 않음" >&2; exit 1; }

W=$(mktemp -d /root/docflow-scan.XXXXXX)
clear_ns() { # 이 네임스페이스의 이미지를 지우고 내용·스냅숏을 바로 거둔다 (--sync)
  local r
  for r in $($CTR images ls -q 2>/dev/null); do $CTR images rm --sync "$r" >/dev/null 2>&1 || true; done
}
cleanup() {
  clear_ns
  k3s ctr namespaces rm "$NS" >/dev/null 2>&1 || true
  rm -rf "$W"
}
trap cleanup EXIT
# 앞선 실행이 남긴 것이 있으면 비우고 시작한다 (남은 이미지가 이번 결과에 섞이지 않게)
left=$($CTR images ls -q 2>/dev/null | wc -l)
[ "$left" = 0 ] || { say "네임스페이스 $NS에 앞선 실행의 이미지 ${left}개 — 비우고 시작"; clear_ns; }
mkdir -p "$OUT"
SUM="$OUT/summary.tsv"
{ echo "# 묶음 $(basename "$B") tar 스캔, $(date -Iseconds), $("$TRIVY" --version | head -1)"
  printf '이미지\t파일\tHIGH\tCRITICAL\t고친 판 있음(HIGH+CRITICAL)\n'; } > "$SUM"
peak=0; note_peak() { local u; u=$(df -P -BM /root | awk 'NR==2 {gsub("M","",$3); print $3}'); [ "$u" -le "$peak" ] || peak=$u; }
used0=$(df -P -BM /root | awk 'NR==2 {gsub("M","",$3); print $3}')

# 1) 묶음 tar를 하나씩 풀어 네임스페이스에 넣는다 — 층은 풀지 않는다(--no-unpack: trivy는 내보낸 tar만 본다).
#    이번에 넣은 ref만 기록해 그것만 스캔한다 (어느 파일에서 왔는지도)
declare -A FROM
REFS=()
for f in "${TARS[@]}"; do
  say "가져오기 $(basename "$f")"
  zstd -d -q -c "$f" > "$W/x.tar"; note_peak
  before=$($CTR images ls -q | sort)
  $CTR images import --no-unpack --platform linux/amd64 "$W/x.tar" >/dev/null
  rm -f "$W/x.tar"
  for r in $(comm -13 <(echo "$before") <($CTR images ls -q | sort)); do FROM[$r]=$(basename "$f"); REFS+=("$r"); done
done

# 2) 같은 내용(다이제스트)은 한 번만: 이름 하나씩 내보내 trivy --input으로 본다
declare -A SEEN
fail=0
for r in "${REFS[@]}"; do
  dg=$($CTR images ls | awk -v r="$r" '$1 == r {print $3}')
  [ -z "${SEEN[$dg]:-}" ] || continue
  SEEN[$dg]=1
  name=$(echo "${r%%@*}" | sed 's#^docker.io/##; s#^library/##')
  out="$OUT/$(echo "$name" | tr '/:' '__').json"
  say "스캔 $name (${FROM[$r]:-?})"
  $CTR images export --platform linux/amd64 "$W/one.tar" "$r"; note_peak
  if "$TRIVY" image --quiet --timeout 45m --input "$W/one.tar" --scanners vuln --severity HIGH,CRITICAL --format json -o "$out" >> "$OUT/trivy.log" 2>&1; then
    printf '%s\t%s\t%s\t%s\t%s\n' "$name" "${FROM[$r]:-?}" \
      "$(jq '[.Results[]?.Vulnerabilities[]? | select(.Severity=="HIGH")] | length' "$out")" \
      "$(jq '[.Results[]?.Vulnerabilities[]? | select(.Severity=="CRITICAL")] | length' "$out")" \
      "$(jq '[.Results[]?.Vulnerabilities[]? | select((.FixedVersion // "") != "")] | length' "$out")" >> "$SUM"
  else
    printf '%s\t%s\t스캔 실패\t-\t(%s/trivy.log)\n' "$name" "${FROM[$r]:-?}" "$OUT" >> "$SUM"; fail=1
  fi
  rm -f "$W/one.tar"
done
[ ${#REFS[@]} -gt 0 ] || { echo "가져온 이미지가 없음" >&2; fail=1; }

# 3) 정리하고 남은 것이 없는지 본다 (이미지·내용·스냅숏·임시 폴더)
clear_ns
rest="images $($CTR images ls -q | wc -l), content $($CTR content ls -q | wc -l), snapshots $($CTR snapshots ls 2>/dev/null | tail -n +2 | wc -l)"
k3s ctr namespaces rm "$NS" >/dev/null 2>&1 || true
rm -rf "$W"; [ ! -e "$W" ] || rest="$rest, 임시 폴더 남음"
used1=$(df -P -BM /root | awk 'NR==2 {gsub("M","",$3); print $3}')
say "디스크: 시작 ${used0}MB, 최대 ${peak}MB(+$((peak - used0))MB), 끝 ${used1}MB(+$((used1 - used0))MB). 남은 것: $rest"
say "요약: $SUM"; cat "$SUM"
[ "$fail" = 0 ] || { echo "스캔 실패가 있음 — 0이 아닌 값으로 끝냄" >&2; exit 1; }