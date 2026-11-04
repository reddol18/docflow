"""scripts/weekly_metrics.py의 판정 함수 (gh·git 호출 없는 부분)."""
import importlib.util
from pathlib import Path

_p = Path(__file__).parent.parent / "scripts" / "weekly_metrics.py"
_spec = importlib.util.spec_from_file_location("weekly_metrics", _p)
wm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wm)


def test_template_lines_are_not_evidence():
    body = "\n".join([
        "- [x] 버그 리뷰 `/code-review` — 지적과 처리:",  # 체크만 함
        "- `security-reviewer` 판정 (미충족·판단 불가만):",  # 템플릿 그대로
        "- [ ] 계약 리뷰 `/spec-review` — 결과 요약:",
        "<!-- code-review -->",
    ])
    text = wm.evidence(body)
    assert not any(rx.search(text) for rx in wm.MARKS.values())
    filled = wm.evidence("- [x] 버그 리뷰 `/code-review` — 지적과 처리: 3건 고침")
    assert wm.MARKS["버그 리뷰"].search(filled)


def test_pct_excludes_not_applicable_and_counts_late():
    assert wm.pct([True, False, "규칙 전", "코드 없음", None, "늦게 #42"]) == "1/3 (늦게 1)"
    assert wm.pct([True, True]) == "2/2"
    assert wm.pct(["규칙 전", None]) == "–"


def test_non_code_paths():
    for f in ("requirements.lock", "requirements-test.txt", "docs/process/2026-W45.md", ".gitattributes",
              "frontend/package-lock.json"):
        assert wm.NON_CODE.search(f), f
    for f in ("app/auth.py", "scripts/check.ps1", "scripts/mac/deploy.sh", "k8s/docs.yaml", "security/asvs.yaml",
              ".github/pull_request_template.md", ".claude/agents/spec-reviewer.md"):  # 절차를 바꾸는 .md는 코드
        assert not wm.NON_CODE.search(f), f


def test_review_fixes_counts_table_rows():
    text = "\n".join([
        "| 지적 | 반영 |", "|---|---|",
        "| 토큰이 명령줄에 | 헤더 파일로 |",
        "| 해시 고정 | 후속 https://github.com/o/r/issues/46 |",
        "| 경로 백틱 | https://github.com/o/r/issues/40 |",
        "| -Mac 형식 | 그대로 (신뢰 경계 안) |",
        "| useMe 401 | 아님: noRedirect |",
        "", "| 검사 | 결과 |", "|---|---|", "| ruff | 통과 |",  # 다른 표는 세지 않는다
    ])
    assert wm.review_fixes(text) == (1, 2, 2)


def test_criteria_met_skips_template_row():
    body = ("## 완료 조건별 결과\n| 완료 조건 | 결과 | 근거 |\n|---|---|---|\n"
            "| a | 충족 | x |\n| b | 일부 | y |\n| | 충족 / 일부 / 미충족 | |\n")
    assert wm.criteria_met(body) == (1, 2)


def test_scope_changed():
    assert wm.scope_changed("## 범위 밖 변경\n<!-- 설명 -->\n없음\n\n## 테스트\n") is False
    assert wm.scope_changed("## 범위 밖 변경\n- PR 템플릿 주석 한 줄\n## 테스트\n") is True
    assert wm.scope_changed("본문에 그 절이 없음") is None


def test_first_check_passed_uses_first_summary():
    ok = "로컬 검사 `scripts/check.ps1` — 커밋 `abc`\n| ruff | 통과 |"
    bad = "로컬 검사 `scripts/check.ps1` — 커밋 `abc`\n| ruff | **실패** |"
    assert wm.first_check_passed(["본문", bad, ok]) is False
    assert wm.first_check_passed([ok, bad]) is True
    assert wm.first_check_passed(["요약 없음"]) is None
