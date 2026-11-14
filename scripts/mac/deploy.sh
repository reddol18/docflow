#!/bin/sh
# 맥에서: 이미지 다시 빌드(docflow:dev, 프런트 docflow-web:dev = next build → 실행은 next start; 코드만 바뀌면 캐시로 빠름)
# → apply → docflow·web 재시작 → 준비될 때까지 기다림 → 이미지 안 패키지 목록 저장 → 이미지 취약점 요약(보고만, scan_image.sh --summary).
# 보통은 Windows에서 scripts/deploy.ps1로 부른다(복사 + 이 스크립트 + 배포 태그).
set -e
export PATH="/usr/local/bin:$HOME/.local/bin:$PATH"
URL119=https://github.com/reddol18/docflow/issues/119
# 디스크 확인의 시험 값·바꾼 기준은 배포에서 늘 지운다 (실제 측정·실제 문턱·저장소의 기준만) — scripts/mac/disk_check.sh 머리말.
# scripts/deploy.ps1의 사전 확인도 같은 목록 (tests/test_disk_check.py가 둘이 disk_check.sh의 변수를 모두 지우는지 본다)
unset DISK_TEST_IMG_PCT DISK_TEST_IMG_SIZE_GB DISK_TEST_VM_AVAIL_GB DISK_TEST_HOST_AVAIL_GB DISK_GC_HIGH_PCT DISK_GC_LOW_PCT DISK_GC_QUERY DISK_GC_MARGIN_PCT DISK_GC_REBUILD_MARGIN_PCT DISK_IMAGE_FS DISK_MIN_VM_GB DISK_MIN_HOST_GB PIN_TRIES PIN_SLEEP
. ~/doc_platform/scripts/pin_image.sh
# 시간 제한·중단 (Windows scripts/lib/remote.ps1이 이 프로세스 그룹에 TERM): VM 안 빌드는 build_image.sh 트랩이 멈춘다.
# apply·롤아웃은 어디까지 갔는지 모르므로 알리고 실패로 끝낸다
trap 'echo "!! 배포 중단 신호 — apply·롤아웃이 어디까지 갔는지 확인: kubectl get pods -n docs" >&2; exit 143' TERM INT HUP

# 이미지를 kubelet 이미지 GC에서 지키는 것 (https://github.com/reddol18/docflow/issues/119):
#   주: build_image.sh가 빌드 직후 우리 두 이미지를 고정(pin)한다 — '== 이미지 고정' 줄. 배포 경로(deploy·rebuild)에서는
#   두 번 시도해도 안 되면 빌드가 실패로 멈춰 apply·롤아웃까지 가지 않는다 (scripts/mac/pin_image.sh).
#   보조: 재시작 바로 전에 이미지가 containerd(k8s.io)에 있는지·고정됐는지 본다 — '== 이미지 확인' 줄. imagePullPolicy: Never라
#   없으면 새 파드가 ErrImageNeverPull로 뜨지 못한다(2026-11-10 약 10분 중단). 없으면 빌드 캐시로 그 이미지만 다시 만들고
#   (build_image.sh api|web rebuild — 디스크 확인을 그 이미지 크기로), 그래도 없으면 롤아웃하지 않고 멈춘다.
has_image() { colima nerdctl -- --namespace k8s.io image inspect "$1" >/dev/null 2>&1 </dev/null; }
ensure_image() {  # $1 이미지, $2 build_image.sh 대상(api|web), $3 언제
  if ! has_image "$1"; then
    echo "!! 이미지 확인($3): $1 없음 — kubelet 이미지 GC가 지웠을 수 있다. 빌드 캐시로 다시 빌드한다" >&2
    sh ~/doc_platform/scripts/build_image.sh "$2" rebuild </dev/null || { echo "!! $1 다시 빌드 실패 — 롤아웃하지 않음" >&2; return 1; }
    has_image "$1" || { echo "!! 이미지 확인($3): $1 다시 빌드해도 없음 — 롤아웃하지 않음. sh ~/doc_platform/scripts/image_list.sh로 디스크·이미지 확인" >&2; return 1; }
  fi
  if is_pinned "docker.io/library/$1"; then echo "== 이미지 확인($3): $1 있음, 고정(pinned: true)"; return 0; fi
  # 고정이 빠졌으면 다시 고정한다(두 번 시도). 안 되면 롤아웃하지 않는다
  echo "!! 이미지 확인($3): $1 있음, 고정 안 됨 — 다시 고정한다" >&2
  pin_image "$1" "$(image_id "$1")" strict || { echo "!! 이미지 확인($3): $1 고정 실패 — 롤아웃하지 않음" >&2; return 1; }
  echo "== 이미지 확인($3): $1 있음, 다시 고정(pinned: true)"
}
# 롤아웃이 실패하면: 파드가 ErrImageNeverPull이면 원인을 크게 알린다 → 로그 → 실패
rollout_failed() {  # $1 Deployment, $2 logs 인자
  bad=$(kubectl get pods -n docs --no-headers </dev/null 2>/dev/null | grep ErrImageNeverPull || true)
  if [ -n "$bad" ]; then
    echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!" >&2
    echo "!! ErrImageNeverPull — 노드에 이미지가 없어 파드가 뜨지 못함 (imagePullPolicy: Never)" >&2
    printf '%s\n' "$bad" >&2
    echo "!! 원인: 빌드 뒤 kubelet 이미지 GC가 지웠을 가능성이 크다 (이미지 디스크가 GC 문턱 이상, $URL119)" >&2
    echo "!! 확인: sh ~/doc_platform/scripts/disk_check.sh · sh ~/doc_platform/scripts/image_list.sh (읽기만)" >&2
    echo "!! 되살리기: 디스크 정리(사용자 판단) → sh ~/doc_platform/scripts/build_image.sh → kubectl rollout restart deploy/$1 -n docs" >&2
    echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!" >&2
  fi
  kubectl logs deploy/$1 -n docs $2 </dev/null || true
  exit 1
}

