#!/bin/sh
# 맥에서: 데모 Keycloak 올리기 (https://github.com/reddol18/docflow/issues/39). 보통은 Windows의 scripts/keycloak.ps1이 복사한 뒤 부른다.
#   sh ~/doc_platform/scripts/keycloak.sh          디스크 확인 → Secret(없을 때만 무작위) → realm ConfigMap → apply → 준비 대기
#   sh ~/doc_platform/scripts/keycloak.sh verify   docflow 파드에서 discovery 문서 받기 (docflow를 새 docs.yaml로 다시 띄운 뒤)
# 만드는 것 (값은 찍지 않는다 — 이름·있음/새로 만듦만):
#   keycloak/keycloak-admin          password                                 관리자 kcadmin (처음 시작할 때만 쓰인다)
#   keycloak/keycloak-realm-secrets  client-secret, demo-pw-hangaram, demo-pw-seonuri   realm 가져오기 자리표시자
#   docs/oidc                        client-secret                            docflow가 읽는다 — 늘 Keycloak 쪽 값과 같게 맞춘다
#   keycloak/keycloak-realm          ConfigMap (k8s/keycloak/docflow-realm.json, 비밀값 없음)
# 무작위 값은 글자·숫자만: docflow는 토큰 교환에서 client id·secret을 HTTP Basic으로 보내는데(httpx) URL 인코딩을 하지 않는다 —
#   기호가 섞이면 Keycloak 쪽 디코딩과 어긋날 수 있다.
# 비밀값을 바꾸려면: Keycloak은 realm이 이미 있으면 가져오기를 건너뛰므로 Secret만 지워 다시 만들어도 Keycloak 안 값은 그대로다 —
#   관리 화면에서 바꾸거나(client secret을 바꾸면 docs/oidc도 같은 값으로, docflow 재시작) PVC째 지우고(kubectl delete namespace keycloak) 다시.
# 이미지는 처음 한 번 받는다(약 253MB 압축, 풀면 그 2배 안팎) → 디스크 확인은 1GB를 더 쓴다고 보고 한다.
set -eu
umask 077
export PATH="/usr/local/bin:$HOME/.local/bin:$PATH"
# 디스크 확인의 시험 값·바꾼 기준은 늘 지운다 (scripts/mac/deploy.sh와 같은 목록 — tests/test_keycloak.py가 본다)
unset DISK_TEST_IMG_PCT DISK_TEST_IMG_SIZE_GB DISK_TEST_VM_AVAIL_GB DISK_TEST_HOST_AVAIL_GB DISK_GC_HIGH_PCT DISK_GC_LOW_PCT DISK_GC_QUERY DISK_GC_MARGIN_PCT DISK_GC_REBUILD_MARGIN_PCT DISK_IMAGE_FS DISK_MIN_VM_GB DISK_MIN_HOST_GB
NS=keycloak
K=~/doc_platform/k8s

if [ "${1:-}" = verify ]; then
  # docflow 파드 안에서 (hostAliases keycloak.localhost → Keycloak Service, NetworkPolicy docflow-egress·keycloak-from-docflow를 지나)
  # discovery 문서를 받아 issuer가 docflow 설정(OIDC_ISSUER)과 같은지 본다. 앱 모듈은 불러오지 않는다(DB 연결 등 부작용 없이)
  kubectl get pods -n $NS </dev/null
  kubectl exec -n docs deploy/docflow -- python -c 'import httpx, os; i = os.environ["OIDC_ISSUER"]; d = httpx.get(i + "/.well-known/openid-configuration", timeout=10).json(); print("issuer 같음:", d["issuer"] == i, d["issuer"]); print("token_endpoint:", d["token_endpoint"]); print("PKCE:", d.get("code_challenge_methods_supported"))' </dev/null
  exit 0
fi

echo "== 디스크 여유 확인 (이미지 처음 받기 몫 1GB)"
sh ~/doc_platform/scripts/disk_check.sh 1 </dev/null || { echo "디스크 여유가 모자람 — Keycloak을 올리지 않음 (위 메시지)" >&2; exit 1; }

D=$(mktemp -d "${TMPDIR:-/tmp}/docflow-kc.XXXXXX")
trap 'rm -rf "$D"' EXIT
trap 'exit 1' HUP INT TERM   # ssh가 끊겨도(HUP) EXIT 트랩으로 임시 폴더를 지운다

