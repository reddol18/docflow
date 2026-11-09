"""김해가 아닌 스키마로 앱이 시작하고 검토 화면·검색·내보내기가 김해 칸 이름 없이 동작하는지 (https://github.com/reddol18/docflow/issues/72).

스키마마다 새 프로세스(tests/schema_app_probe.py)에서 FIELD_SCHEMA·EXPORT_TEMPLATE을 주고 앱을 불러와 철 하나를 끝까지 돌린다.
  isad_g        기본 스키마 그대로 — 검색 결과에 제목·생산일자 열 (완료 조건 2)
  gimhae_renamed 김해 매핑에서 칸 이름만 바꿈 — 문서번호 칸 이름이 달라도 잘라낸 칸이 검토 화면에 (완료 조건 1)
  museum·road_ledger  두 번째 고객 시험 스키마 (지어낸 값, tests/fixtures/schemas) (완료 조건 4)
  court_filing  세 번째 고객 시험 스키마 — 소송기록 스캔식 (지어낸 값, 칸 구조만)
LLM·OCR·DB 호출 없음. 값은 모두 지어낸 것."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).parent / "fixtures"
SCHEMAS = ROOT / "app" / "field_schemas"
GIMHAE_NAMES = {"docno", "serial", "date", "recipient", "title"}

_spec = importlib.util.spec_from_file_location("rename_replay", ROOT / "eval" / "rename_replay.py")
rr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rr)


def _schema_dir(tmp):
    d = tmp / "schemas"
    d.mkdir()
    shutil.copy(SCHEMAS / "isad_g.yaml", d / "isad_g.yaml")  # 기관 매핑의 base는 같은 폴더에 있어야 한다
    return d


def _setup(case, tmp):
    """→ (FIELD_SCHEMA, EXPORT_TEMPLATE)"""
    if case == "isad_g":
        return SCHEMAS / "isad_g.yaml", FIX / "export" / "isad_g.yaml"
    d = _schema_dir(tmp)
    if case == "gimhae_renamed":
        p = rr.write_schema(d)
        t = (ROOT / "app" / "export_templates" / "aihub88.yaml").read_text(encoding="utf-8")
        for old, new in rr.RENAME.items():
            t = t.replace(f"field.{old},", f"field.{new},").replace(f"field.{old}}}", f"field.{new}}}")
        (tmp / "tpl.yaml").write_text(t, encoding="utf-8")
        return p, tmp / "tpl.yaml"
    if case == "road_ledger_rules":  # 칸 검증 규칙 스키마 — 코드표는 스키마 폴더 안 codes/, 양식은 도로대장 것 그대로
        shutil.copytree(FIX / "schemas" / "codes", d / "codes")
        shutil.copy(FIX / "schemas" / f"{case}.yaml", d / f"{case}.yaml")
        return d / f"{case}.yaml", FIX / "export" / "road_ledger.yaml"
    shutil.copy(FIX / "schemas" / f"{case}.yaml", d / f"{case}.yaml")
    return d / f"{case}.yaml", FIX / "export" / f"{case}.yaml"


# 칸 검증 규칙 스키마로 색인이 끝난 뒤 문서 수정(PATCH) 차례 — 값은 지어낸 것 (https://github.com/reddol18/docflow/issues/87)
RID_A = "ABCDEFGHJK0123456789XYZ"  # 고정 길이 식별자 23자리 (지어낸 값)
RID_B = "ZYXWVUTSRQ9876543210ABC"
STEPS = {"road_ledger_rules": [
    {"doc": 0, "fields": {"rdid": RID_A, "road_rank": "R101"}},  # 0번 문서: 모든 규칙 통과
    {"doc": 1, "fields": {"rdid": RID_A, "road_rank": "R999"}},  # 1번 문서: 0번과 식별자 중복 + 코드표에 없는 코드
    {"doc": 1, "fields": {"rdid": RID_B, "road_rank": "R201"}},  # 1번을 고치면 0번의 중복 표시도 사라진다
]}


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    cache = {}

    def get(case):
        if case not in cache:
            tmp = tmp_path_factory.mktemp(case)
            schema, tpl = _setup(case, tmp)
            env = {k: v for k, v in os.environ.items() if k not in ("FIELD_SCHEMA", "EXPORT_TEMPLATE", "FLAG_CONFIG")}
            env.update(FIELD_SCHEMA=str(schema), EXPORT_TEMPLATE=str(tpl), LLM_PROVIDER="openai", AUTH_MODES="local",
                       PYTHONUTF8="1")
            (tmp / "work").mkdir()
            out = tmp / "out.json"
            args = [str(tmp / "work"), str(out)]
            if case in STEPS:
                (tmp / "steps.json").write_text(json.dumps(STEPS[case], ensure_ascii=False), encoding="utf-8")
                args.append(str(tmp / "steps.json"))
            p = subprocess.run([sys.executable, "-I", str(Path(__file__).parent / "schema_app_probe.py"), *args], env=env,
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
            assert p.returncode == 0, p.stderr[-3000:]  # 앱이 시작하고 흐름이 끝까지 돌아야 한다
            cache[case] = json.loads(out.read_text(encoding="utf-8"))
        return cache[case]
    return get


def _common(r):
    """어느 스키마든: 검토에서 멈추고, 고친 값이 들어가 색인이 끝나고, 검색·내보내기가 그 스키마 칸으로 나온다"""
    names = {f["name"] for f in r["schema"]["fields"]}
    keys = {f["flag_key"] for f in r["schema"]["fields"]}
    assert r["waiting"] and r["flagged"], "LLM 확신도 낮은 칸이 검토 표시로 잡혀야 한다"
    assert all(set(d["flags"]) <= keys for d in r["flagged"]), "검토 표시는 화면이 아는 키(flag_key)에만"
    assert r["review_submit"] == 200 and r["status"]["stage"] == "done"
    fixed = next(d for d in r["docs"] if d["doc_id"] == r["fixed"]["doc_id"])
    assert fixed["fields"][r["fixed"]["field"]] == "사람이 고친 가상 값" and fixed["reviewed"]
    cols = r["schema"]["search_columns"]
    assert cols and set(cols) <= names
    assert len(r["hits"]) == len(r["docs"]) and all(set(cols) <= set(h["fields"]) for h in r["hits"])
    # 칸 값은 fields 아래 — 순위·철 키는 스키마 칸 이름에 덮이지 않는다
    assert all(set(h) == {"doc_id", "rrf", "vector_rank", "keyword_rank", "vol", "fields"} and h["vol"] == r["docs"][0]["vol"]
               for h in r["hits"])
    assert r["export_status"] == 200 and r["export_check"]["not_reviewed"] == 0
    # 기본 흐름도 미확정을 센다 (사용자 결정 2026-11-09): 검사 목록이 없으니 모든 건이 미확정 — 출력은 막지 않는다
    assert r["export_check"]["unconfirmed"] == len(r["docs"])
    assert all(len(rows) == 1 + (len(r["docs"]) if i else 1) for i, rows in enumerate(r["xlsx"].values()))
    return names


@pytest.mark.parametrize("case", ["museum", "road_ledger", "court_filing"])
def test_second_customer_schema_runs_without_gimhae_names(probe, case):
    r = probe(case)
    names = _common(r)
    assert not names & GIMHAE_NAMES
    for d in r["docs"]:
        assert not set(d["fields"]) & GIMHAE_NAMES and not set(d["flags"]) & GIMHAE_NAMES
        assert d["hw"] == {}  # 손글씨 규칙 칸이 없으면 잘라 읽지 않는다
    assert r["crops"] == [] and "손글씨 판독" not in r["review_html"]
    assert all(not set(h["fields"]) & GIMHAE_NAMES for h in r["hits"])
    # 화면 표시명이 검토 화면에 나온다
    for f in r["schema"]["fields"]:
        if f["editable"]:
            assert f">{f['label']}</label>" in r["review_html"]
    # 통제어휘·쪽수 규칙 칸 값이 내보내기에 들어간다 (김해 규칙 없이)
    sheet = list(r["xlsx"].values())[1]
    head, rows = sheet[0], sheet[1:]
    if case == "museum":
        assert rows[0][head.index("유형")] == "문서" and rows[0][head.index("소장처")] == "가상 박물관"
        assert rows[0][head.index("쪽수")] == 2 and rows[2][head.index("쪽수")] == 1
        assert r["schema"]["search_columns"] == ["produced_on", "reg_no", "material_name", "producer", "material_type"]
    elif case == "court_filing":  # 세 번째 고객 — 소송기록 스캔식 (지어낸 통제어휘 두 개, 면수 규칙)
        last = rows[-1]  # 고치지 않은 마지막 건
        assert last[head.index("사건종류")] == "민사" and last[head.index("분류")] == "서면"
        assert last[head.index("제출일자")] == "1999-03-13" and last[head.index("면수")] == 1
        assert last[head.index("예외처리사유")] is None  # 사람 입력 칸 — 빈칸은 엑셀에서 빈 셀
        assert r["schema"]["search_columns"] == ["filed_on", "case_no", "case_kind", "doc_name", "doc_class"]
    else:
        assert rows[-1][head.index("조서분류")] == "총괄" and rows[-1][head.index("조사일자")] == "19990313"  # 고치지 않은 마지막 건
        assert [h for h in r["schema"]["search_columns"]] == [f["name"] for f in r["schema"]["fields"]]  # 기본 = 보이는 칸
    # LLM 확신도 표시는 추출 칸에만 (요약 llm_generate·사람 입력·규칙 칸 제외)
    llm_keys = {f["flag_key"] for f in r["schema"]["fields"] if f["method"] in ("llm_extract", "vocab")}
    assert set().union(*(d["flags"] for d in r["flagged"])) == llm_keys


def test_schemas_without_rules_have_no_machine_flags(probe):
    """규칙(validate) 없는 스키마: 기계 검수 표시가 없고 출력 전 검증 모양도 그대로(rule 건수 칸 없음) — 지금과 같다"""
    for case in ("road_ledger", "museum", "isad_g", "gimhae_renamed"):
        r = probe(case)
        assert "rule" not in r["export_check"], case
        assert not any(x.startswith("기계 검수") for d in r["docs"] for rs in d["flags"].values() for x in rs), case
        assert not any(p["kind"] == "rule" for p in r["export_check"]["problems"]), case


def _machine(fl):
    return {k: [x for x in rs if x.startswith("기계 검수")] for k, rs in fl.items() if any(x.startswith("기계 검수") for x in rs)}


def test_rules_schema_flags_machine_check_and_recomputes_on_edit(probe):
    """도로대장식 규칙 스키마 (지어낸 값): 사람 검토 전에 기계 검수 표시가 붙고, 문서 수정 때 철 전체를 다시 본다.
    출력 전 검증의 rule 문제는 검토 표시와 같은 규칙으로 센다"""
    r = probe("road_ledger_rules")
    _common(r)
    # 사람 검토 전 (validate 노드): LLM 식별자 '가상 도로 식별자 i'는 길이·형식 위반, 사람 입력 코드는 빈칸(필수)
    for d in r["flagged"]:
        m = _machine(d["flags"])
        assert m["rdid"] == ["기계 검수 · 도로 식별자 · 길이 23자 아님", "기계 검수 · 도로 식별자 · 형식(정규식) 불일치"]
        assert m["road_rank"] == ["기계 검수 · 도로 종류 코드 · 필수 칸 빈칸"]
        assert "sheet_title" not in m  # 30자 이내
        assert "LLM" in d["flags"]["rdid"]  # 다른 표시(확신도)와 같이 붙는다
    # 검토 화면이 깨진 규칙을 보인다 (값은 싣지 않는다)
    assert "기계 검수 · 도로 종류 코드 · 필수 칸 빈칸" in r["review_html"]
    ec = r["export_check"]
    assert ec["rule"] == 3 * len(r["docs"])  # 문서마다 식별자 2 + 코드 1
    p = next(x for x in ec["problems"] if x["kind"] == "rule" and x["field"] == "road_rank")
    assert p["rule"] == "required" and p["column"] == "ROAD_RANK" and "value" not in p

    s0, s1, s2 = r["steps"]
    assert all(s["status"] == 200 for s in r["steps"])
    # 1) 0번 문서를 맞는 값으로 → 0번 기계 검수 표시가 사라지고, 다른 표시(확신도)는 남는다
    assert _machine(s0["flags"][0]) == {} and "LLM" in s0["flags"][0]["sheet_title"]
    ids = s0["ids"]
    # 다른 문서 표시는 그대로 → 고친 문서만 저장, 응답 reflagged 비어 있음 (화면은 목록을 다시 읽지 않는다)
    assert s0["saved"] == [ids[0]] and s0["flags_saved"] == [] and s0["reflagged"] == []
    assert s0["export_check"]["rule"] == 3 * (len(r["docs"]) - 1)
    # 2) 1번 문서가 0번과 같은 식별자 + 코드표에 없는 코드 → 두 문서 모두 중복, 1번은 코드표 위반.
    #    0번은 표시만 다시 쓴다(칸 값·검토 여부는 건드리지 않음), 응답이 0번을 알려 준다
    assert _machine(s1["flags"][0]) == {"rdid": ["기계 검수 · 도로 식별자 · 철 안 중복"]}
    assert _machine(s1["flags"][1]) == {"rdid": ["기계 검수 · 도로 식별자 · 철 안 중복"],
                                        "road_rank": ["기계 검수 · 도로 종류 코드 · 코드표에 없는 값"]}
    assert s1["saved"] == [ids[1]] and s1["flags_saved"] == [ids[0]] and s1["reflagged"] == [ids[0]]
    # 기계 검수 표시는 '검토 안 끝난 건'으로 세지 않는다 (rule 문제로만)
    assert s1["export_check"]["not_reviewed"] == 0
    # /schema가 칸마다 규칙과 기계 검수 사유 글자를 내려 준다 (화면이 표시 종류를 가린다)
    v = {f["name"]: f.get("validate") for f in r["schema"]["fields"]}
    assert v["road_rank"]["rules"] == ["required", "length", "code_table"] and v["surveyed_on"] is None
    assert "기계 검수 · 도로 식별자 · 철 안 중복" in v["rdid"]["reasons"]
    rules1 = sorted((x["doc_id"], x["rule"]) for x in s1["export_check"]["problems"] if x["kind"] == "rule"
                    and x["field"] in ("rdid", "road_rank") and x["doc_id"] in ids[:2])
    assert [k for _, k in rules1] == ["unique", "code_table", "unique"]
    # 3) 1번을 다른 맞는 값으로 → 0번의 중복 표시도 사라진다 (철 전체를 다시 봄)
    assert _machine(s2["flags"][0]) == {} and _machine(s2["flags"][1]) == {} and s2["reflagged"] == [ids[0]]
    assert s2["export_check"]["rule"] == 3 * (len(r["docs"]) - 2)


def test_isad_g_search_has_title_and_date_columns(probe):
    """기본 스키마(isad_g.yaml)로 띄운 서버 — 검색 결과 열에 제목·생산일자, 값도 그 칸에서"""
    r = probe("isad_g")
    _common(r)
    label = {f["name"]: f["label"] for f in r["schema"]["fields"]}
    cols = r["schema"]["search_columns"]
    assert [label[c] for c in cols] == ["생산일자", "참조코드", "제목", "생산부서"]
    assert {"title", "dates"} <= set(cols)
    h = r["hits"][0]
    assert h["fields"]["dates"].startswith("1999-03-") and h["fields"]["title"].startswith("가상 제목")


def test_renamed_docno_shows_crop_on_review(probe):
    """칸 이름을 바꾼 김해 매핑: 문서번호 칸(doc_number)도 rule로 손글씨 칸을 찾아 검토 화면에 잘라낸 칸이 보인다"""
    r = probe("gimhae_renamed")
    names = _common(r)
    assert not names & GIMHAE_NAMES  # 칸 이름은 모두 바뀌었다 (title도 subject)
    hw = {f["name"]: f["hw"] for f in r["schema"]["fields"]}
    assert hw == {"doc_number": "docno", "issued": "date", "addressee": None, "subject": None}
    assert {f["name"]: f["rule"] for f in r["schema"]["fields"]}["addressee"] == "recipient"
    did = r["flagged"][0]["doc_id"]
    assert r["crops"] and f"{did}_docno.png" in r["crops"] and r["crop_status"] == 200
    html = r["review_html"]
    assert f"/crops/{r['docs'][0]['vol']}/{did}_docno.png" in html and f"{did}_date.png" in html
    assert "손글씨 판독: 가상 12345-100" in html
    # 김해 표시 규칙이 rule로 칸을 찾아 돈다: 일련번호 표시는 serial_no(문서번호 칸의 flag_key), 일자·수신 규칙도
    flag_keys = set().union(*(d["flags"] for d in r["flagged"]))
    assert flag_keys <= {"serial_no", "issued", "addressee", "subject"} and "subject" in flag_keys
    assert {f["name"]: f["flag_key"] for f in r["schema"]["fields"]}["doc_number"] == "serial_no"
    d = r["docs"][-1]  # 고치지 않은 건
    assert d["fields"]["doc_number"].startswith("가상 12345-") and d["fields"]["serial_no"] == "100"
    assert d["fields"]["issued"] == "1999-03-10"
    # 내보내기 열도 바꾼 칸 이름으로
    head, *rows = list(r["xlsx"].values())[1]
    assert rows[-1][head.index("문서번호")] == d["fields"]["doc_number"]


def test_renamed_schema_helper_keeps_rules():
    """eval/rename_replay.py 매핑: 칸 이름·칸을 가리키는 키만 바뀌고 rule·isad·지시문 내용은 그대로"""
    raw = yaml.safe_load((SCHEMAS / "gimhae.yaml").read_text(encoding="utf-8"))
    s = rr.renamed_schema(raw)
    assert [f["name"] for f in s["fields"]] == ["doc_number", "serial_no", "issued", "addressee", "subject"]
    assert [f.get("rule") for f in s["fields"]] == [f.get("rule") for f in raw["fields"]]
    assert s["fields"][0]["flag_key"] == "serial_no" and s["fields"][1]["derive_from"] == "doc_number"
    assert s["search"]["year"] == "issued" and s["search"]["columns"] == ["issued", "doc_number", "subject", "addressee"]
    assert rr.rename_doc(rr.rename_doc({"fields": {"docno": 1}, "hw": {"docno": 2}}, rr.RENAME), rr.BACK) == {
        "fields": {"docno": 1}, "hw": {"docno": 2}}
