#!/bin/sh
# 맥에서: TLS 인증서·키를 docs 네임스페이스 Secret으로 넣는다 (https://github.com/reddol18/docflow/issues/48).
# Windows의 scripts/tls_secrets.ps1이 ssh 표준 입력으로 "<이름> <base64>" 줄을 넘긴다 — 직접 부르지 않는다.
# 키 값은 화면에 찍지 않는다 (인증서 지문·만료만). 받은 파일은 umask 077 임시 폴더에만 두고 끝나면(실패·끊김에도) 지운다.
#   tls-web·tls-postgres·tls-qdrant·tls-redis   kubernetes.io/tls (서비스마다 따로 — 하나가 새도 다른 이름을 흉내 내지 못하게)
#   docflow-ca                        ConfigMap (CA 인증서, 공개 값 — docflow가 Postgres·Qdrant 인증서를 확인)
#   qdrant-api-key                    없을 때만 무작위로 만든다 (32바이트). 바꾸려면 지우고 다시 부른 뒤 qdrant·docflow 재시작
#   redis-auth                        대기열 Redis 비밀번호 (https://github.com/reddol18/docflow/issues/5) — 같은 방식. 바꾸면
#                                     redis·docflow·docflow-worker 재시작
# 새 클러스터: docs 네임스페이스가 없으면 만든다 (레이블은 뒤의 apply가 k8s/docs.yaml대로 붙인다) — deploy.sh는 이 Secret이 있어야 apply한다.
# 교체는 원자적: 있으면 replace(한 번에 바꿈), 없으면 create. 'kubectl apply'는 쓰지 않는다 — last-applied 주석에 키가 한 벌 더
#   남아 'kubectl describe'·'get -o yaml'의 주석으로 보인다. 맥 kubectl은 1.15라 '--dry-run=client'가 없다(2026-11-10 확인) → 되는 쪽을 고른다.
# 이미 떠 있는 파드는 새 인증서를 바로 읽지 않는다 (qdrant는 cert_ttl 3600초마다) — 갱신이면 postgres·web을 다시 띄운다.
set -eu
umask 077
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
NS=docs
HERE=$(cd "$(dirname "$0")" && pwd)
D=$(mktemp -d "${TMPDIR:-/tmp}/docflow-tls.XXXXXX")
trap 'rm -rf "$D"' EXIT
trap 'exit 1' HUP INT TERM PIPE   # ssh가 끊겨도(HUP, 출력이 막히면 PIPE) EXIT 트랩으로 임시 폴더를 지운다
if ! kubectl get ns $NS >/dev/null 2>&1 </dev/null; then
  kubectl create namespace $NS >/dev/null </dev/null
  echo "namespace $NS: 새로 만듦"
fi
# put·DRY: 같은 폴더의 kube_put.sh (keycloak.sh와 같이 씀)
. "$HERE/kube_put.sh"
# 표준 입력은 scripts/lib/remote.ps1 Invoke-Mac이 BOM 없이 넘긴다 (https://github.com/reddol18/docflow/issues/117) — BOM 떼기는 지웠다.
# CR 떼기는 남긴다: 손으로 CRLF 파일을 넘겨도 base64 끝에 CR이 붙지 않게 (해 없는 보호)
tr -d '\r' | while read -r n b; do
  [ -n "$n" ] || continue
  case "$n" in
    ca.crt|web.crt|web.key|postgres.crt|postgres.key|qdrant.crt|qdrant.key|redis.crt|redis.key) ;;
    *) echo "모르는 이름: $n" >&2; exit 1 ;;
  esac
  printf '%s' "$b" | base64 -D > "$D/$n"
done
grep -q 'BEGIN CERTIFICATE' "$D/ca.crt" || { echo "ca.crt가 없거나 인증서가 아님" >&2; exit 1; }
for s in web postgres qdrant redis; do
  grep -q 'BEGIN CERTIFICATE' "$D/$s.crt" 2>/dev/null || { echo "$s.crt가 없거나 인증서가 아님" >&2; exit 1; }
  grep -q 'PRIVATE KEY' "$D/$s.key" 2>/dev/null || { echo "$s.key가 없거나 키가 아님" >&2; exit 1; }
  # 키와 인증서가 짝인지, CA로 확인되는지 (넣기 전에)
  [ "$(openssl x509 -noout -pubkey -in "$D/$s.crt")" = "$(openssl pkey -pubout -in "$D/$s.key")" ] \
    || { echo "$s: 키와 인증서가 짝이 아님" >&2; exit 1; }
  openssl verify -CAfile "$D/ca.crt" "$D/$s.crt" >/dev/null || { echo "$s: CA로 확인되지 않음" >&2; exit 1; }
done
for s in web postgres qdrant redis; do
  put $NS secret "tls-$s" secret tls "tls-$s" --cert="$D/$s.crt" --key="$D/$s.key"
  fp=$(openssl x509 -noout -fingerprint -sha256 -in "$D/$s.crt" | cut -d= -f2)
  end=$(openssl x509 -noout -enddate -in "$D/$s.crt" | cut -d= -f2)
  echo "secret $NS/tls-$s: SHA-256 $fp, 만료 $end"
done
put $NS configmap docflow-ca configmap docflow-ca --from-file=ca.crt="$D/ca.crt"
echo "configmap $NS/docflow-ca: SHA-256 $(openssl x509 -noout -fingerprint -sha256 -in "$D/ca.crt" | cut -d= -f2)"
if kubectl get secret qdrant-api-key -n $NS >/dev/null 2>&1 </dev/null; then
  echo "secret $NS/qdrant-api-key: 있음 (그대로)"
else
  head -c 32 /dev/urandom | base64 | tr -d '/+=\n' > "$D/api-key"
  put $NS secret qdrant-api-key secret generic qdrant-api-key --from-file=api-key="$D/api-key"
  echo "secret $NS/qdrant-api-key: 새로 만듦 (값은 찍지 않음)"
fi
if kubectl get secret redis-auth -n $NS >/dev/null 2>&1 </dev/null; then
  echo "secret $NS/redis-auth: 있음 (그대로)"
else
  head -c 32 /dev/urandom | base64 | tr -d '/+=\n' > "$D/redis-pass"
  put $NS secret redis-auth secret generic redis-auth --from-file=password="$D/redis-pass"
  echo "secret $NS/redis-auth: 새로 만듦 (값은 찍지 않음)"
fi
