"""칸 검증 규칙 — 기계 검수 (https://github.com/reddol18/docflow/issues/87).
규칙별 통과·위반, 스키마를 읽을 때 멈추는 경우(정규식·모르는 키·길이 충돌·코드표 파일), 검토 표시와 출력 전 검증이
같은 코드를 쓰는지, 규칙 없는 스키마(김해·ISAD(G))가 그대로인지. 값·코드는 모두 지어낸 것. LLM·DB 호출 없음."""
import copy
import shutil
import time
from pathlib import Path

import pytest
import yaml

from app import export, field_rules, fields, flags

FIX = Path(__file__).parent / "fixtures"
RULES_SCHEMA = FIX / "schemas" / "road_ledger_rules.yaml"
RID = "ABCDEFGHJK0123456789XYZ"  # 지어낸 고정 길이 식별자 23자리


def rules_schema(tmp_path, edit=None, codes="R101\tA\nR102\tB\n"):
    """도로대장식 규칙 스키마를 tmp 폴더에 (base·코드표 함께). edit(raw dict)로 고쳐 쓴다"""
    shutil.copy(fields.BASE, tmp_path / "isad_g.yaml")
    (tmp_path / "codes").mkdir(exist_ok=True)
    (tmp_path / "codes" / "road_rank.txt").write_text(codes, encoding="utf-8")
    raw = yaml.safe_load(RULES_SCHEMA.read_text(encoding="utf-8"))
    if edit:
        edit(raw)
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return fields.load(p)


def field(raw, name):
    return next(f for f in raw["fields"] if f["name"] == name)


def doc(i, **values):
    return {"doc_id": f"d{i}", "pages": [f"d{i}"], "fields": values, "flags": {}}


def broken(docs, s):
    return [sorted((f["name"], k) for f, k in v) for v in field_rules.violations(docs, s)]


# ---------- 규칙별 통과·위반 ----------

def test_fixture_with_real_code_table_loads(tmp_path):
    shutil.copy(fields.BASE, tmp_path / "isad_g.yaml")  # base는 같은 폴더에
    shutil.copy(RULES_SCHEMA, tmp_path / "s.yaml")
    shutil.copytree(FIX / "schemas" / "codes", tmp_path / "codes")
    s = fields.load(tmp_path / "s.yaml")  # 시험 코드표(codes/road_rank.txt)를 스키마 폴더 기준으로 읽는다
    r = fields.by_name(s)["road_rank"]["validate"]
    assert r.codes == {"R101", "R102", "R201", "R301"} and r.length == 4 and r.required
    assert fields.by_name(s)["rdid"]["validate"].names() == ["required", "length", "pattern", "unique"]


def test_each_rule_pass_and_fail(tmp_path):
    s = rules_schema(tmp_path)
    ok = dict(rdid=RID, road_rank="R101", sheet_title="가상 조서")
    assert broken([doc(0, **ok)], s) == [[]]
    cases = {
        "required": dict(ok, road_rank=""),
        "length": dict(ok, rdid=RID[:-1]),
        "max_length": dict(ok, sheet_title="가" * 31),
        "pattern": dict(ok, rdid=RID[:-1] + "a"),
        "code_table": dict(ok, road_rank="R999"),
    }
    got = {k: broken([doc(0, **v)], s)[0] for k, v in cases.items()}
    assert got == {"required": [("road_rank", "required")], "length": [("rdid", "length"), ("rdid", "pattern")],
                   "max_length": [("sheet_title", "max_length")], "pattern": [("rdid", "pattern")],
                   "code_table": [("road_rank", "code_table")]}
    # 경계: 정확히 30자는 통과, 코드는 정확히 같아야 (앞뒤 빈칸은 다른 값 — 표기 규칙 strip이 먼저 지운다)
    assert broken([doc(0, **dict(ok, sheet_title="가" * 30))], s) == [[]]
    assert broken([doc(0, **dict(ok, road_rank="r101"))], s) == [[("road_rank", "code_table")]]


