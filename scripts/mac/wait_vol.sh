#!/bin/sh
# 지정한 철의 실행이 끝날 때까지 기다린다 (로그의 'run <철> stopped/finished' 또는 Traceback).
# 사용: sh wait_vol.sh <철> [최대 초]
export PATH="$HOME/.local/bin:$PATH"
V="$1"; max=${2:-7200}; t=0
until kubectl logs deploy/docflow -n docs | grep -qE "run $V stopped/finished|Traceback"; do
  sleep 30; t=$((t+30)); [ $t -ge "$max" ] && echo "timeout ${max}s" && break; done
kubectl logs deploy/docflow -n docs | grep -E '^run |Traceback|Error' | tail -12
echo "ocr pages: $(kubectl logs deploy/docflow -n docs | grep -c '^ocr ')"
