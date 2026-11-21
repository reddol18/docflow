#!/bin/bash
# 노드 바깥 송신 막기·세기 (https://github.com/reddol18/docflow/issues/81). 폐쇄망을 흉내 내고(바깥 인터페이스로 나가는 것 전부 버림),
# 무엇이 나가려 했는지 센다 — 파드(10.42/16)에서 온 것과 노드 자신의 것을 따로.
#   egress_guard.sh on  [인터페이스]   기본 = IPv4 기본 경로의 인터페이스(WSL은 eth0). 규칙을 넣고(이미 있으면 비우고 다시) 카운터를 0으로
#   egress_guard.sh status             카운터 (패킷·바이트) — 0이면 아무것도 나가려 하지 않았다
#   egress_guard.sh off                규칙을 뺀다 (인터페이스를 찾지 않는다 — 기본 경로가 없어도 된다)
# mangle POSTROUTING에 건다: k3s·kube-router·flannel이 filter FORWARD 맨 위에 ACCEPT 규칙을 넣어도 그 뒤에 지나는 곳이고,
# nat POSTROUTING(MASQUERADE)보다 앞이라 파드 주소(10.42.x)가 그대로 보인다. 노드 자신이 내는 것도 여기를 지난다.
# 통과시키는 것은 바깥에서 시작된 연결의 응답뿐이다: conntrack 방향이 REPLY인 ESTABLISHED·RELATED 패킷 (예: 들어온 ssh의 응답).
# 노드·파드가 연 연결은 guard를 켜기 전에 맺었더라도 ORIGINAL 방향이라 막고 센다 — 켜기 전에 열어 둔 바깥 연결로 계속 나가지 못하게.
# 통과한 응답은 established 규칙 카운터에 따로 보인다.
# WSL: 배포판들은 네트워크를 함께 쓴다 — 켜 두면 같은 VM의 다른 배포판도 바깥에 못 나간다.
set -euo pipefail
CMD=${1:-status}
POD_CIDR=${POD_CIDR:-10.42.0.0/16}
CH=DOCFLOW-EGRESS
ipt() { iptables -w -t mangle "$@"; }
ip6() { ip6tables -w -t mangle "$@"; }
default_if() { ip -4 route show default 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}'; }
unhook() { # 어느 인터페이스로 걸었든 이 체인으로 가는 POSTROUTING 점프를 모두 뺀다
  ipt -S POSTROUTING 2>/dev/null | grep -- "-j $CH\$" | sed 's/^-A /-D /' | while read -r r; do ipt $r; done || true
  ip6 -S POSTROUTING 2>/dev/null | grep -- "-j $CH\$" | sed 's/^-A /-D /' | while read -r r; do ip6 $r; done || true
}

case "$CMD" in
  on)
    IF=${2:-${EGRESS_IF:-$(default_if)}}
    [ -n "$IF" ] || { echo "인터페이스를 못 찾음 — 두 번째 인자로 준다" >&2; exit 1; }
    unhook   # 다시 켜도 점프는 하나 (예전 인터페이스로 건 점프가 남지 않게)
    ipt -N $CH 2>/dev/null || ipt -F $CH
    ipt -A $CH -m conntrack --ctstate ESTABLISHED,RELATED --ctdir REPLY -m comment --comment established -j RETURN
    # 링크 로컬 멀티캐스트(224.0.0.0/24)는 같은 링크에서 끝나 바깥 송신이 아니다 — 세지 않고 통과 (카운터는 따로 보인다)
    ipt -A $CH -d 224.0.0.0/24 -m comment --comment link-local-mcast -j RETURN
    # NFLOG 그룹 5: 버리는 패킷 사본 — evidence.sh capture-start가 tcpdump -i nflog:5로 받아 무엇이 나가려 했는지(DNS 이름 등) 남긴다
    ipt -A $CH -j NFLOG --nflog-group 5
    ipt -A $CH -s "$POD_CIDR" -j LOG --log-prefix "DOCFLOW-POD-EGRESS " --log-level 4
    ipt -A $CH -s "$POD_CIDR" -m comment --comment pod-egress -j DROP
    ipt -A $CH -j LOG --log-prefix "DOCFLOW-NODE-EGRESS " --log-level 4
    ipt -A $CH -m comment --comment node-egress -j DROP
    ipt -I POSTROUTING 1 -o "$IF" -j $CH
    ip6 -N $CH 2>/dev/null || ip6 -F $CH
    ip6 -A $CH -m conntrack --ctstate ESTABLISHED,RELATED --ctdir REPLY -m comment --comment established -j RETURN
    # IPv6도 같게: 링크 로컬 멀티캐스트(ff02::/16 — 이웃 찾기·라우터 요청·MLD)는 통과, 나머지는 NFLOG 사본을 남기고 버린다.
    # (2026-11-22 재시험: v6-egress 1개·56바이트가 잡혔는데 사본이 없어 무엇인지 못 봤다 — 크기로는 라우터 요청으로 보인다)
    ip6 -A $CH -d ff02::/16 -m comment --comment link-local-mcast -j RETURN
    ip6 -A $CH -j NFLOG --nflog-group 5
    ip6 -A $CH -m comment --comment v6-egress -j DROP
    ip6 -I POSTROUTING 1 -o "$IF" -j $CH
    ipt -Z $CH; ip6 -Z $CH
    echo "egress_guard: $IF 바깥 송신 막음 (mangle POSTROUTING → $CH), 카운터 0"
    ;;
  status)
    echo "== IPv4 mangle $CH (pkts bytes)"; ipt -L $CH -v -n -x
    echo "== IPv6 mangle $CH"; ip6 -L $CH -v -n -x
    echo "== POSTROUTING 연결"; { ipt -S POSTROUTING; ip6 -S POSTROUTING; } | grep -- "-j $CH" || echo "(연결 안 됨)"
    ;;
  off)
    unhook
    ipt -F $CH 2>/dev/null || true; ipt -X $CH 2>/dev/null || true
    ip6 -F $CH 2>/dev/null || true; ip6 -X $CH 2>/dev/null || true
    echo "egress_guard: 풀림"
    ;;
  *) echo "on|status|off" >&2; exit 1 ;;
esac
