# Secret·ConfigMap 넣기 — tls_secrets.sh·keycloak.sh가 불러 쓴다 (. "$HERE/kube_put.sh"). 혼자 실행하지 않는다.
# 부르는 쪽이 먼저 $D(umask 077 임시 폴더, 끝나면 지움)를 만든다.
# 'kubectl apply'는 쓰지 않는다 — last-applied 주석에 값이 한 벌 더 남아 'kubectl describe'·'get -o yaml'의 주석으로 보인다.
# 맥 kubectl은 1.15라 '--dry-run=client'가 없다(2026-11-10 확인) → 되는 쪽을 고른다.
DRY=--dry-run=client
kubectl create configmap dry-run-probe --from-literal=a=b $DRY -o name >/dev/null 2>&1 </dev/null || DRY=--dry-run

# put 네임스페이스 종류 이름 <kubectl create 인자…>: 오브젝트를 만들어(클러스터에 보내지 않고) 있으면 replace(한 번에 바꿈), 없으면 create
put() {
  put_ns=$1; put_kind=$2; put_name=$3; shift 3
  kubectl create "$@" -n "$put_ns" $DRY -o yaml </dev/null > "$D/obj.yaml"
  if kubectl get "$put_kind" "$put_name" -n "$put_ns" >/dev/null 2>&1 </dev/null; then
    kubectl replace -f "$D/obj.yaml" >/dev/null </dev/null
  else
    kubectl create -f "$D/obj.yaml" >/dev/null </dev/null
  fi
  rm -f "$D/obj.yaml"
}
