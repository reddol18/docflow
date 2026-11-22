"""scripts/gate_status.py — docflow/local-check commit status를 올리는 조건 (https://github.com/reddol18/docflow/issues/85).
git·gh는 가짜로 바꾼다 (실제 GitHub 호출·status 없음). 조건별: 통과·실패·건너뜀·커밋 안 된 변경·head 바뀜·push 안 함."""
import importlib.util
import re
from pathlib import Path

import pytest

_p = Path(__file__).parent.parent / "scripts" / "gate_status.py"
_spec = importlib.util.spec_from_file_location("gate_status", _p)
gs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gs)

SHA = "abc1234def5678900000000000000000000000000"
ALL = [("frontend", "pass"), ("ruff", "pass"), ("lock", "pass"), ("pytest", "pass"),
       ("gitleaks", "pass"), ("osv", "pass"), ("semgrep", "pass"), ("trivy", "pass")]
QUICK = [(n, "skip" if n in ("osv", "semgrep", "trivy") else s) for n, s in ALL]
CODE_FILES = "app/main.py\nREADME.md\n"
DOC_FILES = "docs/process/2026-W45.md\nREADME.md\n"


def args(results, start_sha=SHA, start_dirty=False, skip_opts=()):
    out = ["local-check", "--start-sha", start_sha, "--at", "2026-11-09 21:00"]
    out += ["--start-dirty"] if start_dirty else []
    for n, s in results:
        out += ["--result", f"{n}={s}"]
    for o in skip_opts:
        out += ["--skip-opt", o]
    return out


class Fake:
    """git·gh 대역. pushed=False면 status POST가 422 (없는 커밋), status_code≠0이면 git status 실패"""

    def __init__(self, head=SHA, dirty="", files=CODE_FILES, pushed=True, post_fails=False, status_code=0):
        self.head, self.dirty, self.files, self.pushed, self.post_fails = head, dirty, files, pushed, post_fails
        self.status_code = status_code
        self.gh_calls = []

    def git(self, *a):
        if a[0] == "rev-parse":
            return 0, self.head + "\n"
        if a[0] == "status":
            return self.status_code, "" if self.status_code else self.dirty
        if a[0] == "diff":
            return 0, self.files
        raise AssertionError(a)

    def gh(self, *a):
        self.gh_calls.append(a)
        if a[:2] == ("api", "-X"):
            if not self.pushed:
                raise SystemExit("gh api 실패: No commit found for SHA (HTTP 422)")
            if self.post_fails:
                raise SystemExit("gh api 실패: HTTP 403")
            return "{}"
        raise AssertionError(a)  # 미리 조회하지 않는다

    @property
    def posts(self):
        return [c for c in self.gh_calls if c[:2] == ("api", "-X")]


def run(monkeypatch, argv, **kw):
    f = Fake(**kw)
    monkeypatch.setattr(gs, "git", f.git)
    monkeypatch.setattr(gs.mg, "gh", f.gh)
    return gs.main(argv), f

def test_all_pass_clean_pushed_posts_success_to_head(monkeypatch, capsys):
    code, f = run(monkeypatch, args(ALL))
    assert code == 0
    assert len(f.posts) == 1
    p = f.posts[0]
    assert p[3] == f"repos/{gs.mg.REPO}/statuses/{SHA}"
    assert "state=success" in p and "context=docflow/local-check" in p
    desc = next(x for x in p if x.startswith("description="))
    assert "막는 검사 모두 통과" in desc and "2026-11-09 21:00" in desc and len(desc) <= len("description=") + 140
    assert "올림" in capsys.readouterr().out


