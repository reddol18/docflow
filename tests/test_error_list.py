"""검수 오류목록 — 오류 모으기·재검수 판정·담당자 가리기·양식·수식 막기 (https://github.com/reddol18/docflow/issues/88).
DB 없이 순수 함수로. 철·문서·값·사유·사람은 모두 지어낸 것."""
import datetime as dt
import io

import pytest
import yaml
from openpyxl import load_workbook

from app import error_list, export, fields, workflow

VOL = "1234567-1995-0001"
T0 = dt.datetime(2026, 11, 9, 23, 0, tzinfo=dt.timezone.utc)  # 한국 시간으로는 11-10
DUP = "기계 검수 · 제목 · 철 안 중복"
LONG = "기계 검수 · 제목 · 10자 초과"


def t(n):
    return T0 + dt.timedelta(minutes=n)


@pytest.fixture(scope="module")
def schema():
    """김해 스키마 + 제목 칸 규칙(10자까지·철 안 중복)"""
    raw = yaml.safe_load(fields.DEFAULT.read_text(encoding="utf-8"))
    next(f for f in raw["fields"] if f["name"] == "title")["validate"] = {"max_length": 10, "unique": "volume"}
    return fields.parse(raw, fields.load(fields.BASE))


THREE = workflow.load(workflow.DIR / "three_stage.yaml")
DEFAULT = workflow.load(workflow.DEFAULT)
USERS = {2: "가상 작업자", 3: "가상 품질관리자", 1: "가상 감독관"}


def who(uid):
    return {"user_id": uid, "username": f"login{uid}", "name": USERS[uid], "sso": False}


def doc(did, title, flags=None, **f):
    return {"doc_id": did, "pages": [did, did + "b"], "flags": flags or {}, "reviewed": True,
            "fields": {"docno": "가나 100-1", "date": "1995-03-07", "recipient": "가상시장", "title": title} | f}


def edit(n, did, field, old, new, uid, source="edit", ts=None):
    return {"id": n, "ts": ts or t(n), "doc_id": did, "field": field, "old": old, "new": new, "source": source, **who(uid)}


def log(n, did, stage, rnd, action, to, uid, reason=None, fixed=None, ts=None):
    return {"id": n, "ts": ts or t(n), "doc_id": did, "stage": stage, "round": rnd, "action": action, "to_stage": to,
            "reason": reason, "fixed_fields": fixed, **who(uid)}


def scenario():
    """3단계 흐름 철 하나:
    d01 제목 확신도 표시 → 작업자가 고침, 표시 없는 수신도 고침(입력 작업 — 오류 아님), 품질관리자가 일자를 고치며 승인
    d02 제목이 그때 d03과 같았던 것(철 안 중복)을 고침, 품질관리자 반려(사유) → 수신 고쳐 재제출 → 2차 합격 → 감독관 합격
    d03 제목을 나중에 바꿈(그 뒤로는 중복 아님 — 시각별 값), 품질관리자 반려 → 고친 칸 없이 재제출
    d04 확신도 표시만 있고 아무도 고치지 않음 → 오류 아님"""
    docs = [doc("d01", "가상 제목 하나", {"title": ["확신도"]}, date="1995-03-08", recipient="가상구청"),
            doc("d02", "가상 제목 둘", recipient="가상군청"), doc("d03", "가상 제목 넷"),
            doc("d04", "가상 제목 다섯", {"title": ["확신도"], "recipient": ["LLM"]})]
    edits = [edit(1, "d01", "title", "가상 제목 원본", "가상 제목 하나", 2),
             edit(1, "d01", "recipient", "가상시장", "가상구청", 2),
             edit(2, "d02", "title", "가상 제목 셋", "가상 제목 둘", 2),
             edit(6, "d03", "title", "가상 제목 셋", "가상 제목 넷", 2),
             edit(11, "d02", "recipient", "가상시장", "=가상군청", 2),
             edit(12, "d02", "recipient", "=가상군청", "가상군청", 2),
             edit(30, "d01", "date", "1995-03-07", "1995-03-08", 3, "stage:qc")]
    logs = [log(3, "d01", "entry", 1, "approve", "qc", 2, fixed=["recipient", "title"]),
            log(4, "d02", "entry", 1, "approve", "qc", 2, fixed=["title"]),
            log(7, "d03", "entry", 1, "approve", "qc", 2, fixed=["title"]),
            log(10, "d02", "qc", 1, "reject", "entry", 3, reason="=가상 사유: 수신 오기"),
            log(13, "d02", "entry", 2, "approve", "qc", 2, fixed=["recipient"]),
            log(14, "d02", "qc", 2, "approve", "supervisor", 3),
            log(15, "d02", "supervisor", 2, "approve", "_confirmed", 1),
            log(20, "d03", "qc", 1, "reject", "entry", 3, reason="가상 사유: 쪽 누락"),
            log(21, "d03", "entry", 2, "approve", "qc", 2),
            log(31, "d01", "qc", 1, "approve", "supervisor", 3, fixed=["date"])]
    return docs, edits, logs


