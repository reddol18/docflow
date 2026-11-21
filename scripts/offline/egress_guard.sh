#!/bin/bash
# 노드 바깥 송신 막기·세기 (https://github.com/reddol18/docflow/issues/81). 폐쇄망을 흉내 내고(바깥 인터페이스로 나가는 것 전부 버림),
# 무엇이 나가려 했는지 센다 — 파드(10.42/16)에서 온 것과 노드 자신의 것을 따로.
#   egress_guard.sh on  [인터페이스]   기본 = IPv4 기본 경로의 인터페이스(WSL은 eth0). 규칙을 넣고(이미 있으면 비우고 다시) 카운터를 0으로
#   egress_guard.sh status             카운터 (패킷·바이트) — 0이면 아무것도 나가려 하지 않았다
#   egress_guard.sh off                규칙을 뺀다
# mangle POSTROUTING에 건다: k3s·kube-router·flannel이 filter FORWARD 맨 위에 ACCEPT 규칙을 넣어도 그 뒤에 지나는 곳이고,
# nat POSTROUTING(MASQUERADE)보다 앞이라 파드 주소(10.42.x)가 그대로 보인다. 노드 자신이 내는 것도 여기를 지난다.
# 이미 맺어진 연결(ESTABLISHED·RELATED)의 패킷은 먼저 RETURN — 들어온 연결(예: ssh)의 응답은 막지 않고 카운터에도 들지 않는다.
# 새 연결(NEW)만 세고 버린다. RETURN 규칙의 카운터는 따로 보인다 (established).
# WSL: 배포판들은 네트워크를 함께 쓴다 — 켜 두면 같은 VM의 다른 배포판도 바깥에 못 나간다.
set -euo pipefail
CMD=${1:-status}
default_if() { ip -4 route show default 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}'; }
IF=${2:-${EGRESS_IF:-$(default_if)}}
[ -n "$IF" ] || [ "$CMD" = status ] || { echo "인터페이스를 못 찾음 — 두 번째 인자로 준다" >&2; exit 1; }
POD_CIDR=${POD_CIDR:-10.42.0.0/16}
CH=DOCFLOW-EGRESS
ipt() { iptables -w -t mangle "$@"; }
ip6() { ip6tables -w -t mangle "$@"; }

case "$CMD" in
  on)
    ipt -N $CH 2>/dev/null || ipt -F $CH
    # NFLOG 그룹 5: 버리는 패킷 사본 — evidence.sh capture-start가 tcpdump -i nflog:5로 받아 무엇이 나가려 했는지(DNS 이름 등) 남긴다
    ipt -A $CH -m conntrack --ctstate ESTABLISHED,RELATED -m comment --comment established -j RETURN
    ipt -A $CH -j NFLOG --nflog-group 5
    ipt -A $CH -s "$POD_CIDR" -j LOG --log-prefix "DOCFLOW-POD-EGRESS " --log-level 4
    ipt -A $CH -s "$POD_CIDR" -m comment --comment pod-egress -j DROP
    ipt -A $CH -j LOG --log-prefix "DOCFLOW-NODE-EGRESS " --log-level 4
    ipt -A $CH -m comment --comment node-egress -j DROP
    ipt -C POSTROUTING -o "$IF" -j $CH 2>/dev/null || ipt -I POSTROUTING 1 -o "$IF" -j $CH
    ip6 -N $CH 2>/dev/null || ip6 -F $CH
    ip6 -A $CH -m conntrack --ctstate ESTABLISHED,RELATED -m comment --comment established -j RETURN
    ip6 -A $CH -m comment --comment v6-egress -j DROP
    ip6 -C POSTROUTING -o "$IF" -j $CH 2>/dev/null || ip6 -I POSTROUTING 1 -o "$IF" -j $CH
    ipt -Z $CH; ip6 -Z $CH
    echo "egress_guard: $IF 바깥 송신 막음 (mangle POSTROUTING → $CH), 카운터 0"
    ;;
  status)
    echo "== IPv4 mangle $CH (pkts bytes)"; ipt -L $CH -v -n -x
    echo "== IPv6 mangle $CH"; ip6 -L $CH -v -n -x
    echo "== POSTROUTING 연결"; ipt -S POSTROUTING | grep -- "-j $CH" || echo "(연결 안 됨)"
    ;;
  off)
    # 어느 인터페이스로 걸었든 이 체인으로 가는 POSTROUTING 규칙을 모두 뺀다
    ipt -S POSTROUTING | grep -- "-j $CH\$" | sed 's/^-A /-D /' | while read -r r; do ipt $r; done || true
    ipt -F $CH 2>/dev/null || true; ipt -X $CH 2>/dev/null || true
    ip6 -S POSTROUTING | grep -- "-j $CH\$" | sed 's/^-A /-D /' | while read -r r; do ip6 $r; done || true
    ip6 -F $CH 2>/dev/null || true; ip6 -X $CH 2>/dev/null || true
    echo "egress_guard: 풀림"
    ;;
  *) echo "on|status|off" >&2; exit 1 ;;
esac
