"""색인 칸 스키마 (https://github.com/reddol18/docflow/issues/9) — 읽기·검사, 칸 목록을 스키마에서 가져오기, 표기 규칙, 고정값.
LLM·OCR은 부르지 않는다 (가짜로 바꿔 끼움). 값은 지어낸 것."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app import fields

GIMHAE = fields.DIR / "gimhae.yaml"

# 스키마로 옮기기 전 app/llm.py EXTRACT_PROMPT 원문 — 김해 지시문은 한 글자도 바뀌면 안 된다 (점수가 그 지시문으로 잰 것)
OLD_PROMPT = (Path(__file__).parent / "fixtures" / "gimhae_prompt_before_9.txt").read_text(encoding="utf-8")


def base():
    return fields.load(fields.BASE)


def gimhae_raw():
    import yaml
    return yaml.safe_load(GIMHAE.read_text(encoding="utf-8"))


def with_fixed(values):
    """김해 매핑 + 사업 고정값 칸 (상위계층·저작권·공개구분·이용조건)"""
    s = gimhae_raw()
    for name, isad, label in [("parent_unit", "parent_unit", "상위계층"), ("copyright", "copyright", "저작권"),
                              ("access_conditions", "access_conditions", "공개구분"),
                              ("use_conditions", "use_conditions", "이용조건")]:
        s["fields"].append({"name": name, "label": label, "level": "volume", "method": "fixed",
                            "value": values[name], "isad": isad})
    return fields.parse(s, base())


# ---------- 읽기·검사 ----------

def test_gimhae_prompt_unchanged():
    assert fields.extract_prompt(fields.load(GIMHAE)) == OLD_PROMPT


def test_default_is_gimhae_and_lists():
    s = fields.SCHEMA
    assert fields.editable(s) == ("docno", "date", "recipient", "title")
    assert fields.shown(s) == ("docno", "date", "recipient", "title")  # 일련번호는 숨김
    assert [f["name"] for f in fields.doc_fields(s)] == ["docno", "serial", "date", "recipient", "title"]
    assert fields.flag_keys(s) == {"serial": "docno"}


def test_base_fields_have_isad_area_and_gimhae_maps_to_base():
    b = base()
    assert all(f["area"] in fields.AREAS for f in b["fields"])
    assert {"identity", "context", "content", "access", "allied", "notes", "control"} == {f["area"] for f in b["fields"]}
    names = {f["name"] for f in b["fields"]}
    g = fields.load(GIMHAE)
    assert g["base"] == "isad_g.yaml"
    assert {f["name"]: f["isad"] for f in g["fields"]} == {
        "docno": "reference_code", "serial": "reference_code", "date": "dates", "recipient": "scope_content", "title": "title"}
    assert all(f["isad"] in names for f in g["fields"])


def bad(mutate, msg, mapping=True):
    s = gimhae_raw()
    mutate(s)
    with pytest.raises(fields.SchemaError, match=msg):
        fields.parse(s, base() if mapping else None)


@pytest.mark.parametrize("key", ["name", "label", "level", "method"])
def test_rejects_missing_field_item(key):
    bad(lambda s: s["fields"][4].pop(key), f"{key} 없음")


def test_rejects_bad_schema():
    bad(lambda s: s["fields"][4].update(method="ocr_magic"), "모르는 추출 방식")
    bad(lambda s: s["fields"][4].update(level="box"), "level")
    bad(lambda s: s["fields"][4].update(colour="red"), "모르는 키")
    bad(lambda s: s["fields"][4].pop("prompt"), "prompt 없음")  # llm_extract
    bad(lambda s: s["fields"][4].update(method="vocab"), "vocab 목록 없음")
    bad(lambda s: s["fields"][4].update(method="fixed"), "prompt는")
    bad(lambda s: s["fields"][4].update(isad="nope"), "isad가 기본 스키마")
    bad(lambda s: s["fields"][4].update(name="docno"), "중복")
    bad(lambda s: s["fields"][4].update(name="doc_id"), "예약어")
    bad(lambda s: s["fields"][4].update(normalize=["rot13"]), "모르는 표기 규칙")
    bad(lambda s: s["fields"][0].update(rule=""), "rule 없음")
    bad(lambda s: s["fields"][1].update(derive_from="nothing"), "derive_from")
    bad(lambda s: s["search"].update(keyword=["nothing"]), "search.keyword")
    bad(lambda s: s.pop("extract"), "extract")
    bad(lambda s: s.pop("fields"), "fields 없음")
    bad(lambda s: s.update(fieldz=[]), "모르는 키")
    bad(lambda s: None, "ISAD\\(G\\) 영역", mapping=False)  # 기본 스키마로 읽으면 area가 있어야 한다


def test_fixed_needs_value_and_volume_level_limited():
    s = gimhae_raw()
    s["fields"].append({"name": "copyright", "label": "저작권", "level": "volume", "method": "fixed", "isad": "copyright"})
    with pytest.raises(fields.SchemaError, match="고정값"):
        fields.parse(s, base())
    s["fields"][-1].update(value="", method="llm_extract", prompt="저작권")
    with pytest.raises(fields.SchemaError, match="철 단위"):
        fields.parse(s, base())


def test_load_files(tmp_path):
    (tmp_path / "x.yaml").write_text("name: x\nbase: ../isad_g.yaml\nfields: []\n", encoding="utf-8")
    with pytest.raises(fields.SchemaError, match="base 파일"):
        fields.load(tmp_path / "x.yaml")
    (tmp_path / "y.yaml").write_text("name: [\n", encoding="utf-8")
    with pytest.raises(fields.SchemaError, match="YAML"):
        fields.load(tmp_path / "y.yaml")
    b = fields.load(fields.BASE)  # 기본 스키마도 그대로 쓸 수 있다
    assert "dates" in fields.extract_prompt(b)
    with pytest.raises(fields.SchemaError, match="모르는 rule"):
        fields.check_rules(b, ["docno"])


# ---------- 칸 목록을 스키마에서 ----------

OTHER = {"name": "다른 기관", "extract": {"preamble": "앞말", "rules": "규칙"},
         "fields": [{"name": "subject", "label": "건명", "level": "document", "method": "llm_extract", "prompt": "건명",
                     "isad": "title"},
                    {"name": "kind", "label": "종류", "level": "document", "method": "vocab", "prompt": "문서 종류",
                     "vocab": ["허가", "신고"], "isad": "scope_content"},
                    {"name": "made", "label": "생산일자", "level": "document", "method": "llm_extract", "prompt": "생산일자",
                     "normalize": ["strip", "date_unknown", "decade"], "isad": "dates"},
                    {"name": "shelf", "label": "위치", "level": "document", "method": "manual", "isad": "location"},
                    {"name": "owner", "label": "저작권", "level": "volume", "method": "fixed", "value": "가상시",
                     "isad": "copyright"}],
         "search": {"keyword": ["subject"], "embed": ["subject", "made"], "year": "made"}}


def other():
    return fields.parse(copy.deepcopy(OTHER), base())


def test_llm_extract_keys_and_prompt_from_schema(monkeypatch):
    from app import llm
    seen = {}

    body = {"made": {"value": None, "confidence": 0}, "subject": {"value": "가상 건명", "confidence": 0.9},
            "kind": {"value": "신고", "confidence": 1}, "docno": {"value": "끼어든 칸"}}

    def create(**kw):
        seen["prompt"] = kw["messages"][0]["content"]
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
                               choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(body)))])
    monkeypatch.setattr(llm.store, "reserve_budget", lambda *a: 1)  # 비용 한도 예약·정산은 test_budget_pg에서
    monkeypatch.setattr(llm.store, "settle_budget", lambda *a: None)
    monkeypatch.setattr(llm, "client", lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    out = llm.extract("v", ["줄"], other())
    assert list(out) == ["subject", "kind", "made"]  # 스키마의 prompt 칸만, 스키마 순서. 모르는 칸(docno)은 버림
    assert out["kind"] == {"value": "신고", "confidence": 1.0} and out["made"] == {"value": "", "confidence": 0.0}
    assert "아래 3칸을 뽑는다." in seen["prompt"] and "- kind: 문서 종류 (다음 중 하나: 허가, 신고)" in seen["prompt"]
    assert out.failed == ()  # value null은 빈 값 (틀린 칸 아님)
    # 모양이 틀린 칸(confidence가 글자, 칸 누락)은 그 칸만 빈 값·추출 실패, 맞는 칸은 그대로
    # (https://github.com/reddol18/docflow/issues/27 — tests/test_llm_boundary.py)
    body = {"subject": {"value": "가상 건명", "confidence": 0.9}, "kind": {"value": "신고", "confidence": "x"}}
    out = llm.extract("v", ["줄"], other())
    assert out.failed == ("kind", "made") and out.problems == ("kind.confidence: float_type", "made: 칸 없음")
    assert out["subject"] == {"value": "가상 건명", "confidence": 0.9} and out["kind"] == {"value": "", "confidence": 0.0}
    no_llm = copy.deepcopy(OTHER)
    no_llm["fields"] = [f for f in no_llm["fields"] if "prompt" not in f]
    no_llm.pop("extract")
    no_llm["search"] = {"keyword": ["shelf"], "embed": ["shelf"]}
    assert llm.extract("v", ["줄"], fields.parse(no_llm, base())) == {}  # prompt 칸이 없으면 부르지 않는다


def test_assemble_and_search_helpers():
    s = other()
    v = fields.assemble(s, {"subject": {"value": " 가상 건명 "}, "kind": {"value": "몰라"}, "made": {"value": "1950년대"}}, {})
    assert v == {"subject": " 가상 건명 ", "kind": "", "made": "1950-00-00 ~ 1959-00-00", "shelf": "", "owner": "가상시"}
    assert fields.year_of(s, v) == 1950
    assert fields.year_of(s, {"made": "9999-99-99"}) is None
    assert fields.search_text(s, v) == " 가상 건명 "
    assert fields.embed_text(s, v) == "건명:  가상 건명 \n생산일자: 1950-00-00 ~ 1959-00-00"
    assert fields.editable(s) == ("subject", "kind", "made", "shelf")  # 고정값은 못 고친다
    assert "owner" not in fields.shown(s)  # 철 단위 칸은 문서 화면에 안 보이지만 값은 모든 건에 있다


def test_review_and_score_lists_follow_schema():
    from app import review_input
    assert review_input.EDITABLE == fields.editable(fields.SCHEMA)


def test_schema_endpoint():
    from app import main
    body = main.schema_view(None)  # 권한(보기)은 tests/test_auth.py가 경로 목록으로 본다
    assert [f["name"] for f in body["fields"]] == ["docno", "date", "recipient", "title"]
    assert body["fields"][0] == {"name": "docno", "label": "문서번호", "level": "document", "method": "rule",
                                 "editable": True, "flag_key": "serial", "isad": "reference_code",
                                 "rule": "docno", "hw": "docno", "crop": None}
    # 손글씨 칸·규칙은 rule로 (https://github.com/reddol18/docflow/issues/72)
    assert {f["name"]: (f["rule"], f["hw"]) for f in body["fields"]} == {
        "docno": ("docno", "docno"), "date": ("date", "date"), "recipient": ("recipient", None), "title": (None, None)}
    assert body["search_columns"] == ["date", "docno", "title", "recipient"]  # 예전 고정 검색 화면과 같은 열·순서
    assert "prompt" not in json.dumps(body)
    # 내보내는 키를 정확히 — 지시문·고정값·주기가 새지 않게
    assert set(body) == {"name", "fields", "search_columns", "machine_flag_prefix"}  # 머리: 기계 검수 표시 구분 (#87)
    assert all(set(f) == {"name", "label", "level", "method", "editable", "flag_key", "isad", "rule", "hw", "crop"}
               for f in body["fields"])


# ---------- 표기 규칙 ----------

@pytest.mark.parametrize("raw, want", [("미상", "9999-99-99"), (" 불명 ", "9999-99-99"), ("1997-03-04", "1997-03-04"),
                                        ("미상인 문서", "미상인 문서")])
def test_date_unknown(raw, want):
    assert fields.date_unknown(raw) == want


@pytest.mark.parametrize("raw, want", [("1950년대", "1950-00-00 ~ 1959-00-00"), ("1990 년대", "1990-00-00 ~ 1999-00-00"),
                                        ("1955년대", "1955년대"), ("1950년", "1950년")])
def test_decade(raw, want):
    assert fields.decade(raw) == want


@pytest.mark.parametrize("raw, want", [("제1서고 3서가 2층 5열", "1-3-2-5"), ("1서고-12서가-3층-4열", "1-12-3-4"),
                                        ("A-3-2-5", "A-3-2-5"), ("1서고 3서가", "1서고 3서가"), ("", "")])
def test_location(raw, want):
    assert fields.location(raw) == want


@pytest.mark.parametrize("raw, want", [("公文書 목록", "공문서 목록"), ("道路 占用 許可", "도로 점용 허가"), ("한글만", "한글만"),
                                        ("不許 1997年 公告", "불허 1997년 공고"),
                                        ("金海", "금해")])  # 한계: 글자마다 대표 음 하나 — 지명·성의 金(김)은 못 가린다
def test_hanja_hangul(raw, want):
    assert fields.hanja_hangul(raw) == want


def test_normalize_chain():
    f = {"normalize": ["strip", "date_unknown", "decade"]}
    assert fields.normalize(f, " 미상 ") == "9999-99-99"
    assert fields.normalize(f, None) == ""


# ---------- 사업 고정값: 철 하나를 돌리면 모든 건에 ----------

def test_fixed_values_fill_every_document(tmp_path, monkeypatch):
    from langgraph.checkpoint.memory import MemorySaver

    from app import graph, layout, llm, ocr, store
    vals = {"parent_unit": "가상시-건설과-인허가", "copyright": "가상시", "access_conditions": "공개", "use_conditions": "제한 없음"}
    schema = with_fixed(vals)
    monkeypatch.setattr(graph, "SCHEMA", schema)
    monkeypatch.setattr(store, "DATA", tmp_path)
    pages = ["v1-0001", "v1-0002", "v1-0003", "v1-0004"]
    (tmp_path / "pages" / "v1").mkdir(parents=True)
    for p in pages:
        (tmp_path / "pages" / "v1" / f"{p}.jpg").write_bytes(b"x")
    starts = {"v1-0001", "v1-0003", "v1-0004"}
    monkeypatch.setattr(ocr, "ocr_page", lambda path: {"lines": [path.stem], "items": []})
    monkeypatch.setattr(ocr, "read_image", lambda path: np.zeros((100, 100, 3), np.uint8))
    monkeypatch.setattr(layout, "is_start", lambda d: (d["page"] in starts, {}))
    monkeypatch.setattr(layout, "value_box", lambda items, k, ratio: ((0, 0, 10, 10), None))
    monkeypatch.setattr(layout, "fix_recipient", lambda d, v: (v, ""))
    monkeypatch.setattr(llm, "extract", lambda vol, lines, s=None: {
        "docno": {"value": "가상 12345-7", "confidence": 0.99}, "date": {"value": "1999-03-04", "confidence": 0.99},
        "recipient": {"value": "가상시장", "confidence": 0.99}, "title": {"value": f"가상 제목 {lines[0]}", "confidence": 0.99}})
    monkeypatch.setattr(llm, "read_cell", lambda vol, k, png: ("가상 12345-7" if k == "docno" else "99. 3. 4.", 0.99))
    saved = []
    monkeypatch.setattr(store, "event", lambda *a, **k: None)
    monkeypatch.setattr(store, "save_documents", lambda docs: saved.extend(docs))
    monkeypatch.setattr(store, "upsert_vectors", lambda docs, vecs: None)
    monkeypatch.setattr(llm, "embed", lambda vol, texts, **k: [[0.0]] * len(texts))
    g = graph.build(MemorySaver())
    g.invoke({"vol": "v1", "review_mode": "auto"}, {"configurable": {"thread_id": "v1"}})
    docs = g.get_state({"configurable": {"thread_id": "v1"}}).values["docs"]
    assert [d["doc_id"] for d in docs] == ["v1-0001", "v1-0003", "v1-0004"]
    for d in docs:
        assert {k: d["fields"][k] for k in vals} == vals
        assert d["fields"]["date"] == "1999-03-04" and d["fields"]["serial"] == "7"
    assert len(saved) == 3 and all(d["fields"]["copyright"] == "가상시" for d in saved)


def test_renamed_rule_fields(monkeypatch):
    """매핑에서 칸 이름을 바꿔도 규칙은 rule 이름으로 재료를 찾고, 다시 계산은 derive_from 칸에서 (버그 리뷰 지적)"""
    from app import graph
    s = gimhae_raw()
    ren = {"docno": "doc_number", "recipient": "addressee"}
    for f in s["fields"]:
        f["name"] = ren.get(f["name"], f["name"])
        if f.get("derive_from") == "docno":
            f["derive_from"] = "doc_number"
        if f.get("flag_key"):
            f.pop("flag_key")
    s["search"] = {"keyword": ["title", "addressee", "doc_number"], "embed": ["title"], "year": "date"}
    schema = fields.parse(s, base())
    monkeypatch.setattr(graph, "SCHEMA", schema)
    b = {"doc_number": {"value": "가상 12345-9"}, "addressee": {"value": "가상시장"}}
    assert graph._llm_of(b, "docno") == "가상 12345-9" and graph._llm_of(b, "recipient") == "가상시장"
    v = graph.rederive({"doc_number": "가상 12345-42", "serial": "9"}, {"doc_number": "가상 12345-42"})
    assert v["serial"] == "42"


def test_human_input_normalized(monkeypatch):
    s = other()
    assert fields.normalize_input(s, {"made": " 미상 ", "shelf": "x", "nope": "y"}) == {"made": "9999-99-99", "shelf": "x",
                                                                                       "nope": "y"}
    from app import review_input
    monkeypatch.setattr(fields, "SCHEMA", s)
    monkeypatch.setattr(review_input, "EDITABLE", fields.editable(s))
    flagged = [{"doc_id": "d", "fields": {"made": "9999-99-99", "subject": "가"}}]
    assert review_input.clean_fixes(flagged, [("d", "made", "미상"), ("d", "subject", "나")]) == {"d": {"subject": "나"}}


def test_schema_endpoint_isad_for_base(monkeypatch):
    from app import main
    monkeypatch.setattr(main.schema_fields, "SCHEMA", base())
    body = main.schema_view(None)
    assert next(f for f in body["fields"] if f["name"] == "title")["isad"] == "title"


def test_rederive_serial_on_edit():
    from app import graph
    v = graph.rederive({"docno": "가상 12345-88", "serial": "7", "title": "t"}, {"docno": "가상 12345-88"})
    assert v["serial"] == "88"
    assert graph.rederive({"docno": "x", "serial": "7"}, {"title": "t"})["serial"] == "7"
