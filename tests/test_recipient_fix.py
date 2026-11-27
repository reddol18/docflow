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


def test_title_or_body_line_not_appended():
    """수신 다음 줄이 제목·본문이면(항목명이 '제' 한 글자·\"제'목\"으로 읽히거나 아예 안 읽혀도) 이어 붙이지 않는다"""
    addr = (800, [(200, "수신", 160), (450, "가상시 가상동 7-1", 400)])
    for nxt in [(880, [(200, "제", 60), (450, "가상 사업 준공 통보", 500)]),
                (880, [(200, "제'목", 120), (450, "가상 시설 사용 허가 알림", 600)]),
                (880, [(450, "가상 보증금 반환 통보", 500)]),
                (880, [(450, "가상 서류 제출 요청", 500)]),
                (880, [(450, "1.귀하께서 제출한 가상동 서류에 대하여", 900)])]:
        d = doc([addr, nxt])
        assert layout.rule_recipient(d) == "가상시 가상동 7-1"
        assert layout.fix_recipient(d, "가상시 가상동 7-1") == ("가상시 가상동 7-1", "")
    # 주소 다음 줄의 상호·'대표'는 그대로 이어 붙인다
    d = doc([addr, (880, [(450, "가상물산 대표", 400)])])
    assert layout.fix_recipient(d, "가상시 가상동 7-1") == ("가상시 가상동 7-1 가상물산 대표", "잘림")
    assert layout.title_or_body("가상 허가 통보") and not layout.title_or_body("(주)가상물산 대표")


def test_lot_number_split_keeps_llm_spacing():
    """번지가 '12-' / '34번지'로 줄이 나뉘어 읽혀도 규칙 값으로 바꿀 때 LLM 띄어쓰기를 그대로 둔다"""
    d = doc([(800, [(200, "수신", 160), (450, "가상시 가상동 12-", 400)]),
             (880, [(450, "34번지(주)가상산업", 400)]),
             (960, [(450, "대표", 100)])])
    assert layout.rule_recipient(d) == "가상시 가상동 12-34번지(주)가상산업 대표"  # '-' 뒤에 띄어쓰기를 넣지 않는다
    assert layout.fix_recipient(d, "가상시 가상동 12-34번지 (주)가상산업") == (
        "가상시 가상동 12-34번지 (주)가상산업 대표", "잘림")
    d = doc([(800, [(200, "수신", 160), (450, "가상구 가상동5-", 400)]),
             (880, [(450, "6번지 가상빌라 101호", 400)]), (960, [(450, "가상정밀대표", 300)])])
    assert layout.fix_recipient(d, "가상구 가상동 5-6번지 가상빌라 101호") == (
        "가상구 가상동 5-6번지 가상빌라 101호 가상정밀대표", "잘림")


def test_merge_with_rule():
    f = layout.merge_with_rule
    # 앞·가운데를 규칙 값 원문에서 채움 (원문 띄어쓰기·괄호 그대로)
    assert f("(주)가상전자 대표", "가상시 가상동 5-6 (주)가상전자 대표") == "가상시 가상동 5-6 (주)가상전자 대표"
    assert f("가상시 (주)가상전자", "가상시 가상동 5-6(주)가상전자") == "가상시 가상동 5-6(주)가상전자"
    # 겹치는 낱말은 LLM 띄어쓰기
    assert f("가상시 가상동 5-6번지", "가상시 가상동5- 6번지 가상상사") == "가상시 가상동 5-6번지 가상상사"
    # 원문에서 붙어 있던 뒷부분은 붙인다
    assert f("가상시 가상공업", "가상시 가상공업사") == "가상시 가상공업사"
    # 낱말이 차례대로 안 맞으면 규칙 값 그대로
    assert f("가상시 없는말", "가상시 가상동 1") == "가상시 가상동 1"


