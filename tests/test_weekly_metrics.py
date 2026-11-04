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
    for f in ("app/auth.py", "scripts/check.ps1", "scripts/mac/deploy.sh", "k8s/docs.yaml", "security/asvs.yaml"):
        assert not wm.NON_CODE.search(f), f
