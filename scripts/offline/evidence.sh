#!/bin/bash
# 외부 송신 0 증거 모으기 (https://github.com/reddol18/docflow/issues/81). root로, 설치한 노드에서. 결과는 <출력 폴더>에만 쓴다
# (문서 글·값은 남기지 않는다 — 정책·카운터·연결 목록·패킷 머리만).
#   evidence.sh capture-start <출력>   tcpdump 둘을 뒤에서 띄운다: cni0(파드 쪽, 클러스터 밖으로 가려는 것) · eth0(실제로 나간 것)
#   evidence.sh capture-stop  <출력>   멈추고 패킷 수를 센다
#   evidence.sh snapshot <출력> <이름> (a) 정책·CoreDNS·LLM 설정 (b) 카운터 (c) conntrack — 시험 전·후에 한 번씩
#   evidence.sh probe <출력>           (e) 대조 시험: 일부러 바깥으로 나가 보고 막히는지·카운터가 오르는지
# 클러스터 안 주소: 파드 10.42/16, 서비스 10.43/16. 그 밖으로 가는 연결·패킷이 '외부 송신'이다.
set -uo pipefail
CMD=${1:?capture-start|capture-stop|snapshot|probe}
OUT=${2:?출력 폴더}
mkdir -p "$OUT"
K="k3s kubectl"
NODE_IP=$(ip -4 -o addr show eth0 | awk '{print $4}' | cut -d/ -f1)
INTERNAL='not (dst net 10.42.0.0/16 or dst net 10.43.0.0/16 or dst net 127.0.0.0/8)'

case "$CMD" in
  capture-start)
    # cni0: 파드가 클러스터 밖 주소로 보낸 패킷 (막히기 전에 보인다 — 시도까지 센다)
    nohup tcpdump -n -i cni0 -w "$OUT/cni0.pcap" "ip and $INTERNAL and not dst host $NODE_IP" >/dev/null 2>"$OUT/cni0.tcpdump.log" &
    echo $! > "$OUT/cni0.pid"
    # eth0: 실제로 노드 밖으로 나간 패킷 (막힌 것은 보이지 않는다). ARP 등 IP 아닌 것은 뺀다
    nohup tcpdump -n -i eth0 -w "$OUT/eth0.pcap" "(ip and src host $NODE_IP) or ip6" >/dev/null 2>"$OUT/eth0.tcpdump.log" &
    echo $! > "$OUT/eth0.pid"
    sleep 2; echo "capture: cni0 pid $(cat "$OUT/cni0.pid"), eth0 pid $(cat "$OUT/eth0.pid") (노드 $NODE_IP)"
    ;;
  capture-stop)
    for i in cni0 eth0; do
      [ -f "$OUT/$i.pid" ] && kill -INT "$(cat "$OUT/$i.pid")" 2>/dev/null; sleep 1
      n=$(tcpdump -n -r "$OUT/$i.pcap" 2>/dev/null | wc -l)
      echo "$i: 패킷 $n개" | tee "$OUT/$i.count"
      tcpdump -n -r "$OUT/$i.pcap" 2>/dev/null | head -50 > "$OUT/$i.head.txt"
    done
    ;;
  snapshot)
    L=${3:?이름}; D="$OUT/$L"; mkdir -p "$D"
    # (a) 정책
    $K get networkpolicy -A -o wide > "$D/a_netpol_list.txt" 2>&1
    $K get networkpolicy -A -o yaml > "$D/a_netpol.yaml" 2>&1
    $K -n kube-system get cm coredns -o jsonpath='{.data.Corefile}' > "$D/a_coredns_Corefile.txt" 2>&1
    ls -l /var/lib/rancher/k3s/server/manifests/ > "$D/a_k3s_manifests.txt" 2>&1
    for d in docflow docflow-worker; do
      $K -n docs get deploy $d -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}' \
        | grep -E '^(LLM_|EXTRACT_MODEL|CELL_MODEL|EMBED_|CHAT_MODEL|AUTH_MODES)' > "$D/a_env_$d.txt"
    done
    # (b) 노드 바깥 송신 막기 카운터
    bash "$(dirname "$0")/egress_guard.sh" status > "$D/b_egress_guard.txt" 2>&1
    dmesg 2>/dev/null | grep -E 'DOCFLOW-(POD|NODE)-EGRESS' > "$D/b_kernel_log.txt" || true
    # (c) conntrack: 클러스터 밖·루프백 밖으로 가는 연결 (지금 있는 것 + 최근 기록)
    conntrack -L 2>/dev/null > "$D/c_conntrack_all.txt"
    awk '{for(i=1;i<=NF;i++) if($i ~ /^dst=/){split($i,a,"="); print a[2]; break}}' "$D/c_conntrack_all.txt" \
      | grep -vE '^(10\.42\.|10\.43\.|127\.)' | grep -vx "$NODE_IP" | sort | uniq -c > "$D/c_conntrack_external.txt"
    echo "[$L] conntrack 전체 $(wc -l < "$D/c_conntrack_all.txt")줄, 바깥 목적지 $(wc -l < "$D/c_conntrack_external.txt")곳"
    grep -E 'pod-egress|node-egress|v6-egress' "$D/b_egress_guard.txt" | awk -v l="$L" '{print "[" l "] " $NF, "pkts", $1, "bytes", $2}'
    ;;
  probe)
    # (e) 대조: 막는 장치가 실제로 일하는지. ① docs 네임스페이스에 docflow와 같은 레이블의 파드 — NetworkPolicy가 막는다
    #     ② 정책 없는 임시 네임스페이스의 파드 — 노드 막기(egress_guard)가 막고 pod-egress 카운터가 오른다
    #     ③ 바깥 이름 DNS — CoreDNS가 위쪽으로 넘기지 않는다
    PY='import socket,sys
