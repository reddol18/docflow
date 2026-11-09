"""병합 전 절차 흔적 검사와 squash 병합 (https://github.com/reddol18/docflow/issues/83). gh만 쓴다 — LLM 호출 없음(토큰 0).
보통은 `scripts/merge.ps1`로 부른다.

  uv run --no-project python -I scripts/merge_gate.py 84                       # 확인만 (병합 안 함)
  uv run --no-project python -I scripts/merge_gate.py 84 --merge               # 확인하고, 통과면 그 head로 squash 병합
  ... 84 --merge --expect-head <sha>                                           # 사용자가 확인한 head가 아니면 거부

흔적은 저장소 주인·멤버·협업자(authorAssociation OWNER/MEMBER/COLLABORATOR)가 쓴 PR 본문·댓글·리뷰·줄 댓글에서만 찾는다
(공개 저장소 — 외부인이 지어낸 요약·리뷰 표는 세지 않는다, https://github.com/reddol18/docflow/issues/85).
확인하는 흔적 (글자로 찾는다)
  로컬 검사   PR 본문(템플릿 줄 뺌)·댓글·리뷰의 `scripts/check.ps1` 요약 중, PR head 커밋에 대한 것("+ 커밋 안 된 변경" 아님)이고
              막는 검사(CHECKS) 모두 '통과', trivy는 '보고만'/'통과'. '건너뜀'은 거부 —
              단 코드 없는 PR은 osv·semgrep·trivy 건너뜀(-Quick) 허용
  계약 리뷰   댓글·리뷰 본문·줄 댓글 중 한 글 안에 "spec-review"와 '| 지적 | 반영 |' 표 (코드 없는 PR은 면제)
  버그 리뷰   같은 방식으로 "code-review" (코드 없는 PR은 면제)
  보안 리뷰   닫거나 가리키는 이슈(닫는 이슈·제목의 #N·본문 줄 머리 Refs #N/이 저장소 이슈 URL)의 '보안 영향' 칸에
              체크가 있으면 같은 방식으로 "security-reviewer"
  local-check status   head 커밋의 commit status `docflow/local-check`가 success (`scripts/gate_status.py`가 조건이 맞을 때만
              올린다 — 글자 요약은 누구나 붙일 수 있으므로 이것으로 실제 통과를 확인한다)
  리뷰는 PR 본문에서 찾지 않는다 (템플릿을 채운 글과 구별할 수 없다).
`scripts/weekly_metrics.py`와 함수를 공유한다(evidence·check_blocks·block_failed·_tables·issue_refs·is_no_code·needs_security·
MARKS). 주간 지표는 흔적이 있는지만 세고, 이 검사가 더 엄격하다(head 기준, 표 필요, 건너뜀 거부, 리뷰는 본문 제외).
병합: squash, 제목은 PR 제목 + " (#N)", 판정한 head 커밋 그대로만(`--match-head-commit`).
  병합 실패(status 반영 지연 등)면 3초 뒤 head를 다시 확인하고 두 번까지 다시 시도한다.
commit status `docflow/review-gate` (https://github.com/reddol18/docflow/issues/85): main ruleset(`scripts/protect_main.ps1`)이
  이것과 `docflow/local-check`를 요구한다. success는 --merge(사용자 승인 뒤)에서 판정 통과일 때만, 병합 직전에 올린다 —
  판정만 하는 호출에서는 올리지 않으므로 웹 화면·`gh pr merge`로 승인 없이 병합할 수 없다.
  판정이 거부인데 head에 이미 success가 있으면(그 뒤 근거가 바뀜) 같은 context에 failure를 올려 덮는다.
한계: 흔적의 내용이 참인지는 모른다 (weekly_metrics와 같음).
종료 코드: 0 통과(--merge면 병합함), 1 빠진 흔적,
           2 병합할 수 없음(이미 병합·닫힘·초안, --expect-head와 head 다름, 가리킨 이슈가 없음 등 — 메시지를 본다).
"""
import argparse
import datetime as dt
import importlib.util
import json
import re
import sys
import time
from pathlib import Path

_spec = importlib.util.spec_from_file_location("weekly_metrics", Path(__file__).with_name("weekly_metrics.py"))
wm = importlib.util.module_from_spec(_spec)  # python -I로 돌리므로 import 경로 대신 파일로 불러온다
_spec.loader.exec_module(wm)

