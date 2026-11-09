"""scripts/merge_gate.py — 병합 전 절차 흔적 검사 (https://github.com/reddol18/docflow/issues/83).
PR 데이터는 지어낸 것이고 gh는 가짜로 바꾼다 (실제 GitHub 호출·병합 없음)."""
import importlib.util
import json
from pathlib import Path

import pytest

_p = Path(__file__).parent.parent / "scripts" / "merge_gate.py"
_spec = importlib.util.spec_from_file_location("merge_gate", _p)
mg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mg)

HEAD = "abc1234def5678900000000000000000000000000"
CHECK_OK = ("로컬 검사 `scripts/check.ps1` — 커밋 `abc1234`, 2026-11-07 12:00\n"
            "| 검사 | 결과 | 초 |\n|---|---|---|\n| frontend | 통과 | 10 |\n| ruff | 통과 | 0 |\n| pytest | 통과 | 18 |\n"
            "| gitleaks | 통과 | 1 |\n| osv | 통과 | 2 |\n| semgrep | 통과 | 12 |\n"
            "| trivy | 보고만 (출력 확인) | 9 |\n\n막는 검사 모두 통과")
QUICK = (CHECK_OK.replace("| osv | 통과 | 2 |", "| osv | 건너뜀 | 0 |")
         .replace("| semgrep | 통과 | 12 |", "| semgrep | 건너뜀 | 0 |")
         .replace("| trivy | 보고만 (출력 확인) | 9 |", "| trivy | 건너뜀 | 0 |"))
SPEC = "## 계약 리뷰 (spec-reviewer)\n| 지적 | 반영 |\n|---|---|\n| 조건 2 근거 없음 | 본문에 추가 |"
CODE = "## 코드 리뷰 (`/code-review high`)\n| 지적 | 반영 |\n|---|---|\n| 경계값 | 고침 |"
SEC = "## 보안 리뷰 (security-reviewer)\n| 지적 | 반영 |\n|---|---|\n| (낮) 로그 | 그대로 |"
ISSUE_SEC = "## 목표\nx\n\n## 보안 영향\n- [x] 새 엔드포인트\n- [ ] 로그\n"
ISSUE_PLAIN = "## 목표\nx\n\n## 보안 영향\n- [ ] 새 엔드포인트\n"
DOCS = ("docs/process/2026-W45.md", "README.md", "frontend/package-lock.json")


def make_pr(body=CHECK_OK, comments=(SPEC, CODE), files=("app/main.py",), title="기능 (#7)", head=HEAD,
            state="OPEN", closes=(), reviews=()):
    return {"number": 90, "title": title, "body": "Closes #7\n\n## 테스트\n" + body,
            "comments": [{"body": c} for c in comments], "reviews": [{"body": r} for r in reviews],
            "files": [{"path": f} for f in files], "headRefOid": head, "state": state, "isDraft": False,
            "closingIssuesReferences": [{"number": n} for n in closes]}


class FakeGh:
    def __init__(self, pr, issues=None, inline=()):
        self.pr, self.issues, self.inline, self.calls = pr, issues or {}, list(inline), []

    def __call__(self, *args):
        self.calls.append(args)
        if args[:2] == ("pr", "view"):
            return json.dumps(self.pr)
        if args[:2] == ("pr", "merge"):
            return ""
        if args[0] == "api" and "--paginate" in args:
            return "\n".join(json.dumps(b) for b in self.inline) + "\n"  # --jq '.[].body | @json'
        if args[0] == "api":
            n = int(args[1].rsplit("/", 1)[1])
            if n not in self.issues:
                raise SystemExit(f"gh api {args[1]} 실패: gh: Not Found (HTTP 404)")
            return json.dumps(self.issues[n])
        raise AssertionError(args)

    @property
    def merges(self):
        return [c for c in self.calls if c[:2] == ("pr", "merge")]


PLAIN = {7: {"body": ISSUE_PLAIN}}


def run(monkeypatch, pr, issues=PLAIN, *flags, inline=()):
    fake = FakeGh(pr, issues, inline)
    monkeypatch.setattr(mg, "gh", fake)
    return mg.main([str(pr["number"]), *flags]), fake


def missing(pr, issues=None, inline=()):
    return [k for k, v, _ in mg.judge(pr, list(inline), issues or {}) if v is False]


def test_all_evidence_passes_and_merges_squash_at_judged_head(monkeypatch, capsys):
    code, fake = run(monkeypatch, make_pr(title="기능 추가"), PLAIN, "--merge")
    assert code == 0
    assert fake.merges == [("pr", "merge", "90", "--repo", mg.REPO, "--squash", "--subject", "기능 추가 (#90)",
                            "--match-head-commit", HEAD)]
    out = capsys.readouterr().out
    assert "판정: 통과" in out and f"head {HEAD}," in out  # merge.ps1이 이 줄에서 head를 읽는다
    assert len([c for c in fake.calls if c[:2] == ("pr", "view")]) == 1  # 판정 한 번


