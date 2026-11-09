"""납품 형식 출력 — 기관 엑셀 일괄등록 양식 (https://github.com/reddol18/docflow/issues/12).

양식은 YAML 매핑(`app/export_templates/*.yaml`): 양식 열 ↔ 원천(철·건·색인 칸·고정값) ↔ 형식 변환.
철 목록 시트와 건 색인 시트를 따로 내고, 건은 철 안 쪽 순서(원질서)대로 둔다.
출력 전 검증은 필수 열 빈칸·형식 위반·검토 안 끝난 건을 센다. 검토 흐름 설정이 count_unconfirmed면
마지막 단계 승인 전 건도 '미확정'으로 센다. 스키마에 칸 검증 규칙(validate)이 있으면 그 위반도 센다 —
검토 표시와 같은 코드(app/field_rules.violations)로 지금 값을 다시 본다."""
import datetime as dt
import io
import os
import re
from pathlib import Path

import yaml

from app import field_rules, fields

DEFAULT_TEMPLATE = Path(__file__).parent / "export_templates" / "aihub88.yaml"
FIELDS = tuple(f["name"] for f in fields.doc_fields(fields.SCHEMA))  # 색인 스키마의 문서별 칸 (https://github.com/reddol18/docflow/issues/9)
VOL_KEYS = ("pages", "docs")
DOC_KEYS = ("seq", "first_page", "last_page", "pages")
FORMATS = ("text", "int", "year", "date")
DATE_OUT = {"YYYY-MM-DD": "{y}-{m}-{d}", "YYYYMMDD": "{y}{m}{d}", "YYYY.MM.DD": "{y}.{m}.{d}"}
SHEETS = ("volumes", "documents")
CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")  # XML 1.0이 못 담는 글자
SHEET_BAD = re.compile(r"[\[\]:*?/\\]")  # 엑셀 시트 이름에 못 쓰는 글자 (31자까지)
PROBLEMS_MAX = 200  # 응답에 싣는 문제 목록 상한 (건수는 모두 센다)


class TemplateError(ValueError):
    pass


def _check_source(src, sheet, groups):
    if src in ("vol", "const"):
        return
    kind, _, key = src.partition(".")
    ok = {"vol": set(VOL_KEYS) | groups, "doc": set(DOC_KEYS) if sheet == "documents" else set(),
          "field": set(FIELDS) if sheet == "documents" else set()}
    if key not in ok.get(kind, set()):
        raise TemplateError(f"{sheet}: 모르는 source {src!r}")


def load_template(path=None):
    """양식 매핑을 읽고 검사한다 — 빠진 항목·모르는 원천·형식은 거부"""
    path = Path(path or os.environ.get("EXPORT_TEMPLATE") or DEFAULT_TEMPLATE)
    t = yaml.safe_load(path.read_text(encoding="utf-8"))
    return parse_template(t)