def test_empty_value_checks_only_required(tmp_path):
    s = rules_schema(tmp_path)
    # sheet_title은 required가 없다 → 빈칸이면 아무 규칙도 보지 않는다. rdid 빈칸은 required만 (길이·형식·중복 아님)
    out = broken([doc(0, rdid="", road_rank="R101", sheet_title=""), doc(1, rdid="", road_rank="R101")], s)
    assert out == [[("rdid", "required")], [("rdid", "required")]]


def test_unique_within_volume(tmp_path):
    s = rules_schema(tmp_path)
    docs = [doc(0, rdid=RID, road_rank="R101"), doc(1, rdid=RID, road_rank="R101"),
            doc(2, rdid=RID[::-1], road_rank="R101")]
    assert broken(docs, s) == [[("rdid", "unique")], [("rdid", "unique")], []]
    # 다른 철(따로 부름)과는 비교하지 않는다
    assert broken(docs[:1], s) == [[]]


def test_pattern_value_cap(tmp_path):
    """값이 VALUE_MAX자를 넘으면 정규식을 돌리지 않고 위반 (형식 검사 길이 초과)"""
    s = rules_schema(tmp_path, lambda raw: field(raw, "sheet_title").update(validate={"pattern": "[가-힣 ]+"}))
    long = "가" * (field_rules.VALUE_MAX + 1)
    assert broken([doc(0, rdid=RID, road_rank="R101", sheet_title=long)], s)[0] == [("sheet_title", "pattern_long")]
    f = fields.by_name(s)["sheet_title"]
    assert field_rules.reason(f, "pattern_long") == "기계 검수 · 조서명 · 형식 검사 길이 초과"
    assert field_rules.rule_key("pattern_long") == "pattern"


# ---------- 검토 표시 ----------

def test_machine_flags_text_and_flag_key(tmp_path):
    def edit(raw):  # 표시 키는 칸의 flag_key (https://github.com/reddol18/docflow/issues/72)
        field(raw, "road_rank")["flag_key"] = "rdid"
    s = rules_schema(tmp_path, edit)
    out = flags.machine([doc(0, rdid=RID, road_rank="R999")], s)
    assert out == [{"rdid": ["기계 검수 · 도로 종류 코드 · 코드표에 없는 값"]}]
    # 값은 사유에 싣지 않는다
    assert "R999" not in str(out)


def test_refresh_keeps_other_flags_and_returns_same_objects(tmp_path):
    s = rules_schema(tmp_path)
    a = dict(doc(0, rdid=RID, road_rank="R101"), flags={"rdid": ["LLM", "기계 검수 · 도로 식별자 · 철 안 중복"]})
    b = dict(doc(1, rdid=RID[::-1], road_rank="R101"), flags={"rdid": ["기계 검수 · 도로 식별자 · 철 안 중복"]})
    c = dict(doc(2, rdid="", road_rank="R101"), flags={"rdid": ["기계 검수 · 도로 식별자 · 필수 칸 빈칸"]})
    out = flags.refresh([a, b, c], s)
    assert out[0]["flags"] == {"rdid": ["LLM"]} and out[1]["flags"] == {} and out[2] is c  # c는 그대로 — 같은 객체
    assert a["flags"] == {"rdid": ["LLM", "기계 검수 · 도로 식별자 · 철 안 중복"]}  # 원본은 바꾸지 않는다


@pytest.mark.parametrize("path", [fields.DEFAULT, fields.BASE])
def test_schemas_without_rules_unchanged(path, monkeypatch):
    """김해·ISAD(G) 스키마는 규칙이 없다: 기계 검수 표시 없음, refresh는 같은 목록 그대로, 출력 전 검증 모양 그대로"""
    s = fields.load(path)
    monkeypatch.setattr(export, "FIELDS", tuple(f["name"] for f in fields.doc_fields(s)))  # 양식 확인은 이 스키마 칸으로
    assert field_rules.ruled(s) == []
    docs = [{"doc_id": "d0", "pages": ["d0"], "fields": {f["name"]: "" for f in s["fields"]},
             "flags": {"x": ["기계 검수 · 예전 값"]}}]
    assert flags.machine(docs, s) == [{}]
    assert flags.refresh(docs, s) is docs  # 규칙이 없으면 아무것도 건드리지 않는다
    t = export.load_template(FIX / "export" / "isad_g.yaml") if path == fields.BASE else export.load_template()
    with_schema = export.build(t, [("v", ["d0"], docs)], schema=s)
    assert with_schema == export.build(t, [("v", ["d0"], docs)])
    assert "rule" not in export.summary(with_schema[1])


