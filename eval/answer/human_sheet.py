"""사람 채점표 만들기 — 심판 점수를 가린 채점 파일 (https://github.com/reddol18/docflow/issues/21 4단계).

답변 파일(eval/answer/gen_answers.py 결과)만 읽는다 — 심판 결과는 읽지 않으므로 표에 심판 점수가 들어갈 수 없다.
표 한 줄 = 질의 하나: 질문·답·참고 답·근거 문서·가져온 문서(순위·넘긴 글)·답의 인용, 그리고 지표마다 칸 하나(P/F/N).
그 질의에 해당하지 않는 지표 칸은 '-'로 채워 둔다 (답 있는 질의: '모른다' 칸, 답 없는 질의: 나머지 칸).
'채점 기준' 시트에 지표별 짧은 기준(RUBRIC)을 둔다 — 심판 지시문(judge_prompts_ko.yaml)·이진 기준(thresholds)과 뜻을 맞췄다.
결과: <답변 폴더>/human/<답변 label>_human.xlsx (또는 --csv). 이미 있으면 덮지 않는다 (채운 표를 지우지 않게).

실행 (심판 환경, openpyxl):
  & $py -I -X utf8 eval/answer/human_sheet.py --answers <work>\\answers\\gpt4omini_20261122.json
"""
import argparse
import csv
import json
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]
# (지표 키, 표 머리, 기준) — 머리는 '키 (한국어)' 꼴: agreement.py가 괄호 앞 키로 맞춘다
RUBRIC = [
    ("faithfulness", "faithfulness (근거 충실도)",
     "답의 사실 주장(값·이름·날짜 등)이 모두 '가져온 문서' 글에 있으면 P, 문서에 없거나 다른 주장이 하나라도 있으면 F. "
     "답이 '없다/모른다'뿐이면 N."),
    ("answer_relevancy", "answer_relevancy (질문 적합도)",
     "답이 질문에 직접 답하면 P(맞고 틀림은 보지 않는다). 딴소리·회피·'없다/모른다'면 F."),
    ("context_precision", "context_precision (문맥 정밀도)",
     "참고 답에 필요한 문서(근거 문서)가 가져온 문서 1~2위 안에 있으면 P, 3위 아래이거나 없으면 F. "
     "근거 문서가 여러 건이면 위쪽 순위가 대부분 필요한 문서일 때 P."),
    ("context_recall", "context_recall (문맥 재현율)",
     "참고 답의 내용이 거의 다(80% 이상) 가져온 문서 글에서 확인되면 P, 빠진 내용이 있으면 F (근거 문서 3건 중 2건만 있으면 F)."),
    ("citation_accuracy", "citation_accuracy (인용 정확도)",
     "답의 근거 표시 [철/문서]가 모두 가져온 문서이고 그 문서 글이 해당 내용을 뒷받침하면 P. 근거 표시 없이 값을 답했거나 "
     "엉뚱한 문서를 달았으면 F. 답이 '없다/모른다'뿐이면 N."),
    ("refusal", "refusal (모른다)",
     "답 없는 질의만: 답이 '자료에 없어 답할 수 없다'는 뜻이면 P, 지어낸 값을 답했으면 F."),
]
VALUES = "P(통과) / F(실패) / N(해당 없음)"
BASE_COLS = ["id", "유형", "답 있음", "질문", "답 (gpt-4o-mini)", "참고 답", "근거 문서", "가져온 문서 (순위·넘긴 글)", "답의 인용"]
CONTEXT_CHARS = 700  # 넘긴 글은 600자 + 칸 값 — 다 보이게


def applicable(metric, answerable):
    return (metric == "refusal") != bool(answerable)


def sheet_rows(answers):
    """답변 파일 → 표 줄 [dict]. 지표 칸은 비우거나 '-'(해당 없음)"""
    out = []
    for r in answers["rows"]:
        if "error" in r:
            raise SystemExit(f"답변이 없는 질의 {r['id']} — 답변을 다시 만든다")
        ctx = "\n\n".join(f"{c['rank']}위 {c['key']}\n{c['context'][:CONTEXT_CHARS]}" for c in r["retrieved"])
        row = {"id": r["id"], "유형": r["type"], "답 있음": "예" if r["answerable"] else "아니오", "질문": r["question"],
               "답 (gpt-4o-mini)": r["answer"], "참고 답": r["expected_answer"], "근거 문서": "\n".join(r["source_docs"]) or "-",
               "가져온 문서 (순위·넘긴 글)": ctx, "답의 인용": "\n".join(r.get("cited") or []) or "(없음)"}
        for key, head, _ in RUBRIC:
            row[head] = "" if applicable(key, r["answerable"]) else "-"
        row["메모"] = ""
        out.append(row)
    return out


def columns():
    return [*BASE_COLS, *(h for _, h, _ in RUBRIC), "메모"]


def write_xlsx(path, rows):
    from openpyxl import Workbook  # noqa: PLC0415
    from openpyxl.styles import Alignment, Font, PatternFill  # noqa: PLC0415
    from openpyxl.worksheet.datavalidation import DataValidation  # noqa: PLC0415
    wb = Workbook()
    ws = wb.active
    ws.title = "채점"
    cols = columns()
    ws.append(cols)
    for r in rows:
        ws.append([r[c] for c in cols])
    widths = {"질문": 40, "답 (gpt-4o-mini)": 50, "참고 답": 40, "가져온 문서 (순위·넘긴 글)": 90,
              "근거 문서": 30, "답의 인용": 30, "메모": 30}
    grey = PatternFill("solid", fgColor="DDDDDD")
    dv = DataValidation(type="list", formula1='"P,F,N"', allow_blank=True)
    ws.add_data_validation(dv)
    for j, c in enumerate(cols, 1):
        letter = ws.cell(1, j).column_letter
        ws.column_dimensions[letter].width = widths.get(c, 14)
        ws.cell(1, j).font = Font(bold=True)
        for i in range(2, len(rows) + 2):
            cell = ws.cell(i, j)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            if c in {h for _, h, _ in RUBRIC}:
                if cell.value == "-":
                    cell.fill = grey
                else:
                    dv.add(cell)
    ws.freeze_panes = "B2"
    rb = wb.create_sheet("채점 기준")
    rb.append(["지표", "기준", "값"])
    for _, head, text in RUBRIC:
        rb.append([head, text, VALUES])
    rb.append(["공통", "심판 점수는 이 표에 없다. "
               "'-' 칸은 그 질의에 해당하지 않는 지표다. 가져온 문서 글만 보고 매긴다(원본 이미지 아님).", ""])
    rb.column_dimensions["A"].width = 32
    rb.column_dimensions["B"].width = 110
    for row in rb.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    wb.save(path)


def write_csv(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns())
        w.writeheader()
        w.writerows(rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--answers", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--csv", action="store_true", help="xlsx 대신 CSV(UTF-8 BOM)")
    a = ap.parse_args(argv)
    ans = json.loads(Path(a.answers).read_text(encoding="utf-8"))
    ext = ".csv" if a.csv else ".xlsx"
    p = Path(a.out) if a.out else Path(a.answers).resolve().parent / "human" / f"{ans['label']}_human{ext}"
    if p.resolve().is_relative_to(APP_ROOT):
        raise SystemExit("채점표는 저장소 밖에 쓴다")
    if p.exists():
        raise SystemExit(f"이미 있다 — 채운 표를 덮지 않는다: {p}")
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = sheet_rows(ans)
    (write_csv if a.csv else write_xlsx)(p, rows)
    print(f"채점표 {len(rows)}줄: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
