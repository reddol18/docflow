#!/bin/sh
# 맥에서: PVC 데이터 소유자를 k8s/docs.yaml의 runAsUser에 맞춘다 (https://github.com/reddol18/docflow/issues/29).
# k3s local-path PVC는 hostPath라 fsGroup이 듣지 않는다 — 컨테이너를 root에서 일반 사용자로 바꾸면 예전 파일을 못 쓴다.
#   postgres  uid 70   — 예전 root 진입 스크립트가 이미 70으로 바꿔 두었다 (보통 고칠 것 없음, check로 확인만)
#   qdrant    uid 1000 — 예전에 root로 돌아 저장소 파일이 root 소유. 처음 한 번 넘긴다
#   docflow   uid 1000 — 전부터 runAsUser 1000 (check로 확인만)
#
#   sh owner_migrate.sh check     # 지금 돌고 있는 파드 안에서 소유자가 다른 파일이 있으면 종료 코드 1 (deploy.sh가 apply 전에 부른다)
#                                 # 파드가 없으면(0으로 줄임 등) 그 서비스는 확인하지 않고 넘어간다
#   sh owner_migrate.sh qdrant    # qdrant를 0으로 줄이고, root 일회용 파드(같은 qdrant 이미지 — 새로 받지 않음)로 chown -R 1000:1000.
#                                 # qdrant는 0인 채로 둔다 — 이어서 deploy(새 매니페스트 apply)가 1000으로 띄운다.
#                                 # 0인 동안 옛 매니페스트(root)로 다시 띄우면 새 파일이 root 소유로 생기니 바로 배포한다.
#                                 # 여러 번 돌려도 같다 (이미 1000이면 바뀌는 것 없음)
# 일회용 root 파드를 매 시작마다 도는 init 컨테이너로 두지 않는 이유: 파드가 root 권한을 계속 갖게 되고(trivy KSV-0118) 한 번이면 된다.
# 되돌리기: 옛 매니페스트를 apply하면 root(DAC_OVERRIDE)는 1000 소유 파일도 읽고 쓴다 — 소유자를 되돌릴 필요 없음.
#           다만 root로 돈 동안 생긴 파일은 root 소유라 다시 앞으로 올 때 이 스크립트를 한 번 더 (check가 막아 준다).
set -eu
export PATH="/usr/local/bin:$HOME/.local/bin:$PATH"
NS=docs

running() {  # 준비된 파드가 있으면 0
  [ "$(kubectl get deploy "$1" -n $NS -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)" -ge 1 ] 2>/dev/null
}

check_one() {  # 서비스 경로 uid [mindepth — 1이면 마운트 폴더 자체는 빼고 본다]
  if ! running "$1"; then echo "$1: 돌고 있는 파드 없음 — 확인 건너뜀"; return 0; fi
  # exec·find가 실패하면(못 읽는 폴더 등) 확인 못 함 = 막는다
  if ! bad=$(kubectl exec -n $NS "deploy/$1" -- find "$2" -mindepth "${4:-0}" ! -user "$3" -print); then
    echo "$1: $2 소유자를 확인하지 못함 (위 오류)" >&2
    return 1
  fi
  if [ -n "$bad" ]; then
    bad=$(echo "$bad" | head -5)
    echo "$1: $2 아래에 uid $3 소유가 아닌 파일이 있음 (예):" >&2
    echo "$bad" >&2
    return 1
  fi
  echo "$1: $2 소유자 uid $3 확인"
}

case "${1:-}" in
  check)
    ok=0
    check_one postgres /var/lib/postgresql/data/pg 70 || ok=1
    check_one qdrant /qdrant/storage 1000 || ok=1
    check_one docflow /data 1000 1 || ok=1  # 전부터 1000으로 돈다 — 확인만 (PVC 폴더 자체는 local-path가 만든 root 0777)
    if [ $ok = 1 ]; then
      echo "소유자가 매니페스트와 다릅니다. 백업한 뒤 'sh ~/doc_platform/scripts/owner_migrate.sh qdrant'(postgres면 원인부터 확인) 후 다시 배포" >&2
    fi
    exit $ok
    ;;
  qdrant)
    kubectl scale deploy/qdrant -n $NS --replicas=0
    kubectl wait --for=delete pod -l app=qdrant -n $NS --timeout=120s || true
    if kubectl get pod -l app=qdrant -n $NS -o name | grep -q .; then echo "qdrant 파드가 아직 있음 — 멈춤" >&2; exit 1; fi
    kubectl delete pod qdrant-chown -n $NS --ignore-not-found
    kubectl apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata: {name: qdrant-chown, namespace: docs, labels: {purpose: owner-migrate}}
spec:
  restartPolicy: Never
  containers:
  - name: chown
    image: qdrant/qdrant:v1.20.0
    imagePullPolicy: IfNotPresent
    command: [sh, -c, "chown -R 1000:1000 /qdrant/storage && echo left: && find /qdrant/storage ! -user 1000 | head -5"]
    securityContext: {runAsUser: 0, allowPrivilegeEscalation: false, capabilities: {drop: [ALL], add: [CHOWN, DAC_READ_SEARCH, FOWNER]}}
    resources: {requests: {cpu: 100m, memory: 64Mi}, limits: {memory: 128Mi}}
    volumeMounts: [{name: data, mountPath: /qdrant/storage}]
  volumes: [{name: data, persistentVolumeClaim: {claimName: qdrant-data}}]
EOF
    kubectl wait --for=jsonpath='{.status.phase}'=Succeeded pod/qdrant-chown -n $NS --timeout=180s \
      || { kubectl logs qdrant-chown -n $NS; kubectl describe pod qdrant-chown -n $NS | tail -20; exit 1; }
    kubectl logs qdrant-chown -n $NS
    kubectl delete pod qdrant-chown -n $NS
    echo "qdrant 저장소를 1000 소유로 바꿈. qdrant는 0인 채 — 이어서 배포(scripts/deploy.ps1)하면 새 매니페스트로 뜬다"
    ;;
  *)
    echo "사용법: sh owner_migrate.sh check|qdrant" >&2; exit 2
    ;;
esac
