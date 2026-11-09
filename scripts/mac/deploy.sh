#!/bin/sh
# 맥에서: 이미지 다시 빌드(앱 코드만 바뀌면 캐시로 빠름) → docflow 재시작 → 준비될 때까지 기다림 → 이미지 안 패키지 목록 저장
# → 이미지 취약점 요약(보고만, scan_image.sh --summary).
# 보통은 Windows에서 scripts/deploy.ps1로 부른다(복사 + 이 스크립트 + 배포 태그).
set -e
export PATH="/usr/local/bin:$HOME/.local/bin:$PATH"
# 빌드 전 디스크 여유 확인 (반드시). 모자라면 여기서 멈춘다 — scripts/mac/disk_check.sh
sh ~/doc_platform/scripts/disk_check.sh
# 데이터 소유자가 매니페스트의 runAsUser와 맞는지 (https://github.com/reddol18/docflow/issues/29). 다르면 apply 전에 멈춘다
sh ~/doc_platform/scripts/owner_migrate.sh check
sh ~/doc_platform/scripts/build_image.sh
kubectl apply -f ~/doc_platform/k8s/docs.yaml >/dev/null
# postgres·qdrant 이미지를 바꾸면 apply가 둘을 다시 띄운다(Recreate) — docflow는 시작할 때 DB에 붙으므로 먼저 기다린다
for d in postgres qdrant; do
  kubectl rollout status deploy/$d -n docs --timeout=300s || { kubectl logs deploy/$d -n docs --tail=40; exit 1; }
done
kubectl rollout restart deploy/docflow -n docs >/dev/null
kubectl rollout status deploy/docflow -n docs --timeout=300s || { kubectl logs deploy/docflow -n docs --tail=40; exit 1; }
kubectl get pods -n docs
# 이미지 안 설치 결과(pip freeze --all). deploy.ps1이 받아 저장소의 requirements.lock 판과 맞대 본다 (https://github.com/reddol18/docflow/issues/33)
# 롤아웃 뒤라 여기서 실패해도 배포를 실패로 만들지 않는다 (deploy.ps1이 경고만 하고 태그를 남긴다)
rm -f ~/doc_platform/requirements.freeze
kubectl exec -n docs deploy/docflow -- cat /app/requirements.freeze > ~/doc_platform/requirements.freeze \
  || { echo "경고: 이미지 패키지 목록을 꺼내지 못함" >&2; rm -f ~/doc_platform/requirements.freeze; }
grep -iE '^(langgraph|langgraph-checkpoint|langgraph-checkpoint-postgres|psycopg|psycopg-pool|qdrant-client|openai|fastapi|uvicorn|paddleocr|paddlepaddle|paddlex)==' ~/doc_platform/requirements.freeze || true
kubectl logs deploy/docflow -n docs --tail=5
# 이미지 취약점 요약 — 보고만 (https://github.com/reddol18/docflow/issues/46). 롤아웃 뒤에 돌리고, 지적이 있든 스캔이
# 실패하든(해시 불일치·디스크 부족 포함) 배포는 성공으로 끝난다. 막는 것은 범위 밖 (고칠 판 없는 CRITICAL이 있다)
echo "== 이미지 스캔 (보고만)"
scan_t0=$(date +%s)
sh ~/doc_platform/scripts/scan_image.sh --summary </dev/null || echo "경고: 이미지 스캔을 끝내지 못함 — 배포는 그대로 (위 메시지)" >&2
echo "이미지 스캔 $(( $(date +%s) - scan_t0 ))초"
