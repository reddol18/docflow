"""scripts/gate_status.py — docflow/local-check commit status를 올리는 조건 (https://github.com/reddol18/docflow/issues/85).
git·gh는 가짜로 바꾼다 (실제 GitHub 호출·status 없음). 조건별: 통과·실패·건너뜀·커밋 안 된 변경·head 바뀜·push 안 함."""
import importlib.util
from pathlib import Path

import pytest

_p = Path(__file__).parent.parent / "scripts" / "gate_status.py"
_spec = importlib.util.spec_from_file_location("gate_status", _p)
gs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gs)

SHA = "abc1234def5678900000000000000000000000000"
ALL = [("frontend", True, "pass"), ("ruff", True, "pass"), ("lock", True, "pass"), ("pytest", True, "pass"),
       ("gitleaks", True, "pass"), ("osv", True, "pass"), ("semgrep", True, "pass"), ("trivy", False, "pass")]
QUICK = [(n, b, "skip" if n in ("osv", "semgrep", "trivy") else s) for n, b, s in ALL]
CODE_FILES = "app/main.py\nREADME.md\n"
DOC_FILES = "docs/process/2026-W45.md\nREADME.md\n"


def args(results, start_sha=SHA, start_dirty=False, skip_opts=()):
    out = ["local-check", "--start-sha", start_sha, "--at", "2026-11-09 21:00"]
    out += ["--start-dirty"] if start_dirty else []
    for n, b, s in results:
        out += ["--result", f"{n}:{int(b)}:{s}"]
    for o in skip_opts:
        out += ["--skip-opt", o]
    return out


class Fake:
    """git·gh 대역. pushed=False면 commits API가 404"""

    def __init__(self, head=SHA, dirty="", files=CODE_FILES, pushed=True, post_fails=False):
        self.head, self.dirty, self.files, self.pushed, self.post_fails = head, dirty, files, pushed, post_fails
        self.gh_calls = []

    def git(self, *a):
        if a[0] == "rev-parse":
            return 0, self.head + "\n"
        if a[0] == "status":
            return 0, self.dirty
        if a[0] == "diff":
            return 0, self.files
        raise AssertionError(a)

    def gh(self, *a):
        self.gh_calls.append(a)
        if a[:2] == ("api", "-X"):
            if self.post_fails:
                raise SystemExit("gh api 실패: HTTP 403")
            return "{}"
        if a[0] == "api" and "/commits/" in a[1]:
            if not self.pushed:
                raise SystemExit("gh api 실패: No commit found for SHA (HTTP 422)")
            return self.head
        raise AssertionError(a)

    @property
    def posts(self):
        return [c for c in self.gh_calls if c[:2] == ("api", "-X")]


def run(monkeypatch, argv, **kw):
    f = Fake(**kw)
    monkeypatch.setattr(gs, "git", f.git)
    monkeypatch.setattr(gs, "gh", f.gh)
    return gs.main(argv), f


def test_all_pass_clean_pushed_posts_success_to_head(monkeypatch, capsys):
    code, f = run(monkeypatch, args(ALL))
    assert code == 0
    assert len(f.posts) == 1
    p = f.posts[0]
    assert p[3] == f"repos/{gs.REPO}/statuses/{SHA}"
    assert "state=success" in p and "context=docflow/local-check" in p
    desc = next(x for x in p if x.startswith("description="))
    assert "막는 검사 모두 통과" in desc and "2026-11-09 21:00" in desc and len(desc) <= len("description=") + 140
    assert "올림" in capsys.readouterr().out


@pytest.mark.parametrize("results, why", [
    ([(n, b, "fail" if n == "pytest" else s) for n, b, s in ALL], "막는 검사 실패: pytest"),
    ([(n, b, "skip" if n == "frontend" else s) for n, b, s in ALL], "건너뜀: frontend"),
    (QUICK, "건너뜀: osv, semgrep, trivy"),  # 코드 있는 PR의 -Quick
    ([r for r in ALL if r[0] != "lock"], "검사 결과 없음: lock"),
])
def test_failed_or_skipped_or_missing_check_posts_nothing(monkeypatch, capsys, results, why):
    code, f = run(monkeypatch, args(results))
    assert code == 1 and f.gh_calls == []  # GitHub에 아무것도 묻지도 올리지도 않는다
    assert why in capsys.readouterr().out


def test_trivy_failing_is_report_only(monkeypatch):
    res = [(n, b, "fail" if n == "trivy" else s) for n, b, s in ALL]
    code, f = run(monkeypatch, args(res))
    assert code == 0 and len(f.posts) == 1


def test_pg_skip_option_counts_as_skip(monkeypatch, capsys):
    code, f = run(monkeypatch, args(ALL, skip_opts=["pg"]))
    assert code == 1 and f.posts == []
    assert "pg(옵션)" in capsys.readouterr().out


def test_no_code_pr_may_skip_quick_checks(monkeypatch, capsys):
    code, f = run(monkeypatch, args(QUICK), files=DOC_FILES)
    assert code == 0 and len(f.posts) == 1
    assert "코드 없는 PR" in next(x for x in f.posts[0] if x.startswith("description="))


def test_no_code_pr_still_cannot_skip_other_checks(monkeypatch):
    res = [(n, b, "skip" if n in ("osv", "semgrep", "trivy", "pytest") else s) for n, b, s in ALL]
    code, f = run(monkeypatch, args(res), files=DOC_FILES)
    assert code == 1 and f.posts == []


def test_unknown_diff_counts_as_code(monkeypatch):
    code, f = run(monkeypatch, args(QUICK), files="")  # origin/main이 없어 diff를 못 구함 → 코드 있음으로
    assert code == 1 and f.posts == []


@pytest.mark.parametrize("kw, start_dirty, why", [
    ({"dirty": " M app/main.py\n"}, False, "커밋 안 된 변경"),
    ({"dirty": "?? scratch.txt\n"}, False, "커밋 안 된 변경"),  # 추적 안 된 파일도
    ({}, True, "커밋 안 된 변경"),  # 시작할 때 변경이 있었고 검사 중에 치웠어도
    ({"head": "fff0000" + "0" * 33}, False, "HEAD가 바뀜"),
])
def test_dirty_tree_or_moved_head_posts_nothing(monkeypatch, capsys, kw, start_dirty, why):
    code, f = run(monkeypatch, args(ALL, start_dirty=start_dirty), **kw)
    assert code == 1 and f.gh_calls == []
    assert why in capsys.readouterr().out


def test_unpushed_head_posts_nothing(monkeypatch, capsys):
    code, f = run(monkeypatch, args(ALL), pushed=False)
    assert code == 1 and f.posts == []
    assert "GitHub에 없음" in capsys.readouterr().out


def test_post_failure_is_reported_not_raised(monkeypatch, capsys):
    code, f = run(monkeypatch, args(ALL), post_fails=True)
    assert code == 2
    assert "올리기 실패" in capsys.readouterr().out


def test_rules_follow_merge_gate():
    """check.ps1 막는 검사 목록·코드 없는 PR 건너뜀은 merge_gate와 한 곳에서 온다"""
    assert gs.mg.NO_CODE_SKIP == {"osv", "semgrep", "trivy"}
    assert set(gs.mg.CHECKS) | set(gs.mg.REPORT_ONLY) == {n for n, _, _ in ALL}