# 디스크 확인은 build_image.sh가 빌드 직전에 한다(더 쓸 2GB 포함). Windows deploy.ps1도 복사 전에 한 번 본다
# 데이터 소유자가 매니페스트의 runAsUser와 맞는지 (https://github.com/reddol18/docflow/issues/29). 다르면 apply 전에 멈춘다
sh ~/doc_platform/scripts/owner_migrate.sh check
# TLS 인증서·Qdrant API 키·CA가 있어야 한다 (https://github.com/reddol18/docflow/issues/48). 없이 apply하면 postgres·qdrant가
# 뜨지 못한 채 내려간다 → apply 전에 멈춘다. 넣는 법: Windows에서 .\scripts\ca.ps1 issue → .\scripts\tls_secrets.ps1
kubectl get secret tls-web tls-postgres tls-qdrant tls-redis qdrant-api-key redis-auth -n docs >/dev/null </dev/null \
  && kubectl get configmap docflow-ca -n docs >/dev/null </dev/null \
  || { echo "TLS Secret(tls-web·tls-postgres·tls-qdrant·tls-redis·qdrant-api-key·redis-auth)·ConfigMap docflow-ca가 없음 — Windows에서 scripts/tls_secrets.ps1 먼저. 배포하지 않음" >&2; exit 1; }
# OIDC client secret (https://github.com/reddol18/docflow/issues/39). 마운트는 optional — 없으면 docflow가 OIDC만 끄고(경고) 로컬 로그인으로
# 뜬다(app/auth.py oidc_startup). 데모 Keycloak 없이도 배포는 그대로 간다. 켜려면 Windows에서 scripts/keycloak.ps1
if ! kubectl get secret oidc -n docs >/dev/null 2>&1 </dev/null; then
  echo "== Secret oidc 없음 — 이번 배포의 docflow는 OIDC(SSO) 꺼짐, 로컬 로그인만. 켜려면 scripts/keycloak.ps1 (https://github.com/reddol18/docflow/issues/39)"
