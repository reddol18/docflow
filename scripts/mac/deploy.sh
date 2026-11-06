#!/bin/sh
# 맥에서: 이미지 다시 빌드(앱 코드만 바뀌면 캐시로 빠름) → docflow 재시작 → 준비될 때까지 기다림 → 고정된 패키지 버전 저장.
# 보통은 Windows에서 scripts/deploy.ps1로 부른다(복사 + 이 스크립트 + 배포 태그).
set -e
export PATH="/usr/local/bin:$HOME/.local/bin:$PATH"
# 빌드 전 디스크 여유 확인 (반드시). 모자라면 여기서 멈춘다 — scripts/mac/disk_check.sh
sh ~/doc_platform/scripts/disk_check.sh
sh ~/doc_platform/scripts/build_image.sh
kubectl apply -f ~/doc_platform/k8s/docs.yaml >/dev/null
# postgres·qdrant 이미지를 바꾸면 apply가 둘을 다시 띄운다(Recreate) — docflow는 시작할 때 DB에 붙으므로 먼저 기다린다
for d in postgres qdrant; do
  kubectl rollout status deploy/$d -n docs --timeout=300s || { kubectl logs deploy/$d -n docs --tail=40; exit 1; }
done
kubectl rollout restart deploy/docflow -n docs >/dev/null
kubectl rollout status deploy/docflow -n docs --timeout=300s || { kubectl logs deploy/docflow -n docs --tail=40; exit 1; }
kubectl get pods -n docs
kubectl exec -n docs deploy/docflow -- cat /app/requirements.lock > ~/doc_platform/requirements.lock
grep -iE '^(langgraph|langgraph-checkpoint|langgraph-checkpoint-postgres|psycopg|psycopg-pool|qdrant-client|openai|fastapi|uvicorn|paddleocr|paddlepaddle|paddlex)==' ~/doc_platform/requirements.lock
kubectl logs deploy/docflow -n docs --tail=5
