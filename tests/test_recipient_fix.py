"""수신 보정 규칙 — 결재란 낱말·잘린 주소 (https://github.com/reddol18/docflow/issues/1).
시험 값은 모두 지어낸 것이다 (가상 기관·주소·상호). 틀린 꼴만 실제 사례 유형을 본떴다."""
from app import layout

W, H = 2464, 3504


def page(rows):
    """rows = [(y, [(x, 글자, 너비), …]), …] → OCR 결과 모양 {page, lines, items} (tests/test_line_crops.py와 같은 꼴)"""
    items, lines = [], []
    for n, (y, words) in enumerate(rows):
        for x, t, w in words:
            items.append({"text": t, "score": 0.9, "box": [x, y, x + w, y + 50], "line": n})
        lines.append(" ".join(t for _, t, _ in sorted(words)))
    items.append({"text": ".", "score": 0.9, "box": [W - 10, H - 10, W, H], "line": len(rows)})  # 쪽 크기
    lines.append(".")
    return {"page": "p1", "lines": lines, "items": items}


HEAD = [(500, [(200, "문서번호", 220), (450, "가상 12345-678", 400)]),
        (600, [(200, "시행일자", 220), (450, "1997. 3. 4.", 300)]),
        (700, [(200, "경유", 160)])]
TITLE = [(1200, [(200, "제목", 160), (450, "가상 허가 통보", 500)])]


def doc(rows):
    return page(HEAD + rows + TITLE)


def test_strip_approval_tail():
    f = layout.strip_approval_tail
    assert f("가상시 가상면 가상리 국") == "가상시 가상면 가상리"
    assert f("가상시 가상동 1-2 실장") == "가상시 가상동 1-2"
    assert f("가상시 가상동 가상아파트 부시장") == "가상시 가상동 가상아파트"
    assert f("가상구 가상동 과장") == "가상구 가상동"
    assert f("가상위생과장 국장") == "가상위생과장"
    assert f("가상리 국 과장") == "가상리"  # 여러 개
    # 이름에 붙은 글자·낱말 하나뿐인 값은 그대로
    assert f("가상환경관리국") == "가상환경관리국"
    assert f("가상시 가상환경관리국") == "가상시 가상환경관리국"
    assert f("가상위생과장") == "가상위생과장"
    assert f("과장") == "과장"
    assert f("") == ""


def test_approval_word_prefers_rule_value():
    """LLM이 결재란 '국'을 수신 끝에 붙이고 번지를 빠뜨림 → 규칙 값(수신 칸 OCR)"""
    d = doc([(800, [(200, "수신", 160), (450, "가상시 가상면 가상리 574번지", 600), (1300, "국", 60)])])
    assert layout.rule_recipient(d) == "가상시 가상면 가상리 574번지"
    assert layout.fix_recipient(d, "가상시 가상면 가상리 국") == ("가상시 가상면 가상리 574번지", "결재란·잘림")


def test_approval_word_with_dropped_company():
    """'부시장'·'실장'이 상호 자리에 들어감 — 규칙 값이 다음 줄 상호·'대표'까지 가짐"""
    d = doc([(800, [(200, "수신", 160), (450, "가상시 가상동 가상아파트 1동 101호", 700), (1300, "부시장", 160)]),
             (880, [(450, "가상기업 대표", 400), (1300, "국", 60)])])
    assert layout.fix_recipient(d, "가상시 가상동 가상아파트 부시장") == (
        "가상시 가상동 가상아파트 1동 101호 가상기업 대표", "결재란·잘림")  # 규칙 값이 B(결재란 뗀 것)로 시작


def test_llm_kept_company_only():
    """LLM이 주소를 버리고 상호만 남김 → B가 규칙 값의 부분 문자열이면 규칙 값"""
    d = doc([(800, [(200, "수신", 160), (450, "가상시 가상면 가상리 841-2번지", 600), (1300, "과", 60)]),
             (880, [(450, "(주)가상에너지 대표", 400)])])
    assert layout.fix_recipient(d, "(주)가상에너지 대표") == ("가상시 가상면 가상리 841-2번지 (주)가상에너지 대표", "규칙")
    # 가운데(번지)만 빠뜨린 값도 — 낱말이 차례대로 다 들어 있으면
    assert layout.fix_recipient(d, "가상시 가상면 가상리 (주)가상에너지 대표")[1] == "규칙"