def parse_template(t):
    if not isinstance(t, dict) or not isinstance(t.get("sheets"), dict):
        raise TemplateError("sheets 없음")
    rx = None
    if t.get("volume_id"):
        try:
            rx = re.compile(t["volume_id"])
        except re.error as e:
            raise TemplateError(f"volume_id 정규식 오류: {e}") from None
    groups = set(rx.groupindex) if rx else set()
    sheets = {}
    for sheet in SHEETS:
        s = t["sheets"].get(sheet)
        if not isinstance(s, dict) or not s.get("title") or not isinstance(s.get("columns"), list) or not s["columns"]:
            raise TemplateError(f"{sheet}: title·columns 필요")
        if len(str(s["title"])) > 31 or SHEET_BAD.search(str(s["title"])):
            raise TemplateError(f"{sheet}: 시트 이름은 31자까지, []:*?/\\ 못 씀")
        cols = []
        for c in s["columns"]:
            if not isinstance(c, dict) or not c.get("name") or not c.get("source"):
                raise TemplateError(f"{sheet}: 열마다 name·source 필요 ({c!r})")
            _check_source(c["source"], sheet, groups)
            fmt = c.get("format", "text")
            if fmt not in FORMATS:
                raise TemplateError(f"{sheet}.{c['name']}: 모르는 format {fmt!r}")
            if fmt == "date" and c.get("out", "YYYY-MM-DD") not in DATE_OUT:
                raise TemplateError(f"{sheet}.{c['name']}: 모르는 날짜 out {c.get('out')!r}")
            if c["source"] == "const" and not isinstance(c.get("value", ""), str):
                raise TemplateError(f"{sheet}.{c['name']}: const value는 문자열")
            cols.append({"name": str(c["name"]), "source": c["source"], "format": fmt,
                         "out": c.get("out", "YYYY-MM-DD"), "required": bool(c.get("required")),
                         "value": c.get("value", "")})
        names = [c["name"] for c in cols]
        if len(set(names)) != len(names):
            raise TemplateError(f"{sheet}: 열 이름 중복")
        sheets[sheet] = {"title": str(s["title"]), "columns": cols}
    if sheets["volumes"]["title"].casefold() == sheets["documents"]["title"].casefold():  # 엑셀은 대소문자를 안 가린다
        raise TemplateError("두 시트 이름이 같음")
    return {"name": t.get("name", ""), "volume_id": rx, "sheets": sheets}


def convert(value, col):
    """원천 값 → 양식 칸 값. (값, 형식 위반 여부). 빈 값은 위반이 아니다 (빈칸은 required로 센다)"""
    v = CONTROL.sub("", "" if value is None else str(value)).strip()  # 제어 문자는 xlsx에 못 쓴다 (openpyxl 오류)
    if not v:
        return "", False
    fmt = col["format"]
    if fmt == "int":  # ASCII 숫자만 ('①'·'²'는 isdigit()이 참이지만 int()가 못 읽는다)
        return (int(v), False) if re.fullmatch(r"[0-9]+", v) else (v, True)
    if fmt == "year":
        return (v, False) if re.fullmatch(r"[0-9]{4}", v) and v != "0000" else (v, True)
    if fmt == "date":
        m = re.fullmatch(r"([0-9]{4})([-.]?)([0-9]{2})\2([0-9]{2})", v)  # 구분자는 한 가지로
        try:
            dt.date(int(m.group(1)), int(m.group(3)), int(m.group(4))) if m else None
        except ValueError:
            m = None  # 없는 날짜 (02-30, 04-31, 0000년)
        if not m:
            return v, True  # 일부만 읽은 날짜(예: 1997-03)도 위반 — 사람이 채워야 한다
        return DATE_OUT[col["out"]].format(y=m.group(1), m=m.group(3), d=m.group(4)), False
    return v, False


def ordered_docs(pages, docs):
    """철 안 쪽 순서(원질서)대로. 쪽 목록에 없는 시작 쪽은 뒤로 (이름순)"""
    pos = {p: i for i, p in enumerate(pages)}
    return sorted(docs, key=lambda d: (pos.get(d["pages"][0], len(pos)), d["pages"][0]))


def _vol_values(t, vol, pages, docs):
    m = t["volume_id"].fullmatch(vol) if t["volume_id"] else None
    vals = {"vol": vol, "vol.pages": len(pages), "vol.docs": len(docs)}
    for g in t["volume_id"].groupindex if t["volume_id"] else ():
        vals[f"vol.{g}"] = m.group(g) if m else ""
    return vals


def _row(cols, vals, where, problems):
    out = []
    for c in cols:
        raw = c["value"] if c["source"] == "const" else vals.get(c["source"], "")
        v, bad = convert(raw, c)
        if c["required"] and v == "":
            problems.append({**where, "column": c["name"], "kind": "required_blank"})
        elif bad:
            problems.append({**where, "column": c["name"], "kind": "format", "value": v})
        out.append(v)
    return out


