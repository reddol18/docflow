from app.review_input import clean_fixes

FLAGGED = [{"doc_id": "v-0001", "fields": {"docno": "가나 12345-1", "serial": "1", "date": "1999-03-10",
                                         "recipient": "가나시장", "title": "제목"}, "flags": {"date": ["x"]}}]


def test_keeps_only_changed_editable_cells_of_flagged_docs():
    pairs = [
        ("v-0001", "date", " 1999-03-11 "),   # 바뀜 → 남김 (앞뒤 공백 제거)
        ("v-0001", "title", "제목"),          # 그대로 → 버림
        ("v-0001", "serial", "9"),            # 직접 못 고치는 칸 → 버림
        ("v-0001", "evil", "x"),              # 모르는 칸 → 버림
        ("v-9999", "date", "2000-01-01"),     # 검토 대기 아닌 문서 → 버림
    ]
    assert clean_fixes(FLAGGED, pairs) == {"v-0001": {"date": "1999-03-11"}}


def test_empty_when_nothing_changed():
    assert clean_fixes(FLAGGED, [("v-0001", "recipient", "가나시장")]) == {}
