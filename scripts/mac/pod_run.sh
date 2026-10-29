#!/bin/sh
# ~/doc_platform/tmp 의 파일을 docflow Pod의 /tmp로 복사하고, 그 안에서 파이썬 스크립트를 돌린다.
# 사용: sh pod_run.sh <스크립트 이름> [인자...]
set -e
export PATH="$HOME/.local/bin:$PATH"
POD=$(kubectl get pod -n docs -l app=docflow -o jsonpath='{.items[0].metadata.name}')
for f in ~/doc_platform/tmp/*; do kubectl cp "$f" "docs/$POD:/tmp/$(basename "$f")" >/dev/null; done
S="$1"; shift
kubectl exec -n docs "$POD" -- python "/tmp/$S" "$@"
