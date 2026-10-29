#!/bin/sh
# docs 네임스페이스 비밀값. 값은 화면에 찍지 않는다.
#   pg-auth  Postgres 비밀번호 (없을 때만 무작위로 만듦)
#   openai   표준 입력으로 받은 OpenAI 키 (Windows에서 .env 값만 ssh 표준 입력으로 넘김). 길이·앞 3글자만 출력
set -eu
export PATH="$HOME/.local/bin:$PATH"
kubectl get ns docs >/dev/null
if ! kubectl get secret pg-auth -n docs >/dev/null 2>&1; then
  head -c 24 /dev/urandom | base64 | tr -d '/+=\n' | kubectl create secret generic pg-auth -n docs --from-file=password=/dev/stdin >/dev/null
  echo "secret docs/pg-auth created"
fi
kubectl delete secret openai -n docs --ignore-not-found >/dev/null
# PowerShell 파이프가 UTF-8 BOM(EF BB BF)을 앞에 붙였던 적이 있다 → 지운다
perl -pe 's/^\xEF\xBB\xBF//' | tr -d '\r\n' | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//" \
  | kubectl create secret generic openai -n docs --from-file=api-key=/dev/stdin >/dev/null
n=$(kubectl get secret openai -n docs -o jsonpath='{.data.api-key}' | base64 -d | wc -c | tr -d ' ')
p=$(kubectl get secret openai -n docs -o jsonpath='{.data.api-key}' | base64 -d | cut -c1-3)
echo "secret docs/openai created: ${n} chars, prefix ${p}..."
