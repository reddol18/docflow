#!/bin/sh
# 맥에서: 데모 Keycloak 올리기 (https://github.com/reddol18/docflow/issues/39). 보통은 Windows의 scripts/keycloak.ps1이 복사한 뒤 부른다.
#   sh ~/doc_platform/scripts/keycloak.sh          디스크 확인 → clusterIP 확인 → Secret → realm ConfigMap → apply → 준비 대기
#                                                  → (그 뒤에만) docs/oidc 맞추기 → docflow가 지금 Secret 판으로 떠 있게
#   sh ~/doc_platform/scripts/keycloak.sh verify   docflow 파드에서 OIDC 켜짐(Secret 파일·/auth/config)·discovery(issuer·주소 셋의 출처) — 아니면 종료 코드 1
# 배치: 이 파일·kube_put.sh·disk_check.sh는 ~/doc_platform/scripts, 매니페스트는 ~/doc_platform/k8s (이 파일 폴더의 ../k8s).
#
# 비밀값의 정본은 Secret keycloak/keycloak-realm-secrets 하나다 (값은 찍지 않는다 — 이름·있음/새로 만듦만):
#   keycloak/keycloak-admin          password                                 관리자 kcadmin (master realm을 처음 만들 때만 쓰인다)
#   keycloak/keycloak-realm-secrets  client-secret, demo-pw-hangaram, demo-pw-seonuri   realm 가져오기 자리표시자
#   docs/oidc                        client-secret                            docflow가 읽는다 — 정본과 다를 때만 바꾼다. docflow는 판(resourceVersion)이 다르면 다시 띄운다
#   keycloak/keycloak-realm          ConfigMap (k8s/keycloak/docflow-realm.json, 비밀값 없음)
# 값을 돌리지(rotate) 않는다: Keycloak은 realm이 이미 있으면 가져오기를 건너뛰므로, realm이 든 PVC(keycloak-data)가 있는데 Secret이 없으면
#   새 무작위 값은 Keycloak 안 값과 어긋난다 → 만들지 않고 멈춘다(되살리는 길을 찍는다). Secret은 PVC가 없을 때(처음)만 만든다.
#   값을 바꾸려면 관리 화면에서 바꾸고 Secret도 같은 값으로(client secret이면 이 스크립트를 다시 → docs/oidc·docflow 재시작),
#   또는 kubectl delete namespace keycloak 뒤 다시(전부 새로).
# 무작위 값은 글자·숫자만: docflow는 토큰 교환에서 client id·secret을 HTTP Basic으로 보내는데(httpx) URL 인코딩을 하지 않는다.
# 이미지는 처음 한 번 받는다(약 253MB 압축, 풀면 그 2배 안팎) → 디스크 확인은 1GB를 더 쓴다고 보고 한다.
# 시험: tests/test_keycloak.py가 가짜 kubectl(tests/fixtures/fakekube)로 분기를 돌린다.
set -eu
umask 077
export PATH="/usr/local/bin:$HOME/.local/bin:$PATH"
# 디스크 확인의 시험 값·바꾼 기준은 늘 지운다 (scripts/mac/deploy.sh와 같은 목록 — tests/test_keycloak.py가 본다)
unset DISK_TEST_IMG_PCT DISK_TEST_IMG_SIZE_GB DISK_TEST_VM_AVAIL_GB DISK_TEST_HOST_AVAIL_GB DISK_GC_HIGH_PCT DISK_GC_LOW_PCT DISK_GC_QUERY DISK_GC_MARGIN_PCT DISK_GC_REBUILD_MARGIN_PCT DISK_IMAGE_FS DISK_MIN_VM_GB DISK_MIN_HOST_GB
NS=keycloak
HERE=$(cd "$(dirname "$0")" && pwd)
K="$HERE/../k8s"
URL39=https://github.com/reddol18/docflow/issues/39
# verify가 docflow 파드 안에서 돌리는 확인 (아래 'verify'). 시험(tests/test_keycloak.py)이 이 글자를 꺼내 가짜 응답으로 돌린다
VERIFY_PY='
import os, sys, httpx
from urllib.parse import urlsplit
def origin(u):
    p = urlsplit(u)
    return (p.scheme, (p.hostname or "").lower(), p.port or {"http": 80, "https": 443}.get(p.scheme))
ok = True
f = os.environ.get("OIDC_CLIENT_SECRET_FILE", "")
if f and os.path.isfile(f) and os.path.getsize(f) > 0:
    print("client secret 파일: 있음 (비어 있지 않음)")
else:
    print("client secret 파일: **없음·빔** (Secret docs/oidc → docflow 재시작)"); ok = False
