#!/bin/sh
# 맥에서: PVC 데이터 소유자를 k8s/docs.yaml의 runAsUser에 맞춘다 (https://github.com/reddol18/docflow/issues/29).
# k3s local-path PVC는 hostPath라 fsGroup이 듣지 않는다 — 컨테이너를 root에서 일반 사용자로 바꾸면 예전 파일을 못 쓴다.
#   postgres  uid 70   — 예전 root 진입 스크립트가 이미 70으로 바꿔 두었다 (보통 고칠 것 없음, check로 확인만)
#   qdrant    uid 1000 — 예전에 root로 돌아 저장소 파일이 root 소유. 처음 한 번 넘긴다
#   docflow   uid 1000 — 전부터 runAsUser 1000 (check로 확인만)
#
#   sh owner_migrate.sh check     # 소유자가 다른 파일이 있거나 확인하지 못하면 종료 코드 1 (deploy.sh가 apply 전에 부른다)
#                                 # 준비된 파드가 있으면 그 안에서(exec), 없으면(0으로 줄임 등) 읽기 전용 확인 파드로 본다.
#                                 # Deployment가 없으면(kubectl 오류) 실패 — 처음 설치는 README '실행'대로 kubectl apply로 한다
#   sh owner_migrate.sh qdrant    # qdrant를 0으로 줄이고, root 일회용 파드로 chown -R 1000:1000 — 남은 파일이 있으면 실패.
#                                 # qdrant는 0인 채로 둔다 — 이어서 deploy(새 매니페스트 apply)가 1000으로 띄운다.
#                                 # 0인 동안 옛 매니페스트(root)로 다시 띄우면 새 파일이 root 소유로 생기니 바로 배포한다.
#                                 # 여러 번 돌려도 같다 (이미 1000이면 바뀌는 것 없음)
# 판정: 마운트 폴더 자체는 local-path가 만든 root 0777(docflow는 0:1000 2777)이라 소유자가 아니라 '그 uid가 쓸 수 있는지'
#       (그 uid 소유이거나 누구나 쓰기)로 보고, 그 아래 파일·폴더는 모두 그 uid 소유여야 한다.
# 일회용 파드(확인·chown)는 실행 중인 Deployment의 이미지를 그대로 쓴다(새로 받지 않음). seccomp RuntimeDefault·읽기 전용 루트·
#   SA 토큰 없음·capability는 꼭 필요한 것만(확인 DAC_READ_SEARCH, chown CHOWN·DAC_READ_SEARCH). 확인 파드는 PVC를 읽기 전용으로 붙인다.
# 매 시작마다 도는 init 컨테이너로 두지 않는 이유: 파드가 root 권한을 계속 갖게 되고(trivy KSV-0118) 한 번이면 된다.
# 되돌리기: 옛 매니페스트를 apply하면 root(DAC_OVERRIDE)는 1000 소유 파일도 읽고 쓴다 — 소유자를 되돌릴 필요 없음.
#           다만 root로 돈 동안 생긴 파일은 root 소유라 다시 앞으로 올 때 이 스크립트를 한 번 더 (check가 막아 준다).
set -eu
export PATH="/usr/local/bin:$HOME/.local/bin:$PATH"
NS=docs
HERE=$(cd "$(dirname "$0")" && pwd)

# 일회용 파드를 띄우기 전에 디스크 여유부터 (records_poc/CLAUDE.md 디스크 규칙)
sh "$HERE/disk_check.sh"

# 서비스 → "마운트 경로 볼 경로 uid PVC"
spec_of() {
  case "$1" in
    postgres) echo "/var/lib/postgresql/data /var/lib/postgresql/data/pg 70 pg-data" ;;
    qdrant)   echo "/qdrant/storage /qdrant/storage 1000 qdrant-data" ;;
    docflow)  echo "/data /data 1000 docflow-data" ;;
  esac
}

# 볼 경로 아래(폴더 자체 제외) uid가 아닌 것은 "BAD 경로", 폴더 자체를 그 uid가 못 쓰면 "BADROOT 경로", 끝까지 보면 "SCAN-OK"
# find가 하나라도 실패하면(못 읽는 폴더 등) SCAN-OK가 없다 → 확인 못 함
scan_cmd() {  # 볼경로 uid
  echo "set -e; p=$1; u=$2; find \"\$p\" -maxdepth 0 \\( -user \"\$u\" -o -perm -0002 \\) | grep -q . || echo \"BADROOT \$p\";" \
       "find \"\$p\" -mindepth 1 ! -user \"\$u\" -exec echo BAD {} \\;; echo SCAN-OK"
}

deploy_field() {  # 서비스 jsonpath — kubectl 오류면 실패
  kubectl get deploy "$1" -n $NS -o "jsonpath=$2"
}

wait_pod() {  # 파드 이름 — Succeeded면 0, Failed·시간 초과면 1
  i=0
  while [ $i -lt 90 ]; do
    ph=$(kubectl get pod "$1" -n $NS -o jsonpath='{.status.phase}')
    case "$ph" in
      Succeeded) return 0 ;;
      Failed) echo "$1: Failed" >&2; return 1 ;;
    esac
    i=$((i + 1)); sleep 2
  done
  echo "$1: 180초 안에 끝나지 않음 (상태 $ph)" >&2
  return 1
}

