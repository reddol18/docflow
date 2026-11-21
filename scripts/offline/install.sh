#!/bin/bash
# 폐쇄망 설치 (https://github.com/reddol18/docflow/issues/81): 묶음만으로 k3s 단일 노드 + docflow(docs) + Ollama(llm)를 띄운다.
# 인터넷을 쓰지 않는다 — 받기(pull·curl·apt)가 하나도 없다. root로, 묶음 폴더 안의 scripts/에서:
#   sudo bash <묶음>/scripts/install.sh
# 환경변수:
#   REQUIRE_EGRESS_GUARD=1  바깥 송신 막기(scripts/egress_guard.sh on)가 걸려 있지 않으면 시작하지 않는다 (시험용)
#   NEED_ROOT_GB            / 여유 하한 (기본 45GB: 이미지 풀기·모델·데이터)
#   ADMIN_USER              첫 관리자 이름 (기본 admin). 비밀번호는 무작위로 만들어 /root/docflow-admin-password(0600)에만 둔다
# 절차·되돌리기: docs/offline_install.md
set -euo pipefail
B=$(cd "$(dirname "$0")/.." && pwd)
NEED_ROOT_GB=${NEED_ROOT_GB:-45}
ADMIN_USER=${ADMIN_USER:-admin}
MODEL_DIR=/var/lib/docflow/ollama
K="k3s kubectl"
say() { printf '[install %s] %s\n' "$(date +%H:%M:%S)" "$*"; }
free_gb() { df -P -BG "$1" | awk 'NR==2 {gsub("G","",$4); print $4}'; }

[ "$(id -u)" = 0 ] || { echo "root로 돌린다" >&2; exit 1; }
have=$(free_gb /); say "디스크 / 여유 ${have}GB (필요 ${NEED_ROOT_GB}GB)"
[ "$have" -ge "$NEED_ROOT_GB" ] || { echo "디스크 모자람 — 멈춤" >&2; exit 1; }

say "1/9 묶음 확인 (sha256sum -c)"
(cd "$B" && sha256sum --quiet -c SHA256SUMS)