# ---------- 출력 전 검증이 같은 규칙을 쓴다 ----------

def test_export_check_uses_same_rules(tmp_path, monkeypatch):
    s = rules_schema(tmp_path)
    monkeypatch.setattr(export, "FIELDS", tuple(f["name"] for f in fields.doc_fields(s)))
    t = export.load_template(FIX / "export" / "road_ledger.yaml")
    docs = [doc(0, rdid=RID, road_rank="R999", sheet_class="총괄"), doc(1, rdid=RID, road_rank="R101", sheet_class="총괄")]
    _, problems = export.build(t, [("v", ["d0", "d1"], docs)], schema=s)
    rule = [p for p in problems if p["kind"] == "rule"]
    want = [(d["doc_id"], f["name"], field_rules.rule_key(k), field_rules.reason(f, k))
            for d, vs in zip(docs, field_rules.violations(docs, s)) for f, k in vs]
    assert [(p["doc_id"], p["field"], p["rule"], p["reason"]) for p in rule] == want
    assert {(p["doc_id"], p["rule"], p["column"]) for p in rule} == {
        ("d0", "code_table", "ROAD_RANK"), ("d0", "unique", "RDID"), ("d1", "unique", "RDID")}
    assert export.summary(problems, rules=True)["rule"] == 3
    # 검토 표시와 같은 사유
    assert {r for fl in flags.machine(docs, s) for rs in fl.values() for r in rs} == {p["reason"] for p in rule}


# ---------- 스키마를 읽을 때 멈춤 ----------

@pytest.mark.parametrize("rule,msg", [
    ({"lenght": 3}, "모르는 규칙"),
    ({"required": "yes"}, "required"),
    ({"length": 0}, "length"),
    ({"length": True}, "length"),
    ({"length": 5, "max_length": 4}, "length 5 > max_length 4"),
    ({"pattern": "[A-Z"}, "정규식 오류"),
    ({"pattern": "(a+)+b"}, "되짚기"),
    ({"pattern": "(a|ab)*c"}, "되짚기"),
    ({"pattern": "([a-z]+\\s?)*"}, "되짚기"),
    ({"pattern": "(a)\\1"}, "역참조"),
    ({"pattern": "a" * 201}, "200자"),
    ({"unique": True}, "unique는 volume"),
    ({}, "규칙 객체"),
    ({"code_table": "codes/none.txt"}, "파일 없음"),
    ({"code_table": "../isad_g.yaml"}, "폴더 안 상대 경로"),
    ({"code_table": "codes/../../x.txt"}, "폴더 안 상대 경로"),
    ({"code_table": "/etc/passwd"}, "폴더 안 상대 경로"),
    ({"code_table": "C:\\x.txt"}, "폴더 안 상대 경로"),
    ({"code_table": "codes/road_rank.txt", "length": 3}, "코드표 코드 2개"),
])
def test_bad_rules_fail_at_load(tmp_path, rule, msg):
    with pytest.raises(fields.SchemaError, match=msg) as e:
        rules_schema(tmp_path, lambda raw: field(raw, "road_rank").update(validate=rule))
    assert "칸 road_rank" in str(e.value)


SAFE = ["[A-Z0-9]{23}", "[A-Z]{2}-\\d+", "\\d{4}-\\d{2}-\\d{2}", "(\\d{2}-){3}\\d{2}", "R[0-9]+", "[가-힣]+(시|군|구)",
        "a?b*c+", "[A-Z0-9 ]+x", "[^\\d]+-[0-9]+", "[가-힣 ]+", "[0-9]+(-[0-9]+)?", "^[A-Z]{3}[0-9]{2}$"]
# 리뷰에서 실측한 우회 패턴 (원자 그룹 2.35초·이어진 a* 9.1초 등) — 모두 시작할 때 거부해야 한다
BYPASS = ["(?>(a+)+b)", "a*a*a*a*a*a*a*b", "(\\w*){30}", "(a|.){60}", "\\w*\\w*\\w*\\w*\\w*\\w*\\w*\\w*",
          "(?:ab|cd){3}", ".*x.*", "[가-힣]+\\d+", "(\\d+)?\\d+", "(?i)abc", "(?i:a)b", "(?=a)a", "(?<!a)b", "(a)(?(1)b|c)"]