if ! kubectl get ns $NS >/dev/null 2>&1 </dev/null; then
  kubectl create namespace $NS >/dev/null </dev/null   # 레이블은 뒤의 apply가 k8s/keycloak.yaml대로 붙인다
  echo "namespace $NS: 새로 만듦"
fi
# 맥 kubectl은 1.15라 '--dry-run=client'가 없을 수 있다 (scripts/mac/tls_secrets.sh와 같은 방식)
DRY=--dry-run=client
kubectl create configmap dry-run-probe --from-literal=a=b $DRY -o name >/dev/null 2>&1 </dev/null || DRY=--dry-run
# put 네임스페이스 종류 이름 <kubectl create 인자…>: 있으면 replace, 없으면 create. 'kubectl apply'는 쓰지 않는다 (값이 주석에 한 벌 더 남음)
put() {
  ns=$1; kind=$2; name=$3; shift 3
  kubectl create "$@" -n "$ns" $DRY -o yaml </dev/null > "$D/obj.yaml"
  if kubectl get "$kind" "$name" -n "$ns" >/dev/null 2>&1 </dev/null; then
    kubectl replace -f "$D/obj.yaml" >/dev/null </dev/null
  else
    kubectl create -f "$D/obj.yaml" >/dev/null </dev/null
  fi
  rm -f "$D/obj.yaml"
}
rand() { head -c 32 /dev/urandom | base64 | tr -d '/+=\n' > "$1"; [ -s "$1" ]; }

if kubectl get secret keycloak-admin -n $NS >/dev/null 2>&1 </dev/null; then
  echo "secret $NS/keycloak-admin: 있음 (그대로)"
else
  rand "$D/admin"
  put $NS secret keycloak-admin secret generic keycloak-admin --from-file=password="$D/admin"
  echo "secret $NS/keycloak-admin: 새로 만듦 (값은 찍지 않음)"
fi
if kubectl get secret keycloak-realm-secrets -n $NS >/dev/null 2>&1 </dev/null; then
  echo "secret $NS/keycloak-realm-secrets: 있음 (그대로)"
else
  rand "$D/cs"; rand "$D/pw1"; rand "$D/pw2"
  put $NS secret keycloak-realm-secrets secret generic keycloak-realm-secrets \
    --from-file=client-secret="$D/cs" --from-file=demo-pw-hangaram="$D/pw1" --from-file=demo-pw-seonuri="$D/pw2"
  echo "secret $NS/keycloak-realm-secrets: 새로 만듦 (값은 찍지 않음)"
fi
# docs/oidc는 늘 Keycloak 쪽 값으로 맞춘다 (파일로만 옮기고 찍지 않는다)
kubectl get secret keycloak-realm-secrets -n $NS -o 'jsonpath={.data.client-secret}' </dev/null | base64 -D > "$D/cs-docs"
[ -s "$D/cs-docs" ] || { echo "keycloak-realm-secrets의 client-secret을 읽지 못함" >&2; exit 1; }
put docs secret oidc secret generic oidc --from-file=client-secret="$D/cs-docs"
echo "secret docs/oidc: Keycloak client secret과 같게 맞춤 (값은 찍지 않음)"

put $NS configmap keycloak-realm configmap keycloak-realm --from-file=docflow-realm.json="$K/keycloak/docflow-realm.json"
echo "configmap $NS/keycloak-realm: k8s/keycloak/docflow-realm.json (realm이 이미 있으면 Keycloak이 가져오기를 건너뛴다)"

kubectl apply -f "$K/keycloak.yaml" </dev/null
# 처음엔 이미지 받기 + 첫 시작(H2 만들기·realm 가져오기)으로 몇 분 걸린다
if ! kubectl rollout status deploy/keycloak -n $NS --timeout=600s </dev/null; then
  kubectl get pods -n $NS </dev/null || true
  kubectl logs deploy/keycloak -n $NS --tail=60 </dev/null || true
  exit 1
fi
kubectl get pods -n $NS </dev/null
kubectl logs deploy/keycloak -n $NS --tail=5 </dev/null || true
echo "== 다음: docflow를 새 k8s/docs.yaml로 다시 띄운 뒤(scripts/deploy.ps1) sh ~/doc_platform/scripts/keycloak.sh verify"
