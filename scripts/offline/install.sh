#!/bin/bash
# 폐쇄망 설치 (https://github.com/reddol18/docflow/issues/81): 묶음만으로 k3s 단일 노드 + docflow(docs) + Ollama(llm)를 띄운다.
# 인터넷을 쓰지 않는다 — 받기(pull·curl·apt)가 하나도 없다. root로, 묶음 폴더 안의 scripts/에서:
#   sudo bash <묶음>/scripts/install.sh
# 환경변수:
#   REQUIRE_EGRESS_GUARD=1  바깥 송신 막기(scripts/egress_guard.sh on)가 걸려 있지 않으면 시작하지 않는다 (시험용)
#   OS_QUIET=1              OS가 스스로 바깥에 묻는 서비스(시간 동기화·apt·motd 타이머)를 끈다 (아래 2단계 설명)
#   NEED_ROOT_GB            / 여유 하한 (기본 45GB: 이미지 풀기·모델·데이터)
#   ADMIN_USER              첫 관리자 이름 (기본 admin). 비밀번호는 무작위로 만들어 /root/docflow-admin-password(0600)에만 둔다
#   SUMS_SHA256             (필수) SHA256SUMS 파일 자체의 sha256 — 묶음과 다른 길(문서·전화·별도 매체)로 받은 값.
#                           설치 전에 견주고 다르면 멈춘다 (묶음 안의 SHA256SUMS까지 함께 바꿔치기한 경우를 막는다). 없으면 멈춘다
#   ALLOW_NO_SUMS=1         SUMS_SHA256 없이 진행 (따로 받은 값이 없을 때 위험을 알고 명시적으로만)
# 인자: --tools-only (도구 .deb까지만), --verify-only (1단계 묶음 확인까지만 — 옮긴 묶음을 미리 확인할 때)
# 절차·되돌리기: docs/offline_install.md
set -euo pipefail
B=${BUNDLE:-$(cd "$(dirname "$0")/.." && pwd)}
NEED_ROOT_GB=${NEED_ROOT_GB:-45}
ADMIN_USER=${ADMIN_USER:-admin}
MODEL_DIR=/var/lib/docflow/ollama
K="k3s kubectl"
say() { printf '[install %s] %s\n' "$(date +%H:%M:%S)" "$*"; }
free_gb() { df -P -BG "$1" | awk 'NR==2 {gsub("G","",$4); print $4}'; }

[ "$(id -u)" = 0 ] || { echo "root로 돌린다" >&2; exit 1; }
have=$(free_gb /); say "디스크 / 여유 ${have}GB (필요 ${NEED_ROOT_GB}GB)"
[ "$have" -ge "$NEED_ROOT_GB" ] || { echo "디스크 모자람 — 멈춤" >&2; exit 1; }

say "1/9 묶음 확인 (SHA256SUMS 자체 → 목록 밖 파일 없음 → sha256sum -c)"
# 심볼릭 링크가 하나라도 있으면 멈춘다 — 목록에 있는 이름이라도 링크면 묶음 밖 파일을 가리킬 수 있다 (SHA256SUMS 자신 포함)
links=$(cd "$B" && find . -type l -printf '%P\n')
[ -z "$links" ] || { echo "묶음에 심볼릭 링크가 있음 — 멈춤:" >&2; printf '  %s\n' $links >&2; exit 1; }
[ -f "$B/SHA256SUMS" ] || { echo "SHA256SUMS 없음 — 멈춤" >&2; exit 1; }
sums=$(sha256sum "$B/SHA256SUMS" | cut -d' ' -f1)
if [ -n "${SUMS_SHA256:-}" ]; then
  [ "$sums" = "$(printf '%s' "$SUMS_SHA256" | tr 'A-F' 'a-f')" ] \
    || { echo "SHA256SUMS 해시가 따로 받은 값(SUMS_SHA256)과 다름 — 묶음이 바뀌었다, 멈춤" >&2; exit 1; }
  say "  SHA256SUMS 해시 = SUMS_SHA256 (일치)"