fi
# 빌드 → 바로 고정('== 이미지 고정' 줄). 고정이 확인되지 않으면 build_image.sh가 실패로 멈춰 apply에 가지 않는다 —
# 그래서 apply 전 확인은 따로 두지 않는다(고정 줄이 같은 것을 방금 확인했다)
sh ~/doc_platform/scripts/build_image.sh all deploy </dev/null
kubectl apply -f ~/doc_platform/k8s/docs.yaml >/dev/null
# postgres·qdrant 이미지를 바꾸면 apply가 둘을 다시 띄운다(Recreate) — docflow는 시작할 때 DB에 붙으므로 먼저 기다린다
# redis = 실행 대기열 (https://github.com/reddol18/docflow/issues/5). docflow·워커가 시작할 때 붙으므로 같이 먼저 기다린다
for d in postgres qdrant redis; do
  kubectl rollout status deploy/$d -n docs --timeout=300s || rollout_failed $d --tail=40
done
# 위 기다림 동안에도 GC가 돌 수 있어 재시작 바로 전에 본다 (고정이 됐으면 지워지지 않는다 — 보조 확인)
ensure_image docflow:dev api "docflow 재시작 전"
kubectl rollout restart deploy/docflow -n docs >/dev/null
kubectl rollout status deploy/docflow -n docs --timeout=300s || rollout_failed docflow --tail=40
# 워커(같은 이미지 docflow:dev, python -m app.worker). 태그가 같아 apply만으로는 다시 뜨지 않는다. 지금 쥔 작업은 되찾기가 잇는다
kubectl rollout restart deploy/docflow-worker -n docs >/dev/null
kubectl rollout status deploy/docflow-worker -n docs --timeout=300s || rollout_failed docflow-worker --tail=40
# 프런트 운영 서버 (next start + TLS 종단, https://github.com/reddol18/docflow/issues/48). 이미지 태그가 같아(docflow-web:dev) apply만으로는 다시 뜨지 않는다
ensure_image docflow-web:dev web "web 재시작 전"
kubectl rollout restart deploy/web -n docs >/dev/null
kubectl rollout status deploy/web -n docs --timeout=180s || rollout_failed web "--all-containers=true --tail=40"
kubectl get pods -n docs
# 이미지 안 설치 결과(pip freeze --all). deploy.ps1이 받아 저장소의 requirements.lock 판과 맞대 본다 (https://github.com/reddol18/docflow/issues/33)
# 롤아웃 뒤라 여기서 실패해도 배포를 실패로 만들지 않는다 (deploy.ps1이 경고만 하고 태그를 남긴다)
rm -f ~/doc_platform/requirements.freeze
kubectl exec -n docs deploy/docflow -- cat /app/requirements.freeze > ~/doc_platform/requirements.freeze \
  || { echo "경고: 이미지 패키지 목록을 꺼내지 못함" >&2; rm -f ~/doc_platform/requirements.freeze; }
grep -iE '^(langgraph|langgraph-checkpoint|langgraph-checkpoint-postgres|psycopg|psycopg-pool|qdrant-client|openai|fastapi|uvicorn|paddleocr|paddlepaddle|paddlex)==' ~/doc_platform/requirements.freeze || true
kubectl logs deploy/docflow -n docs --tail=5
# 이미지 취약점 요약 — 보고만 (https://github.com/reddol18/docflow/issues/46). 롤아웃 뒤에 돌리고, 지적이 있든 스캔이
# 실패하든 배포는 성공으로 끝난다. 막는 것은 범위 밖 (고칠 판 없는 CRITICAL이 있다).
# 종료 코드 2(검사 도구 무결성 실패: trivy 해시 불일치·고정 파일 형식)는 따로 눈에 띄게 알린다 — 누가 도구를 바꿨을 수 있다.
# 시험용 경로 변수는 지우고 부른다 (배포 때는 늘 저장소의 고정 파일·기본 도구 폴더)
echo "== 이미지 스캔 (보고만)"
unset DOCFLOW_TOOL_PINS DOCFLOW_TOOLS_DIR
scan_t0=$(date +%s)
scan_rc=0
sh ~/doc_platform/scripts/scan_image.sh --summary </dev/null || scan_rc=$?
case "$scan_rc" in
  0) ;;
  2) echo "!! 검사 도구 무결성 실패 — trivy를 실행하지 않았다. 위 메시지를 확인하고 통제자에게 알릴 것 (배포는 그대로)" >&2 ;;
  *) echo "경고: 이미지 스캔을 끝내지 못함 (종료 코드 $scan_rc) — 배포는 그대로 (위 메시지)" >&2 ;;
esac
echo "이미지 스캔 $(( $(date +%s) - scan_t0 ))초"
