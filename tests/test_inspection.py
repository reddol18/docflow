"""2단계 검증 — 검사 목록 만들기 (https://github.com/reddol18/docflow/issues/10). DB 없이."""
from app import inspection


def test_sample_size_rounds_up():
    assert inspection.sample_size(10, 0.3) == 3  # 0.3×10 부동소수 오차로 4가 되지 않는다
    assert inspection.sample_size(10, 0.03) == 1  # 올림: 작은 철에서도 최소 1건
    assert inspection.sample_size(100, 0.05) == 5
    assert inspection.sample_size(101, 0.05) == 6
    assert inspection.sample_size(7, 1) == 7
    assert inspection.sample_size(0, 0.05) == 0 and inspection.sample_size(5, 0) == 0


def _docs(a, b):
    flagged = [{"doc_id": f"f{i:02d}", "flags": {"title": ["확신도"]}} for i in range(a)]
    plain = [{"doc_id": f"p{i:02d}", "flags": {}} for i in range(b)]
    return flagged + plain


def test_plan_flagged_all_and_sample_ceil():
    a, b, p = 3, 41, 0.05  # 41×0.05 = 2.05 → 3
    rows, s = inspection.plan(_docs(a, b), p, seed=7)
    assert [d for d, r in rows.items() if r == "flagged"] == ["f00", "f01", "f02"]  # 표시 문서 a건 모두
    sample = [d for d, r in rows.items() if r == "sample"]
    assert len(sample) == 3 and all(d.startswith("p") for d in sample)
    assert s == {"flagged": 3, "unflagged": 41, "sample": 3, "rate": p, "fields100": [], "seed": 7}
    assert inspection.plan(_docs(a, b), p, seed=7)[0] == rows  # 시드가 같으면 같은 표본
    assert len(rows) == a + 3


def test_plan_field100_includes_every_document():
    rows, _ = inspection.plan(_docs(2, 20), 0.1, fields100=["title"], seed=1)
    assert len(rows) == 22  # 표시·표본 여부와 상관없이 모든 문서
    assert sum(r == "sample" for r in rows.values()) == 2 and sum(r == "field100" for r in rows.values()) == 18
