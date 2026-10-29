#!/bin/sh
# 맥에서: 이미지 다시 빌드(앱 코드만 바뀌면 캐시로 빠름) → docflow 재시작 → 준비될 때까지 기다림 → 고정된 패키지 버전 저장.
set -e
export PATH="/usr/local/bin:$HOME/.local/bin:$PATH"
sh ~/doc_platform/scripts/build_image.sh
kubectl apply -f ~/doc_platform/k8s/docs.yaml >/dev/null
kubectl rollout restart deploy/docflow -n docs >/dev/null
kubectl rollout status deploy/docflow -n docs --timeout=300s || { kubectl logs deploy/docflow -n docs --tail=40; exit 1; }
kubectl get pods -n docs
kubectl exec -n docs deploy/docflow -- cat /app/requirements.lock > ~/doc_platform/requirements.lock
grep -iE '^(langgraph|langgraph-checkpoint|langgraph-checkpoint-postgres|psycopg|psycopg-pool|qdrant-client|openai|fastapi|uvicorn|paddleocr|paddlepaddle|paddlex)==' ~/doc_platform/requirements.lock
kubectl logs deploy/docflow -n docs --tail=5
