"""app/flags.py 검토 표시 규칙. LLM·DB 없이 돈다 (스키마 도우미 app.fields와 칸 이름 바꾸기 eval/rename_replay.py만 쓴다)."""
import importlib.util
from pathlib import Path

import yaml

from app import fields
from app import flags as F

_spec = importlib.util.spec_from_file_location("rename_replay", Path(__file__).parent.parent / "eval" / "rename_replay.py")
rr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rr)


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


def test_order_rule_and_mismatch_toggle():
    docs = clean_volume()
    # 일련번호 103인데 날짜가 104번(1999-03-14)보다 열흘 늦음
    docs[3]["fields"]["date"] = docs[3]["info"]["b_date"] = "1999-03-24"
    assert "순서" in run(docs, rules=("order",))[3]["date"]
    docs[2]["info"]["b_date"] = "1999-03-01"
    assert "불일치" in run(docs)[2]["date"]
    assert "date" not in run(docs, mismatch_date=False)[2]



# ---------- 스키마 칸 이름으로: rule 이름으로 칸을 찾는다 (https://github.com/reddol18/docflow/issues/72) ----------

def gimhae():
    return fields.load(fields.DIR / "gimhae.yaml")


def renamed_gimhae():
    """칸 이름만 바꾼 김해 매핑 (eval/rename_replay.py와 같은 바꾸기)"""
    return fields.parse(rr.renamed_schema(yaml.safe_load((fields.DIR / "gimhae.yaml").read_text(encoding="utf-8"))),
                        fields.load(fields.BASE))


def S(*fs):
    """시험용 스키마 dict — 표시 규칙이 보는 키만 (기본: 건 단위)"""
    return {"fields": [{"level": "document", **f} for f in fs]}


def messy_volume():
    docs = clean_volume()
    docs[1]["fields"]["recipient"] = "가나시 다라구 마바동 국장"
    docs[2]["fields"]["docno"] = "-20"
    docs[3]["info"]["b_serial"] = "999"
    docs[3]["info"]["b_date"] = "1999-03-01"
    docs[4]["b"]["title"]["confidence"] = 0.5
    docs[4]["b"]["recipient"]["confidence"] = 0.5
    docs[5]["fields"]["date"] = "1999-02-30"
    return docs


def test_for_schema_gimhae_same_as_flags():
    docs = messy_volume()
    want = F.flags(docs)
    assert any(want) and F.for_schema(docs, gimhae()) == want
    assert F.targets(gimhae()) == F.LEGACY_TARGETS  # 김해 매핑에서 찾은 칸 = 김해 칸 이름 기본값
    # 일련번호 표시는 문서번호 칸의 flag_key(serial)로, 수신은 LLM 사유가 결재란보다 먼저 (예전 순서)
    assert want[4]["recipient"] == ["LLM"] and "앞부분 없음" in want[2]["serial"]


def test_for_schema_renamed_fields_same_flags():
    docs = messy_volume()
    want = [rr.rename_keys(fl, rr.RENAME) for fl in F.flags(docs)]
    assert F.for_schema([rr.rename_doc(d, rr.RENAME) for d in docs], renamed_gimhae()) == want
    assert set().union(*want) == {"serial_no", "issued", "addressee", "subject"}


def test_for_schema_skips_missing_roles_and_checks_llm_fields():
    """일자 rule만 있는 스키마: 일자 규칙만 돌고, LLM 추출·통제어휘 칸은 확신도로, 요약·사람 입력 칸은 보지 않는다"""
    schema = S({"name": "made", "method": "rule", "rule": "date", "prompt": "p"},
               {"name": "subject", "method": "llm_extract", "prompt": "p"},
               {"name": "kind", "method": "vocab", "prompt": "p", "flag_key": "subject"},
               {"name": "summary", "method": "llm_generate", "prompt": "p"},
               {"name": "shelf", "method": "manual"})
    docs = [{"fields": {"made": d["fields"]["date"], "subject": "가", "kind": "나", "summary": "다", "shelf": ""},
             "b": {"made": {"confidence": 0.1}, "subject": {"confidence": 0.99}, "kind": {"confidence": 0.1},
                   "summary": {"confidence": 0.1}},
             "info": d["info"]} for d in clean_volume()]
    docs[2]["fields"]["made"] = docs[2]["info"]["b_date"] = "1999-02-30"
    out = F.for_schema(docs, schema)
    assert out[2] == {"made": ["형식"], "subject": ["LLM"]}  # 통제어휘 kind의 표시는 flag_key(subject)로
    assert out[0] == {"subject": ["LLM"]}  # 손글씨 규칙 칸(made, rule date)은 LLM 확신도를 보지 않는다
    assert F.for_schema(docs, S({"name": "shelf", "method": "manual"})) == [{} for _ in docs]


def test_for_schema_ignores_hidden_and_volume_llm_fields():
    """LLM 확신도는 화면에 보이는 건 단위 칸만 — 숨김 칸·철 단위 칸은 사람이 볼 곳이 없으니 표시하지 않는다"""
    schema = S({"name": "subject", "method": "llm_extract", "prompt": "p"},
               {"name": "secret", "method": "hidden", "prompt": "p"},
               {"name": "series", "method": "llm_extract", "prompt": "p", "level": "volume"})
    assert F.targets(schema).llm == {"llm:subject": "subject"}
    docs = [{"fields": {}, "b": {n: {"confidence": 0.1} for n in ("subject", "secret", "series")}, "info": {}}]
    assert F.for_schema(docs, schema) == [{"subject": ["LLM"]}]


def test_for_schema_llm_field_named_like_a_role():
    """규칙 역할 이름과 같은 이름의 LLM 칸(date)이 다른 칸의 일자 규칙 표시와 섞이지 않는다"""
    schema = S({"name": "issued", "method": "rule", "rule": "date", "prompt": "p"},
               {"name": "date", "method": "llm_extract", "prompt": "p"})
    docs = [{"fields": {"issued": d["fields"]["date"], "date": "x"}, "b": {"date": {"confidence": 0.1}}, "info": d["info"]}
            for d in clean_volume()]
    docs[1]["fields"]["issued"] = docs[1]["info"]["b_date"] = ""
    out = F.for_schema(docs, schema)
    assert out[1] == {"issued": ["형식"], "date": ["LLM"]} and out[0] == {"date": ["LLM"]}


def test_flags_does_not_pick_rules_from_document_keys():
    """규칙은 targets의 역할 → 칸 이름으로만 고른다: 문서에 김해 이름 키(date)가 있어도 역할이 없으면 일자 규칙은 돌지 않는다"""
    docs = [{"fields": {"date": "아님"}, "b": {}, "info": d["info"]} for d in clean_volume()]
    assert F.for_schema(docs, S({"name": "date", "method": "manual"})) == [{} for _ in docs]


def test_default_is_legacy_plus_documented_rules():
    assert set(F.DEFAULT["rules"]) <= set(F.RULES)
    assert {k: v for k, v in F.DEFAULT.items() if k != "rules"}.keys() == {k for k in F.LEGACY if k != "rules"}
