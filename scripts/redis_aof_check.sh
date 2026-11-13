#!/bin/sh
# Redis AOF 재시작 시험 — 매니페스트(k8s/docs.yaml) redis 초기화 컨테이너의 ACL 줄 그대로로 Redis를 띄워
# 대기열 Lua가 남기는 MULTI/EXEC 묶음이 재시작 뒤 살아남는지 본다 (https://github.com/reddol18/docflow/issues/5).
# 원인: "user default off"만 두면 default 사용자가 -@all로 리셋되고, AOF 적재기가 EXEC를 default 권한으로 다시 검사해
#       NOPERM으로 묶음을 버린다 (XLEN 3 → 0).
# 실행 (WSL, Windows에서): wsl -e sh /mnt/e/.../scripts/redis_aof_check.sh /home/<나>/redis74/root/usr/bin/redis-server [매니페스트]
# 결과: before/after가 같고 NOPERM 0줄이면 통과(종료 코드 0), 아니면 1.
set -eu
SERVER=${1:?redis-server 경로}
MANIFEST=${2:-$(dirname "$0")/../k8s/docs.yaml}
CLI=$(dirname "$SERVER")/redis-cli
PORT=${REDIS_AOF_CHECK_PORT:-56381}

# 초기화 컨테이너의 printf 형식 문자열 (user default ...\nuser docflow on >%s ...\n) — 매니페스트에서 바로 읽어 어긋나지 않게
FMT=$(sed -n 's/.*printf "\(user default [^"]*\)" "\$p".*/\1/p' "$MANIFEST")
[ -n "$FMT" ] || { echo "매니페스트에서 ACL printf 줄을 못 찾음: $MANIFEST" >&2; exit 2; }

D=$(mktemp -d /tmp/redis_aof_check.XXXXXX)
PW=Chk$(date +%s)$$
trap 'stop; rm -rf "$D"' EXIT
# shellcheck disable=SC2059
printf "$FMT" "$PW" > "$D/auth.conf"
cat > "$D/redis.conf" <<EOF
port $PORT
bind 127.0.0.1
dir $D
appendonly yes
appendfsync always
save ""
protected-mode yes
include $D/auth.conf
EOF

start() { "$SERVER" "$D/redis.conf" --daemonize yes --pidfile "$D/pid" --logfile "$D/log"; i=0; until cli PING >/dev/null 2>&1; do i=$((i+1)); [ $i -lt 50 ] || { echo "시작 실패" >&2; cat "$D/log" >&2; exit 2; }; sleep 0.1; done; }
stop() { [ -f "$D/pid" ] || return 0; p=$(cat "$D/pid"); kill "$p" 2>/dev/null || true; i=0; while kill -0 "$p" 2>/dev/null && [ $i -lt 50 ]; do i=$((i+1)); sleep 0.1; done; rm -f "$D/pid"; }
cli() { REDISCLI_AUTH=$PW "$CLI" -p "$PORT" --user docflow "$@"; }
state() { echo "XLEN q=$(cli XLEN '{docflow:q}:t') dead=$(cli XLEN '{docflow:q}:dead') pending=$(cli XPENDING '{docflow:q}:t' w | head -1)"; }

echo "default 줄: $(head -1 "$D/auth.conf")"
start
# 대기열 Lua와 같은 종류의 명령을 스크립트 안에서 (Lua 효과는 MULTI/EXEC로 AOF에 남는다)
cli EVAL "pcall(redis.call, 'XGROUP', 'CREATE', KEYS[1], 'w', '0', 'MKSTREAM'); for i = 1, 4 do redis.call('XADD', KEYS[1], '*', 'a', '0', 'body', 'j' .. i) end; return 1" 1 '{docflow:q}:t' >/dev/null
# XREADGROUP → XCLAIM + XGROUP SETID 로 전파
cli EVAL "return redis.call('XREADGROUP', 'GROUP', 'w', 'c1', 'COUNT', 2, 'STREAMS', KEYS[1], '>')" 1 '{docflow:q}:t' >/dev/null
# 죽은 작업 옮기기: XADD dead(MAXLEN) + XACK + XDEL, 그리고 XAUTOCLAIM·XTRIM
cli EVAL "local p = redis.call('XPENDING', KEYS[1], 'w', '-', '+', 1); local id = p[1][1]; redis.call('XADD', KEYS[2], 'MAXLEN', '~', 100, '*', 'id', id); redis.call('XACK', KEYS[1], 'w', id); redis.call('XDEL', KEYS[1], id); redis.call('TIME'); return 1" 2 '{docflow:q}:t' '{docflow:q}:dead' >/dev/null
cli EVAL "redis.call('XAUTOCLAIM', KEYS[1], 'w', 'c2', 0, '0-0', 'COUNT', 1, 'JUSTID'); redis.call('XTRIM', KEYS[1], 'MAXLEN', 1000); return 1" 1 '{docflow:q}:t' >/dev/null
B=$(state); echo "before: $B"
stop
start
A=$(state); echo "after:  $A"
N=$(grep -c NOPERM "$D/log" || true)
echo "NOPERM 로그 줄: $N"
echo "인증 없는 PING: $("$CLI" -p "$PORT" PING 2>&1)"
echo "default로 AUTH: $("$CLI" -p "$PORT" AUTH default x 2>&1)"
[ "$B" = "$A" ] && [ "$N" = 0 ]