def test_safe_patterns_accepted():
    for p in SAFE:
        field_rules.compile_pattern(p)


@pytest.mark.parametrize("p", BYPASS)
def test_bypass_patterns_rejected(p):
    with pytest.raises(field_rules.RuleError, match="validate.pattern"):
        field_rules.compile_pattern(p)


def _worst_inputs(n=field_rules.VALUE_MAX):
    """상한 길이의 되짚기 유발 입력: 한 글자 반복 + 끝에 안 맞는 글자, 구분자 섞인 반복"""
    out = [c * (n - 1) + "!" for c in "aA1가 -_x."]
    out += [("AB-" + "1" * n)[: n - 1] + "!", ("12-" * n)[: n - 1] + "!", ("가 " * n)[: n - 1] + "1", "R" + "0" * (n - 2) + "!"]
    return out


@pytest.mark.parametrize("p", SAFE + [f["validate"]["pattern"] for f in yaml.safe_load(
    RULES_SCHEMA.read_text(encoding="utf-8"))["fields"] if "pattern" in (f.get("validate") or {})])
def test_allowed_patterns_fast_on_worst_input(p):
    """받은 패턴(시험 픽스처·흔한 식별자 모양)은 200자 최악 입력에서 0.1초 안"""
    rx = field_rules.compile_pattern(p)
    inputs = _worst_inputs()
    t = time.perf_counter()
    for s in inputs:
        rx.fullmatch(s)
    assert time.perf_counter() - t < 0.1


@pytest.mark.parametrize("codes,msg", [("# 주석만\n\n", "코드 없음"), ("R101\nR101\n", "두 번"), ("\tA\n", "빈 줄")])
def test_bad_code_table(tmp_path, codes, msg):
    with pytest.raises(fields.SchemaError, match=msg):
        rules_schema(tmp_path, codes=codes)


def test_code_table_symlink_outside_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "c.txt").write_text("R101\n", encoding="utf-8")
    inner = tmp_path / "schema"
    inner.mkdir()
    try:
        (inner / "codes").symlink_to(outside, target_is_directory=True)
    except OSError:  # Windows에서 심볼릭 링크 권한이 없으면 디렉터리 정션(권한 필요 없음)으로
        try:
            import _winapi
            _winapi.CreateJunction(str(outside), str(inner / "codes"))
        except (ImportError, OSError):
            pytest.skip("링크를 만들 수 없음")
    with pytest.raises(field_rules.RuleError, match="폴더 밖"):
        field_rules.code_path("codes/c.txt", inner)


def test_code_table_needs_file_schema():
    raw = yaml.safe_load(RULES_SCHEMA.read_text(encoding="utf-8"))
    with pytest.raises(fields.SchemaError, match="파일에서 읽은 스키마에서만"):
        fields.parse(raw, fields.load(fields.BASE))


@pytest.mark.parametrize("edit,msg", [
    (lambda f: f.update(method="hidden", validate={"required": True}), "고칠 수 있는 건 단위 칸만"),
    (lambda f: f.update(level="volume", method="manual", validate={"required": True}), "고칠 수 있는 건 단위 칸만"),
    (lambda f: f.update(method="fixed", value="R101", validate={"required": True}), "고칠 수 있는 건 단위 칸만"),
])
def test_rules_only_on_editable_fields(tmp_path, edit, msg):
    """고칠 수 없는 칸(숨김·고정값·철 단위)의 규칙은 풀 수 없는 표시를 만든다 — 시작할 때 거부"""
    with pytest.raises(fields.SchemaError, match=msg):
        rules_schema(tmp_path, lambda raw: edit(field(raw, "road_rank")))


def test_parsed_schema_reparses_and_copies(tmp_path):
    s = rules_schema(tmp_path)
    again = fields.parse(copy.deepcopy(s), fields.load(fields.BASE))  # 읽어 둔 규칙(Rules)은 그대로 받는다
    assert fields.by_name(again)["rdid"]["validate"] == fields.by_name(s)["rdid"]["validate"]