elif [ "${ALLOW_NO_SUMS:-0}" = 1 ]; then
  say "  !! SUMS_SHA256 없이 진행 (ALLOW_NO_SUMS=1) — SHA256SUMS 해시 $sums 를 따로 받은 값과 손으로 견줄 것"
else
  echo "SUMS_SHA256(따로 받은 SHA256SUMS의 sha256)이 필요함 — 멈춤. 없이 하려면 ALLOW_NO_SUMS=1 (docs/offline_install.md '묶음 확인')" >&2
  exit 1
fi
# 목록에 없는 것이 끼어 있으면 멈춘다 — 파일뿐 아니라 폴더가 아닌 모든 것(장치·소켓·FIFO 등)을 본다.
# debs/*.deb·images/*.tar.zst를 glob으로 읽으므로 끼워 넣은 파일이 root로 설치될 수 있다
extra=$(cd "$B" && comm -13 <(sed -E 's/^[0-9a-f]{64} [ *]//' SHA256SUMS | sort) <(find . ! -type d ! -path ./SHA256SUMS -printf '%P\n' | sort))
[ -z "$extra" ] || { echo "SHA256SUMS에 없는 것이 묶음에 있음 — 멈춤:" >&2; printf '  %s\n' $extra >&2; exit 1; }
(cd "$B" && sha256sum --quiet --strict -c SHA256SUMS)
say "  목록 밖·링크 없음, 해시 모두 일치"
[ "${1:-}" != "--verify-only" ] || { say "묶음 확인만 하고 끝"; exit 0; }

