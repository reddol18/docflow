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
            "| 검사 | 결과 | 초 |\n|---|---|---|\n| ruff | 통과 | 0 |\n| pytest | 통과 | 18 |\n"
            "| trivy | 보고만 (출력 확인) | 9 |\n\n막는 검사 모두 통과")
SPEC = "## 계약 리뷰 (spec-reviewer)\n| 지적 | 반영 |\n|---|---|\n| 조건 2 근거 없음 | 본문에 추가 |"
CODE = "## 코드 리뷰 (`/code-review high`)\n| 지적 | 반영 |\n|---|---|\n| 경계값 | 고침 |"
SEC = "## 보안 리뷰 (security-reviewer)\n| 지적 | 반영 |\n|---|---|\n| (낮) 로그 | 그대로 |"
ISSUE_SEC = "## 목표\nx\n\n## 보안 영향\n- [x] 새 엔드포인트\n- [ ] 로그\n"
ISSUE_PLAIN = "## 목표\nx\n\n## 보안 영향\n- [ ] 새 엔드포인트\n"


def make_pr(body=CHECK_OK, comments=(SPEC, CODE), files=("app/main.py",), title="기능 (#7)", head=HEAD,
            state="OPEN", closes=(), reviews=()):
    return {"number": 90, "title": title, "body": "Closes #7\n\n## 테스트\n" + body,
            "comments": [{"body": c} for c in comments], "reviews": [{"body": r} for r in reviews],
            "files": [{"path": f} for f in files], "headRefOid": head, "state": state, "isDraft": False,
            "closingIssuesReferences": [{"number": n} for n in closes]}


class FakeGh:
    def __init__(self, pr, issues=None, inline=""):
        self.pr, self.issues, self.inline, self.calls = pr, issues or {}, inline, []

    def __call__(self, *args):
        self.calls.append(args)
        if args[:2] == ("pr", "view"):
            return json.dumps(self.pr)
        if args[:2] == ("pr", "merge"):
            return ""
        if args[0] == "api" and "--paginate" in args:
            return self.inline
        if args[0] == "api":
            n = int(args[1].rsplit("/", 1)[1])
            return json.dumps(self.issues.get(n, {"number": n, "pull_request": {}}))
        raise AssertionError(args)

    @property
    def merges(self):
        return [c for c in self.calls if c[:2] == ("pr", "merge")]


def run(monkeypatch, pr, issues=None, merge=False, inline=""):
    fake = FakeGh(pr, issues, inline)
    monkeypatch.setattr(mg, "gh", fake)
    code = mg.main([str(pr["number"])] + (["--merge"] if merge else []))
    return code, fake


def missing(pr, issues=None, inline=()):
    return [k for k, v, _ in mg.judge(pr, list(inline), issues or {}) if v is False]


def test_all_evidence_passes_and_merges_squash(monkeypatch, capsys):
    code, fake = run(monkeypatch, make_pr(title="기능 추가"), {7: {"body": ISSUE_PLAIN}}, merge=True)
    assert code == 0
    assert fake.merges == [("pr", "merge", "90", "--repo", mg.REPO, "--squash", "--subject", "기능 추가 (#90)",
                            "--match-head-commit", HEAD)]
    assert "판정: 통과" in capsys.readouterr().out


def test_dry_run_never_merges(monkeypatch):
    code, fake = run(monkeypatch, make_pr(), {7: {"body": ISSUE_PLAIN}})
    assert code == 0 and not fake.merges


def test_subject_keeps_existing_suffix():
    assert mg.subject({"number": 90, "title": "기능 (#90)"}) == "기능 (#90)"
    assert mg.subject({"number": 90, "title": "기능 (#7)"}) == "기능 (#7) (#90)"


