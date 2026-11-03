"""app/flags.py 검토 표시 규칙. 표준 라이브러리만 쓰므로 CI 0층에서 무거운 의존성 없이 돈다."""
from app import flags as F


def doc(i, serial="100", date="1999-03-10", recipient="가나시장", title="제목", b_serial=None, b_date=None,
        conf_serial=0.99, conf_date=0.99, rec_conf=0.95, title_conf=0.95, docno=None, prefix="55142"):
    return {
        "doc_id": f"v-{i:04d}",
        "fields": {"docno": docno if docno is not None else f"창업 {prefix}-{serial}", "serial": serial,
                   "date": date, "recipient": recipient, "title": title},
        "b": {"recipient": {"confidence": rec_conf}, "title": {"confidence": title_conf}},
        "info": {"prefix": prefix, "blank": False, "conf_serial": conf_serial, "conf_date": conf_date,
                 "b_serial": serial if b_serial is None else b_serial,
                 "b_date": date if b_date is None else b_date},
    }


def run(docs, **cfg):
    return F.flags(docs, {**F.LEGACY, **cfg})


def clean_volume(n=7):
    return [doc(i, serial=str(100 + i), date=f"1999-03-{10 + i:02d}") for i in range(n)]


def test_clean_volume_has_no_flags():
    assert all(fl == {} for fl in run(clean_volume()))
    assert all(fl == {} for fl in F.flags(clean_volume()))  # 기본 설정(추가 규칙 포함)도 같다


def test_mismatch_between_text_and_handwriting():
    docs = clean_volume()
    docs[3]["info"]["b_serial"] = "999"
    docs[3]["info"]["b_date"] = "1999-03-01"
    fl = run(docs)[3]
    assert "불일치" in fl["serial"] and "불일치" in fl["date"]


def test_date_out_of_flow_and_format():
    docs = clean_volume()
    docs[3]["fields"]["date"] = docs[3]["info"]["b_date"] = "1999-09-30"
    docs[4]["fields"]["date"] = docs[4]["info"]["b_date"] = "1999-02-30"  # 없는 날
    fl = run(docs)
    assert "흐름" in fl[3]["date"]
    assert "형식" in fl[4]["date"]


def test_confidence_threshold_is_configurable():
    docs = clean_volume()
    docs[2]["info"]["conf_date"] = 0.85
    assert "date" not in run(docs, conf_date=0.8)[2]
    assert "확신도" in run(docs, conf_date=0.9)[2]["date"]


def test_rec_sign_catches_approval_titles_mixed_in():
    docs = clean_volume()
    # 값은 모두 지어낸 것 (실제 문서의 수신처를 쓰지 않는다)
    docs[1]["fields"]["recipient"] = "가나시 다라구 마바동 국장"
    docs[2]["fields"]["recipient"] = "가나도지사 국장 법무담당관"
    docs[3]["fields"]["recipient"] = "가나시장 창업민원과장"  # 정상: 기관장 + 부서 과장
    fl = run(docs, rules=("rec_sign",))
    assert "결재란" in fl[1]["recipient"]
    assert "결재란" in fl[2]["recipient"]
    assert "recipient" not in fl[3]


def test_doc_head_flags_docno_without_department():
    docs = clean_volume()
    docs[2]["fields"]["docno"] = "-20"
    assert "앞부분 없음" in run(docs, rules=("doc_head",))[2]["serial"]
    assert "serial" not in run(docs)[2]  # 규칙을 켜지 않으면 표시 없음


def test_rec_part_address_without_name():
    docs = clean_volume()
    docs[1]["fields"]["recipient"] = "가나시 다라구 마바동 12-3번지"
    docs[2]["fields"]["recipient"] = "(주)가나산업 대표"
    fl = run(docs, rules=("rec_part",))
    assert "이름 없음" in fl[1]["recipient"]
    assert "주소 없음" in fl[2]["recipient"]


def test_default_is_legacy_plus_documented_rules():
    assert set(F.DEFAULT["rules"]) <= set(F.RULES)
    assert {k: v for k, v in F.DEFAULT.items() if k != "rules"}.keys() == {k for k in F.LEGACY if k != "rules"}
