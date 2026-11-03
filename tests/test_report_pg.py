"""공정 리포트 SQL (app/report.py)을 실제 Postgres에 돌린다. TEST_PG_URL이 없으면 건너뛴다.

CI는 postgres 서비스 컨테이너로 돌린다(.github/workflows/ci.yml). 로컬에서는 아무 빈 Postgres나:
  $env:TEST_PG_URL = "postgresql://postgres@127.0.0.1:5432/postgres"; pytest tests/test_report_pg.py
표를 지우고 다시 채우므로 운영 DB에 대고 돌리지 말 것.
"""
import datetime as dt
import json
import os

import pytest

PG = os.environ.get("TEST_PG_URL")
pytestmark = pytest.mark.skipif(not PG, reason="TEST_PG_URL 없음")


@pytest.fixture(scope="module")
def mods():
    os.environ["PG_URL"] = PG
    from app import report, store
    if os.environ.get("TEST_PG_NO_PREPARE"):  # PGlite 소켓 서버처럼 prepared statement에 약한 시험 서버용
        store.pool.kwargs["prepare_threshold"] = None
    store.qdrant = lambda: type("Q", (), {"collection_exists": lambda self, n: True})()
    store.init()
    yield report, store
    store.pool.close()


def test_daily_report(mods):
    report, store = mods
    kst = store.KST
    d1 = dt.datetime(2026, 11, 2, 10, 0, tzinfo=kst)
    d2 = dt.datetime(2026, 11, 3, 0, 30, tzinfo=kst)  # UTC로는 2일 15:30 → KST 3일로 세야 한다
    with store.pool.connection() as c:
        for t in ["events", "documents", "usage", "review_time"]:
            c.execute(f"DELETE FROM {t}")
        ev = [(d1, "ocr_page", "end", None, None), (d1, "ocr_page", "end", None, None), (d2, "ocr_page", "end", None, None),
              (d1, "merge", "end", "v1-0001", None), (d1, "merge", "end", "v1-0001", None),  # 같은 문서 두 번 → 1건
              (d1, "merge", "end", "v1-0005", None),
              (d2, "review", "submit", None, {"docs": 2, "cells": 3, "fixed_docs": 1, "fixed_cells": 2}),
              (d2, "edit", "end", "v1-0001", {"fields": ["title"]})]
        for ts, node, phase, doc, info in ev:
            c.execute("INSERT INTO events (ts, vol, node, phase, doc_id, info) VALUES (%s,'v1',%s,%s,%s,%s)",
                      (ts, node, phase, doc, json.dumps(info) if info else None))
        c.execute("INSERT INTO documents (doc_id, vol, pages, flags, created_at) VALUES "
                  "('v1-0001','v1','{v1-0001}','{\"date\":[\"x\"]}',%s), ('v1-0005','v1','{v1-0005}','{}',%s)", (d1, d1))
        c.execute("INSERT INTO usage (ts, vol, kind, model, tin, tout, usd) VALUES (%s,'v1','extract','m',1,1,0.0123)", (d1,))
    store.add_review_times("v1", [
        {"doc_id": "v1-0001", "mode": "review", "active_ms": 20000, "opened_at": d2, "fields_changed": 1},
        {"doc_id": "v1-0001", "mode": "review", "active_ms": 10000, "opened_at": d2, "fields_changed": 0},  # 나눠 봄 → 30초
        {"doc_id": "v1-0005", "mode": "review", "active_ms": 50000, "opened_at": d2, "fields_changed": 0},
    ])
    with store.pool.connection() as c:
        c.execute("UPDATE review_time SET ts = %s", (d2,))

    r1, r2, r3 = report.daily(dt.date(2026, 11, 1), dt.date(2026, 11, 3))
    assert r1["pages_ocr"] == 0 and r1["fix_rate"] is None
    assert (r2["pages_ocr"], r3["pages_ocr"]) == (2, 1)
    assert (r2["docs_extracted"], r2["docs_indexed"], r2["docs_flagged"], r2["flag_rate"]) == (2, 2, 1, 0.5)
    assert (r3["docs_reviewed"], r3["cells_reviewed"], r3["cells_fixed"], r3["fix_rate"]) == (2, 3, 2, 0.667)
    assert r3["cells_edited"] == 1
    assert (r3["review_docs_timed"], r3["review_sec_median"], r3["review_min_total"]) == (2, 40.0, 1.3)
    assert r2["usd"] == 0.0123
    assert report.review_time_summary(dt.date(2026, 11, 1), dt.date(2026, 11, 3))["review"]["docs"] == 2
    assert report.review_time_summary(dt.date(2026, 11, 1), dt.date(2026, 11, 2)) == {}  # 기간 밖은 안 셈

    csv = report.to_csv([r1, r2, r3])
    assert csv.startswith("﻿날짜,OCR 면")
    assert "2026-11-03,1,0,0,0,,2,3,2,0.667,1,2,40.0,1.3,0.0" in csv
