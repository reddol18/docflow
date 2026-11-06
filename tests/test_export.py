"""납품 형식 출력 — 양식 매핑·형식 변환·원질서·출력 전 검증 (https://github.com/reddol18/docflow/issues/12). DB 없이.
값은 모두 지어낸 것."""
import io

import pytest
import yaml
from openpyxl import load_workbook

from app import export

T = export.load_template()  # 기본 예시 양식 (app/export_templates/aihub88.yaml)


def doc(did, pages, date="1995-03-07", title="가상 제목", docno="가나 100-1", flags=None, reviewed=False):
    return {"doc_id": did, "pages": pages, "flags": flags or {}, "reviewed": reviewed,
            "fields": {"docno": docno, "serial": "1", "date": date, "recipient": "가상시장", "title": title}}


def read_back(body):
    wb = load_workbook(io.BytesIO(body))
    return {ws.title: [list(r) for r in ws.iter_rows(values_only=True)] for ws in wb.worksheets}


def test_xlsx_columns_and_formats_follow_template():
    pages = ["p01", "p02", "p03"]
    sheets, problems = export.build(T, [("1234567-1995-0001", pages, [doc("p01", ["p01", "p02"]), doc("p03", ["p03"])])])
    out = read_back(export.to_xlsx(sheets))
    raw = yaml.safe_load(export.DEFAULT_TEMPLATE.read_text(encoding="utf-8"))["sheets"]
    vt, dt_ = raw["volumes"]["title"], raw["documents"]["title"]
    assert list(out) == [vt, dt_]  # 철 목록 / 건 색인 두 시트
    assert out[vt][0] == [c["name"] for c in raw["volumes"]["columns"]]  # 열 이름·순서가 양식과 같다
    assert out[dt_][0] == [c["name"] for c in raw["documents"]["columns"]]
    assert out[vt][1] == ["1234567-1995-0001", "1234567", "1995", None, 3, 2]  # 철 이름에서 기관코드·연도
    assert out[dt_][1] == ["1234567-1995-0001", 1, "가나 100-1", "19950307", "가상시장", "가상 제목", "p01", "p02", 2]
    assert problems == []


def test_document_rows_keep_page_order():
    pages = ["a", "b", "c", "d"]
    docs = [doc("c", ["c", "d"]), doc("a", ["a"]), doc("b", ["b"])]  # 상태 안 순서가 섞여 있어도
    sheets, _ = export.build(T, [("v", pages, docs)])
    rows = sheets[T["sheets"]["documents"]["title"]][1:]
    assert [(r[1], r[6]) for r in rows] == [(1, "a"), (2, "b"), (3, "c")]  # 철의 쪽 순서(pages)대로, 순번 1부터


def test_check_counts_blank_format_and_unreviewed():
    docs = [doc("p1", ["p1"]),
            doc("p2", ["p2"], title=""),  # 필수 빈칸
            doc("p3", ["p3"], date="1997-03"),  # 일부만 읽은 날짜 → 형식 위반
            doc("p4", ["p4"], date="", docno=""),  # 필수 빈칸 2
            doc("p5", ["p5"], flags={"title": ["확신도"]}),  # 검토 표시가 있는데 검토 안 됨
            doc("p6", ["p6"], flags={"title": ["확신도"]}, reviewed=True)]  # 검토 끝남 → 안 셈
    _, problems = export.build(T, [("1234567-1995-0001", [f"p{i}" for i in range(1, 7)], docs)])
    s = export.summary(problems)
    assert (s["required_blank"], s["format"], s["not_reviewed"]) == (3, 1, 1)
    assert {(p["doc_id"], p["column"]) for p in problems if p["kind"] == "required_blank"} == \
        {("p2", "제목"), ("p4", "생산일자"), ("p4", "문서번호")}


def test_volume_name_not_matching_is_blank_required():
    _, problems = export.build(T, [("임시철", ["x"], [doc("x", ["x"])])])
    assert {p["column"] for p in problems if p["kind"] == "required_blank"} == {"생산기관코드", "생산연도"}


def test_cells_never_become_formulas():
    sheets, _ = export.build(T, [("v", ["x"], [doc("x", ["x"], title="=HYPERLINK(\"http://example.invalid\")\x07")])])
    wb = load_workbook(io.BytesIO(export.to_xlsx(sheets)))
    cell = wb.worksheets[1].cell(row=2, column=6)
    assert cell.data_type == "s" and cell.value == "=HYPERLINK(\"http://example.invalid\")"  # 제어 문자는 빠짐


@pytest.mark.parametrize("bad, msg", [
    ({}, "sheets"),
    ({"sheets": {"volumes": {"title": "a", "columns": [{"name": "x", "source": "vol"}]}}}, "documents"),
    ({"sheets": {"volumes": {"title": "a", "columns": [{"name": "x", "source": "field.title"}]},
                 "documents": {"title": "b", "columns": [{"name": "x", "source": "vol"}]}}}, "모르는 source"),
    ({"sheets": {"volumes": {"title": "a", "columns": [{"name": "x", "source": "vol"}]},
                 "documents": {"title": "b", "columns": [{"name": "x", "source": "field.nope"}]}}}, "모르는 source"),
    ({"sheets": {"volumes": {"title": "a", "columns": [{"name": "x", "source": "vol"}]},
                 "documents": {"title": "b", "columns": [{"name": "x", "source": "field.date", "format": "money"}]}}}, "format"),
    ({"sheets": {"volumes": {"title": "a", "columns": [{"name": "x", "source": "vol"}]},
                 "documents": {"title": "b", "columns": [{"name": "x", "source": "field.date", "format": "date",
                                                           "out": "DD/MM/YYYY"}]}}}, "날짜 out"),
    ({"sheets": {"volumes": {"title": "a", "columns": [{"name": "x", "source": "vol"}]},
                 "documents": {"title": "b", "columns": [{"name": "x", "source": "vol"}, {"name": "x", "source": "vol"}]}}},
     "중복"),
])
def test_template_rejects_bad_mapping(bad, msg):
    with pytest.raises(export.TemplateError, match=msg):
        export.parse_template(bad)


def test_date_out_formats():
    col = {"format": "date", "out": "YYYY.MM.DD"}
    assert export.convert("1995-03-07", col) == ("1995.03.07", False)
    assert export.convert("19950307", col) == ("1995.03.07", False)
    assert export.convert("1995-13-07", col)[1] is True
    assert export.convert("", col) == ("", False)