def collect(schema, wf=THREE, status=None, data=None):
    docs, edits, logs = data or scenario()
    return error_list.collect(VOL, docs, edits, logs, schema, wf, status or {})


def by_key(rows):
    return {(r["doc.id"], r["field.name"]): r for r in rows}


def uid(r):
    return (r["_actor"] or {}).get("user_id")


def test_rows_only_real_errors_with_values_from_history(schema):
    rows = collect(schema)
    # d01 수신(표시 없는 입력 작업)·d03 제목(그때 중복 아님)·d04(표시만, 수정 없음)는 행이 없다
    assert [(r["doc.id"], r["field.name"]) for r in rows] == [
        ("d01", "date"), ("d01", "title"), ("d02", "recipient"), ("d02", "title"), ("d03", "")]
    k = by_key(rows)
    r = k[("d01", "title")]
    assert (r["error.kind"], r["error.reason"], r["value.before"], r["value.after"], r["action"], uid(r)) == (
        "표시", "확신도", "가상 제목 원본", "가상 제목 하나", "수정", 2)
    assert r["action.date"] == "2026-11-10"  # 한국 시간 날짜
    assert r["verdict"] == "품질관리자: 합격"  # 감독관은 아직
    r = k[("d01", "date")]
    assert (r["error.kind"], r["error.reason"], uid(r)) == ("검수 수정", "검수 수정 · 품질관리자", 3)
    assert r["verdict"] == "품질관리자: 합격 (검수 수정)"
    # 고쳐서 지금은 표시가 없는 규칙 위반: 고치기 전 값이 그 시각 d03 값(나중에 바뀜)과 같았다
    r = k[("d02", "title")]
    assert (r["error.kind"], r["error.reason"], r["value.before"], r["value.after"]) == ("규칙", DUP, "가상 제목 셋", "가상 제목 둘")
    # 판정 횟수는 단계마다: 품질관리자는 두 번(반려·합격), 감독관은 처음
    assert r["verdict"] == "품질관리자: 합격 (2차) · 감독관: 합격"
    r = k[("d02", "recipient")]
    assert (r["error.kind"], r["error.reason"], r["value.before"], r["value.after"]) == (
        "반려", "반려 · 품질관리자: =가상 사유: 수신 오기", "가상시장", "가상군청")
    r = k[("d03", "")]
    assert (r["field.label"], r["action"], uid(r), r["value.before"], r["value.after"]) == ("(문서)", "재제출", 2, "", "")
    assert r["verdict"] == "품질관리자: 불합격(반려)"  # 재제출 뒤 아직 다시 안 봄 — 마지막 판정은 반려
    assert {r["doc.seq"] for r in rows if r["doc.id"] == "d02"} == {2}


def test_remaining_machine_violation_is_unfixed_row(schema):
    """(라) 지금도 남은 기계 검수 위반은 고치지 않았어도 '미조치' 행. 사람 표시만 있는 칸은 행 없음"""
    docs = [doc("d01", "가상 제목이 아주 아주 깁니다", {"title": [LONG, "확신도"], "recipient": ["LLM"]})]
    rows = collect(schema, DEFAULT, {"d01": "excluded"}, (docs, [], []))
    assert len(rows) == 1
    r = rows[0]
    assert (r["field.name"], r["error.reason"], r["error.kind"], r["action"]) == ("title", f"{LONG} / 확신도", "규칙·표시", "미조치")
    assert (r["value.before"], r["value.after"], r["action.date"], r["_actor"], r["verdict"]) == (
        "가상 제목이 아주 아주 깁니다", "", "", None, "검사 제외")


