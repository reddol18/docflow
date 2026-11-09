"""공정·품질 리포트 (https://github.com/reddol18/docflow/issues/11).

물량은 철 / 건(문서) / 면(쪽) 단위. 날짜는 KST.
  면 올림        파일 시스템의 쪽 이미지
  면 OCR 끝      events(ocr_page end)
  건 추출 끝     events(merge end, 문서별 중복 제거)
  건 색인 끝     documents.created_at (처음 색인된 날)
  검토 표시      documents.flags가 비어 있지 않은 문서 (색인 때 저장되므로 색인된 문서 기준)
  검토 수정      events(review submit)에 남긴 표시된 칸 수·고친 칸 수, 완료 뒤 수정은 events(edit end)
  검토 시간      review_time (검토 화면에서 확정할 때 보낸 문서별 실제 작업 시간, 자리 비움 제외)
검사(2인 검증) 단계는 아직 없다(https://github.com/reddol18/docflow/issues/10) → 오류율은 '검토에서 고친 칸 비율'로 대신한다.

SQL의 f-string에 끼우는 것은 이 모듈의 상수(열 이름, PER_DOC)뿐이고, 값은 모두 %s 매개변수다 (각 줄의 noqa: S608).
"""
import csv
import datetime as dt
import io
import statistics

from app import store

KST = store.KST
COLUMNS = [
    ("day", "날짜"),
    ("pages_ocr", "OCR 면"),
    ("docs_extracted", "추출 건"),
    ("docs_indexed", "색인 건"),
    ("docs_flagged", "검토 표시 건"),
    ("flag_rate", "검토 표시 비율"),
    ("docs_reviewed", "검토 건"),
    ("cells_reviewed", "검토 칸(표시된 칸)"),
    ("cells_fixed", "검토에서 고친 칸"),
    ("fix_rate", "검토 수정률"),
    ("cells_edited", "완료 뒤 고친 칸"),
    ("review_docs_timed", "시간 잰 건"),
    ("review_sec_median", "건당 검토 초(중앙값)"),
    ("review_min_total", "검토 분(합계)"),
    ("usd", "GPT 비용 USD"),
]

# 기간 안에서 한 문서를 여러 번 나눠 확정한 경우 합쳐서 한 건으로 본다. 날짜는 마지막으로 확정한 날.
PER_DOC = ("SELECT vol, doc_id, mode, sum(active_ms) AS active_ms, max(ts) AS ts "
           "FROM review_time WHERE ts >= %s AND ts < %s GROUP BY vol, doc_id, mode")


def _day(col):
    return f"(({col}) AT TIME ZONE 'Asia/Seoul')::date"


def _rows(sql, args=()):
    with store.pool.connection() as c:
        return c.execute(sql, args).fetchall()


def _bounds(day_from: dt.date, day_to: dt.date):
    return (dt.datetime.combine(day_from, dt.time(), KST),
            dt.datetime.combine(day_to + dt.timedelta(days=1), dt.time(), KST))