run_pod() {  # 이름 서비스 마운트 PVC 읽기전용(true|false) 추가capability(JSON 목록) 명령 → 로그를 표준 출력으로. 실패면 1
  name=$1; svc=$2; mnt=$3; pvc=$4; ro=$5; caps=$6; cmd=$7
  img=$(deploy_field "$svc" '{.spec.template.spec.containers[0].image}')
  pull=$(deploy_field "$svc" '{.spec.template.spec.containers[0].imagePullPolicy}')
  [ -n "$img" ] || { echo "$svc: 이미지를 읽지 못함" >&2; return 1; }
  [ "$pull" = Never ] || pull=IfNotPresent
  kubectl delete pod "$name" -n $NS --ignore-not-found >/dev/null
  # 명령은 JSON 문자열로 넣는다 (따옴표·역슬래시 이스케이프)
  jcmd=$(printf '%s' "$cmd" | sed 's/\\/\\\\/g; s/"/\\"/g')
  kubectl apply -f - >/dev/null <<EOF
{"apiVersion": "v1", "kind": "Pod",
 "metadata": {"name": "$name", "namespace": "$NS", "labels": {"purpose": "owner-migrate"}},
 "spec": {"restartPolicy": "Never", "automountServiceAccountToken": false,
  "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
  "containers": [{"name": "run", "image": "$img", "imagePullPolicy": "$pull", "command": ["sh", "-c", "$jcmd"],
   "securityContext": {"runAsUser": 0, "allowPrivilegeEscalation": false, "readOnlyRootFilesystem": true,
                       "capabilities": {"drop": ["ALL"], "add": $caps}},
   "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}, "limits": {"memory": "128Mi"}},
   "volumeMounts": [{"name": "data", "mountPath": "$mnt", "readOnly": $ro}]}],
  "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "$pvc", "readOnly": $ro}}]}}
EOF
  rc=0
  wait_pod "$name" || rc=1
  kubectl logs "$name" -n $NS || rc=1
  [ $rc = 0 ] || kubectl describe pod "$name" -n $NS | tail -15 >&2 || true
  kubectl delete pod "$name" -n $NS --ignore-not-found >/dev/null || true
  return $rc
}

check_one() {  # 서비스
  set -- "$1" $(spec_of "$1")
  svc=$1; mnt=$2; path=$3; uid=$4; pvc=$5
  ready=$(deploy_field "$svc" '{.status.readyReplicas}') || { echo "$svc: Deployment를 읽지 못함" >&2; return 1; }
  if [ "${ready:-0}" -ge 1 ]; then
    how="실행 중인 파드"
    out=$(kubectl exec -n $NS "deploy/$svc" -- sh -c "$(scan_cmd "$path" "$uid")") || out="$out
(exec 실패)"
  else
    how="읽기 전용 확인 파드"
    out=$(run_pod "owner-check-$svc" "$svc" "$mnt" "$pvc" true '["DAC_READ_SEARCH"]' "$(scan_cmd "$path" "$uid")") || out="$out
(확인 파드 실패)"
  fi
  if ! echo "$out" | grep -x SCAN-OK >/dev/null; then
    echo "$svc: $path 소유자를 확인하지 못함 ($how):" >&2
    echo "$out" | tail -5 >&2
    return 1
  fi
  if echo "$out" | grep -q '^BAD'; then
    echo "$svc: $path 에 uid $uid 가 쓸 수 없는 폴더나 다른 소유자 파일이 있음 ($how, 예):" >&2
    echo "$out" | grep '^BAD' | head -5 >&2
    return 1
  fi
  echo "$svc: $path 소유자 uid $uid 확인 ($how)"
}

case "${1:-}" in
  check)
    ok=0
    check_one postgres || ok=1
    check_one qdrant || ok=1
    check_one docflow || ok=1
    if [ $ok = 1 ]; then
      echo "소유자가 매니페스트와 다르거나 확인하지 못했습니다. 백업한 뒤 'sh ~/doc_platform/scripts/owner_migrate.sh qdrant'(postgres·docflow면 원인부터 확인) 후 다시 배포" >&2
    fi
    exit $ok
    ;;
  qdrant)
    deploy_field qdrant '{.spec.template.spec.containers[0].image}' >/dev/null
    kubectl scale deploy/qdrant -n $NS --replicas=0
    kubectl wait --for=delete pod -l app=qdrant -n $NS --timeout=120s || true
    if kubectl get pod -l app=qdrant -n $NS -o name | grep -q .; then echo "qdrant 파드가 아직 있음 — 멈춤" >&2; exit 1; fi
    # chown 뒤 1000이 아닌 것이 남으면 실패 (test -z)
    run_pod owner-chown-qdrant qdrant /qdrant/storage qdrant-data false '["CHOWN", "DAC_READ_SEARCH"]' \
      'chown -R 1000:1000 /qdrant/storage && left=$(find /qdrant/storage ! -user 1000) && test -z "$left" && echo chown-ok' \
      | tee /dev/stderr | grep -x chown-ok >/dev/null || { echo "chown 실패 또는 1000이 아닌 파일이 남음 — qdrant는 0인 채" >&2; exit 1; }
    echo "qdrant 저장소를 1000 소유로 바꿈. qdrant는 0인 채 — 이어서 배포(scripts/deploy.ps1)하면 새 매니페스트로 뜬다"
    ;;
  *)
    echo "사용법: sh owner_migrate.sh check|qdrant" >&2; exit 2
    ;;
esac