@pytest.mark.parametrize("results, why", [
    ([(n, "fail" if n == "pytest" else s) for n, s in ALL], "막는 검사 실패: pytest"),
    ([(n, "skip" if n == "frontend" else s) for n, s in ALL], "건너뜀: frontend"),
    (QUICK, "건너뜀: osv, semgrep, trivy"),  # 코드 있는 PR의 -Quick
    ([r for r in ALL if r[0] != "lock"], "검사 결과 없음: lock"),
])
def test_failed_or_skipped_or_missing_check_posts_nothing(monkeypatch, capsys, results, why):
    code, f = run(monkeypatch, args(results))
    assert code == 1 and f.gh_calls == []  # GitHub에 아무것도 묻지도 올리지도 않는다
    assert why in capsys.readouterr().out


def test_trivy_failing_blocks(monkeypatch, capsys):
    # https://github.com/reddol18/docflow/issues/29 부터 trivy도 막는 검사
    res = [(n, "fail" if n == "trivy" else s) for n, s in ALL]
    code, f = run(monkeypatch, args(res))
    assert code == 1 and f.gh_calls == []
    assert "막는 검사 실패: trivy" in capsys.readouterr().out


def test_pg_skip_option_counts_as_skip(monkeypatch, capsys):
    code, f = run(monkeypatch, args(ALL, skip_opts=["pg"]))
    assert code == 1 and f.posts == []
    assert "pg(옵션)" in capsys.readouterr().out


def test_no_code_pr_may_skip_quick_checks(monkeypatch, capsys):
    code, f = run(monkeypatch, args(QUICK), files=DOC_FILES)
    assert code == 0 and len(f.posts) == 1
    assert "코드 없는 PR" in next(x for x in f.posts[0] if x.startswith("description="))


def test_no_code_pr_still_cannot_skip_other_checks(monkeypatch):
    res = [(n, "skip" if n in ("osv", "semgrep", "trivy", "pytest") else s) for n, s in ALL]
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
    assert code == 1 and len(f.posts) == 1  # GitHub이 없는 커밋이라 거부 → 올라간 것 없음
    assert "GitHub에 없음" in capsys.readouterr().out


def test_post_failure_is_reported_not_raised(monkeypatch, capsys):
    code, f = run(monkeypatch, args(ALL), post_fails=True)
    assert code == 2
    assert "올리기 실패" in capsys.readouterr().out


def test_rules_follow_merge_gate():
    """check.ps1 막는 검사 목록·코드 없는 PR 건너뜀은 merge_gate와 한 곳에서 온다"""
    assert gs.mg.NO_CODE_SKIP == {"osv", "semgrep", "trivy"}
    assert set(gs.mg.CHECKS) == {n for n, _ in ALL}


def test_git_status_failure_counts_as_dirty(monkeypatch, capsys):
    """fail-closed: git status가 실패하면 변경이 있는 것으로 본다"""
    code, f = run(monkeypatch, args(ALL), status_code=128)
    assert code == 1 and f.gh_calls == []
    assert "커밋 안 된 변경" in capsys.readouterr().out


def test_blocking_comes_from_merge_gate_not_caller(monkeypatch, capsys):
    """check.ps1이 무엇을 넘기든 막는 검사는 merge_gate.CHECKS — 막는 검사 실패면 올리지 않는다"""
    res = [(n, "fail" if n == "gitleaks" else s) for n, s in ALL]
    code, f = run(monkeypatch, args(res))
    assert code == 1 and f.gh_calls == []
    assert "막는 검사 실패: gitleaks" in capsys.readouterr().out


def test_duplicate_result_rejected(monkeypatch, capsys):
    """같은 이름이 두 번(예: pytest=fail 뒤 pytest=pass)이면 거부"""
    res = [(n, "fail" if n == "pytest" else s) for n, s in ALL] + [("pytest", "pass")]
    code, f = run(monkeypatch, args(res))
    assert code == 1 and f.gh_calls == []
    assert "두 번: pytest" in capsys.readouterr().out


@pytest.mark.parametrize("bad", ["pytest", "pytest=ok", "=pass", "pytest:1:pass"])
def test_bad_result_argument_rejected(bad):
    with pytest.raises(SystemExit):
        gs.main(["local-check", "--start-sha", SHA, "--at", "x", "--result", bad])