def daily(day_from: dt.date, day_to: dt.date):
    """날짜별 처리량·검토·비용. 빈 날도 행을 만든다."""
    rng = _bounds(day_from, day_to)
    out = {}

    def put(rows, key):
        for r in rows:
            out.setdefault(r["day"], {})[key] = r["n"]

    put(_rows(f"SELECT {_day('ts')} AS day, count(*) AS n FROM events "  # noqa: S608
              "WHERE node='ocr_page' AND phase='end' AND ts >= %s AND ts < %s GROUP BY 1", rng), "pages_ocr")
    put(_rows(f"SELECT {_day('ts')} AS day, count(DISTINCT doc_id) AS n FROM events "  # noqa: S608
              "WHERE node='merge' AND phase='end' AND ts >= %s AND ts < %s GROUP BY 1", rng), "docs_extracted")
    put(_rows(f"SELECT {_day('created_at')} AS day, count(*) AS n FROM documents "  # noqa: S608
              "WHERE created_at >= %s AND created_at < %s GROUP BY 1", rng), "docs_indexed")
    put(_rows(f"SELECT {_day('created_at')} AS day, count(*) AS n FROM documents "  # noqa: S608
              "WHERE flags IS NOT NULL AND flags <> '{}'::jsonb AND created_at >= %s AND created_at < %s GROUP BY 1",
              rng), "docs_flagged")
    # 검토 제출: 표시된 문서·칸을 사람이 보고 고친 수. 완료 뒤 수정(edit)은 따로 센다(본 칸 수를 모르므로 수정률에서 뺀다)
    for key, expr in [("docs_reviewed", "info->>'docs'"), ("cells_reviewed", "info->>'cells'"),
                      ("cells_fixed", "info->>'fixed_cells'")]:
        put(_rows(f"SELECT {_day('ts')} AS day, sum(({expr})::int)::int AS n FROM events "  # noqa: S608
                  "WHERE node='review' AND phase='submit' AND ts >= %s AND ts < %s GROUP BY 1", rng), key)
    put(_rows(f"SELECT {_day('ts')} AS day, sum(jsonb_array_length(info->'fields'))::int AS n FROM events "  # noqa: S608
              # 설정 흐름의 검수 단계에서 승인하며 고친 칸(node 'stage:<단계>')도 수정으로 센다 (https://github.com/reddol18/docflow/issues/86)
              "WHERE (node='edit' OR node LIKE 'stage:%%') AND phase='end' AND ts >= %s AND ts < %s GROUP BY 1", rng),
        "cells_edited")
    put(_rows(f"SELECT {_day('ts')} AS day, sum(usd)::float AS n FROM usage "  # noqa: S608
              "WHERE ts >= %s AND ts < %s GROUP BY 1", rng), "usd")
    times = {}
    for r in _rows(f"SELECT {_day('ts')} AS day, active_ms FROM ({PER_DOC}) t WHERE mode='review'", rng):  # noqa: S608
        times.setdefault(r["day"], []).append(r["active_ms"])

    rows = []
    d = day_from
    while d <= day_to:
        r = {k: 0 for k, _ in COLUMNS[1:]} | out.get(d, {})
        ms = times.get(d, [])
        r["day"] = d.isoformat()
        r["flag_rate"] = round(r["docs_flagged"] / r["docs_indexed"], 3) if r["docs_indexed"] else None
        r["review_docs_timed"] = len(ms)
        r["review_sec_median"] = round(statistics.median(ms) / 1000, 1) if ms else None
        r["review_min_total"] = round(sum(ms) / 60000, 1)
        r["fix_rate"] = round(r["cells_fixed"] / r["cells_reviewed"], 3) if r["cells_reviewed"] else None
        r["usd"] = round(float(r["usd"] or 0), 4)
        rows.append(r)
        d += dt.timedelta(days=1)
    return rows


def volume_counts(summaries):
    """철별 물량과 단계별 진척 (지금 시점). summaries = main.summary() 결과 목록.
    남은 건은 추출이 끝난 문서 중 색인 안 된 것이다. 아직 OCR·추출 전인 철의 건 수는 모르므로 남은 면으로 본다."""
    vols = []
    for s in summaries:
        vols.append({
            "vol": s["vol"], "stage": s["stage"],
            "pages": s["uploaded"], "pages_ocr": s["ocr_done"],
            "docs": s["documents"], "docs_flagged": s["flagged_documents"],
            "docs_indexed": s["indexed"] or 0,
        })
    tot = {k: sum(v[k] for v in vols) for k in ["pages", "pages_ocr", "docs", "docs_flagged", "docs_indexed"]}
    tot["volumes"] = len(vols)
    tot["volumes_done"] = sum(v["stage"] == "done" for v in vols)
    tot["pages_left"] = tot["pages"] - tot["pages_ocr"]
    tot["docs_left"] = tot["docs"] - tot["docs_indexed"]
    return {"total": tot, "volumes": vols}


def review_time_summary(day_from: dt.date, day_to: dt.date):
    rows = _rows("SELECT mode, count(*) AS n, percentile_cont(0.5) WITHIN GROUP (ORDER BY active_ms) AS med, "  # noqa: S608
                 f"avg(active_ms) AS avg, sum(active_ms) AS total FROM ({PER_DOC}) t GROUP BY mode",
                 _bounds(day_from, day_to))
    return {r["mode"]: {"docs": r["n"], "median_sec": round(float(r["med"]) / 1000, 1),
                        "mean_sec": round(float(r["avg"]) / 1000, 1),
                        "total_min": round(float(r["total"]) / 60000, 1)} for r in rows}


INSPECTION_COLUMNS = [("vol", "철"), ("rate", "표본 비율"), ("sample", "표본 수"), ("inspected", "검사한 표본"),
                      ("fixed", "검사에서 고친 표본"), ("error_rate", "추정 오류율")]


def inspection_csv(stats):
    """철별 표본 검사 결과 (https://github.com/reddol18/docflow/issues/10) — 날짜별 표 아래에 빈 줄을 두고 붙인다"""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([])
    w.writerow([label for _, label in INSPECTION_COLUMNS])
    for r in stats:
        w.writerow(["" if r[k] is None else r[k] for k, _ in INSPECTION_COLUMNS])
    return buf.getvalue()


def to_csv(rows):
    """엑셀에서 한글이 깨지지 않게 BOM을 붙인다."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([label for _, label in COLUMNS])
    for r in rows:
        w.writerow(["" if r[k] is None else r[k] for k, _ in COLUMNS])
    return "﻿" + buf.getvalue()
