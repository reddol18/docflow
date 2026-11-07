"""병합 전 절차 흔적 검사와 squash 병합 (https://github.com/reddol18/docflow/issues/83). gh만 쓴다 — LLM 호출 없음(토큰 0).
보통은 `scripts/merge.ps1`로 부른다.

  uv run --no-project python -I scripts/merge_gate.py 84            # 확인만 (병합 안 함)
  uv run --no-project python -I scripts/merge_gate.py 84 --merge    # 흔적이 다 있으면 squash 병합

확인하는 흔적 (PR 본문(템플릿 줄 뺌)·댓글·리뷰 본문·줄 댓글의 글자, 규칙은 `scripts/weekly_metrics.py`와 같다)
  로컬 검사   `scripts/check.ps1` 요약이 PR head 커밋에 대한 것이고("+ 커밋 안 된 변경" 아님) 요약 표에 실패가 없음
  계약 리뷰   한 글 안에 "spec-review"와 '| 지적 | 반영 |' 표 (코드 없는 PR은 면제)
  버그 리뷰   한 글 안에 "code-review"와 '| 지적 | 반영 |' 표 (코드 없는 PR은 면제)
  보안 리뷰   닫거나 가리키는 이슈(닫는 이슈·제목의 #N·본문 Refs #N)의 '보안 영향' 칸에 체크가 있으면,
              한 글 안에 "security-reviewer"와 '| 지적 | 반영 |' 표
병합: squash, 제목은 PR 제목 + " (#N)", `--match-head-commit`로 확인한 커밋 그대로만.
한계: 글자로 찾으므로 흔적의 내용이 참인지는 모른다 (weekly_metrics와 같음).
종료 코드: 0 통과(--merge면 병합함), 1 빠진 흔적, 2 병합할 수 없는 상태(이미 병합·닫힘·초안).
"""
import argparse
import importlib.util
import json
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location("weekly_metrics", Path(__file__).with_name("weekly_metrics.py"))
wm = importlib.util.module_from_spec(_spec)  # python -I로 돌리므로 import 경로 대신 파일로 불러온다
_spec.loader.exec_module(wm)

REPO = wm.REPO
FIELDS = "number,title,body,comments,reviews,files,headRefOid,state,isDraft,closingIssuesReferences"


def gh(*args):
    return wm.sh("gh", *args)


def fetch(n):
    pr = json.loads(gh("pr", "view", str(n), "--repo", REPO, "--json", FIELDS))
    inline = gh("api", "--paginate", f"repos/{REPO}/pulls/{n}/comments", "--jq", ".[].body")
    issues = {}
    for i in sorted(wm.issue_refs(pr["title"], pr["body"], [c["number"] for c in pr["closingIssuesReferences"]])):
        if i == n:
            continue
        got = json.loads(gh("api", f"repos/{REPO}/issues/{i}"))
        if "pull_request" not in got:  # "PR #38"의 38처럼 PR 번호는 이슈가 아니다
            issues[i] = got.get("body") or ""
    return pr, [inline] if inline.strip() else [], issues


def head_check(texts, head):
    """(통과?, 설명, 건너뛴 검사)"""
    blocks = [b for t in texts for b in wm.check_blocks(t)]
    if not blocks:
        return False, "로컬 검사 요약 없음 (`scripts/check.ps1` 출력 끝의 요약을 PR에 붙인다)", []
    mine = [(m, b) for m, b in blocks if m and len(m.group(1)) >= 7 and head.lower().startswith(m.group(1).lower())]
    if not mine:
        seen = ", ".join(sorted({m.group(1) for m, _ in blocks if m})) or "커밋 없음"
        return False, f"로컬 검사 요약이 head 커밋 `{head[:7]}`에 대한 것이 아님 (있는 요약: {seen})", []
    clean = [b for m, b in mine if "커밋 안 된 변경" not in m.group(2)]
    if not clean:
        return False, f"head 커밋 `{head[:7]}` 요약이 '+ 커밋 안 된 변경'뿐 (커밋한 뒤 다시 돌린다)", []
    ok = [b for b in clean if not wm.block_failed(b) and any(r.get("결과") for r in wm._tables(b, "검사", "결과"))]
    if not ok:
        return False, f"head 커밋 `{head[:7]}` 요약에 실패한 검사가 있거나 요약 표가 없음", []
    skipped = sorted({r.get("검사", "") for r in wm._tables(ok[-1], "검사", "결과") if r.get("결과", "").startswith("건너뜀")})
    return True, f"head 커밋 `{head[:7]}` 요약, 막는 검사 실패 없음", skipped


