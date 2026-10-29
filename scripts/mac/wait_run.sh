#!/bin/sh
# docflow 로그에 실행 끝(run ... stopped/finished) 또는 오류(Traceback)가 나올 때까지 기다린 뒤 요약을 보여 준다.
# 사용: sh wait_run.sh <최대 초>
export PATH="$HOME/.local/bin:$PATH"
t=0; max=${1:-1800}
until kubectl logs deploy/docflow -n docs --since=60m | grep -qE 'stopped/finished|Traceback'; do
  sleep 10; t=$((t+10)); [ $t -ge "$max" ] && echo "timeout ${max}s" && break; done
kubectl logs deploy/docflow -n docs --since=60m | grep -vE 'healthz|ccache|warnings.warn|Creating model|Model files|Connectivity' | tail -40