modes = httpx.get("http://127.0.0.1:8000/auth/config", timeout=10).json().get("modes") or []
print("docflow /auth/config modes:", modes, "" if "oidc" in modes else "**oidc 꺼짐**")
ok = ok and "oidc" in modes
i = os.environ.get("OIDC_ISSUER", "")
d = httpx.get(i.rstrip("/") + "/.well-known/openid-configuration", timeout=10).json() if i else {}
bad = [k for k in ("authorization_endpoint", "token_endpoint", "jwks_uri") if not isinstance(d.get(k), str) or origin(d[k]) != origin(i)]
print("issuer:", d.get("issuer"), "같음" if i and d.get("issuer") == i else "**다름** (설정 " + i + ")")
print("주소 출처:", "issuer와 같음" if not bad else "**다름** " + ", ".join(bad))
print("PKCE:", d.get("code_challenge_methods_supported"))
sys.exit(0 if ok and i and d.get("issuer") == i and not bad else 1)
'

if [ "${1:-}" = verify ]; then
  # docflow 파드 안에서 (hostAliases keycloak.localhost → Keycloak Service, NetworkPolicy docflow-to-keycloak·keycloak-from-docflow를 지나)
  # discovery 문서를 받아 app/auth.py check_discovery와 같은 규칙으로 본다: issuer가 OIDC_ISSUER와 같고, docflow가 따라가는 주소
  # (authorization_endpoint·token_endpoint·jwks_uri)의 scheme·호스트·포트가 issuer와 같다.
  # 그리고 docflow가 OIDC를 실제로 켰는지: client secret 파일이 비어 있지 않고(크기만 본다 — 값은 읽어 찍지 않음),
  # 파드 안 /auth/config(루프백)의 modes에 oidc가 있다 (Secret 없이 떠서 OIDC를 끈 docflow면 실패). 하나라도 아니면 종료 코드 1.
  # 앱 모듈은 불러오지 않는다(DB 연결 등 부작용 없이)
  kubectl get pods -n $NS </dev/null
  kubectl exec -n docs deploy/docflow -- python -c "$VERIFY_PY" </dev/null \
    || { echo "!! 확인 실패 — docflow가 SSO를 시작하지 못한다 (위 **표시** 줄, $URL39)" >&2; exit 1; }
  exit 0
fi

echo "== 디스크 여유 확인 (이미지 처음 받기 몫 1GB)"
sh "$HERE/disk_check.sh" 1 </dev/null || { echo "디스크 여유가 모자람 — Keycloak을 올리지 않음 (위 메시지)" >&2; exit 1; }

D=$(mktemp -d "${TMPDIR:-/tmp}/docflow-kc.XXXXXX")
trap 'rm -rf "$D"' EXIT
trap 'exit 1' HUP INT TERM   # ssh가 끊겨도(HUP) EXIT 트랩으로 임시 폴더를 지운다
. "$HERE/kube_put.sh"   # put·DRY (tls_secrets.sh와 같이 씀)
# base64 풀기: 맥은 -D, GNU는 -d
if printf 'YQ==' | base64 -D >/dev/null 2>&1; then B64D="base64 -D"; else B64D="base64 -d"; fi
stop() { echo "!! $*" >&2; exit 1; }