def test_whitespace_only_is_not_approval_strip():
    """띄어쓰기만 다른 값은 결재란을 뗀 것으로 보지 않는다 — 앞 두 글자만 같은 딴 규칙 값으로 바뀌지 않게"""
    assert layout.strip_approval_tail("가상시  가상동 가상상사") == "가상시  가상동 가상상사"
    d = doc([(800, [(200, "수신", 160), (450, "가상시청 가상민원봉사실 가상", 700)])])
    assert layout.fix_recipient(d, "가상시  가상동 가상상사") == ("가상시  가상동 가상상사", "")


def test_approval_word_needs_overlap_with_rule():
    """결재란 낱말이 있어도 규칙 값이 LLM 값과 거의 안 겹치면(앞 두 글자만 같음) 바꾸지 않는다"""
    d = doc([(800, [(200, "수신", 160), (450, "가상군 가상읍 9-9 다른공업사 대표", 800)])])
    assert layout.fix_recipient(d, "가상시 가상동 1-2 가상테크 실장") == ("가상시 가상동 1-2 가상테크", "결재란")
    # 가운데 결재란 낱말은 빼고 견주고, 값에서도 뺀다
    d = doc([(800, [(200, "수신", 160), (450, "가상도지사", 300), (1300, "국장", 120)]),
             (880, [(450, "가상기획담당관", 300)])])
    assert layout.fix_recipient(d, "가상도지사 국장") == ("가상도지사 가상기획담당관", "결재란·잘림")
    assert layout.fix_recipient(d, "가상도지사 국장 가상기획") == ("가상도지사 가상기획담당관", "결재란·잘림")


def test_company_line_not_mistaken_for_body_or_title():
    """'주)가상산업'(가나다 번호가 아님)·'(주)가상 폐기물처리'(상호 표지)는 이어 붙인다"""
    addr = (800, [(200, "수신", 160), (450, "가상시 가상동 3-4", 400)])
    d = doc([addr, (880, [(450, "주)가상산업 대표", 400)])])
    assert layout.rule_recipient(d) == "가상시 가상동 3-4 주)가상산업 대표"
    d = doc([addr, (880, [(450, "(주)가상 폐기물처리", 400)])])
    assert layout.rule_recipient(d) == "가상시 가상동 3-4 (주)가상 폐기물처리"
    assert layout.title_or_body("가. 가상 시설 현황") and layout.title_or_body("가상 시설 정비 결과 보고")
    # 상호 표지가 없으면 낱말 하나뿐인 제목도, 줄 가운데 '대표'가 있는 제목도 제목
    assert layout.title_or_body("가상공사계획승인신청") and layout.title_or_body("가상상사 대표자 변경 신청")
    assert not layout.title_or_body("(주)가상폐기물처리") and not layout.title_or_body("가상정밀 대표")


def test_one_char_title_label_with_text_in_one_box():
    """'제 가상사업 승인'이 항목명 자리에서 시작하는 한 상자로 읽혀도 제목 줄로 보고 이어 붙이지 않는다"""
    d = doc([(800, [(200, "수신", 160), (450, "가상시 가상동 7-1", 400)]), (880, [(200, "제 가상사업 승인 알림판", 900)]),
             (960, [(450, "가상 둘째 줄", 400)])])  # 제목 둘째 줄 (멈추지 않으면 이 줄이 붙는다)
    assert layout.rule_recipient(d) == "가상시 가상동 7-1"


def test_middle_approval_word_dropped_when_rule_agrees():
    """LLM 값 가운데 결재란 낱말 — 규칙 값과 나머지가 같으면 그 낱말만 뗀다 (길이·같음 판정은 뗀 값으로)"""
    d = doc([(800, [(200, "수신", 160), (450, "가상도지사", 300), (1300, "국장", 120)]),
             (880, [(450, "가상기획담당관", 300)])])
    assert layout.fix_recipient(d, "가상도지사 국장 가상기획담당관") == ("가상도지사 가상기획담당관", "결재란")


