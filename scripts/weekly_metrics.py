"""주간 개발 과정 지표 (https://github.com/reddol18/docflow/issues/24). gh + git만 쓴다 — LLM 호출 없음(토큰 0).

  uv run --no-project python -I scripts/weekly_metrics.py            # 이번 주 (월~일, KST)
  uv run --no-project python -I scripts/weekly_metrics.py --week 2026-W45
  ... --out docs/process                                              # `docs/process/2026-W45.md`에 저장

무엇을 재나
- 흐름: 병합한 PR 수, PR 크기, 열고 병합하기까지 걸린 시간, 배포 태그 수
- 절차 지킴: 병합한 PR마다 PR 본문·댓글에 아래 흔적이 있는지
    로컬 검사   `scripts/check.ps1`가 출력하는 요약 줄 "로컬 검사 `scripts/check.ps1` — 커밋 ..."
    계약 리뷰   "spec-reviewer" 또는 "spec-review"
    버그 리뷰   "code-review"
    보안 리뷰   "security-reviewer" — 닫는 이슈의 '보안 영향' 칸에 체크가 있을 때만 필요
    배포        병합 뒤 deploy-* 태그가 이 PR의 병합 커밋을 포함하는지
- 이슈: 연 것·닫은 것, 우선순위별 열린 수, 위험 수용(라벨 risk-accepted) 열린 수
- 보안: `security/asvs.yaml`의 미충족·일부·미판정 항목 수 (지금 작업 트리 기준)
흔적은 글자로 찾는다: PR 본문(템플릿 줄을 뺀 나머지)·댓글·리뷰 본문·줄 댓글.
템플릿 줄을 체크만 하고 결과를 안 적었으면 흔적이 아니다. 리뷰를 돌렸으면 결과를 PR 댓글로 남긴다.
한계: 글자로 찾으므로 "code-review는 다음에" 같은 언급도 잡힌다. 절차를 잊었는지 보는 용도이지 증명이 아니다.
병합 커밋이 이 PC에 없으면 멈춘다 (먼저 `git fetch --tags`).
"""
import argparse
import datetime as dt
import json
import re
import statistics
import subprocess
import sys
from pathlib import Path

REPO = "reddol18/docflow"
ROOT = Path(__file__).resolve().parent.parent
KST = dt.timezone(dt.timedelta(hours=9))
MARKS = {
    "로컬 검사": re.compile(r"로컬 검사 `scripts/check\.ps1` — 커밋"),
    "계약 리뷰": re.compile(r"spec-review"),
    "버그 리뷰": re.compile(r"code-review"),
    "보안 리뷰": re.compile(r"security-reviewer"),
}
SEC_SECTION = re.compile(r"#+\s*보안 영향\s*\n(.*?)(?=\n#+\s|\Z)", re.S)


def sh(*args):
    r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8")  # noqa: S603 — 고정된 gh·git 인자
    if r.returncode:
        raise SystemExit(f"{' '.join(args[:3])} 실패: {r.stderr.strip()[:500]}")
    return r.stdout


def gh_json(*args):
    return json.loads(sh("gh", *args, "--repo", REPO) or "[]")