say "2/9 도구 .deb (없는 것만)"
missing=()
for d in "$B"/debs/*.deb; do
  [ -e "$d" ] || continue
  p=$(dpkg-deb -f "$d" Package); a=$(dpkg-deb -f "$d" Architecture)
  dpkg-query -W -f='${Status}' "$p:$a" 2>/dev/null | grep -q 'install ok installed' \
    || dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q 'install ok installed' || missing+=("$d")
done
if [ ${#missing[@]} -gt 0 ]; then dpkg -i "${missing[@]}" >/dev/null; say "  넣음: ${#missing[@]}개"; else say "  모두 있음"; fi
# OS_QUIET=1: 운영체제가 스스로 바깥에 묻는 서비스를 끈다 — 2026-11-21 WSL 시험에서 Ubuntu 24.04 기본 systemd-timesyncd가
# ntp.ubuntu.com을 계속 물었다(노드 DNS, 막기에서 버려짐). 현장에 내부 NTP가 있으면 끄는 대신 /etc/systemd/timesyncd.conf NTP=를 그 주소로.
# apt·motd·ua 타이머와 snapd도 바깥(archive.ubuntu.com·api.snapcraft.io 등)에 묻는다. 기본은 건드리지 않는다 (설치 노드의 OS 설정은 현장 정책)
if [ "${OS_QUIET:-0}" = 1 ]; then
  # snapd: 부팅할 때마다 api.snapcraft.io를 묻는다 (2026-11-22 시험 — 배포판을 다시 켠 뒤 노드 DNS 24개가 버려짐, nflog로 확인)
  for u in systemd-timesyncd.service motd-news.timer apt-daily.timer apt-daily-upgrade.timer ua-timer.timer snapd.snap-repair.timer snapd.service snapd.socket snapd.seeded.service; do
    systemctl disable --now "$u" >/dev/null 2>&1 && say "  끔: $u" || true
  done
fi
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
  # kubeconfig(/etc/rancher/k3s/k3s.yaml, cluster-admin)는 k3s 기본 0600 그대로 — 노드의 다른 사용자가 읽지 못하게 (root로 쓴다)
  echo "disable: [traefik]"
  # cgroup v1만 있는 커널(예: WSL 기본 5.15)은 kubelet이 거부한다 — 그때만 허용 (2026-11-21 결정: 이 우회를 유지)
  # runtime-request-timeout 10m: 묶음에서 막 가져온 큰 이미지(ollama 약 3.4GB)는 첫 컨테이너 만들기(스냅숏 풀기)가 kubelet 기본 2분을 넘는다.
  # 2026-11-21 시험: 23:40:25 CreateContainer 시작 → 23:42:25 kubelet이 2분에 끊음(RST_STREAM CANCEL) → containerd는 23:42:27에
  # 끝냈지만 이미 버려짐 → attempt:1로 다시 만들어 23:43:39 시작. 프로세스가 죽은 적은 없는데 restartCount가 1이 됐다
  ka='"runtime-request-timeout=10m"'
  if [ ! -f /sys/fs/cgroup/cgroup.controllers ]; then ka="$ka, \"fail-cgroupv1=false\""; fi
  echo "kubelet-arg: [$ka]"
} > /etc/rancher/k3s/config.yaml
INSTALL_K3S_SKIP_DOWNLOAD=true INSTALL_K3S_SKIP_SELINUX_RPM=true sh "$B/k3s/install.sh" >/dev/null
# 처음 기동은 airgap 이미지(약 4GB) 가져오기 때문에 몇 분 NotReady일 수 있다
for i in $(seq 1 300); do $K get nodes --no-headers 2>/dev/null | awk '{print $2}' | grep -qx Ready && break; sleep 2; done
$K get nodes -o wide

say "4/9 CoreDNS: 바깥 이름을 위쪽 DNS로 넘기지 않게"
for i in $(seq 1 90); do $K -n kube-system get cm coredns >/dev/null 2>&1 && $K -n kube-system get deploy coredns >/dev/null 2>&1 && break; sleep 2; done
touch /var/lib/rancher/k3s/server/manifests/coredns.yaml.skip   # k3s가 기본 Corefile로 되돌리지 않게
P=$(mktemp); trap 'rm -f "$P"' EXIT   # 고정 이름(/tmp/…)을 쓰지 않는다 — 다른 사용자가 먼저 만들거나 링크를 걸 수 없게
python3 -I -c 'import json,sys; print(json.dumps({"data": {"Corefile": open(sys.argv[1]).read()}}))' "$B/manifests/Corefile" > "$P"
$K -n kube-system patch configmap coredns --type merge --patch-file "$P" >/dev/null
rm -f "$P"
$K -n kube-system rollout restart deploy/coredns >/dev/null
$K -n kube-system rollout status deploy/coredns --timeout=300s

say "5/9 모델 풀기 → $MODEL_DIR"
mkdir -p "$MODEL_DIR"
tar -C "$MODEL_DIR" -xf "$B/models/ollama-models.tar"
mkdir -p "$MODEL_DIR/manifests-v2" "$MODEL_DIR/metadata"   # k8s/offline/llm.yaml이 emptyDir로 덮는 마운트 지점 (모델 폴더는 읽기 전용)
chown -R 1000:1000 "$MODEL_DIR"; chmod -R a+rX "$MODEL_DIR"
du -sh "$MODEL_DIR"

say "6/9 내부 CA·서버 인증서·비밀값 (값은 찍지 않는다)"
$K get ns docs >/dev/null 2>&1 || $K create namespace docs >/dev/null
CA=/root/docflow-ca; umask 077; mkdir -p "$CA"
names=(web:docflow.localhost postgres:postgres.docs.svc.cluster.local qdrant:qdrant.docs.svc.cluster.local redis:redis.docs.svc.cluster.local)
if [ ! -f "$CA/ca.crt" ]; then
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
nameConstraints=critical,${perm}excluded;IP:0.0.0.0/0.0.0.0,excluded;IP:::/::
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
say "끝. 접속(root로 — kubeconfig 0600): sudo k3s kubectl -n docs port-forward svc/docflow 8000:8000 (API) / svc/web 8443:8443 (화면)"