REPO = wm.REPO
FIELDS = "number,title,body,comments,reviews,files,headRefOid,state,isDraft,closingIssuesReferences,url"
CHECKS = ("frontend", "ruff", "lock", "pytest", "gitleaks", "osv", "semgrep")  # `scripts/check.ps1`의 막는 검사
# 나중에 생긴 막는 검사: 생기기 전 요약에는 줄이 없으므로 없으면 통과, 있으면 '통과'여야 한다(건너뜀 거부).
# lock: https://github.com/reddol18/docflow/issues/33 (check.ps1 lock 단계)
ADDED_LATER = {"lock"}
REPORT_ONLY = {"trivy": ("보고만", "통과")}  # 막지 않는 검사: 돌리기는 해야 한다
NO_CODE_SKIP = {"osv", "semgrep", "trivy"}  # 코드 없는 PR이 건너뛰어도 되는 검사 (check.ps1 -Quick)
# main ruleset이 요구하는 commit status (https://github.com/reddol18/docflow/issues/85, `scripts/protect_main.ps1`)
CONTEXT_CHECK = "docflow/local-check"  # `scripts/gate_status.py`가 `scripts/check.ps1` 끝에서 올린다
CONTEXT_REVIEW = "docflow/review-gate"  # 이 파일이 --merge 판정 통과일 때 올린다
TRUSTED = {"OWNER", "MEMBER", "COLLABORATOR"}  # 흔적으로 세는 글쓴이 (authorAssociation)
MERGE_TRIES = 3
sleep = time.sleep  # 시험에서 바꾼다


class Stop(Exception):
    """병합할 수 없음 (종료 코드 2)"""


def gh(*args):
    return wm.sh("gh", *args)


def post_status(sha, context, description, state="success", target_url=None):
    """commit status 하나를 올린다 (실패면 SystemExit(메시지) — wm.sh와 같음)"""
    args = ["api", "-X", "POST", f"repos/{REPO}/statuses/{sha}", "-f", f"state={state}",
            "-f", f"context={context}", "-f", f"description={description[:140]}"]
    if target_url:
        args += ["-f", f"target_url={target_url}"]
    gh(*args)


def statuses(sha):
    """head 커밋의 context별 최신 status 상태 {context: state}"""
    got = json.loads(gh("api", f"repos/{REPO}/commits/{sha}/status"))
    return {s["context"]: s["state"] for s in got.get("statuses") or []}


def fetch(n):
    pr = json.loads(gh("pr", "view", str(n), "--repo", REPO, "--json", FIELDS))
    # gh pr view에는 PR 글쓴이의 authorAssociation이 없다 — 본문을 흔적으로 셀지 정한다
    try:
        pr["bodyAuthorAssociation"] = gh("api", f"repos/{REPO}/pulls/{n}", "--jq", ".author_association").strip()
        pr["statuses"] = statuses(pr["headRefOid"])
    except SystemExit as e:
        raise Stop(f"PR 글쓴이·commit status를 읽지 못함: {e}") from None
    # 줄 댓글은 하나씩 따로 본다 (합치면 다른 글의 이름과 표가 붙는다)
    out = gh("api", "--paginate", f"repos/{REPO}/pulls/{n}/comments", "--jq",
             ".[] | {body, authorAssociation: .author_association} | @json")
    inline = [json.loads(ln) for ln in out.splitlines() if ln.strip()]
    issues = {}
    for i in sorted(wm.issue_refs(pr["title"], pr["body"], [c["number"] for c in pr["closingIssuesReferences"]])):
        if i == n:
            continue
        try:
            got = json.loads(gh("api", f"repos/{REPO}/issues/{i}"))
        except SystemExit as e:  # wm.sh는 gh 실패를 SystemExit(메시지)로 올린다
            if re.search(r"404|Not Found", str(e)):
                raise Stop(f"PR이 가리키는 #{i}가 이 저장소에 없습니다 (제목·본문의 번호를 확인)") from None
            raise Stop(f"#{i} 이슈를 읽지 못함: {e}") from None
        if "pull_request" not in got:  # "PR #38"의 38처럼 PR 번호는 이슈가 아니다
            issues[i] = got.get("body") or ""
    return pr, inline, issues