def test_reject_without_resubmission_and_rule_on_before_value(schema):
    docs = [doc("d01", "가상 제목")]
    edits = [edit(1, "d01", "title", "가상 제목이 아주 아주 깁니다", "가상 제목", 2)]
    logs = [log(2, "d01", "entry", 1, "approve", "qc", 2), log(3, "d01", "qc", 1, "reject", "entry", 3, reason="가상")]
    k = by_key(collect(schema, data=(docs, edits, logs)))
    assert k[("d01", "title")]["error.reason"] == LONG  # 10자 초과였던 값을 고침 (지금은 표시 없음)
    assert k[("d01", "title")]["verdict"] == "품질관리자: 불합격(반려)"
    r = k[("d01", "")]
    assert (r["action"], r["action.date"], r["_actor"], r["verdict"]) == ("미조치", "", None, "품질관리자: 불합격(반려)")


def test_verdict_later_stage_fix_and_return(schema):
    """뒤 단계가 고치며 승인해도 앞 단계 판정은 그대로. 뒤 단계가 반려해 입력까지 되돌아가면 앞 단계는 재검수 전"""
    docs = [doc("d01", "가상 제목", {"title": ["확신도"]}), doc("d02", "가상 제목 둘", {"title": ["확신도"]})]
    edits = [edit(1, "d01", "title", "가상 원래", "가상 제목", 1, "stage:supervisor"),
             edit(2, "d02", "title", "가상 원래 둘", "가상 제목 둘", 2)]
    logs = [log(3, "d01", "entry", 1, "approve", "qc", 2), log(4, "d01", "qc", 1, "approve", "supervisor", 3),
            log(5, "d01", "supervisor", 1, "approve", "_confirmed", 1, fixed=["title"], ts=t(0)),
            log(6, "d02", "entry", 1, "approve", "qc", 2), log(7, "d02", "qc", 1, "approve", "supervisor", 3),
            log(8, "d02", "supervisor", 1, "reject", "entry", 1, reason="가상")]
    k = by_key(collect(schema, data=(docs, edits, logs)))
    assert k[("d01", "title")]["verdict"] == "품질관리자: 합격 · 감독관: 합격 (검수 수정)"
    assert k[("d02", "title")]["verdict"] == "품질관리자: 재검수 전 · 감독관: 불합격(반려)"


def test_default_flow_sample_inspection(schema):
    """기본 흐름(입력 on_index → 표본 검사): 검사로 고친 칸 (flag_key 칸). 예전 판 backfill 기록처럼 승인 기록 시각이
    수정보다 이르더라도(차지 시각) 검사 단계가 고친 칸이면 합격 (검수 수정)"""
    docs = [doc("d01", "가상 제목", {"serial": ["형식"]}, docno="가나 100-2"), doc("d02", "가상 제목 둘", {"title": ["LLM"]})]
    edits = [edit(1, "d01", "docno", "가나 100-1", "가나 100-2", 3, "inspect")]
    logs = [log(2, "d01", "inspect", 1, "approve", "_confirmed", 3, fixed=None, ts=t(0))]
    rows = collect(schema, DEFAULT, {"d01": "confirmed", "d02": "excluded"}, (docs, edits, logs))
    assert [(r["doc.id"], r["field.name"]) for r in rows] == [("d01", "docno")]  # d02는 표시만
    r = rows[0]  # 김해: 일련번호 표시는 문서번호 칸에서 고친다 (flag_key)
    assert (r["field.label"], r["error.reason"], r["verdict"]) == ("문서번호", "형식", "검사: 합격 (검수 수정)")
    assert collect(schema, DEFAULT, {}, (docs, edits, []))[0]["verdict"] == "미판정"


def test_no_errors_no_rows(schema):
    docs = [doc("d01", "가상 제목")]
    edits = [edit(1, "d01", "recipient", "가상시장", "가상구청", 2, "review")]  # 표시·규칙·반려 없는 입력 작업
    assert collect(schema, DEFAULT, {}, (docs, edits, [])) == []


def test_unique_timeline_scales(schema):
    """철 안 중복 다시 보기: 고친 문서만 시각별로 센다 — 문서 2,000건 중 고친 문서 200건도 금방"""
    import time
    docs = [doc(f"d{i:04}", f"가상{i}") for i in range(2000)]
    edits = [edit(i, f"d{i:04}", "title", f"가상{i + 1}", f"가상{i}", 2) for i in range(0, 2000, 10)]
    start = time.perf_counter()
    rows = collect(schema, DEFAULT, {}, (docs, edits, []))
    assert time.perf_counter() - start < 2
    assert len(rows) == 200 and all(r["error.reason"] == DUP for r in rows)  # 고치기 전 값 = 다음 문서 값


