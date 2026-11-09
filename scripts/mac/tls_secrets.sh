#!/bin/sh
# 맥에서: TLS 인증서·키를 docs 네임스페이스 Secret으로 넣는다 (https://github.com/reddol18/docflow/issues/48).
# Windows의 scripts/tls_secrets.ps1이 ssh 표준 입력으로 "<이름> <base64>" 줄을 넘긴다 — 직접 부르지 않는다.
# 키 값은 화면에 찍지 않는다 (인증서 지문·만료만). 받은 파일은 umask 077 임시 폴더에만 두고 끝나면(실패해도) 지운다.
#   tls-web·tls-postgres·tls-qdrant   kubernetes.io/tls (서비스마다 따로 — 하나가 새도 다른 이름을 흉내 내지 못하게)
#   docflow-ca                        ConfigMap (CA 인증서, 공개 값 — docflow가 Postgres·Qdrant 인증서를 확인)
#   qdrant-api-key                    없을 때만 무작위로 만든다 (32바이트). 바꾸려면 지우고 다시 부른 뒤 qdrant·docflow 재시작
# 이미 떠 있는 파드는 새 인증서를 바로 읽지 않는다 (qdrant는 cert_ttl 3600초마다) — 갱신이면 postgres·web을 다시 띄운다.
set -eu
umask 077
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
NS=docs
kubectl get ns $NS >/dev/null </dev/null
D=$(mktemp -d "${TMPDIR:-/tmp}/docflow-tls.XXXXXX")
trap 'rm -rf "$D"' EXIT
trap 'exit 1' INT TERM
tr -d '\r' | while read -r n b; do
  [ -n "$n" ] || continue
  case "$n" in
    ca.crt|web.crt|web.key|postgres.crt|postgres.key|qdrant.crt|qdrant.key) ;;
    *) echo "모르는 이름: $n" >&2; exit 1 ;;
  esac
  printf '%s' "$b" | base64 -D > "$D/$n"
done
grep -q 'BEGIN CERTIFICATE' "$D/ca.crt" || { echo "ca.crt가 없거나 인증서가 아님" >&2; exit 1; }
for s in web postgres qdrant; do
  grep -q 'BEGIN CERTIFICATE' "$D/$s.crt" 2>/dev/null || { echo "$s.crt가 없거나 인증서가 아님" >&2; exit 1; }
  grep -q 'PRIVATE KEY' "$D/$s.key" 2>/dev/null || { echo "$s.key가 없거나 키가 아님" >&2; exit 1; }
  # 키와 인증서가 짝인지, CA로 확인되는지 (넣기 전에)
  [ "$(openssl x509 -noout -pubkey -in "$D/$s.crt")" = "$(openssl pkey -pubout -in "$D/$s.key")" ] \
    || { echo "$s: 키와 인증서가 짝이 아님" >&2; exit 1; }
  openssl verify -CAfile "$D/ca.crt" "$D/$s.crt" >/dev/null || { echo "$s: CA로 확인되지 않음" >&2; exit 1; }
done
for s in web postgres qdrant; do
  kubectl delete secret "tls-$s" -n $NS --ignore-not-found >/dev/null </dev/null
  kubectl create secret tls "tls-$s" -n $NS --cert="$D/$s.crt" --key="$D/$s.key" >/dev/null </dev/null
  fp=$(openssl x509 -noout -fingerprint -sha256 -in "$D/$s.crt" | cut -d= -f2)
  end=$(openssl x509 -noout -enddate -in "$D/$s.crt" | cut -d= -f2)
  echo "secret $NS/tls-$s: SHA-256 $fp, 만료 $end"
done
kubectl delete configmap docflow-ca -n $NS --ignore-not-found >/dev/null </dev/null
kubectl create configmap docflow-ca -n $NS --from-file=ca.crt="$D/ca.crt" >/dev/null </dev/null
echo "configmap $NS/docflow-ca: SHA-256 $(openssl x509 -noout -fingerprint -sha256 -in "$D/ca.crt" | cut -d= -f2)"
if kubectl get secret qdrant-api-key -n $NS >/dev/null 2>&1 </dev/null; then
  echo "secret $NS/qdrant-api-key: 있음 (그대로)"
else
  head -c 32 /dev/urandom | base64 | tr -d '/+=\n' | kubectl create secret generic qdrant-api-key -n $NS --from-file=api-key=/dev/stdin >/dev/null
  echo "secret $NS/qdrant-api-key: 새로 만듦 (값은 찍지 않음)"
fi
