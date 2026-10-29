#!/bin/sh
# 공문서 플랫폼 실습 동안 어제의 은행 게이트웨이 스택을 내려 메모리를 비운다. 지우지 않고 0개로만 줄인다.
# 되돌리기: sh free_bank_stack.sh undo
export PATH="$HOME/.local/bin:$PATH"
if [ "${1:-}" = undo ]; then
  kubectl annotate scaledobject ollama -n sllm autoscaling.keda.sh/paused-replicas- >/dev/null
  kubectl scale deploy/gw-api deploy/gw-dispatcher -n gateway --replicas=1
  exit
fi
# KEDA가 다시 1개로 올리지 않게 일시정지 후 0개
kubectl annotate scaledobject ollama -n sllm --overwrite autoscaling.keda.sh/paused-replicas=0 >/dev/null
kubectl scale deploy/ollama -n sllm --replicas=0
kubectl scale deploy/gw-api deploy/gw-dispatcher -n gateway --replicas=0
sleep 20
kubectl get pods -A --no-headers | grep -v kube-system
kubectl top node 2>/dev/null || true