def sheet(t, rows, key=b"k" * 32):
    return error_list.build(t, rows, key)[t["sheet"]["title"]]


def test_masked_actor_hides_ids_and_names(schema):
    t = error_list.load_template()
    assert t["actor"] == "masked"  # 기본은 가린 ID
    rows = collect(schema)
    out = sheet(t, rows)
    col = out[0].index("담당자")
    got = [r[col] for r in out[1:]]
    flat = " ".join(str(v) for r in out for v in r)
    assert not any(n in flat for n in USERS.values()) and "login" not in flat  # 표시 이름·로그인 이름이 없다
    assert all(g.startswith("담당자-") and len(g) == len("담당자-") + error_list.MASK_LEN for g in got)
    assert len({g for g, r in zip(got, rows) if uid(r) == 2}) == 1  # 한 파일 안에서는 같은 사람이 같은 값
    assert got[0] != got[1]  # 다른 사람(품질관리자·작업자)은 다른 값
    assert sheet(t, rows, b"x" * 32)[1][col] != got[0]  # 키가 다르면(다른 내보내기) 다른 값 — 파일끼리 잇지 못한다
    assert error_list.masked_id(b"k" * 32, None) == ""


def test_name_actor_and_template_columns(schema):
    t = error_list.parse_template({**yaml.safe_load(error_list.DEFAULT_TEMPLATE.read_text(encoding="utf-8")), "actor": "name"})
    out = sheet(t, collect(schema))
    assert out[0] == ["철번호", "건순번", "문서", "칸", "오류 구분", "오류 내용", "수정 전 값", "수정 후 값", "조치", "담당자",
                      "조치일", "재검수 판정"]
    assert out[1] == [VOL, 1, "d01", "시행일자", "검수 수정", "검수 수정 · 품질관리자", "1995-03-07", "1995-03-08", "수정",
                      "가상 품질관리자", "2026-11-10", "품질관리자: 합격 (검수 수정)"]
    # 표시 이름이 없으면 로그인 이름 대신 가린 ID, SSO 사용자는 구별 표시
    row = collect(schema)[0]
    assert error_list.actor(t, b"k" * 32, {"_actor": {**row["_actor"], "name": None}}).startswith("담당자-")
    assert error_list.actor(t, b"k" * 32, {"_actor": {**row["_actor"], "sso": True}}) == "가상 품질관리자 (SSO)"


def test_template_rejects_unknown():
    base = yaml.safe_load(error_list.DEFAULT_TEMPLATE.read_text(encoding="utf-8"))
    for bad in ({**base, "actor": "username"}, {**base, "actr": "name"},
                {**base, "sheet": {"title": "x", "columns": [{"name": "a", "source": "user.id"}]}},
                {**base, "sheet": {"title": "a/b", "columns": [{"name": "a", "source": "vol"}]}},
                {**base, "sheet": {"title": "x", "columns": [{"name": "a", "source": "vol"}, {"name": "a", "source": "actor"}]}}):
        with pytest.raises(export.TemplateError):
            error_list.parse_template(bad)


def test_formula_values_stay_text(schema):
    """반려 사유·값이 '='·'+'·'-'·'@'로 시작해도 수식이 아니라 글자 — 따옴표 접두까지"""
    t = error_list.load_template()
    rows = collect(schema)
    rows[0] = {**rows[0], "value.before": "+1+1", "value.after": "@SUM(A1)", "error.reason": "-2"}
    wb = load_workbook(io.BytesIO(export.to_xlsx(error_list.build(t, rows, b"k" * 32))))
    ws = wb.worksheets[0]
    cells = [c for row in ws.iter_rows() for c in row if isinstance(c.value, str) and c.value[:1] in "=+-@"]
    assert {c.value for c in cells} >= {"+1+1", "@SUM(A1)", "-2"}
    assert cells and all(c.data_type == "s" and c.quotePrefix for c in cells)
    assert not any(c.data_type == "f" for row in ws.iter_rows() for c in row)


def test_long_cell_capped(schema):
    t = error_list.load_template()
    rows = [{**collect(schema)[0], "error.reason": "가" * 40000}]
    out = sheet(t, rows)
    assert len(out[1][out[0].index("오류 내용")]) == error_list.CELL_TEXT_MAX