# 고정 clusterIP (k8s/keycloak.yaml Service, k8s/docs.yaml hostAliases) — 가정: 클러스터 서비스 대역 안이고 다른 Service가 쓰지 않는다
IP=$(sed -n 's/.*clusterIP:[[:space:]]*\([0-9][0-9.]*\).*/\1/p' "$K/keycloak.yaml" | head -1)
[ -n "$IP" ] || stop "k8s/keycloak.yaml에서 clusterIP를 찾지 못함"
CIDRS=$(kubectl get servicecidrs -o 'jsonpath={range .items[*]}{.spec.cidrs}{" "}{end}' </dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+' || true)
[ -n "$CIDRS" ] || stop "서비스 대역(ServiceCIDR)을 읽지 못함 — clusterIP $IP가 대역 안인지 모름. 멈춤"
in_cidr() {  # IPv4 $1이 $2(a.b.c.d/n) 안이면 0
  awk -v ip="$1" -v c="$2" 'function n(s,  a) { split(s, a, "."); return ((a[1] * 256 + a[2]) * 256 + a[3]) * 256 + a[4] }
    BEGIN { split(c, p, "/"); m = 2 ^ (32 - p[2]); exit !(int(n(ip) / m) == int(n(p[1]) / m)) }'
}
ok=""
for c in $CIDRS; do if in_cidr "$IP" "$c"; then ok=$c; fi; done
[ -n "$ok" ] || stop "clusterIP $IP가 서비스 대역($(echo $CIDRS)) 밖 — k8s/keycloak.yaml Service와 k8s/docs.yaml hostAliases를 그 대역의 빈 IP로 바꾼다"
# 목록과 종료 상태를 따로 받는다 (파이프면 kubectl 실패가 awk의 성공에 묻혀 '비어 있음'으로 읽힌다) — 못 읽으면 멈춘다
SVCS=$(kubectl get svc -A -o 'jsonpath={range .items[*]}{.metadata.namespace}/{.metadata.name} {.spec.clusterIP}{"\n"}{end}' </dev/null) \
  || stop "Service 목록을 읽지 못함 — clusterIP $IP가 비어 있는지 모름. 멈춤"
OWNER=$(printf '%s\n' "$SVCS" | awk -v ip="$IP" '$2 == ip { print $1 }')
[ -z "$OWNER" ] || [ "$OWNER" = "$NS/keycloak" ] || stop "clusterIP $IP를 이미 $OWNER가 쓴다 — k8s/keycloak.yaml Service와 k8s/docs.yaml hostAliases를 다른 빈 IP로"
echo "== clusterIP $IP: 서비스 대역 $ok 안, $( [ -n "$OWNER" ] && echo "이미 $OWNER" || echo "비어 있음")"

if ! kubectl get ns $NS >/dev/null 2>&1 </dev/null; then
  kubectl create namespace $NS >/dev/null </dev/null   # 레이블은 뒤의 apply가 k8s/keycloak.yaml대로 붙인다
  echo "namespace $NS: 새로 만듦"
fi
rand() { head -c 32 /dev/urandom | base64 | tr -d '/+=\n' > "$1"; [ -s "$1" ]; }
HAS_PVC=""
if kubectl get pvc keycloak-data -n $NS >/dev/null 2>&1 </dev/null; then HAS_PVC=1; fi
# Secret이 없을 때: PVC가 없으면(처음) 만들고, 있으면(realm이 이미 들어 있을 수 있음) 멈춘다
need_secret() {  # $1 이름, $2 되살릴 때의 --from-file 키들
  if kubectl get secret "$1" -n $NS >/dev/null 2>&1 </dev/null; then echo "secret $NS/$1: 있음 (그대로)"; return 1; fi
  [ -z "$HAS_PVC" ] || stop "Secret $NS/$1이 없는데 PVC $NS/keycloak-data가 있음 — 그 안의 Keycloak(realm·관리자)은 옛 값을 쓰므로 새 무작위 값을 만들지 않는다. 멈춤.
   전부 새로: kubectl delete namespace keycloak (H2·Secret 함께 지움) 뒤 이 스크립트를 다시
   옛 값으로 되살리기: kubectl create secret generic $1 -n $NS $2 (각 파일에 옛 값) 뒤 다시 ($URL39)"
  return 0
}
if need_secret keycloak-admin "--from-file=password=<파일>"; then
  rand "$D/admin"
  put $NS secret keycloak-admin secret generic keycloak-admin --from-file=password="$D/admin"
  echo "secret $NS/keycloak-admin: 새로 만듦 (값은 찍지 않음)"
fi
if need_secret keycloak-realm-secrets "--from-file=client-secret=<파일> --from-file=demo-pw-hangaram=<파일> --from-file=demo-pw-seonuri=<파일>"; then
  rand "$D/cs"; rand "$D/pw1"; rand "$D/pw2"
  put $NS secret keycloak-realm-secrets secret generic keycloak-realm-secrets \
    --from-file=client-secret="$D/cs" --from-file=demo-pw-hangaram="$D/pw1" --from-file=demo-pw-seonuri="$D/pw2"
  echo "secret $NS/keycloak-realm-secrets: 새로 만듦 (값은 찍지 않음)"
fi
# realm JSON이 바뀌었는데 realm이 이미 있으면(PVC) Keycloak은 가져오기를 건너뛴다 — 바뀐 매퍼·그룹이 들어가지 않으니 알린다
if [ -n "$HAS_PVC" ] && kubectl get configmap keycloak-realm -n $NS >/dev/null 2>&1 </dev/null; then
  kubectl get configmap keycloak-realm -n $NS -o 'jsonpath={.data.docflow-realm\.json}' </dev/null > "$D/realm-old" || : > "$D/realm-old"
  if [ "$(cat "$D/realm-old")" != "$(cat "$K/keycloak/docflow-realm.json")" ]; then
    echo "!! realm JSON이 바뀌었지만 Keycloak에는 realm docflow가 이미 있어 가져오기를 건너뛴다 (매퍼·그룹·클라이언트 변경이 들어가지 않음)." >&2
    echo "!!   넣으려면: kubectl delete namespace keycloak 뒤 이 스크립트를 다시 (Secret도 새로 — docs/oidc·docflow도 맞춰진다), 또는 관리 화면에서 realm docflow를 지우고 kubectl rollout restart deploy/keycloak -n keycloak ($URL39)" >&2
  fi
fi
put $NS configmap keycloak-realm configmap keycloak-realm --from-file=docflow-realm.json="$K/keycloak/docflow-realm.json"
echo "configmap $NS/keycloak-realm: k8s/keycloak/docflow-realm.json (realm이 이미 있으면 Keycloak이 가져오기를 건너뛴다)"

kubectl apply -f "$K/keycloak.yaml" </dev/null
# 처음엔 이미지 받기 + 첫 시작(H2 만들기·realm 가져오기)으로 몇 분 걸린다
if ! kubectl rollout status deploy/keycloak -n $NS --timeout=600s </dev/null; then
  kubectl get pods -n $NS </dev/null || true
  kubectl logs deploy/keycloak -n $NS --tail=60 </dev/null || true
  echo "!! Keycloak이 준비되지 않음 — docs/oidc·docflow는 건드리지 않았다. 고친 뒤 이 스크립트를 다시" >&2
  exit 1
fi
kubectl get pods -n $NS </dev/null
kubectl logs deploy/keycloak -n $NS --tail=5 </dev/null || true

# docflow 쪽은 Keycloak이 준비된 뒤에만 건드린다.
# ① docs/oidc는 정본(keycloak-realm-secrets)과 다를 때만 바꾼다 (파일로만 옮기고 찍지 않는다)
kubectl get secret keycloak-realm-secrets -n $NS -o 'jsonpath={.data.client-secret}' </dev/null | $B64D > "$D/cs-kc"
[ -s "$D/cs-kc" ] || stop "keycloak-realm-secrets의 client-secret을 읽지 못함"
: > "$D/cs-docs"
if kubectl get secret oidc -n docs >/dev/null 2>&1 </dev/null; then
  kubectl get secret oidc -n docs -o 'jsonpath={.data.client-secret}' </dev/null | $B64D > "$D/cs-docs"
fi
if cmp -s "$D/cs-kc" "$D/cs-docs"; then
  echo "secret docs/oidc: Keycloak client secret과 같음 (그대로)"
else
  put docs secret oidc secret generic oidc --from-file=client-secret="$D/cs-kc"
  echo "secret docs/oidc: Keycloak client secret과 같게 바꿈 (값은 찍지 않음)"
fi
rm -f "$D/cs-kc" "$D/cs-docs"
# ② docflow는 client secret을 시작할 때 한 번 읽는다. 이번 실행에서 바꿨는지가 아니라 '떠 있는 docflow가 지금 Secret 판으로
#    시작했는지'로 정한다: 파드 템플릿 주석 docflow-oidc-secret-version에 Secret의 resourceVersion을 적고(patch → 새 파드로 롤아웃),
#    다르면 다시 맞춘다. 그래서 바꾼 뒤 재시작 전에 끊긴 실행도 다음 실행이 맞춘다. kubectl 1.15에도 있는 get·patch만 쓴다.
#    배포(kubectl apply -f k8s/docs.yaml)는 매니페스트에 없는 이 주석을 지우지 않는다(3-way 병합)
if VOLS=$(kubectl get deploy docflow -n docs -o 'jsonpath={.spec.template.spec.volumes[*].secret.secretName}' </dev/null 2>/dev/null) \
    && printf ' %s ' "$VOLS" | grep -q ' oidc '; then
  RV=$(kubectl get secret oidc -n docs -o 'jsonpath={.metadata.resourceVersion}' </dev/null) || stop "docs/oidc 판(resourceVersion)을 읽지 못함"
  CUR=$(kubectl get deploy docflow -n docs -o 'jsonpath={.spec.template.metadata.annotations.docflow-oidc-secret-version}' </dev/null) \
    || stop "docflow Deployment를 읽지 못함"
  if [ "$RV" = "$CUR" ]; then
    echo "== docflow: 지금 docs/oidc 판($RV)으로 떠 있음 (재시작 없음)"
  else
    echo "== docflow 재시작: docs/oidc 판 ${CUR:-기록 없음} → $RV"
    kubectl patch deploy docflow -n docs -p "{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{\"docflow-oidc-secret-version\":\"$RV\"}}}}}" </dev/null >/dev/null
    kubectl rollout status deploy/docflow -n docs --timeout=300s </dev/null \
      || stop "docflow 재시작이 준비되지 않음 — kubectl logs deploy/docflow -n docs (고친 뒤 이 스크립트를 다시 부르면 다시 맞춘다)"
  fi
else
  echo "== docflow는 아직 Secret oidc를 마운트하지 않음 — 다음 배포(scripts/deploy.ps1)에서 읽는다"
fi
echo "== 다음: docflow가 새 k8s/docs.yaml로 떠 있으면 sh ~/doc_platform/scripts/keycloak.sh verify"