def test_truncated_value_still_extended():
    """예전 '잘림' 규칙 (규칙 값이 B로 시작하고 더 김)"""
    d = doc([(800, [(200, "수신", 160), (450, "가상시 가상동 12-3번지", 500)]), (880, [(450, "가상상사 대표", 400)])])
    assert layout.fix_recipient(d, "가상시 가상동 12-3번지") == ("가상시 가상동 12-3번지 가상상사 대표", "잘림")


def test_ref_office_after_strip():
    """'가상도지사 국장' → 결재란 낱말을 떼고 머리부 참조 부서장을 붙인다"""
    d = doc([(800, [(200, "수신", 160), (450, "가상도지사", 300), (1300, "국장", 120)]),
             (960, [(200, "참조", 160), (450, "가상담당관", 250)])])
    out, why = layout.fix_recipient(d, "가상도지사 국장")
    assert out == "가상도지사 가상담당관" and "결재란" in why and "참조" in why


def test_rule_value_noise_is_cleaned():
    """규칙 값의 글자 하나짜리 기호·숫자 낱말, '_', 맨 앞 항목명 조각은 떼고 견준다 — 같으면 LLM 값 그대로"""
    d = doc([(800, [(200, "수신", 160), (450, "가상시_가상동 1-2 가상공업사", 600)])])
    assert layout.fix_recipient(d, "가상시 가상동 1-2 가상공업사") == ("가상시 가상동 1-2 가상공업사", "")
    d = doc([(800, [(200, "수신", 160), (450, "가상시장", 200)]), (880, [(450, "1", 30), (520, "가상민원과장", 300)])])
    assert layout.fix_recipient(d, "가상시장") == ("가상시장 가상민원과장", "잘림")


def test_rule_value_not_used_when_it_runs_into_other_labels_or_body():
    # OCR이 다음 항목(시행일자) 줄까지 한 줄로 읽음
    d = doc([(800, [(200, "수신", 160), (450, "가상면 가상리 333-12", 450), (950, "시행일자", 200), (1180, "99.1.2", 150)])])
    assert "시행일자" in layout.rule_recipient(d)
    assert layout.fix_recipient(d, "가상면 가상리 333-12") == ("가상면 가상리 333-12", "")
    # 본문까지 읽어 들인 긴 규칙 값 (정규화 60자 넘음)
    long = "가상면 가상리 333-12 " + "가상 본문 글자가 아주 길게 이어지는 줄 " * 4
    d = doc([(800, [(200, "수신", 160), (450, long, 1900)])])
    assert layout.fix_recipient(d, "가상면 가상리 333-12") == ("가상면 가상리 333-12", "")


def test_unrelated_short_rule_does_not_replace():
    """결재란 낱말이 있어도 규칙 값이 더 짧거나 앞이 다르면 LLM 값(결재란 낱말만 뗌)"""
    d = doc([(800, [(200, "수신", 160), (450, "가상", 120)])])
    assert layout.fix_recipient(d, "가상시 가상구 가상동 1-1 가상테크 과장") == ("가상시 가상구 가상동 1-1 가상테크", "결재란")


def test_internal_and_list_unchanged():
    d = doc([(800, [(200, "수신", 160), (450, "내부결재", 200)])])
    assert layout.fix_recipient(d, "내부결재") == ("내부결재", "")
    assert layout.fix_recipient(d, "내부 결재") == ("내부 결재", "")
    tail = [(3000, [(200, "수신처 : 가상가과장, 가상나과장", 1200)])]
    d = page(HEAD + [(800, [(200, "수신", 160), (450, "수신처 참조", 300)])] + TITLE + tail)
    assert layout.fix_recipient(d, "수신처 참조") == ("가상가과장, 가상나과장", "목록")
