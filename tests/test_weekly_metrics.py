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
    for f in ("docs/process/2026-W45.md", ".gitattributes", "frontend/package-lock.json", "other.lock"):
        assert wm.NON_CODE.search(f), f
    # 이미지가 그대로 설치하는 의존성 목록은 코드 (https://github.com/reddol18/docflow/issues/33)
    for f in ("requirements.lock", "requirements.txt", "requirements-test.txt",
              "app/auth.py", "scripts/check.ps1", "scripts/mac/deploy.sh", "k8s/docs.yaml", "security/asvs.yaml",
              ".github/pull_request_template.md", ".claude/agents/spec-reviewer.md"):  # 절차를 바꾸는 .md는 코드
        assert not wm.NON_CODE.search(f), f


def test_no_code_and_issue_refs_shared_with_merge_gate():
    assert wm.is_no_code(["docs/a.md", "README.md"])
    assert not wm.is_no_code(["docs/a.md", "app/main.py"])
    assert not wm.is_no_code([])
    assert not wm.is_no_code([f"docs/{i}.md" for i in range(wm.FILES_CAP)])  # 잘렸을 수 있으면 코드로
    body = ("Refs #13, #14\n<!-- Refs #99 -->\n본문의 #50 언급은 아님\n예: #76은 `Refs #22`로 (글 속 인용)\nCloses #7\n"
            "Refs: https://github.com/reddol18/docflow/issues/15\nRefs https://github.com/other/repo/issues/16\n")
    assert wm.body_refs(body) == {7, 13, 14, 15}  # 다른 저장소 URL·주석·글 속 인용은 뺌
    assert wm.issue_refs("기능 (#12)", body, [7]) == {7, 12, 13, 14, 15}
    assert wm.linked_issues("기능 (#12)", [7]) == {7, 12}  # 리드타임·'닫는 이슈' 칸은 Refs를 넣지 않는다


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


def test_extra_changes_and_scope_change():
    assert wm.extra_changes("## 범위 밖 변경\n<!-- 설명 -->\n없음\n\n## 테스트\n") is False
    assert wm.extra_changes("## 범위 밖 변경\n- PR 템플릿 주석 한 줄\n## 테스트\n") is True
    assert wm.extra_changes("## 범위 밖 변경\n<!-- 설명 -->\n\n## 테스트\n") is None  # 안 채움
    assert wm.extra_changes("본문에 그 절이 없음") is None
    assert wm.needs_security("## 보안 영향\r\n- [x] 인증·세션 변경\r\n\r\n## 관련\r\n")  # CRLF 이슈 본문
    assert not wm.needs_security("## 보안 영향\n- [ ] 인증·세션 변경\n")
    assert wm.scope_change("- **범위 변경:** PR 대신 파일로") is True
    assert wm.scope_change("## 범위 밖 변경\n- 주석 한 줄") is False  # 다른 개념


def test_bold_cells_and_bug_fields():
    body = "| 완료 조건 | 결과 |\n|---|---|\n| a | **충족** |\n| b | 해당 없음 |\n"
    assert wm.criteria_met(body) == (1, 1)
    assert wm.review_fixes("| 지적 | 반영 |\n|---|---|\n| x | **그대로** (이유) |\n") == (0, 0, 1)
    bug = "### 재현\n1\n\n### 발견 단계\n\n운영\n\n### 원인 PR\n\nhttps://github.com/o/r/pull/38\n"
    assert wm.bug_fields(bug) == ("운영", 38)
    assert wm.bug_fields("형식 없음") == (None, None)


def test_first_check_stops_at_table_end():
    s = ("로컬 검사 `scripts/check.ps1` — 커밋 `a`\n| 검사 | 결과 |\n|---|---|\n| ruff | 통과 |\n\n"
         "막는 검사 모두 통과\n- 수동: 로그인 **실패** 401 확인")
    assert wm.first_check_passed([s]) is True
    s2 = "로컬 검사 `scripts/check.ps1` — 커밋 `a`\n| 검사 | 결과 |\n|---|---|\n| ruff | **실패** |"
    assert wm.first_check_passed([s2]) is False


def test_first_check_passed_uses_first_summary():
    ok = "로컬 검사 `scripts/check.ps1` — 커밋 `abc`\n| ruff | 통과 |"
    bad = "로컬 검사 `scripts/check.ps1` — 커밋 `abc`\n| ruff | **실패** |"
    assert wm.first_check_passed(["본문", bad, ok]) is False
    assert wm.first_check_passed([ok, bad]) is True
    assert wm.first_check_passed(["요약 없음"]) is None