def test_tail_junk_only_whole_words():
    """'일자리경제과'·'시행사'처럼 이름 앞머리가 같은 낱말은 끊지 않는다. 다 끊겨 LLM 값 그대로면 규칙 이름도 없음"""
    addr = (800, [(200, "수신", 160), (450, "가상시 가상동 8-9", 400)])
    d = doc([addr, (880, [(450, "가상시청 일자리경제과", 600)])])
    assert layout.fix_recipient(d, "가상시 가상동 8-9") == ("가상시 가상동 8-9 가상시청 일자리경제과", "잘림")
    d = doc([addr, (880, [(450, "가상건설 시행사 대표", 600)])])
    assert layout.fix_recipient(d, "가상시 가상동 8-9")[0] == "가상시 가상동 8-9 가상건설 시행사 대표"
    d = doc([addr, (880, [(450, "시행 2001.1.2", 300)])])
    assert layout.fix_recipient(d, "가상시 가상동 8-9") == ("가상시 가상동 8-9", "")


def test_lot_digit_needs_area_word_and_not_last():
    f = layout.clean_rule
    assert f("가상리 5 가상농원", keep_lot=True) == "가상리 5 가상농원"
    assert f("가상시장 1 가상민원과장", keep_lot=True) == "가상시장 가상민원과장"  # 앞 낱말이 리·동 꼴이 아님
    assert f("가상농원 가상리 5", keep_lot=True) == "가상농원 가상리"  # 마지막 낱말
    assert f("A 가상상사 대표") == "가상상사 대표"  # 맨 앞 한 글자 조각 (영문도)


def test_lot_digit_kept_when_filling():
    """규칙 값의 번지 숫자 한 글자('가상리 5')는 견줄 때만 떼고 채운 값에는 남긴다"""
    d = doc([(800, [(200, "수신", 160), (450, "가상군 가상면 가상리 5", 500)]), (880, [(450, "가상농원 대표", 400)])])
    assert layout.clean_rule(layout.rule_recipient(d)) == "가상군 가상면 가상리 가상농원 대표"
    assert layout.fix_recipient(d, "가상군 가상면 가상리") == ("가상군 가상면 가상리 5 가상농원 대표", "잘림")


def test_tail_stops_at_header_words():
    """뒤에 붙일 규칙 값이 결재·처리란 낱말('접수번호')까지 읽었으면 거기서 끊는다"""
    d = doc([(800, [(200, "수신", 160), (450, "가상시 가상동 8-9", 400)]), (880, [(450, "가상유통 대표 접수번호 12", 600)])])
    assert layout.fix_recipient(d, "가상시 가상동 8-9") == ("가상시 가상동 8-9 가상유통 대표", "잘림")


def test_split_je_in_value_column_is_not_title():
    """값 칸에서 '제'가 따로 읽힌 상호('제 일가상 대표')는 제목 줄이 아니다 — 항목명 자리의 '제'만 제목"""
    d = doc([(800, [(200, "수신", 160), (450, "가상시 가상동 10-1", 400)]),
             (880, [(450, "제", 50), (520, "일가상 대표", 300)])])
    assert layout.rule_recipient(d) == "가상시 가상동 10-1 제 일가상 대표"


def test_internal_and_list_unchanged():
    d = doc([(800, [(200, "수신", 160), (450, "내부결재", 200)])])
    assert layout.fix_recipient(d, "내부결재") == ("내부결재", "")
    assert layout.fix_recipient(d, "내부 결재") == ("내부 결재", "")
    tail = [(3000, [(200, "수신처 : 가상가과장, 가상나과장", 1200)])]
    d = page(HEAD + [(800, [(200, "수신", 160), (450, "수신처 참조", 300)])] + TITLE + tail)
    assert layout.fix_recipient(d, "수신처 참조") == ("가상가과장, 가상나과장", "목록")