def test_dry_run_never_merges(monkeypatch):
    code, fake = run(monkeypatch, make_pr())
    assert code == 0 and not fake.merges


def test_expect_head_pins_approved_commit(monkeypatch, capsys):
    code, fake = run(monkeypatch, make_pr(), PLAIN, "--merge", "--expect-head", HEAD)
    assert code == 0 and fake.merges[0][-1] == HEAD
    code, fake = run(monkeypatch, make_pr(), PLAIN, "--merge", "--expect-head", "fff0000")
    assert code == 2 and not fake.merges
    assert "head가 바뀌었습니다" in capsys.readouterr().out


def test_subject_keeps_existing_suffix():
    assert mg.subject({"number": 90, "title": "기능 (#90)"}) == "기능 (#90)"
    assert mg.subject({"number": 90, "title": "기능 (#7)"}) == "기능 (#7) (#90)"


@pytest.mark.parametrize("body, why", [
    ("테스트 칸 비움", "요약 없음"),
    (CHECK_OK.replace("abc1234", "1111111"), "head 커밋"),
    (CHECK_OK.replace("`abc1234`,", "`abc1234` + 커밋 안 된 변경,"), "커밋 안 된 변경"),
    (CHECK_OK.replace("| pytest | 통과 | 18 |", "| pytest | **실패** | 18 |").replace("막는 검사 모두 통과", "실패: pytest"),
     "pytest"),
    ("로컬 검사 `scripts/check.ps1` — 커밋 `abc1234`, 막는 검사 모두 통과", "frontend 없음"),  # 표 없음
    (CHECK_OK.replace("| semgrep | 통과 | 12 |\n", ""), "semgrep 없음"),  # 막는 검사 행이 빠짐
    (CHECK_OK.replace("| pytest | 통과 | 18 |", "| pytest | 건너뜀 | 0 |"), "pytest 건너뜀"),
    (QUICK, "osv 건너뜀"),  # 코드 PR의 -Quick
])
def test_local_check_rejected(monkeypatch, capsys, body, why):
    code, fake = run(monkeypatch, make_pr(body=body), PLAIN, "--merge")
    out = capsys.readouterr().out
    assert code == 1 and not fake.merges
    assert "X 로컬 검사" in out and why in out


def test_quick_allowed_only_for_no_code_pr():
    assert missing(make_pr(body=QUICK, comments=(), files=DOCS)) == []
    # 코드 없는 PR이어도 막는 검사 frontend 등은 건너뛰면 안 된다
    skipped = QUICK.replace("| ruff | 통과 | 0 |", "| ruff | 건너뜀 | 0 |")
    assert missing(make_pr(body=skipped, comments=(), files=DOCS)) == ["로컬 검사"]


def test_lock_row_added_later():
    # check.ps1 lock 단계(https://github.com/reddol18/docflow/issues/33) 전 요약: 줄이 없으면 통과
    assert missing(make_pr()) == []
    with_lock = CHECK_OK.replace("| ruff | 통과 | 0 |\n", "| ruff | 통과 | 0 |\n| lock | 통과 | 0 |\n")
    assert missing(make_pr(body=with_lock)) == []
    # 줄이 있으면 통과여야 한다 — 건너뜀은 코드 없는 PR이어도 거부
    skipped = with_lock.replace("| lock | 통과 | 0 |", "| lock | 건너뜀 | 0 |")
    assert missing(make_pr(body=skipped)) == ["로컬 검사"]
    assert missing(make_pr(body=skipped, comments=(), files=DOCS)) == ["로컬 검사"]
    failed = with_lock.replace("| lock | 통과 | 0 |", "| lock | **실패** | 0 |")
    assert missing(make_pr(body=failed)) == ["로컬 검사"]


def test_requirements_change_needs_reviews():
    # 이미지 입력이라 코드 없는 PR이 아니다: 리뷰 필요, -Quick 거부
    assert missing(make_pr(comments=(), files=("requirements.lock",))) == ["계약 리뷰", "버그 리뷰"]
    assert missing(make_pr(body=QUICK, files=("requirements.txt",))) == ["로컬 검사"]


def test_local_check_template_line_is_not_evidence():
    body = "- [x] 로컬 검사 `scripts/check.ps1` 통과 — 출력 끝의 요약 표를 붙인다"
    assert missing(make_pr(body=body)) == ["로컬 검사"]


def test_later_passing_run_on_head_counts():
    failed = CHECK_OK.replace("abc1234", "0000000").replace("| ruff | 통과 | 0 |", "| ruff | **실패** | 0 |")
    assert missing(make_pr(body=failed, comments=(SPEC, CODE, CHECK_OK))) == []