# ---- 병합 게이트 보강 (https://github.com/reddol18/docflow/issues/106)

@pytest.mark.parametrize("msg", ["gh api 실패: Validation Failed (HTTP 422)",
                                 "gh api 실패: This SHA and context has reached the maximum number of statuses. (HTTP 422)"])
def test_other_422_is_post_failure_not_unpushed(monkeypatch, capsys, msg):
    """5: 422 전부가 'push 안 됨'은 아니다 — No commit found만 그렇고 나머지는 올리기 실패(2)"""
    f = Fake()
    monkeypatch.setattr(gs, "git", f.git)

    def gh(*a):
        f.gh_calls.append(a)
        raise SystemExit(msg)
    monkeypatch.setattr(gs.mg, "gh", gh)
    assert gs.main(args(ALL)) == 2
    out = capsys.readouterr().out
    assert "올리기 실패" in out and "GitHub에 없음" not in out


@pytest.mark.parametrize("state, posted", [("fail", False), ("pass", True)])
def test_unknown_check_failure_blocks(monkeypatch, capsys, state, posted):
    """6: CHECKS·REPORT_ONLY에 없는 이름(check.ps1에 새로 생긴 단계)이 실패면 거부, 통과면 그대로"""
    code, f = run(monkeypatch, args(ALL + [("newcheck", state)]))
    assert (code == 0) is posted and (len(f.posts) == 1) is posted
    if not posted:
        assert f.gh_calls == []
        assert "모르는 검사 실패: newcheck" in capsys.readouterr().out


def test_report_only_failure_does_not_block(monkeypatch):
    """6: REPORT_ONLY에 넣은 검사의 실패는 막지 않는다"""
    monkeypatch.setattr(gs.mg, "REPORT_ONLY", frozenset({"newcheck"}))
    code, f = run(monkeypatch, args(ALL + [("newcheck", "fail")]))
    assert code == 0 and len(f.posts) == 1


def test_report_only_is_disjoint_from_checks():
    assert not set(gs.mg.REPORT_ONLY) & set(gs.mg.CHECKS)


# ---- PR #151 리뷰 반영 (https://github.com/reddol18/docflow/issues/106)

def test_report_only_skip_does_not_block(monkeypatch, capsys):
    """C2: 보고만 검사는 건너뛰어도 막지 않는다 (모르는 검사의 건너뜀은 막는다)"""
    code, f = run(monkeypatch, args(ALL + [("newcheck", "skip")]))
    assert code == 1 and f.posts == [] and "건너뜀: newcheck" in capsys.readouterr().out
    monkeypatch.setattr(gs.mg, "REPORT_ONLY", frozenset({"newcheck"}))
    code, f = run(monkeypatch, args(ALL + [("newcheck", "skip")]))
    assert code == 0 and len(f.posts) == 1
    assert "코드 없는 PR" not in next(x for x in f.posts[0] if x.startswith("description="))


def test_check_ps1_steps_match_checks_and_report_only():
    """C1: check.ps1의 막는 단계(Step 이름 $true)는 CHECKS, 막지 않는 단계($false)는 REPORT_ONLY와 같다.
    막지 않는 단계의 실패는 '실패 (보고만)'으로 적어 merge_gate의 '**실패**' 거부에 걸리지 않는다"""
    src = (Path(__file__).parent.parent / "scripts" / "check.ps1").read_text(encoding="utf-8-sig")
    steps = re.findall(r'^\s*Step "([\w-]+)" \$(true|false)\b', src, re.M)
    assert steps, "check.ps1에서 Step 줄을 찾지 못함"
    assert {n for n, b in steps if b == "true"} == set(gs.mg.CHECKS)
    assert {n for n, b in steps if b == "false"} == set(gs.mg.REPORT_ONLY)
    assert 'elseif (-not $r.blocking) { "실패 (보고만)" }' in src