def parse_ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def week_range(label):
    if label:
        y, w = label.split("-W")
        start = dt.datetime.fromisocalendar(int(y), int(w), 1).replace(tzinfo=KST)
    else:
        today = dt.datetime.now(KST)
        start = (today - dt.timedelta(days=today.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    y, w, _ = start.isocalendar()
    return f"{y}-W{w:02d}", start, start + dt.timedelta(days=7)


def _norm(line):
    return re.sub(r"\[[xX ]\]", "[ ]", line.strip())


TEMPLATE = {_norm(ln) for ln in (ROOT / ".github" / "pull_request_template.md").read_text(encoding="utf-8").splitlines()
            if ln.strip()}


def evidence(body):
    """PR 본문에서 템플릿이 원래 갖고 있는 글자는 뺀다: HTML 주석, 체크 안 한 칸, 템플릿 줄 그대로인 줄(체크만 한 것 포함).
    (템플릿에 '/code-review'·'security-reviewer'가 적혀 있으므로 그대로 두면 리뷰를 안 해도 흔적으로 잡힌다)"""
    body = re.sub(r"<!--.*?-->", "", body or "", flags=re.S)
    return "\n".join(ln for ln in body.splitlines()
                     if not re.match(r"\s*- \[ \]", ln) and _norm(ln) not in TEMPLATE)


def needs_security(issue_body):
    m = SEC_SECTION.search(issue_body or "")
    return bool(m and re.search(r"- \[[xX]\]", m.group(1)))


def deploy_tags():
    out = sh("git", "for-each-ref", "refs/tags/deploy-*", "--format=%(refname:short)\t%(creatordate:iso-strict)")
    return [(n, parse_ts(t)) for n, t in (ln.split("\t") for ln in out.splitlines() if ln.strip())]


def deployed(merge_sha):
    """이 커밋을 처음 포함한 배포 태그. 커밋이 이 PC에 없으면 '배포 안 됨'으로 잘못 세지 않게 멈춘다."""
    if subprocess.run(["git", "cat-file", "-e", f"{merge_sha}^{{commit}}"], capture_output=True).returncode:  # noqa: S603, S607
        raise SystemExit(f"병합 커밋 {merge_sha[:8]}이 이 PC에 없습니다. git fetch --tags 후 다시")
    names = sh("git", "tag", "--contains", merge_sha, "--list", "deploy-*", "--sort=creatordate").split()
    return names[0] if names else None


def pr_extra(n):
    """커밋 수와, 댓글이 아니라 리뷰로 남긴 글 (리뷰 본문·줄 댓글)"""
    v = json.loads(sh("gh", "pr", "view", str(n), "--repo", REPO, "--json", "commits,reviews"))
    inline = sh("gh", "api", "--paginate", f"repos/{REPO}/pulls/{n}/comments", "--jq", ".[].body")
    return len(v["commits"]), [r["body"] for r in v["reviews"]] + [inline]


def asvs_counts():
    p = ROOT / "security" / "asvs.yaml"
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    return {s: len(re.findall(rf"status:\s*{s}\b", text)) for s in ("미충족", "일부", "미판정")}


def collect(start, end):
    # commits는 목록에 넣으면 GraphQL 노드 상한을 넘는다 → 이 주 PR만 따로 센다
    fields = "number,title,createdAt,mergedAt,additions,deletions,body,comments,closingIssuesReferences,mergeCommit"
    # 병합 날짜로 찾는다 (최근 N개를 받아 거르면 오래 열려 있던 PR·지난 주가 빠진다)
    span = f"merged:{start.isoformat(timespec='seconds')}..{(end - dt.timedelta(seconds=1)).isoformat(timespec='seconds')}"
    prs = gh_json("pr", "list", "--state", "merged", "--search", span, "--limit", "200", "--json", fields)
    if len(prs) >= 200:
        raise SystemExit("한 주에 병합한 PR이 200개 이상: --limit을 올릴 것")
    prs = [p for p in prs if start <= parse_ts(p["mergedAt"]) < end]
    limit = 1000
    issues = gh_json("issue", "list", "--state", "all", "--limit", str(limit),
                     "--json", "number,title,createdAt,closedAt,labels,state,body")
    if len(issues) >= limit:
        raise SystemExit(f"이슈가 {limit}개 이상이라 열린 수가 틀린다: limit을 올릴 것")
    by_no = {i["number"]: i for i in issues}
    tags = deploy_tags()
    rows = []
    for p in sorted(prs, key=lambda p: p["number"]):
        commits, reviews = pr_extra(p["number"])
        text = "\n".join([evidence(p["body"])] + [c["body"] for c in p["comments"]] + reviews)
        closes = [c["number"] for c in p["closingIssuesReferences"]]
        refs = set(closes) | {int(n) for n in re.findall(r"#(\d+)", p["title"])}  # 제목의 (#N)도 관련 이슈로 본다
        sec = any(needs_security(by_no.get(n, {}).get("body")) for n in refs)
        marks = {k: bool(rx.search(text)) for k, rx in MARKS.items()}
        marks["보안 리뷰"] = marks["보안 리뷰"] if sec else None  # 필요 없으면 세지 않음
        sha = (p.get("mergeCommit") or {}).get("oid")
        rows.append({
            "n": p["number"], "title": p["title"], "size": p["additions"] + p["deletions"], "commits": commits,
            "hours": (parse_ts(p["mergedAt"]) - parse_ts(p["createdAt"])).total_seconds() / 3600,
            "closes": sorted(refs), "marks": marks, "deploy": deployed(sha) if sha else None,
        })
    return rows, issues, [t for t in tags if start <= t[1] < end]


def pct(xs):
    xs = [x for x in xs if x is not None]
    return f"{sum(xs)}/{len(xs)}" if xs else "–"


def render(label, start, end, rows, issues, tags):
    ok = lambda b: "–" if b is None else ("O" if b else "**X**")  # noqa: E731
    opened = [i for i in issues if start <= parse_ts(i["createdAt"]) < end]
    closed = [i for i in issues if i["closedAt"] and start <= parse_ts(i["closedAt"]) < end]
    open_now = [i for i in issues if i["state"] == "OPEN"]
    prio = {p: sum(any(lb["name"] == p for lb in i["labels"]) for i in open_now) for p in ("P0", "P1", "P2", "P3")}
    risk = sum(any(lb["name"] == "risk-accepted" for lb in i["labels"]) for i in open_now)
    last = end - dt.timedelta(days=1)
    out = [f"# 개발 과정 지표 {label}", "",
           f"기간: {start:%Y-%m-%d} ~ {last:%Y-%m-%d} (KST). 만든 시각 {dt.datetime.now(KST):%Y-%m-%d %H:%M}, "
           "`scripts/weekly_metrics.py` (gh + git, 토큰 0)", "",
           "## 요약", "", "| 지표 | 값 |", "|---|---|",
           f"| 병합한 PR | {len(rows)} |",
           f"| PR 크기 중앙값 (추가+삭제 줄) | {statistics.median([r['size'] for r in rows]) if rows else '–'} |",
           f"| 열고 병합까지 중앙값 (시간) | {statistics.median([r['hours'] for r in rows]) if rows else 0:.1f} |",
           f"| 배포 태그 | {len(tags)} |"]
    for k in MARKS:
        out.append(f"| 절차: {k} 흔적 있는 PR | {pct([r['marks'][k] for r in rows])} |")
    out.append(f"| 절차: 배포된 PR | {pct([bool(r['deploy']) for r in rows])} |")
    out += [f"| 이슈 연 것 / 닫은 것 | {len(opened)} / {len(closed)} |",
            f"| 열린 이슈 P0 / P1 / P2 / P3 | {prio['P0']} / {prio['P1']} / {prio['P2']} / {prio['P3']} |",
            f"| 열린 위험 수용 (risk-accepted) | {risk} |",
            "| 보안 체크리스트 미충족 / 일부 / 미판정 (`security/asvs.yaml`) | {미충족} / {일부} / {미판정} |".format(**asvs_counts()),
            ""]
    out += [
            "## PR별 절차", "", "| PR | 크기 | 커밋 | 시간 | 닫는 이슈 | " + " | ".join(MARKS) + " | 배포 |",
            "|---|---|---|---|---|" + "---|" * len(MARKS) + "---|"]
    for r in rows:
        out.append(f"| #{r['n']} {r['title'][:40]} | {r['size']} | {r['commits']} | {r['hours']:.1f} | "
                   f"{', '.join(f'#{n}' for n in r['closes']) or '–'} | "
                   + " | ".join(ok(r["marks"][k]) for k in MARKS) + f" | {r['deploy'] or '**X**'} |")
    out += ["", "O 흔적 있음, **X** 없음, – 해당 없음 (보안 영향 칸이 비었음). "
            "흔적은 PR 본문(템플릿 줄 제외)·댓글·리뷰의 글자로 찾는다.", "",
            "## 회고 (사람이 채움)", "", "- 잘된 것:", "- 빠뜨린 것:", "- 다음 주 바꿀 규칙 1개:", ""]
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", help="예: 2026-W45 (기본: 이번 주)")
    ap.add_argument("--out", help="이 폴더에 YYYY-Www.md로 저장")
    a = ap.parse_args(argv)
    label, start, end = week_range(a.week)
    md = render(label, start, end, *collect(start, end))
    if a.out:
        p = Path(a.out) / f"{label}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(md, encoding="utf-8")
        print(f"저장: {p}")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(md)


if __name__ == "__main__":
    main()