@pytest.mark.parametrize("comments, gone", [
    ((CODE,), ["계약 리뷰"]),
    ((SPEC,), ["버그 리뷰"]),
    ((), ["계약 리뷰", "버그 리뷰"]),
    (("계약 리뷰 spec-review 통과", "code-review 했음"), ["계약 리뷰", "버그 리뷰"]),  # 표 없는 언급
    (("spec-review", "| 지적 | 반영 |\n|---|---|\n| a | 고침 |", CODE), ["계약 리뷰"]),  # 이름과 표가 다른 글
])
def test_review_rejected(monkeypatch, comments, gone):
    pr = make_pr(comments=comments)
    assert missing(pr) == gone
    code, fake = run(monkeypatch, pr, PLAIN, "--merge")
    assert code == 1 and not fake.merges


def test_review_in_pr_body_is_not_evidence():
    # 본문은 템플릿을 채운 글과 구별할 수 없다 — 이름·표가 다 있어도 리뷰 흔적이 아니다
    body = CHECK_OK + "\n\n## 리뷰\n" + SPEC + "\n\n" + CODE
    assert missing(make_pr(body=body, comments=())) == ["계약 리뷰", "버그 리뷰"]


def test_reviews_from_comment_review_body_and_each_inline_comment(monkeypatch):
    assert missing(make_pr(comments=(SPEC + "\n\n" + CODE,))) == []
    assert missing(make_pr(comments=(), reviews=(SPEC, CODE))) == []
    assert missing(make_pr(comments=(SPEC,)), inline=[CODE]) == []
    # 줄 댓글은 합치지 않는다: 이름과 표가 다른 줄 댓글에 있으면 흔적 아님
    assert missing(make_pr(comments=(SPEC,)), inline=["code-review", "| 지적 | 반영 |\n|---|---|\n| a | b |"]) \
        == ["버그 리뷰"]
    # fetch가 줄 댓글을 글마다 따로 받는다 (여러 줄 글 포함)
    monkeypatch.setattr(mg, "gh", FakeGh(make_pr(), PLAIN, ["첫 줄\n둘째 줄", CODE]))
    _, inline, _ = mg.fetch(90)
    assert inline == ["첫 줄\n둘째 줄", CODE]


def test_security_review_needed_when_issue_box_checked(monkeypatch, capsys):
    code, fake = run(monkeypatch, make_pr(), {7: {"body": ISSUE_SEC}}, "--merge")
    assert code == 1 and not fake.merges
    assert "X 보안 리뷰" in capsys.readouterr().out
    code, fake = run(monkeypatch, make_pr(comments=(SPEC, CODE, SEC)), {7: {"body": ISSUE_SEC}}, "--merge")
    assert code == 0 and len(fake.merges) == 1


def test_security_from_title_refs_and_urls_but_not_pr_numbers(monkeypatch):
    pr = make_pr(title="PR #38 리뷰 반영 (#12)")
    pr["body"] = ("Refs #13, #14\nRefs: https://github.com/reddol18/docflow/issues/15\n"
                  "Refs https://github.com/other/repo/issues/16\n" + CHECK_OK)
    issues = {12: {"body": ISSUE_PLAIN}, 13: {"body": ISSUE_PLAIN}, 14: {"body": ISSUE_PLAIN},
              15: {"body": ISSUE_SEC}, 38: {"number": 38, "pull_request": {}}}
    monkeypatch.setattr(mg, "gh", FakeGh(pr, issues))
    got, _, found = mg.fetch(90)
    assert sorted(found) == [12, 13, 14, 15]  # 38은 PR, 16은 다른 저장소
    assert missing(got, found) == ["보안 리뷰"]


def test_missing_issue_is_clear_error(monkeypatch, capsys):
    code, fake = run(monkeypatch, make_pr(title="기능 (#999)"), PLAIN, "--merge")
    assert code == 2 and not fake.merges
    assert "#999가 이 저장소에 없습니다" in capsys.readouterr().out


def test_no_code_pr_skips_reviews_but_not_local_check():
    assert missing(make_pr(comments=(), files=DOCS)) == []
    assert missing(make_pr(body="", comments=(), files=DOCS)) == ["로컬 검사"]
    # 절차를 바꾸는 .md(템플릿)는 코드로 본다
    assert missing(make_pr(comments=(), files=DOCS + (".github/pull_request_template.md",))) == ["계약 리뷰", "버그 리뷰"]
    # 코드 없는 PR이어도 보안 영향 체크가 있으면 보안 리뷰는 요구 (weekly_metrics와 같은 규칙)
    assert missing(make_pr(comments=(), files=DOCS), {7: ISSUE_SEC}) == ["보안 리뷰"]


def test_merged_or_draft_pr_is_not_merged(monkeypatch):
    code, fake = run(monkeypatch, make_pr(state="MERGED"), PLAIN, "--merge")
    assert code == 2 and not fake.merges
    pr = make_pr()
    pr["isDraft"] = True
    code, fake = run(monkeypatch, pr, PLAIN, "--merge")
    assert code == 2 and not fake.merges
