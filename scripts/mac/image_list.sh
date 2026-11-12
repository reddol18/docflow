#!/bin/sh
# colima 이미지 디스크 정리용 목록 — 읽기만 한다 (아무것도 지우지 않음). https://github.com/reddol18/docflow/issues/119
#   sh ~/doc_platform/scripts/image_list.sh
# 보여 주는 것: 이미지 디스크 사용률·GC 문턱(disk_check.sh), containerd(k8s.io) 이미지 ID마다 누가 쓰는지와 판단,
# 고정(pin)된 이미지, 빌드 캐시 크기. 지울지는 사용자가 정한다 — 기준은 README 'colima 디스크 정리 기준'.
# 다른 프로젝트 네임스페이스가 쓰는 이미지는 대상이 아니다.
# '쓰는 곳'은 워크로드 정의(Deployment·StatefulSet·DaemonSet·Job·CronJob·Pod, 0개로 줄인 것 포함) 기준이다.
# 판단은 이미지 ID 단위다 — 같은 ID의 태그 행·다이제스트 행·<none> 행은 한 줄로 묶는다.
# 시험: IMAGE_LIST_TEST_WORKLOADS("네임스페이스<TAB>이미지 이미지…" 줄들)·IMAGE_LIST_TEST_IMAGES("저장소 태그 ID 크기" 줄들)를 주면
#       클러스터 대신 그 값으로 표만 만든다 (tests/test_disk_check.py).
set -u
export PATH="/usr/local/bin:/opt/homebrew/bin:$HOME/.local/bin:$PATH"
OWN_NS=docs                  # 이 저장소(docflow)의 네임스페이스
OWN_REPOS="docflow docflow-web"   # 이 저장소가 빌드하는 이미지 이름 (docker.io/library/ 앞머리를 뗀 정확한 이름)

if [ -n "${IMAGE_LIST_TEST_IMAGES:-}" ]; then
  workloads=${IMAGE_LIST_TEST_WORKLOADS:-}
  images=$IMAGE_LIST_TEST_IMAGES
else
  echo "== 이미지 디스크 (disk_check.sh — 멈춤 기준을 넘으면 '!!' 줄)"
  sh "$(dirname "$0")/disk_check.sh" </dev/null || true
  echo
  # 워크로드가 정의한 이미지. go-template을 쓴다 — 맥 kubectl 1.15의 jsonpath는 range 안의 range가 깨진다(2026-11-13 확인)
  t='{{range .items}}{{.metadata.namespace}}{{"\t"}}{{range .spec.template.spec.initContainers}}{{.image}} {{end}}{{range .spec.template.spec.containers}}{{.image}} {{end}}{{"\n"}}{{end}}'
  workloads=$(
    for k in deployments statefulsets daemonsets jobs; do kubectl get "$k" -A -o go-template="$t" </dev/null 2>/dev/null; done
    kubectl get cronjobs -A -o go-template='{{range .items}}{{.metadata.namespace}}{{"\t"}}{{range .spec.jobTemplate.spec.template.spec.containers}}{{.image}} {{end}}{{"\n"}}{{end}}' </dev/null 2>/dev/null
    kubectl get pods -A -o go-template='{{range .items}}{{.metadata.namespace}}{{"\t"}}{{range .spec.initContainers}}{{.image}} {{end}}{{range .spec.containers}}{{.image}} {{end}}{{"\n"}}{{end}}' </dev/null 2>/dev/null
  ) || true
  [ -n "$workloads" ] || echo "!! 클러스터에서 워크로드를 읽지 못함 — 아래 '쓰는 곳'이 비어 있다. 이 상태로는 아무것도 지우지 말 것" >&2
  images=$(colima nerdctl -- --namespace k8s.io images --format '{{.Repository}} {{.Tag}} {{.ID}} {{.Size}}' </dev/null) \
    || { echo "colima 이미지 목록을 읽지 못함 (colima가 꺼져 있나? 'colima list')" >&2; exit 1; }
fi

