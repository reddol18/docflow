"""공정·품질 리포트 (https://github.com/reddol18/docflow/issues/11).

물량은 철 / 건(문서) / 면(쪽) 단위. 날짜는 KST.
  면 올림        파일 시스템의 쪽 이미지
  면 OCR 끝      events(ocr_page end)
  건 추출 끝     events(merge end, 문서별 중복 제거)
  건 색인 끝     documents.created_at (처음 색인된 날)
  검토 표시      documents.flags가 비어 있지 않은 문서
  검토 수정      events(review submit)에 남긴 표시된 칸 수·고친 칸 수, 완료 뒤 수정은 events(edit end)
  검토 시간      review_time (검토 화면에서 잰 문서별 실제 작업 시간, 자리 비움 제외)
검사(2인 검증) 단계는 아직 없다(https://github.com/reddol18/docflow/issues/10) → 오류율은 '검토에서 고친 칸 비율'로 대신한다.
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


# 한 문서를 여러 번 나눠 본 경우(화면을 닫았다 다시 열기 등) 합쳐서 한 건으로 본다. 날짜는 마지막으로 본 날.
PER_DOC = ("SELECT vol, doc_id, mode, sum(active_ms) AS active_ms, max(ts) AS ts "
           "FROM review_time GROUP BY vol, doc_id, mode")


def _day(col):
    return f"(({col}) AT TIME ZONE 'Asia/Seoul')::date"


def _rows(sql, args=()):
    with store.pool.connection() as c:
        return c.execute(sql, args).fetchall()


def daily(day_from: dt.date, day_to: dt.date):
    """날짜별 처리량·검토·비용. 빈 날도 행을 만든다."""
    lo = dt.datetime.combine(day_from, dt.time(), KST)
    hi = dt.datetime.combine(day_to + dt.timedelta(days=1), dt.time(), KST)
    out = {}

    def put(rows, key):
        for r in rows:
            out.setdefault(r["day"], {})[key] = r["n"]

    rng = (lo, hi)
    put(_rows(f"SELECT {_day('ts')} AS day, count(*) AS n FROM events "
              "WHERE node='ocr_page' AND phase='end' AND ts >= %s AND ts < %s GROUP BY 1", rng), "pages_ocr")
    put(_rows(f"SELECT {_day('ts')} AS day, count(DISTINCT doc_id) AS n FROM events "
              "WHERE node='merge' AND phase='end' AND ts >= %s AND ts < %s GROUP BY 1", rng), "docs_extracted")
    put(_rows(f"SELECT {_day('created_at')} AS day, count(*) AS n FROM documents "
              "WHERE created_at >= %s AND created_at < %s GROUP BY 1", rng), "docs_indexed")
    put(_rows(f"SELECT {_day('created_at')} AS day, count(*) AS n FROM documents "
              "WHERE flags IS NOT NULL AND flags <> '{}'::jsonb AND created_at >= %s AND created_at < %s GROUP BY 1",
              rng), "docs_flagged")
    # 검토 제출: 표시된 문서·칸을 사람이 보고 고친 수. 완료 뒤 수정(edit)은 따로 센다(본 칸 수를 모르므로 수정률에서 뺀다)
    for key, expr in [("docs_reviewed", "info->>'docs'"), ("cells_reviewed", "info->>'cells'"),
                      ("cells_fixed", "info->>'fixed_cells'")]:
        put(_rows(f"SELECT {_day('ts')} AS day, sum(({expr})::int)::int AS n FROM events "
                  "WHERE node='review' AND phase='submit' AND ts >= %s AND ts < %s GROUP BY 1", rng), key)
    put(_rows(f"SELECT {_day('ts')} AS day, sum(jsonb_array_length(info->'fields'))::int AS n FROM events "
              "WHERE node='edit' AND phase='end' AND ts >= %s AND ts < %s GROUP BY 1", rng), "cells_edited")
    put(_rows(f"SELECT {_day('ts')} AS day, sum(usd)::float AS n FROM usage "
              "WHERE ts >= %s AND ts < %s GROUP BY 1", rng), "usd")
    times = {}
    for r in _rows(f"SELECT {_day('ts')} AS day, active_ms FROM ({PER_DOC}) t "
                   "WHERE mode='review' AND ts >= %s AND ts < %s", rng):
        times.setdefault(r["day"], []).append(r["active_ms"])

    rows = []
    d = day_from
    while d <= day_to:
        r = {k: 0 for k, _ in COLUMNS[1:]} | out.get(d, {})
        ms = times.get(d, [])
        r["day"] = d.isoformat()
        r["review_docs_timed"] = len(ms)
        r["review_sec_median"] = round(statistics.median(ms) / 1000, 1) if ms else None
        r["review_min_total"] = round(sum(ms) / 60000, 1)
        r["fix_rate"] = round(r["cells_fixed"] / r["cells_reviewed"], 3) if r["cells_reviewed"] else None
        r["usd"] = round(float(r["usd"] or 0), 4)
        rows.append(r)
        d += dt.timedelta(days=1)
    return rows


def volume_counts(summaries):
    """철별 물량과 단계별 진척. summaries = main.summary() 결과 목록."""
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


def review_time_summary():
    rows = _rows("SELECT mode, count(*) AS n, percentile_cont(0.5) WITHIN GROUP (ORDER BY active_ms) AS med, "
                 f"avg(active_ms) AS avg, sum(active_ms) AS total FROM ({PER_DOC}) t GROUP BY mode")
    return {r["mode"]: {"docs": r["n"], "median_sec": round(float(r["med"]) / 1000, 1),
                        "mean_sec": round(float(r["avg"]) / 1000, 1),
                        "total_min": round(float(r["total"]) / 60000, 1)} for r in rows}


def to_csv(rows):
    """엑셀에서 한글이 깨지지 않게 BOM을 붙인다."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([label for _, label in COLUMNS])
    for r in rows:
        w.writerow(["" if r[k] is None else r[k] for k, _ in COLUMNS])
    return "﻿" + buf.getvalue()