def has_review(texts, key):
    rx = wm.MARKS[key]
    return any(rx.search(t) and wm._tables(t, "지적", "반영") for t in texts)


def judge(pr, inline, issues):
    """→ [(항목, 결과, 설명)]. 결과 True 있음 / False 빠짐 / None 요구하지 않음"""
    texts = wm.evidence_texts(pr["body"], [c["body"] for c in pr["comments"]],
                              [r["body"] for r in pr.get("reviews") or []] + inline)
    no_code = wm.is_no_code([f["path"] for f in pr["files"]])
    ok, why, skipped = head_check(texts, pr["headRefOid"])
    rows = [("로컬 검사", ok, why + (f" — 건너뛴 검사: {', '.join(skipped)}" if skipped else ""))]
    for k in wm.REVIEWS:
        if no_code:
            rows.append((k, None, "코드 없는 PR (`docs/`·README·잠금 파일·의존성 목록만)"))
        else:
            got = has_review(texts, k)
            rows.append((k, got, "결과 댓글 있음" if got else
                         f"결과 댓글 없음 (한 글 안에 '{wm.MARKS[k].pattern}'와 '| 지적 | 반영 |' 표)"))
    sec = sorted(i for i, b in issues.items() if wm.needs_security(b))
    if not sec:
        rows.append(("보안 리뷰", None, "관련 이슈의 보안 영향 칸에 체크 없음"))
    else:
        got = has_review(texts, "보안 리뷰")
        where = ", ".join(f"#{i}" for i in sec)
        rows.append(("보안 리뷰", got, f"{where} 보안 영향 체크 — " + ("결과 댓글 있음" if got else
                     "결과 댓글 없음 (한 글 안에 'security-reviewer'와 '| 지적 | 반영 |' 표)")))
    return rows


def subject(pr):
    tail = f"(#{pr['number']})"
    return pr["title"] if pr["title"].rstrip().endswith(tail) else f"{pr['title']} {tail}"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("pr", type=int)
    ap.add_argument("--merge", action="store_true", help="흔적이 다 있으면 squash 병합")
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    pr, inline, issues = fetch(a.pr)
    rows = judge(pr, inline, issues)
    print(f"PR #{pr['number']} {pr['title']}")
    print(f"head {pr['headRefOid'][:7]}, 상태 {pr['state']}{' (초안)' if pr.get('isDraft') else ''}, "
          f"관련 이슈 {', '.join(f'#{i}' for i in sorted(issues)) or '없음'}")
    mark = {True: "O", False: "X", None: "-"}
    for k, v, why in rows:
        print(f"  {mark[v]} {k}: {why}")
    missing = [k for k, v, _ in rows if v is False]
    if missing:
        print(f"판정: 거부 — 빠진 흔적: {', '.join(missing)}. 병합하지 않습니다.")
        return 1
    print("판정: 통과")
    if pr["state"] != "OPEN" or pr.get("isDraft"):
        print(f"병합할 수 없는 상태입니다 ({pr['state']}{', 초안' if pr.get('isDraft') else ''}).")
        return 2
    if not a.merge:
        return 0
    s = subject(pr)
    gh("pr", "merge", str(pr["number"]), "--repo", REPO, "--squash", "--subject", s,
       "--match-head-commit", pr["headRefOid"])
    print(f"병합함: {s} (head {pr['headRefOid'][:7]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