def _check_ok(block, no_code):
    """요약 덩어리 하나 → 안 맞는 이유 목록 (비면 통과)"""
    rows = {r.get("검사", ""): r.get("결과", "") for r in wm._tables(block, "검사", "결과")}
    bad = ["실패 표시"] if wm.block_failed(block) else []
    for c in CHECKS + tuple(REPORT_ONLY):
        v = rows.get(c)
        ok = REPORT_ONLY.get(c, ("통과",))
        if v is None and c in ADDED_LATER:
            continue  # 그 검사가 생기기 전 요약: 줄이 없으면 넘어간다 (줄이 있으면 아래 규칙 그대로)
        if v is None:
            bad.append(f"{c} 없음")
        elif v.startswith("건너뜀"):
            if not (no_code and c in NO_CODE_SKIP):
                bad.append(f"{c} 건너뜀")
        elif not v.startswith(ok):
            bad.append(f"{c} {v}")
    return bad


def head_check(texts, head, no_code=False):
    """(통과?, 설명)"""
    blocks = [b for t in texts for b in wm.check_blocks(t)]
    if not blocks:
        return False, "로컬 검사 요약 없음 (`scripts/check.ps1` 출력 끝의 요약을 PR에 붙인다)"
    mine = [(m, b) for m, b in blocks if m and len(m.group(1)) >= 7 and head.lower().startswith(m.group(1).lower())]
    if not mine:
        seen = ", ".join(sorted({m.group(1) for m, _ in blocks if m})) or "커밋 없음"
        return False, f"로컬 검사 요약이 head 커밋 `{head[:7]}`에 대한 것이 아님 (있는 요약: {seen})"
    clean = [b for m, b in mine if "커밋 안 된 변경" not in m.group(2)]
    if not clean:
        return False, f"head 커밋 `{head[:7]}` 요약이 '+ 커밋 안 된 변경'뿐 (커밋한 뒤 다시 돌린다)"
    why = [_check_ok(b, no_code) for b in clean]
    if not any(not w for w in why):
        return False, f"head 커밋 `{head[:7]}` 요약이 막는 검사 모두 통과가 아님: {', '.join(why[-1])}"
    return True, f"head 커밋 `{head[:7]}` 요약, 막는 검사 모두 통과"


def has_review(texts, key):
    rx = wm.MARKS[key]
    return any(rx.search(t) and wm._tables(t, "지적", "반영") for t in texts)


def judge(pr, inline, issues):
    """→ [(항목, 결과, 설명)]. 결과 True 있음 / False 빠짐 / None 요구하지 않음"""
    posts = list(pr["comments"]) + list(pr.get("reviews") or []) + list(inline)
    comments = [p["body"] for p in posts if p.get("authorAssociation") in TRUSTED]  # 외부인 글은 흔적이 아니다
    body = wm.evidence(pr["body"]) if pr.get("bodyAuthorAssociation") in TRUSTED else ""
    no_code = wm.is_no_code([f["path"] for f in pr["files"]])
    rows = [("로컬 검사", *head_check([body] + comments, pr["headRefOid"], no_code))]
    st = (pr.get("statuses") or {}).get(CONTEXT_CHECK)
    rows.append(("local-check status", st == "success",
                 f"head 커밋 {CONTEXT_CHECK} = {st or '없음'}" + ("" if st == "success" else
                 " (push한 뒤 `scripts/check.ps1`을 다시 돌린다 — 조건이 맞으면 올린다)")))
    hint = "결과 댓글 없음 (댓글·리뷰 한 글 안에 '{}'와 '| 지적 | 반영 |' 표. PR 본문은 보지 않는다)"
    for k in wm.REVIEWS:
        if no_code:
            rows.append((k, None, "코드 없는 PR (`docs/`·README·package-lock 같은 잠금 파일만)"))
        else:
            got = has_review(comments, k)
            rows.append((k, got, "결과 댓글 있음" if got else hint.format(wm.MARKS[k].pattern)))
    sec = sorted(i for i, b in issues.items() if wm.needs_security(b))
    if not sec:
        rows.append(("보안 리뷰", None, "관련 이슈의 보안 영향 칸에 체크 없음"))
    else:
        got = has_review(comments, "보안 리뷰")
        where = ", ".join(f"#{i}" for i in sec)
        rows.append(("보안 리뷰", got, f"{where} 보안 영향 체크 — " + ("결과 댓글 있음" if got else
                     hint.format("security-reviewer"))))
    return rows