say "2/9 도구 .deb (없는 것만)"
missing=()
for d in "$B"/debs/*.deb; do
  [ -e "$d" ] || continue
  p=$(dpkg-deb -f "$d" Package); a=$(dpkg-deb -f "$d" Architecture)
  dpkg-query -W -f='${Status}' "$p:$a" 2>/dev/null | grep -q 'install ok installed' \
    || dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q 'install ok installed' || missing+=("$d")
done
if [ ${#missing[@]} -gt 0 ]; then dpkg -i "${missing[@]}" >/dev/null; say "  넣음: ${#missing[@]}개"; else say "  모두 있음"; fi
# --tools-only: 여기까지만 (iptables가 없는 새 노드에서 egress_guard.sh on을 먼저 걸 때)
[ "${1:-}" != "--tools-only" ] || { say "도구만 넣고 끝"; exit 0; }

if [ "${REQUIRE_EGRESS_GUARD:-0}" = 1 ]; then
  iptables -w -t mangle -S POSTROUTING | grep -q -- '-j DOCFLOW-EGRESS' \
    || { echo "REQUIRE_EGRESS_GUARD=1인데 바깥 송신 막기가 없음 — scripts/egress_guard.sh on 먼저" >&2; exit 1; }
  say "  바깥 송신 막기 확인됨"
fi

say "3/9 k3s (airgap)"
install -m 0755 "$B/k3s/k3s" /usr/local/bin/k3s
mkdir -p /var/lib/rancher/k3s/agent/images /etc/rancher/k3s
cp "$B/k3s/k3s-airgap-images-amd64.tar.zst" "$B"/images/*.tar.zst /var/lib/rancher/k3s/agent/images/
{
  echo "write-kubeconfig-mode: \"644\""
  echo "disable: [traefik]"
  # cgroup v1만 있는 커널(예: WSL 기본 5.15)은 kubelet이 거부한다 — 그때만 허용 (2026-11-21 결정: 이 우회를 유지)
  if [ ! -f /sys/fs/cgroup/cgroup.controllers ]; then echo "kubelet-arg: [\"fail-cgroupv1=false\"]"; fi
} > /etc/rancher/k3s/config.yaml
INSTALL_K3S_SKIP_DOWNLOAD=true INSTALL_K3S_SKIP_SELINUX_RPM=true sh "$B/k3s/install.sh" >/dev/null
for i in $(seq 1 90); do $K get nodes 2>/dev/null | grep -q ' Ready' && break; sleep 2; done
$K get nodes -o wide

say "4/9 CoreDNS: 바깥 이름을 위쪽 DNS로 넘기지 않게"
for i in $(seq 1 90); do $K -n kube-system get cm coredns >/dev/null 2>&1 && $K -n kube-system get deploy coredns >/dev/null 2>&1 && break; sleep 2; done
touch /var/lib/rancher/k3s/server/manifests/coredns.yaml.skip   # k3s가 기본 Corefile로 되돌리지 않게
python3 -c 'import json,sys; print(json.dumps({"data": {"Corefile": open(sys.argv[1]).read()}}))' "$B/manifests/Corefile" > /tmp/corefile-patch.json
$K -n kube-system patch configmap coredns --type merge --patch-file /tmp/corefile-patch.json >/dev/null
rm -f /tmp/corefile-patch.json
$K -n kube-system rollout restart deploy/coredns >/dev/null
$K -n kube-system rollout status deploy/coredns --timeout=300s

say "5/9 모델 풀기 → $MODEL_DIR"
mkdir -p "$MODEL_DIR"
tar -C "$MODEL_DIR" -xf "$B/models/ollama-models.tar"
chown -R 1000:1000 "$MODEL_DIR"; chmod -R a+rX "$MODEL_DIR"
du -sh "$MODEL_DIR"

say "6/9 내부 CA·서버 인증서·비밀값 (값은 찍지 않는다)"
$K get ns docs >/dev/null 2>&1 || $K create namespace docs >/dev/null
CA=/root/docflow-ca; umask 077; mkdir -p "$CA"
names=(web:docflow.localhost postgres:postgres.docs.svc.cluster.local qdrant:qdrant.docs.svc.cluster.local redis:redis.docs.svc.cluster.local)
if [ ! -f "$CA/ca.key" ]; then
  # scripts/ca.py와 같은 모양: CA EC P-384·5년·이름 제약(위 이름만, IP 막음)·EKU serverAuth, 서버 EC P-256·397일
  perm=$(for n in "${names[@]}"; do printf 'permitted;DNS:%s,' "${n#*:}"; done)
  cat > "$CA/ca.cnf" <<EOF
[req]
distinguished_name=dn
prompt=no
[dn]
O=docflow demo
CN=docflow internal demo CA (offline)
[ca_ext]
basicConstraints=critical,CA:TRUE,pathlen:0
keyUsage=critical,keyCertSign,cRLSign,digitalSignature
extendedKeyUsage=serverAuth
nameConstraints=critical,${perm}excluded;IP:0.0.0.0/0.0.0.0,excluded;IP:::/0
subjectKeyIdentifier=hash
EOF
  openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-384 -out "$CA/ca.key" 2>/dev/null
  openssl req -x509 -new -key "$CA/ca.key" -config "$CA/ca.cnf" -extensions ca_ext -days 1826 -sha384 -out "$CA/ca.crt"
fi
for n in "${names[@]}"; do
  s=${n%%:*}; dns=${n#*:}
  printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:%s\nauthorityKeyIdentifier=keyid\n' "$dns" > "$CA/$s.ext"
  openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$CA/$s.key" 2>/dev/null
  openssl req -new -key "$CA/$s.key" -subj "/CN=$dns" -out "$CA/$s.csr"
  openssl x509 -req -in "$CA/$s.csr" -CA "$CA/ca.crt" -CAkey "$CA/ca.key" -CAcreateserial -days 397 -sha256 -extfile "$CA/$s.ext" -out "$CA/$s.crt" 2>/dev/null
  openssl verify -CAfile "$CA/ca.crt" "$CA/$s.crt" >/dev/null
  $K -n docs delete secret "tls-$s" --ignore-not-found >/dev/null
  $K -n docs create secret tls "tls-$s" --cert="$CA/$s.crt" --key="$CA/$s.key" >/dev/null
  echo "  secret docs/tls-$s: $(openssl x509 -noout -fingerprint -sha256 -in "$CA/$s.crt" | cut -d= -f2 | cut -c1-23)…"
done
$K -n docs delete configmap docflow-ca --ignore-not-found >/dev/null
$K -n docs create configmap docflow-ca --from-file=ca.crt="$CA/ca.crt" >/dev/null
rand() { head -c 32 /dev/urandom | base64 | tr -d '/+=\n'; }
for s in pg-auth:password qdrant-api-key:api-key redis-auth:password; do
  name=${s%%:*}; key=${s#*:}
  $K -n docs get secret "$name" >/dev/null 2>&1 && { echo "  secret docs/$name: 있음 (그대로)"; continue; }
  rand > "$CA/.tmp"; $K -n docs create secret generic "$name" --from-file="$key=$CA/.tmp" >/dev/null; rm -f "$CA/.tmp"
  echo "  secret docs/$name: 새로 만듦"
done
umask 022

say "7/9 매니페스트 apply (docs·llm)"
$K apply -f "$B/manifests/docflow-offline.yaml" >/dev/null
for d in llm/ollama docs/postgres docs/qdrant docs/redis docs/docflow docs/docflow-worker docs/web; do
  $K -n "${d%%/*}" rollout status "deploy/${d#*/}" --timeout=900s
done

say "8/9 첫 관리자 ($ADMIN_USER)"
PW=/root/docflow-admin-password
[ -f "$PW" ] || { (umask 077; rand > "$PW"); }
if $K -n docs exec -i deploy/docflow -- python -m app.users add "$ADMIN_USER" --role admin --password-stdin < "$PW" >/dev/null 2>&1; then
  echo "  $ADMIN_USER 만듦 (비밀번호: $PW, 0600)"
else
  echo "  $ADMIN_USER 이미 있거나 만들지 못함 — 'python -m app.users' 로 확인"
fi

say "9/9 확인: 이미지 받기 시도가 없어야 한다"
$K get pods -A -o wide
if $K get events -A 2>/dev/null | grep -iE 'Pulling|ErrImagePull|ImagePullBackOff'; then
  echo "!! 이미지 받기 시도가 있었다 — 묶음에 빠진 이미지" >&2; exit 1
fi
say "끝. 접속: k3s kubectl -n docs port-forward svc/docflow 8000:8000 (API) / svc/web 8443:8443 (화면)"