echo "== containerd(k8s.io) 이미지 ID별 쓰는 곳과 판단 (지우지 않음, 판단은 사용자)"
{
  printf '%s\n' "$workloads" | awk -F '\t' 'NF>=2 { n=split($2, a, " "); for (i=1; i<=n; i++) print "W\t" $1 "\t" a[i] }'
  printf '%s\n' "$images" | awk 'NF>=3 { s=""; for (i=4; i<=NF; i++) s = s (i>4 ? " " : "") $i; print "I\t" $1 "\t" $2 "\t" $3 "\t" s }'
} | awk -F '\t' -v own="$OWN_NS" -v ownrepos="$OWN_REPOS" '
  # 이름 맞추기(정확히): 다이제스트(@sha256:…)를 떼고, 레지스트리 앞머리는 docker.io/library/·docker.io/만 뗀다
  # (다른 레지스트리는 그대로 — ghcr.io/x/y와 x/y는 다른 이미지). 태그가 없으면 :latest
  function norm(r,   p) {
    sub(/@sha256:[0-9a-f]+$/, "", r); sub(/^docker\.io\/library\//, "", r); sub(/^docker\.io\//, "", r)
    p = r; sub(/.*\//, "", p); if (p !~ /:/) r = r ":latest"
    return r
  }
  function repo_of(r) { r = norm(r); sub(/:[^:\/]*$/, "", r); return r }
  function add(arr, k, v) { if (index(" " arr[k] " ", " " v " ") == 0) arr[k] = (arr[k] == "" ? v : arr[k] " " v) }
  BEGIN { m = split(ownrepos, a, " "); for (j = 1; j <= m; j++) mine_repo[a[j]] = 1
          sys_repo["registry.k8s.io/pause"] = 1; sys_repo["rancher/mirrored-pause"] = 1 }
  $1 == "W" { add(used, norm($3), $2); add(repons, repo_of($3), $2); next }
  $1 == "I" {
    id = $4
    if (!(id in seen)) { seen[id] = 1; order[++n] = id; size[id] = $5 }
    if ($2 != "<none>") {
      r = repo_of($2 ":x"); add(repos, id, r)
      if ($3 != "<none>") { k = norm($2 ":" $3); add(names, id, k); if (k in used) { u = split(used[k], a, " "); for (j = 1; j <= u; j++) add(idns, id, a[j]) } }
    }
  }
  END {
    printf "%-14s %-10s %-50s %s\n", "ID", "크기", "이름(같은 ID)", "쓰는 곳 → 판단"
    for (i = 1; i <= n; i++) {
      id = order[i]; ns = idns[id]
      nm = names[id]; if (nm == "") nm = (repos[id] == "" ? "<none>" : repos[id] ":<none>"); gsub(/ /, ",", nm)
      if (ns != "") {
        other = ""; mine = 0; m = split(ns, a, " ")
        for (j = 1; j <= m; j++) { if (a[j] == own) mine = 1; else other = other (other == "" ? "" : ",") a[j] }
        if (other == "") v = own " 사용 → 지우지 않음"
        else if (other == "kube-system" && !mine) v = "시스템(kube-system) → 대상 아님"
        else v = (mine ? own "," : "") other " 사용 → 다른 프로젝트·시스템 것, 대상 아님"
      } else {
        # 아무 워크로드도 이 ID를 쓰지 않음 — 같은 이름(저장소)을 누가 쓰는지로 주인을 가린다
        sys = 0; ownname = 0; other = ""; st = (names[id] == "" ? "태그 없음(dangling)" : "워크로드 없음")
        m = split(repos[id], rr, " ")
        for (j = 1; j <= m; j++) {
          if (rr[j] in sys_repo) sys = 1
          if (rr[j] in mine_repo) ownname = 1
          u = split(repons[rr[j]], a, " ")
          for (q = 1; q <= u; q++) { if (a[q] == own) ownname = 1; else if (index("," other ",", "," a[q] ",") == 0) other = other (other == "" ? "" : ",") a[q] }
        }
        if (sys) v = "시스템(pause) → 대상 아님"
        else if (other != "") v = st ", 같은 이름을 " other "가 씀 → 다른 프로젝트·시스템 것, 대상 아님"
        else if (ownname) v = st ", " own " 이미지의 예전 판 → 후보 (사용자 판단)"
        else if (m == 0) v = "이름 없음(dangling), 주인 모름 → 크기·만든 때를 보고 사용자 판단 (다른 프로젝트 것일 수 있음)"
        else v = st ", 주인 모름 → 다른 프로젝트 것일 수 있음. 주인 확인 전엔 지우지 않음"
      }
      printf "%-14s %-10s %-50s %s\n", substr(id, 1, 12), size[id], nm, v
    }
  }'

if [ -z "${IMAGE_LIST_TEST_IMAGES:-}" ]; then
  echo
  echo "== 고정(pin)된 이미지 — kubelet GC가 지우지 않는다 (우리 것은 docflow:dev·docflow-web:dev만이어야 함, build_image.sh)"
  # ctr 실패와 '고정 없음'을 구분한다 (파이프 끝 awk는 늘 성공하므로 출력을 먼저 받는다)
  if refs=$(colima ssh -- sudo ctr -n k8s.io images ls </dev/null 2>/dev/null); then
    pinned=$(printf '%s\n' "$refs" | awk '/io\.cri-containerd\.pinned=pinned/ {print "  " $1}')
    if [ -n "$pinned" ]; then printf '%s\n' "$pinned"; else echo "  (고정된 이미지 없음 — 우리 이미지도 고정 안 됨: 빌드 뒤 '== 이미지 고정' 줄 확인)"; fi
  else
    echo "!! ctr images ls 실패 — 고정 목록을 모른다 (colima ssh -- sudo ctr -n k8s.io images ls 로 확인)" >&2
  fi
  echo
  echo "== 빌드 캐시 (buildkit — 이미지 목록에 안 보이지만 같은 디스크를 쓴다. 이 VM에서 빌드하는 모든 프로젝트가 함께 쓴다)"
  colima ssh -- sudo buildctl du </dev/null 2>/dev/null | tail -2 || echo "(buildctl du를 부르지 못함)"
  echo
  echo "지우는 명령은 이 스크립트가 실행하지 않는다 — README 'colima 디스크 정리 기준'을 보고 사용자가 고른다."
fi
