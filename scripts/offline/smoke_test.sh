#!/bin/bash
# 폐쇄망 설치 확인 시험 (https://github.com/reddol18/docflow/issues/81): 철 하나를 올리기 → 실행(review=auto, OCR·추출·판독·색인)
# → 검색. root로, 설치한 노드에서. API는 kubectl port-forward(노드 127.0.0.1)로 부른다 — 바깥 네트워크를 쓰지 않는다.
#   smoke_test.sh <쪽 이미지 폴더(*.jpg)> <출력 폴더> <검색어>
# 출력 폴더에는 상태·개수·순위만 남긴다 (문서 글·값은 남기지 않는다). 첫 관리자 비밀번호는 install.sh가 만든 파일에서 읽는다.
set -euo pipefail
PAGES=${1:?쪽 이미지 폴더}
OUT=${2:?출력 폴더}
Q=${3:?검색어}
VOL=${VOL:-$(basename "$PAGES")}
ADMIN_USER=${ADMIN_USER:-admin}
PW=${PW_FILE:-/root/docflow-admin-password}
TIMEOUT=${RUN_TIMEOUT:-7200}
PORT=18000
K="k3s kubectl"
mkdir -p "$OUT"
LOG="$OUT/smoke.log"
say() { printf '[smoke %s] %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$LOG"; }
J=$(mktemp -d); trap 'kill $PF 2>/dev/null; rm -rf "$J"' EXIT

$K -n docs port-forward svc/docflow $PORT:8000 >/dev/null 2>&1 & PF=$!
for i in $(seq 1 30); do curl -fs "http://127.0.0.1:$PORT/healthz" >/dev/null && break; sleep 1; done
H=(-H "x-docflow-csrf: 1" -b "$J/c" -c "$J/c")
api() { curl -sS -o "$J/body" -w '%{http_code}' "${H[@]}" "$@"; }

python3 -c 'import json,sys; print(json.dumps({"username": sys.argv[1], "password": open(sys.argv[2]).read().strip()}))' "$ADMIN_USER" "$PW" > "$J/login.json"
code=$(api -H 'content-type: application/json' --data @"$J/login.json" "http://127.0.0.1:$PORT/auth/login"); rm -f "$J/login.json"
say "로그인: HTTP $code"; [ "$code" = 200 ]

n=$(ls "$PAGES"/*.jpg | wc -l)
args=(); for f in "$PAGES"/*.jpg; do args+=(-F "files=@$f;type=image/jpeg"); done
code=$(api "${args[@]}" "http://127.0.0.1:$PORT/volumes/$VOL/pages")
say "올리기: 쪽 $n장 → HTTP $code $(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print({k: d.get(k) for k in ("saved","unchanged","total")})' "$J/body")"
[ "$code" = 200 ]

t0=$(date +%s)
code=$(api -X POST "http://127.0.0.1:$PORT/volumes/$VOL/run?review=auto")
say "실행 시작(review=auto): HTTP $code"; [ "$code" = 200 ]
last=""
while :; do
  code=$(api "http://127.0.0.1:$PORT/volumes/$VOL")
  s=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["stage"], d["running"] or "-", d["ocr_done"], d["documents"], d["indexed"], d["flagged_documents"])' "$J/body")
  [ "$s" = "$last" ] || say "  상태(stage running ocr_done documents indexed flagged): $s"
  last=$s
  set -- $s
  if [ "$2" = "-" ] && [ "$1" != "new" ]; then break; fi
  [ $(( $(date +%s) - t0 )) -lt "$TIMEOUT" ] || { say "시간 초과 ${TIMEOUT}s"; exit 1; }
  sleep 15
done
say "실행 끝: stage=$1, 문서 $4건, 색인 $5건, 검토 표시 $6건, 걸린 시간 $(( $(date +%s) - t0 ))초"
api "http://127.0.0.1:$PORT/volumes/$VOL" >/dev/null
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("사용량(모델별 호출·토큰·비용):", json.dumps(d.get("usage"), ensure_ascii=False))' "$J/body" | tee -a "$LOG"
[ "$1" = done ] || { say "끝 단계가 done이 아님"; exit 1; }

code=$(api -G --data-urlencode "q=$Q" --data-urlencode "k=5" "http://127.0.0.1:$PORT/search")
say "검색: HTTP $code"
[ "$code" = 200 ]
python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); print("검색 결과", len(r), "건 (vol/doc_id, rrf, vector_rank, keyword_rank):"); [print(" ", x["vol"]+"/"+x["doc_id"], x["rrf"], x["vector_rank"], x["keyword_rank"]) for x in r]' "$J/body" | tee -a "$LOG"
say "끝"
