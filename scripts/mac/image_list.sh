#!/bin/sh
# colima 이미지 디스크 정리용 목록 — 읽기만 한다 (아무것도 지우지 않음). https://github.com/reddol18/docflow/issues/119
#   sh ~/doc_platform/scripts/image_list.sh
# 보여 주는 것: 이미지 디스크 사용률·GC 문턱(disk_check.sh), containerd(k8s.io) 이미지마다 누가 쓰는지, 빌드 캐시 크기.
# 지울지는 사용자가 정한다 — 기준은 README 'colima 디스크 정리 기준'. 다른 프로젝트 네임스페이스가 쓰는 이미지는 대상이 아니다.
# 이미지 표의 '쓰는 곳'은 워크로드 정의(Deployment·StatefulSet·DaemonSet·Job·CronJob·Pod, 0개로 줄인 것 포함) 기준이다.
# 시험: IMAGE_LIST_TEST_WORKLOADS("네임스페이스<TAB>이미지" 줄들)·IMAGE_LIST_TEST_IMAGES("저장소 태그 ID 크기" 줄들)를 주면
#       클러스터 대신 그 값으로 표만 만든다 (tests/test_disk_check.py).
set -u
export PATH="/usr/local/bin:/opt/homebrew/bin:$HOME/.local/bin:$PATH"
OWN_NS=docs   # 이 저장소(docflow)의 네임스페이스

if [ -n "${IMAGE_LIST_TEST_IMAGES:-}" ]; then
  workloads=${IMAGE_LIST_TEST_WORKLOADS:-}
  images=$IMAGE_LIST_TEST_IMAGES
else
  echo "== 이미지 디스크 (disk_check.sh — 멈춤 기준을 넘으면 그 줄이 '!!')"
  sh "$(dirname "$0")/disk_check.sh" </dev/null || true
  echo
  # 워크로드가 정의한 이미지 (네임스페이스<TAB>이미지 …). 0개로 줄인 것도 정의는 남아 있어 함께 잡힌다
  tpl='{range .items[*]}{.metadata.namespace}{"\t"}{range .spec.template.spec.initContainers[*]}{.image}{" "}{end}{range .spec.template.spec.containers[*]}{.image}{" "}{end}{"\n"}{end}'
  workloads=$(
    for k in deployments statefulsets daemonsets jobs; do kubectl get "$k" -A -o jsonpath="$tpl" </dev/null 2>/dev/null; done
    kubectl get cronjobs -A -o jsonpath='{range .items[*]}{.metadata.namespace}{"\t"}{range .spec.jobTemplate.spec.template.spec.containers[*]}{.image}{" "}{end}{"\n"}{end}' </dev/null 2>/dev/null
    kubectl get pods -A -o jsonpath='{range .items[*]}{.metadata.namespace}{"\t"}{range .spec.initContainers[*]}{.image}{" "}{end}{range .spec.containers[*]}{.image}{" "}{end}{"\n"}{end}' </dev/null 2>/dev/null
  ) || true
  [ -n "$workloads" ] || echo "!! 클러스터에서 워크로드를 읽지 못함 — 아래 '쓰는 곳'이 비어 있다. 이 상태로는 아무것도 지우지 말 것" >&2
  images=$(colima nerdctl -- --namespace k8s.io images --format '{{.Repository}} {{.Tag}} {{.ID}} {{.Size}}' </dev/null) \
    || { echo "colima 이미지 목록을 읽지 못함 (colima가 꺼져 있나?)" >&2; exit 1; }
fi

echo "== containerd(k8s.io) 이미지와 쓰는 곳 (지우지 않음, 판단은 사용자)"
{
  printf '%s\n' "$workloads" | awk -F '\t' 'NF>=2 { n=split($2, a, " "); for (i=1; i<=n; i++) print "W\t" $1 "\t" a[i] }'
  printf '%s\n' "$images" | awk 'NF>=3 { s=""; for (i=4; i<=NF; i++) s = s (i>4 ? " " : "") $i; print "I\t" $1 "\t" $2 "\t" $3 "\t" s }'
} | awk -F '\t' -v own="$OWN_NS" '
  # 이름 맞추기: 다이제스트(@sha256:…)·docker.io/library/·docker.io/ 앞머리를 떼고, 태그가 없으면 :latest
  function norm(r,   p) {
    sub(/@sha256:[0-9a-f]+$/, "", r); sub(/^docker\.io\/library\//, "", r); sub(/^docker\.io\//, "", r)
    p = r; sub(/.*\//, "", p); if (p !~ /:/) r = r ":latest"
    return r
  }
  function add(arr, k, v) { if (index(" " arr[k] " ", " " v " ") == 0) arr[k] = (arr[k] == "" ? v : arr[k] " " v) }
  function repo_of(r) { r = norm(r); sub(/:[^:\/]*$/, "", r); return r }
  $1 == "W" { add(used, norm($3), $2); add(repons, repo_of($3), $2); next }
  $1 == "I" { n++; repo[n] = $2; tag[n] = $3; id[n] = $4; size[n] = $5
              if ($3 != "<none>") { k = norm($2 ":" $3); if (k in used) { split(used[k], u, " "); for (j in u) add(idns, $4, u[j]) } } }
  END {
    printf "%-14s %-10s %-48s %s\n", "ID", "크기", "이미지", "쓰는 곳 → 판단"
    for (i = 1; i <= n; i++) {
      ns = idns[id[i]]; name = repo[i] ":" tag[i]
      if (ns != "") {
        other = ""; mine = 0; m = split(ns, a, " ")
        for (j = 1; j <= m; j++) { if (a[j] == own) mine = 1; else other = other (other == "" ? "" : ",") a[j] }
        if (other == "") v = own " 사용 → 지우지 않음"
        else if (other ~ /^kube-system$/ && !mine) v = "시스템(kube-system) → 대상 아님"
        else v = (mine ? own "," : "") other " 사용 → 다른 프로젝트·시스템 것, 대상 아님"
      } else {
        # 아무 워크로드도 이 ID를 쓰지 않음 — 같은 저장소(이름)를 누가 쓰는지로 주인을 가린다
        rn = repons[repo_of(repo[i] ":x")]; other = ""; m = split(rn, a, " ")
        for (j = 1; j <= m; j++) if (a[j] != own) other = other (other == "" ? "" : ",") a[j]
        st = (tag[i] == "<none>" ? "태그 없음(dangling)" : "워크로드 없음")
        if (repo[i] ~ /pause/) v = "시스템(pause) → 대상 아님"
        else if (other != "") v = st ", 같은 이름을 " other "가 씀 → 다른 프로젝트 것, 대상 아님"
        else if (repo[i] ~ /(^|\/)docflow(-web)?$/ || rn != "") v = st ", " own " 이미지의 예전 판 → 후보 (사용자 판단)"
        else v = st ", 주인 모름 → 다른 프로젝트 것일 수 있음. 주인 확인 전엔 지우지 않음"
      }
      printf "%-14s %-10s %-48s %s\n", substr(id[i], 1, 12), size[i], name, v
    }
  }'

if [ -z "${IMAGE_LIST_TEST_IMAGES:-}" ]; then
  echo
  echo "== 빌드 캐시 (buildkit — 이미지 목록에 안 보이지만 같은 디스크를 쓴다)"
  colima ssh -- sudo buildctl du </dev/null 2>/dev/null | tail -3 || echo "(buildctl du를 부르지 못함)"
  echo
  echo "지우는 명령은 이 스크립트가 실행하지 않는다 — README 'colima 디스크 정리 기준'을 보고 사용자가 고른다."
fi