def test_regex_check_is_fast_on_long_values(tmp_path):
    """받은 패턴은 상한 길이 값에서도 빨리 끝난다 (시간 상한은 넉넉히)"""
    s = rules_schema(tmp_path, lambda raw: field(raw, "sheet_title").update(validate={"pattern": "[A-Z0-9 ]+x"}))
    docs = [doc(i, rdid=RID, road_rank="R101", sheet_title="A" * field_rules.VALUE_MAX) for i in range(200)]
    t = time.perf_counter()
    field_rules.violations(docs, s)
    assert time.perf_counter() - t < 2


def test_unique_and_code_table_ignore_surrounding_spaces(tmp_path):
    """unique·code_table은 앞뒤 빈칸을 떼고 비교 (대소문자는 그대로). length는 값 그대로"""
    s = rules_schema(tmp_path, lambda raw: field(raw, "rdid").update(validate={"unique": "volume"}))
    docs = [doc(0, rdid=RID, road_rank=" R101 "), doc(1, rdid=f" {RID}", road_rank="R102"),
            doc(2, rdid=RID.lower(), road_rank="r102")]
    assert broken(docs, s) == [[("rdid", "unique"), ("road_rank", "length")], [("rdid", "unique")],
                               [("road_rank", "code_table")]]


def test_refresh_keeps_lookalike_reasons(tmp_path):
    """기계 검수인지는 스키마 규칙이 낼 수 있는 사유 집합으로 가린다 — 머리 글자가 같아도 다른 사유는 지우지 않는다"""
    s = rules_schema(tmp_path)
    other = "기계 검수 · 다른 원천의 사유"
    a = dict(doc(0, rdid=RID, road_rank="R101"), flags={"rdid": [other, "기계 검수 · 도로 식별자 · 철 안 중복"]})
    assert flags.refresh([a], s)[0]["flags"] == {"rdid": [other]}
    reasons = field_rules.machine_reasons(s)
    assert "기계 검수 · 도로 식별자 · 철 안 중복" in reasons and other not in reasons
    assert "기계 검수 · 조서명 · 30자 초과" in reasons and "기계 검수 · 조서명 · 필수 칸 빈칸" not in reasons


def test_machine_flags_not_counted_as_not_reviewed(tmp_path, monkeypatch):
    """기계 검수 표시만 있는 미검토 문서는 not_reviewed가 아니라 rule 문제로만 센다. 다른 표시가 있으면 지금처럼 not_reviewed"""
    s = rules_schema(tmp_path)
    monkeypatch.setattr(export, "FIELDS", tuple(f["name"] for f in fields.doc_fields(s)))
    t = export.load_template(FIX / "export" / "road_ledger.yaml")
    base = dict(rdid=RID, road_rank="R999", sheet_class="총괄")
    m = flags.machine([doc(0, **base)], s)[0]
    only_machine = dict(doc(0, **base), flags=m, reviewed=False)
    with_llm = dict(doc(1, **dict(base, rdid=RID[::-1])), flags={**m, "sheet_title": ["LLM"]}, reviewed=False)
    _, problems = export.build(t, [("v", ["d0", "d1"], [only_machine, with_llm])], schema=s)
    sm = export.summary(problems, rules=True)
    assert sm["not_reviewed"] == 1 and [p["doc_id"] for p in problems if p["kind"] == "not_reviewed"] == ["d1"]
    assert sm["rule"] == 2  # 두 문서 모두 코드표 위반
    # 스키마를 안 주면(옛 호출) 지금처럼 표시가 있으면 미검토
    _, old = export.build(t, [("v", ["d0", "d1"], [only_machine, with_llm])])
    assert export.summary(old)["not_reviewed"] == 2


def test_read_codes_large_table_fast(tmp_path):
    """코드 수만 개도 바로 읽는다 (중복 찾기가 O(n²)이 아님)"""
    p = tmp_path / "c.txt"
    p.write_text("\n".join(f"C{i:05d}" for i in range(50000)), encoding="utf-8")
    t = time.perf_counter()
    assert len(field_rules.read_codes(p)) == 50000
    assert time.perf_counter() - t < 2
