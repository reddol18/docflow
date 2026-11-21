#!/bin/bash
# 외부 송신 0 증거 모으기 (https://github.com/reddol18/docflow/issues/81). root로, 설치한 노드에서. 결과는 <출력 폴더>에만 쓴다
# (문서 글·값은 남기지 않는다 — 정책·카운터·연결 목록·패킷 머리만).
#   evidence.sh capture-start <출력>   tcpdump를 뒤에서 띄운다: cni0(파드 쪽, 클러스터 밖으로 가려는 것) · eth0(실제로 나간 것) ·
#                                      nflog:5(egress_guard가 버린 것 — 무엇을 보내려 했는지)
#   evidence.sh capture-stop  <출력>   멈추고 패킷 수를 센다
#   evidence.sh snapshot <출력> <이름> (a) 정책·CoreDNS·LLM 설정 (b) 카운터 (c) conntrack — 시험 전·후에 한 번씩
#   evidence.sh probe <출력>           (e) 대조 시험: 일부러 바깥으로 나가 보고 막히는지·카운터가 오르는지
#   evidence.sh ollama-log <출력>      Ollama 로그(이전 컨테이너 포함)·재시작 원인을 남기고 ollama.com 시도 수를 센다
# 클러스터 안 주소: 파드 10.42/16, 서비스 10.43/16. 그 밖으로 가는 연결·패킷이 '외부 송신'이다.
# 바깥 인터페이스는 egress_guard.sh와 같은 방법으로 고른다: EGRESS_IF, 없으면 IPv4 기본 경로의 인터페이스. 노드 주소는 그 인터페이스의 IPv4.
# tcpdump는 -Z root: 0700 폴더에 쓰려고 (Ubuntu tcpdump는 기본으로 tcpdump 사용자로 내려가 쓰기를 못 한다).
# pcap은 머리만(-s 128: IP·TCP/UDP 머리와 DNS 이름 정도) — 바깥으로 나가려던 본문(문서 값)을 남기지 않는다. 출력 폴더는 0700
set -uo pipefail
CMD=${1:?capture-start|capture-stop|snapshot|probe|ollama-log}
OUT=${2:?출력 폴더}
mkdir -p "$OUT"; chmod 0700 "$OUT"
umask 077
K="k3s kubectl"
SNAP=128
IF=${EGRESS_IF:-$(ip -4 route show default 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')}
NODE_IP=$( [ -n "$IF" ] && ip -4 -o addr show "$IF" | awk '{print $4; exit}' | cut -d/ -f1 )
case "$CMD" in capture-*|snapshot) [ -n "$IF" ] && [ -n "$NODE_IP" ] || { echo "바깥 인터페이스·노드 주소를 못 찾음 (EGRESS_IF로 준다)" >&2; exit 1; } ;; esac
INTERNAL='not (dst net 10.42.0.0/16 or dst net 10.43.0.0/16 or dst net 127.0.0.0/8)'

case "$CMD" in
  capture-start)
    # 이미 도는 것은 그대로, 아직 없는 인터페이스(cni0는 k3s가 뜬 뒤 생김)는 건너뛴다 — 설치 전·후에 한 번씩 불러도 된다
    cap() { # 이름 인터페이스 필터
      if [ -f "$OUT/$1.pid" ] && kill -0 "$(cat "$OUT/$1.pid")" 2>/dev/null; then echo "capture $1: 이미 도는 중"; return; fi
      case "$2" in nflog:*) ;; *) ip link show "$2" >/dev/null 2>&1 || { echo "capture $1: $2 아직 없음 — 건너뜀"; return; } ;; esac
      nohup tcpdump -n -Z root -s $SNAP -i "$2" -w "$OUT/$1.pcap" $3 >/dev/null 2>"$OUT/$1.tcpdump.log" &
      local pid=$!; echo $pid > "$OUT/$1.pid"; sleep 2
      # 떴는지 확인 — 못 뜨면(인터페이스·필터 오류) pcap이 없어 나중에 '0개'로 보이는 거짓 0을 막는다
      kill -0 $pid 2>/dev/null || { echo "capture $1: tcpdump가 뜨지 못함 — $(tail -2 "$OUT/$1.tcpdump.log")" >&2; rm -f "$OUT/$1.pid"; fail=1; return; }
      echo "capture $1: $2 pid $pid"
    }
    fail=0
    # cni0: 파드가 클러스터 밖 주소로 보낸 패킷 (막히기 전에 보인다 — 시도까지 센다)
    cap cni0 cni0 "ip and $INTERNAL and not dst host $NODE_IP"
    # eth0: 실제로 노드 밖으로 나간 패킷 (막힌 것은 보이지 않는다). 나가는 방향만(-Q out), ARP 등 IP 아닌 것은 뺀다.
    # (2026-11-21 시험: 방향을 안 가렸더니 Windows 쪽에서 들어온 IPv6 mDNS 18개가 섞였다 — 출발 주소가 Windows의 fe80)
    # 링크 로컬 멀티캐스트(ff02::/16, 224.0.0.0/24 — mDNS·ND 등 같은 링크 안에서 끝남)는 바깥 송신이 아니라 뺀다
    cap eth0 "$IF" "-Q out (ip or ip6) and not dst net ff02::/16 and not dst net 224.0.0.0/24"
    # nflog:5: egress_guard가 버린 패킷 사본 (노드·파드가 바깥으로 보내려 한 것 전부 — 무엇을 물었는지까지)
    cap nflog nflog:5 ""
    exit $fail
    ;;
  capture-stop)
    fail=0
    for i in cni0 eth0 nflog; do
      [ -f "$OUT/$i.pid" ] && kill -INT "$(cat "$OUT/$i.pid")" 2>/dev/null; sleep 1
      # pcap이 없거나 읽지 못하면 오류 (0개로 적지 않는다)
      if [ ! -s "$OUT/$i.pcap" ] || ! tcpdump -n -r "$OUT/$i.pcap" > "$OUT/$i.txt" 2>"$OUT/$i.read.log"; then
        echo "$i: 오류 — pcap 없음 또는 읽기 실패 ($(tail -1 "$OUT/$i.read.log" 2>/dev/null))" | tee "$OUT/$i.count" >&2; fail=1; continue
      fi
      echo "$i: 패킷 $(wc -l < "$OUT/$i.txt")개" | tee "$OUT/$i.count"
      head -50 "$OUT/$i.txt" > "$OUT/$i.head.txt"; rm -f "$OUT/$i.txt"
    done
    # 버린 패킷의 목적지·포트·DNS 질의 이름 요약
    tcpdump -n -vv -r "$OUT/nflog.pcap" 2>/dev/null | grep -oE '(A|AAAA|PTR|SRV|HTTPS)\? [^ ]+' | sort | uniq -c > "$OUT/nflog.dns.txt"
    tcpdump -n -r "$OUT/nflog.pcap" 2>/dev/null | awk '{print $3, $4, $5}' | sed -E 's/\.[0-9]+ >/ >/' | sort | uniq -c | sort -rn | head -20 > "$OUT/nflog.flows.txt"
    exit $fail
    ;;
  snapshot)
    L=${3:?이름}; D="$OUT/$L"; mkdir -p "$D"
    # (a) 정책
    $K get networkpolicy -A -o wide > "$D/a_netpol_list.txt" 2>&1
    $K get networkpolicy -A -o yaml > "$D/a_netpol.yaml" 2>&1
    $K -n kube-system get cm coredns -o jsonpath='{.data.Corefile}' > "$D/a_coredns_Corefile.txt" 2>&1
    ls -l /var/lib/rancher/k3s/server/manifests/ > "$D/a_k3s_manifests.txt" 2>&1
    # 모든 파드의 재시작 수·직전 상태(lastState: 종료 코드·까닭·끝난 때) — 재시작이 있으면 까닭을 남긴다
    $K get pods -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}{range .status.containerStatuses[*]} {.name} restarts={.restartCount} last={.lastState}{end}{"\n"}{end}' > "$D/a_pods_restarts.txt" 2>&1
    $K -n llm logs deploy/ollama --previous > "$D/a_ollama_previous.log" 2>&1 || true
    for d in docflow docflow-worker; do
      $K -n docs get deploy $d -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}' \
        | grep -E '^(LLM_|EXTRACT_MODEL|CELL_MODEL|EMBED_|CHAT_MODEL|AUTH_MODES)' > "$D/a_env_$d.txt"
    done
    # (b) 노드 바깥 송신 막기 카운터
    bash "$(dirname "$0")/egress_guard.sh" status > "$D/b_egress_guard.txt" 2>&1
    dmesg 2>/dev/null | grep -E 'DOCFLOW-(POD|NODE)-EGRESS' > "$D/b_kernel_log.txt" || true
    # (c) conntrack: 클러스터 밖·루프백 밖으로 가는 연결 (지금 있는 것 + 최근 기록)
    conntrack -L 2>/dev/null > "$D/c_conntrack_all.txt"
    # 노드·파드가 시작한 연결(처음 src가 노드 주소나 10.42/16)만 보고, 목적지가 클러스터·루프백·노드 자신이 아닌 것을 센다.
    # 바깥(Windows 쪽)에서 들어온 방송(예: 172.31.255.255)·멀티캐스트는 송신이 아니라 뺀다
    awk -v node="$NODE_IP" '{s="";d=""; for(i=1;i<=NF;i++){ if(s=="" && $i ~ /^src=/) s=substr($i,5); else if(d=="" && $i ~ /^dst=/) d=substr($i,5) }
         if ((s==node || s ~ /^10\.42\./) && d !~ /^(10\.42\.|10\.43\.|127\.|22[4-9]\.|23[0-9]\.)/ && d!=node) print $1, s, "->", d}' \
      "$D/c_conntrack_all.txt" | sort | uniq -c > "$D/c_conntrack_external.txt"
    echo "[$L] conntrack 전체 $(wc -l < "$D/c_conntrack_all.txt")줄, 노드·파드가 바깥으로 연 연결 $(wc -l < "$D/c_conntrack_external.txt")종"
    grep -E 'pod-egress|node-egress|v6-egress' "$D/b_egress_guard.txt" \
      | awk -v l="$L" '{n=""; for(i=1;i<=NF;i++) if($i ~ /-egress$/) n=$i; print "[" l "] " n, "pkts", $1, "bytes", $2}'
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
      ov=$(PY="$PY" python3 -I -c 'import json,os,sys; print(json.dumps({"spec": {"automountServiceAccountToken": False,
        "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [{"name": sys.argv[1], "image": "docflow:dev", "imagePullPolicy": "Never", "command": ["python", "-c", os.environ["PY"]],
                        "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"memory": "128Mi"}},
                        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}}]}}))' "$3")
      $K -n "$1" run "$3" --image=docflow:dev --image-pull-policy=Never --restart=Never --labels="$2" --overrides="$ov" >/dev/null
      $K -n "$1" wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$3" --timeout=120s >/dev/null 2>&1 || true
      echo "$1/$3 ($2): $($K -n "$1" logs "$3" 2>&1 | tail -1)" | tee -a "$OUT/e_probe.txt"
      $K -n "$1" delete pod "$3" --wait=false >/dev/null
    }
    # docflow-egress를 받는 레이블 중 app=docflow-worker: 어떤 Service도 이 레이블로 고르지 않는다 (app=docflow는 Service docflow
    # 엔드포인트에 들어가 실제 요청 일부가 probe 파드로 갔다). ReplicaSet 선택자는 pod-template-hash도 보므로 이 파드를 가져가지 않는다
    run docs app=docflow-worker egress-probe-docs
    $K create namespace egress-probe >/dev/null 2>&1 || true
    run egress-probe app=probe egress-probe-open
    $K delete namespace egress-probe --wait=false >/dev/null
    bash "$(dirname "$0")/egress_guard.sh" status | grep -E 'pod-egress' | awk '{print "probe 뒤 pod-egress pkts", $1}' | tee -a "$OUT/e_probe.txt"
    ;;
  ollama-log)
    # 계약 리뷰: Ollama가 ollama.com에 묻지 않았는지 로그로 남긴다. 재시작이 있었으면 이전 컨테이너 로그와 종료 까닭도
    $K -n llm logs deploy/ollama --timestamps > "$OUT/ollama.log" 2>&1
    $K -n llm logs deploy/ollama --timestamps --previous > "$OUT/ollama.previous.log" 2>&1 || true
    $K -n llm get pod -l app=ollama -o jsonpath='{range .items[*]}{.metadata.name} restarts={.status.containerStatuses[0].restartCount} last={.status.containerStatuses[0].lastState}{"\n"}{end}' > "$OUT/ollama.restarts.txt"
    $K -n llm get events --sort-by=.lastTimestamp > "$OUT/ollama.events.txt" 2>&1
    n=$(cat "$OUT/ollama.log" "$OUT/ollama.previous.log" | grep -ciE 'ollama\.com' || true)
    echo "ollama.com 언급 줄 수: $n" | tee "$OUT/ollama.ollama_com.count"
    grep -E 'cloud disabled' "$OUT/ollama.log" | tail -1
    cat "$OUT/ollama.restarts.txt"
    ;;
  *) echo "capture-start|capture-stop|snapshot|probe|ollama-log" >&2; exit 1 ;;
esac