def subject(pr):
    tail = f"(#{pr['number']})"
    return pr["title"] if pr["title"].rstrip().endswith(tail) else f"{pr['title']} {tail}"


def _merge(pr, head):
    """판정한 head 그대로 squash 병합. 실패면 잠깐 뒤 head를 다시 보고 다시 시도 (status 반영 지연 대비)"""
    s = subject(pr)
    for i in range(1, MERGE_TRIES + 1):
        try:
            gh("pr", "merge", str(pr["number"]), "--repo", REPO, "--squash", "--subject", s, "--match-head-commit", head)
            return s
        except SystemExit as e:
            if i == MERGE_TRIES:
                raise Stop(f"병합 실패 ({MERGE_TRIES}번 시도): {e}") from None
            print(f"병합 실패 — 3초 뒤 다시 시도 ({i + 1}/{MERGE_TRIES}): {e}")
        sleep(3)
        now = json.loads(gh("pr", "view", str(pr["number"]), "--repo", REPO, "--json", "headRefOid,state,mergeStateStatus"))
        if now["headRefOid"] != head or now["state"] != "OPEN":
            raise Stop(f"다시 시도하지 않음: head {now['headRefOid'][:7]}, 상태 {now['state']}")
        print(f"  병합 상태 {now.get('mergeStateStatus', '?')}")
    raise Stop("병합 실패")  # 닿지 않음


def run(n, merge=False, expect_head=None):
    pr, inline, issues = fetch(n)
    head = pr["headRefOid"]
    print(f"PR #{pr['number']} {pr['title']}")
    print(f"head {head}, 상태 {pr['state']}{' (초안)' if pr.get('isDraft') else ''}, "
          f"관련 이슈 {', '.join(f'#{i}' for i in sorted(issues)) or '없음'}")
    if expect_head and not head.lower().startswith(expect_head.lower()):
        raise Stop(f"head가 바뀌었습니다: 확인한 {expect_head[:7]} → 지금 {head[:7]}. 다시 확인하세요")
    rows = judge(pr, inline, issues)
    mark = {True: "O", False: "X", None: "-"}
    for k, v, why in rows:
        print(f"  {mark[v]} {k}: {why}")
    missing = [k for k, v, _ in rows if v is False]
    now = f"{dt.datetime.now().astimezone():%Y-%m-%d %H:%M}"
    if missing:
        print(f"판정: 거부 — 빠진 흔적: {', '.join(missing)}. 병합하지 않습니다.")
        if (pr.get("statuses") or {}).get(CONTEXT_REVIEW) == "success":  # 전에 통과했는데 근거가 바뀜 → 덮는다
            try:
                post_status(head, CONTEXT_REVIEW, f"merge_gate 거부: {', '.join(missing)} · {now}", "failure", pr.get("url"))
                print(f"commit status {CONTEXT_REVIEW}: 전의 success를 failure로 덮음 ({head[:7]})")
            except SystemExit as e:
                print(f"commit status {CONTEXT_REVIEW} failure를 올리지 못함: {e}")
        return 1
    print("판정: 통과")
    if pr["state"] != "OPEN" or pr.get("isDraft"):
        raise Stop(f"병합할 수 없는 상태입니다 ({pr['state']}{', 초안' if pr.get('isDraft') else ''}).")
    if not merge:
        return 0
    try:  # ruleset이 요구하므로 병합 직전에 (사용자 승인 뒤의 --merge 호출에서만)
        post_status(head, CONTEXT_REVIEW, f"merge_gate 판정 통과 (로컬 검사·리뷰 흔적) · {now}", "success", pr.get("url"))
    except SystemExit as e:
        raise Stop(f"commit status {CONTEXT_REVIEW}를 올리지 못함: {e}") from None
    print(f"commit status {CONTEXT_REVIEW} = success 올림 ({head[:7]})")
    s = _merge(pr, head)
    print(f"병합함: {s} (head {head[:7]})")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("pr", type=int)
    ap.add_argument("--merge", action="store_true", help="흔적이 다 있으면 판정한 head 그대로 squash 병합")
    ap.add_argument("--expect-head", help="사용자가 확인한 head 커밋. 지금 head가 다르면 거부(종료 코드 2)")
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        return run(a.pr, a.merge, a.expect_head)
    except Stop as e:
        print(f"병합하지 않습니다: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