r=[]
try:
    socket.getaddrinfo("example.com",443); r.append("dns example.com: 풀림")
except Exception as e: r.append("dns example.com: 실패 "+type(e).__name__)
for ip in ("1.1.1.1","8.8.8.8"):
    s=socket.socket(); s.settimeout(5)
    try: s.connect((ip,443)); r.append(ip+":443 연결됨")
    except Exception as e: r.append(ip+":443 실패 "+type(e).__name__)
    finally: s.close()
print("; ".join(r))'
    bash "$(dirname "$0")/egress_guard.sh" status | grep -E 'pod-egress' | awk '{print "probe 전 pod-egress pkts", $1}' | tee "$OUT/e_probe.txt"
    run() { # ns label 이름
      local ov
      ov=$(PY="$PY" python3 -c 'import json,os,sys; print(json.dumps({"spec": {"automountServiceAccountToken": False,
        "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [{"name": sys.argv[1], "image": "docflow:dev", "imagePullPolicy": "Never", "command": ["python", "-c", os.environ["PY"]],
                        "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"memory": "128Mi"}},
                        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}}]}}))' "$3")
      $K -n "$1" run "$3" --image=docflow:dev --image-pull-policy=Never --restart=Never --labels="$2" --overrides="$ov" >/dev/null
      $K -n "$1" wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$3" --timeout=120s >/dev/null 2>&1 || true
      echo "$1/$3 ($2): $($K -n "$1" logs "$3" 2>&1 | tail -1)" | tee -a "$OUT/e_probe.txt"
      $K -n "$1" delete pod "$3" --wait=false >/dev/null
    }
    run docs app=docflow egress-probe-docs
    $K create namespace egress-probe >/dev/null 2>&1 || true
    run egress-probe app=probe egress-probe-open
    $K delete namespace egress-probe --wait=false >/dev/null
    bash "$(dirname "$0")/egress_guard.sh" status | grep -E 'pod-egress' | awk '{print "probe 뒤 pod-egress pkts", $1}' | tee -a "$OUT/e_probe.txt"
    ;;
  *) echo "capture-start|capture-stop|snapshot|probe" >&2; exit 1 ;;
esac