@pytest.mark.parametrize("body, why", [
    ("테스트 칸 비움", "요약 없음"),
    (CHECK_OK.replace("abc1234", "1111111"), "head 커밋"),
    (CHECK_OK.replace("`abc1234`,", "`abc1234` + 커밋 안 된 변경,"), "커밋 안 된 변경"),
    (CHECK_OK.replace("| ruff | 통과 | 0 |", "| ruff | **실패** | 0 |"), "실패"),
    (CHECK_OK.replace("| pytest | 통과 | 18 |", "| pytest | **실패** | 18 |").replace("막는 검사 모두 통과", "실패: pytest"),
     "실패"),
    ("로컬 검사 `scripts/check.ps1` — 커밋 `abc1234`, 막는 검사 모두 통과", "요약 표가 없음"),
])
def test_local_check_rejected(monkeypatch, capsys, body, why):
    code, fake = run(monkeypatch, make_pr(body=body), {7: {"body": ISSUE_PLAIN}}, merge=True)
    out = capsys.readouterr().out
    assert code == 1 and not fake.merges
    assert "X 로컬 검사" in out and why in out


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
    code, fake = run(monkeypatch, pr, {7: {"body": ISSUE_PLAIN}}, merge=True)
    assert code == 1 and not fake.merges


def test_review_in_template_line_is_not_evidence():
    body = CHECK_OK + "\n## 리뷰\n- [x] 버그 리뷰 `/code-review` — 지적과 처리:\n| 지적 | 반영 |\n|---|---|\n| a | b |"
    # 표는 있지만 code-review 글자는 템플릿 줄에만 있다
    assert "버그 리뷰" in missing(make_pr(body=body, comments=(SPEC,)))


def test_one_comment_with_all_reviews_and_review_body():
    assert missing(make_pr(comments=(SPEC + "\n\n" + CODE,))) == []
    assert missing(make_pr(comments=(), reviews=(SPEC, CODE))) == []
    assert missing(make_pr(comments=(SPEC,)), inline=[CODE]) == []


def test_security_review_needed_when_issue_box_checked(monkeypatch, capsys):
    code, fake = run(monkeypatch, make_pr(), {7: {"body": ISSUE_SEC}}, merge=True)
    assert code == 1 and not fake.merges
    assert "X 보안 리뷰" in capsys.readouterr().out
    code, fake = run(monkeypatch, make_pr(comments=(SPEC, CODE, SEC)), {7: {"body": ISSUE_SEC}}, merge=True)
    assert code == 0 and len(fake.merges) == 1


def test_security_from_title_and_refs_but_not_pr_numbers(monkeypatch):
    pr = make_pr(title="PR #38 리뷰 반영 (#12)")
    pr["body"] = "Refs #13, #14\n" + CHECK_OK
    monkeypatch.setattr(mg, "gh", FakeGh(pr, {12: {"body": ISSUE_PLAIN}, 13: {"body": ISSUE_PLAIN},
                                              14: {"body": ISSUE_SEC}}))
    got, _, issues = mg.fetch(90)
    assert sorted(issues) == [12, 13, 14]  # 38은 PR이라 빠진다
    assert missing(got, issues) == ["보안 리뷰"]


def test_no_code_pr_skips_reviews_but_not_local_check():
    docs = ("docs/process/2026-W45.md", "README.md", "requirements.lock")
    assert missing(make_pr(comments=(), files=docs)) == []
    assert missing(make_pr(body="", comments=(), files=docs)) == ["로컬 검사"]
    # 절차를 바꾸는 .md(템플릿)는 코드로 본다
    assert missing(make_pr(comments=(), files=docs + (".github/pull_request_template.md",))) == ["계약 리뷰", "버그 리뷰"]
    # 코드 없는 PR이어도 보안 영향 체크가 있으면 보안 리뷰는 요구 (weekly_metrics와 같은 규칙)
    assert missing(make_pr(comments=(), files=docs), {7: ISSUE_SEC}) == ["보안 리뷰"]


def test_merged_or_draft_pr_is_not_merged(monkeypatch):
    code, fake = run(monkeypatch, make_pr(state="MERGED"), {7: {"body": ISSUE_PLAIN}}, merge=True)
    assert code == 2 and not fake.merges
    pr = make_pr()
    pr["isDraft"] = True
    code, fake = run(monkeypatch, pr, {7: {"body": ISSUE_PLAIN}}, merge=True)
    assert code == 2 and not fake.merges


def test_skipped_checks_are_reported():
    body = CHECK_OK.replace("| pytest | 통과 | 18 |", "| semgrep | 건너뜀 | 0 |")
    rows = mg.judge(make_pr(body=body), [], {})
    assert rows[0][1] is True and "건너뛴 검사: semgrep" in rows[0][2]