def build(t, volumes, unconfirmed=None, schema=None):
    """volumes = [(vol, pages, docs)] → 시트 {title: [머리 행, 값 행...]}, 문제 목록.
    문제: required_blank(필수 열 빈칸), format(형식 위반), not_reviewed(검토 표시가 있는데 사람 검토 안 됨),
    unconfirmed(검토 흐름의 마지막 단계 승인 전 — unconfirmed={vol: {doc_id}}를 줄 때만, https://github.com/reddol18/docflow/issues/86),
    rule(칸 검증 규칙 위반 — schema를 주면 지금 값으로 다시 본다. 검토 표시와 같은 코드 field_rules.violations,
    https://github.com/reddol18/docflow/issues/87). rule 문제에는 칸 이름(field)·규칙 이름(rule), 양식에 그 칸 열이 있으면 열 이름"""
    vs, ds = t["sheets"]["volumes"], t["sheets"]["documents"]
    col_of = {c["source"][len("field."):]: c["name"] for c in ds["columns"] if c["source"].startswith("field.")}
    vrows, drows, problems = [], [], []
    for vol, pages, docs in volumes:
        docs = ordered_docs(pages, docs)
        vvals = _vol_values(t, vol, pages, docs)
        vrows.append(_row(vs["columns"], vvals, {"sheet": vs["title"], "vol": vol, "doc_id": None}, problems))
        broken = field_rules.violations(docs, schema) if schema else [[] for _ in docs]
        for i, d in enumerate(docs, 1):
            vals = {**vvals, "doc.seq": i, "doc.first_page": d["pages"][0], "doc.last_page": d["pages"][-1],
                    "doc.pages": len(d["pages"]), **{f"field.{k}": v for k, v in (d.get("fields") or {}).items()}}
            drows.append(_row(ds["columns"], vals, {"sheet": ds["title"], "vol": vol, "doc_id": d["doc_id"]}, problems))
            if d.get("flags") and not d.get("reviewed"):
                problems.append({"sheet": ds["title"], "vol": vol, "doc_id": d["doc_id"], "column": None,
                                 "kind": "not_reviewed"})
            if unconfirmed is not None and d["doc_id"] in unconfirmed.get(vol, ()):
                problems.append({"sheet": ds["title"], "vol": vol, "doc_id": d["doc_id"], "column": None,
                                 "kind": "unconfirmed"})
            for f, k in broken[i - 1]:
                problems.append({"sheet": ds["title"], "vol": vol, "doc_id": d["doc_id"], "column": col_of.get(f["name"]),
                                 "kind": "rule", "field": f["name"], "rule": field_rules.rule_key(k),
                                 "reason": field_rules.reason(f, k)})
    sheets = {vs["title"]: [[c["name"] for c in vs["columns"]], *vrows],
              ds["title"]: [[c["name"] for c in ds["columns"]], *drows]}
    return sheets, problems


def summary(problems, unconfirmed=False, rules=False):
    """건수 요약. unconfirmed: 미확정 건수 칸을 더한다 (흐름 설정 count_unconfirmed — 기본 흐름은 지금처럼 없음).
    rules: 칸 검증 규칙 위반 건수 칸(rule)을 더한다 (스키마에 validate가 있을 때 — 없는 스키마는 지금과 같은 모양)"""
    kinds = (("required_blank", "format", "not_reviewed") + (("unconfirmed",) if unconfirmed else ())
             + (("rule",) if rules else ()))
    count = {k: sum(p["kind"] == k for p in problems) for k in kinds}
    return {**count, "problems": problems[:PROBLEMS_MAX], "problems_total": len(problems)}


def to_xlsx(sheets):
    """모든 글자 칸을 문자열 형식으로 쓴다 — '='·'+'·'-'·'@'로 시작하는 값이 엑셀에서 수식으로 실행되지 않게
    (openpyxl은 '='로 시작하는 문자열을 수식으로 저장한다). 숫자 열(int)만 숫자로"""
    from openpyxl import Workbook
    wb = Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title)
        for r, row in enumerate(rows, 1):
            for c, v in enumerate(row, 1):
                cell = ws.cell(row=r, column=c)
                cell.value = v
                if isinstance(v, str):
                    cell.data_type = "s"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
