#!/bin/bash
# 노드 바깥 송신 막기·세기 (https://github.com/reddol18/docflow/issues/81). 폐쇄망을 흉내 내고(바깥 인터페이스로 나가는 것 전부 버림),
# 무엇이 나가려 했는지 센다 — 파드(10.42/16)에서 온 것과 노드 자신의 것을 따로.
#   egress_guard.sh on  [인터페이스]   기본 = IPv4 기본 경로의 인터페이스(WSL은 eth0). 규칙을 새로 만들어 바꿔 끼우고 카운터는 0
#   egress_guard.sh status             카운터 (패킷·바이트) — 0이면 아무것도 나가려 하지 않았다
#   egress_guard.sh off                규칙을 뺀다 (인터페이스를 찾지 않는다 — 기본 경로가 없어도 된다)
# mangle POSTROUTING에 건다: k3s·kube-router·flannel이 filter FORWARD 맨 위에 ACCEPT 규칙을 넣어도 그 뒤에 지나는 곳이고,
# nat POSTROUTING(MASQUERADE)보다 앞이라 파드 주소(10.42.x)가 그대로 보인다. 노드 자신이 내는 것도 여기를 지난다.
# 통과시키는 것은 바깥에서 시작된 연결의 응답뿐이다: conntrack 방향이 REPLY인 ESTABLISHED·RELATED 패킷 (예: 들어온 ssh의 응답).
# 노드·파드가 연 연결은 guard를 켜기 전에 맺었더라도 ORIGINAL 방향이라 막고 센다. 통과한 응답은 established 카운터에 따로 보인다.
# 버리는 패킷의 사본(NFLOG, evidence.sh capture-start가 받는다): DNS(포트 53)는 그룹 6에 512바이트까지(질의 이름이 잘리지 않게),
# 그 밖은 그룹 5에 128바이트까지(머리만 — 바깥으로 나가려던 본문을 남기지 않게). 자르는 것은 커널(--nflog-size)이 한다.
# on은 새 체인을 다 만든 뒤에 바꿔 끼운다 — 만드는 중에 실패하면 예전 guard(있었다면)는 그대로이고, 크게 알리고 1로 끝난다.
# WSL: 배포판들은 네트워크를 함께 쓴다 — 켜 두면 같은 VM의 다른 배포판도 바깥에 못 나간다.
set -Eeuo pipefail   # -E: 함수 안 실패에도 ERR 트랩 (on의 failed)
CMD=${1:-status}
POD_CIDR=${POD_CIDR:-10.42.0.0/16}
CH=DOCFLOW-EGRESS
NF=DOCFLOW-EGRESS-NF
ipt() { iptables -w -t mangle "$@"; }
ip6() { ip6tables -w -t mangle "$@"; }
default_if() { ip -4 route show default 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}'; }
unhook() { # $1=ipt|ip6, $2=체인: POSTROUTING에서 그 체인으로 가는 점프를 모두 뺀다
  "$1" -S POSTROUTING 2>/dev/null | grep -- "-j $2\$" | sed 's/^-A /-D /' | while read -r r; do "$1" $r; done || true
}
drop_chain() { "$1" -F "$2" 2>/dev/null || true; "$1" -X "$2" 2>/dev/null || true; }
nflog_rules() { # $1=ipt|ip6, $2=사본 체인
  local t=$1 c=$2 p
  for p in udp tcp; do
    "$t" -A "$c" -p $p --dport 53 -j NFLOG --nflog-group 6 --nflog-size 512
    "$t" -A "$c" -p $p --dport 53 -j RETURN
  done
  "$t" -A "$c" -j NFLOG --nflog-group 5 --nflog-size 128
}

case "$CMD" in
  on)
    IF=${2:-${EGRESS_IF:-$(default_if)}}
    [ -n "$IF" ] || { echo "인터페이스를 못 찾음 — 두 번째 인자로 준다" >&2; exit 1; }
    N=$CH-NEW; NN=$NF-NEW
    failed() {
      echo "!!!!!!!! egress_guard on 실패 — 새 규칙을 넣지 못했다. 예전 guard가 있었다면 그대로, 없었다면 guard 없음 !!!!!!!!" >&2
      echo "!!!!!!!! 'egress_guard.sh status'로 확인할 것. 바깥 송신이 막혀 있다고 믿지 말 것 !!!!!!!!" >&2
      for t in ipt ip6; do unhook $t $N; drop_chain $t $N; drop_chain $t $NN; done
      exit 1
    }
    trap failed ERR
    for t in ipt ip6; do unhook $t $N; drop_chain $t $N; drop_chain $t $NN; $t -N $N; $t -N $NN; nflog_rules $t $NN; done
    ipt -A $N -m conntrack --ctstate ESTABLISHED,RELATED --ctdir REPLY -m comment --comment established -j RETURN
    # 링크 로컬 멀티캐스트(224.0.0.0/24)는 같은 링크에서 끝나 바깥 송신이 아니다 — 세지 않고 통과 (카운터는 따로 보인다)
    ipt -A $N -d 224.0.0.0/24 -m comment --comment link-local-mcast -j RETURN
    ipt -A $N -j $NN
    ipt -A $N -s "$POD_CIDR" -j LOG --log-prefix "DOCFLOW-POD-EGRESS " --log-level 4
    ipt -A $N -s "$POD_CIDR" -m comment --comment pod-egress -j DROP
    ipt -A $N -j LOG --log-prefix "DOCFLOW-NODE-EGRESS " --log-level 4
    ipt -A $N -m comment --comment node-egress -j DROP
    ip6 -A $N -m conntrack --ctstate ESTABLISHED,RELATED --ctdir REPLY -m comment --comment established -j RETURN
    # IPv6도 같게: 링크 로컬 멀티캐스트(ff02::/16 — 이웃 찾기·라우터 요청·MLD)는 통과, 나머지는 사본을 남기고 버린다
    ip6 -A $N -d ff02::/16 -m comment --comment link-local-mcast -j RETURN
    ip6 -A $N -j $NN
    ip6 -A $N -m comment --comment v6-egress -j DROP
    # 바꿔 끼우기: 새 점프를 먼저 맨 앞에 넣고(빈틈 없음) 예전 점프·체인을 지운 뒤 이름을 바꾼다 (점프는 이름을 따라간다)
    for t in ipt ip6; do
      $t -I POSTROUTING 1 -o "$IF" -j $N
      unhook $t $CH; drop_chain $t $CH; drop_chain $t $NF
      $t -E $N $CH; $t -E $NN $NF
    done
    trap - ERR
    echo "egress_guard: $IF 바깥 송신 막음 (mangle POSTROUTING → $CH), 카운터 0"
    ;;
  status)
    echo "== IPv4 mangle $CH (pkts bytes)"; ipt -L $CH -v -n -x
    echo "== IPv6 mangle $CH"; ip6 -L $CH -v -n -x
    echo "== POSTROUTING 연결"; { ipt -S POSTROUTING; ip6 -S POSTROUTING; } | grep -- "-j $CH" || echo "(연결 안 됨)"
    ;;
  off)
    for t in ipt ip6; do
      unhook $t $CH; unhook $t $CH-NEW
      for c in $CH $NF $CH-NEW $NF-NEW; do drop_chain $t $c; done
    done
    echo "egress_guard: 풀림"
    ;;
  *) echo "on|status|off" >&2; exit 1 ;;
esac
