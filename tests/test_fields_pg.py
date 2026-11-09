"""색인 칸 저장소: documents.fields jsonb + 검색용 열(search_text·year), 예전 고정 열 옮기기 (https://github.com/reddol18/docflow/issues/9).
실제 Postgres(PGlite)·Qdrant(메모리 모드). TEST_PG_URL이 없으면 건너뛴다. 표를 지우므로 운영 DB에 대고 돌리지 말 것. 값은 지어낸 것."""
import os

import pytest

from app import fields, store

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_PG_URL"), reason="TEST_PG_URL 없음")
VOL = "1234567-1997-0001"


def doc(did, date, title, recipient="가상시장", docno="가상 12345-1"):
    return {"doc_id": did, "vol": VOL, "pages": [did], "flags": {}, "reviewed": False,
            "fields": {"docno": docno, "serial": "1", "date": date, "recipient": recipient, "title": title}}


@pytest.fixture
def env(pg_store, monkeypatch):
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams
    q = QdrantClient(":memory:")
    q.create_collection(store.COLLECTION, vectors_config=VectorParams(size=2, distance=Distance.COSINE))
    monkeypatch.setattr(store, "qdrant", lambda: q)
    with store.pool.connection() as c:
        c.execute("DELETE FROM documents")
    yield q
    with store.pool.connection() as c:
        c.execute("DELETE FROM documents")


def test_documents_use_fields_jsonb(env):
    with store.pool.connection() as c:
        cols = {r["column_name"]: r["data_type"] for r in c.execute(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'documents'").fetchall()}
    assert cols["fields"] == "jsonb" and "search_text" in cols and cols["year"] == "integer"
    assert not set(store.LEGACY_COLUMNS) & set(cols)


def test_save_search_vectors_and_hybrid(env, monkeypatch):
    from app import main
    docs = [doc("d1", "1997-03-04", "가상 도로 점용 허가"), doc("d2", "1999-01-02", "가상 공장 등록", recipient="가상 도로공사"),
            doc("d3", "미상", "가상 도로 보수")]
    store.save_documents(docs)
    store.upsert_vectors(docs, [[1.0, 0.0], [0.9, 0.1], [0.0, 1.0]])
    rows = {d: r for (_, d), r in store.get_documents([(VOL, "d1"), (VOL, "d3")]).items()}
    assert rows["d1"]["fields"] == docs[0]["fields"] and rows["d1"]["year"] == 1997 and rows["d3"]["year"] is None
    assert rows["d1"]["search_text"] == "가상 도로 점용 허가 가상시장 가상 12345-1"
    # 낱말 검색: 제목·수신·문서번호 (스키마 search.keyword), 연도 필터는 year 열
    assert [r["doc_id"] for r in store.keyword_search(["도로", "점용"])] == ["d1", "d2", "d3"]
    assert [r["doc_id"] for r in store.keyword_search(["도로"], 1998, 2000)] == ["d2"]
    assert [r["doc_id"] for r in store.keyword_search(["12345-1"], 1997, 1997)] == ["d1"]
    pt = env.retrieve(store.COLLECTION, [store.point_id(VOL, "d1")])[0]
    assert pt.payload == {"doc_id": "d1", "vol": VOL, "year": 1997,
                          "fields": {"docno": "가상 12345-1", "date": "1997-03-04", "recipient": "가상시장",
                                     "title": "가상 도로 점용 허가"}}
    assert [p["doc_id"] for p in store.vector_search([1.0, 0.0], 1999, 1999)] == ["d2"]
    # /search 결과: doc_id·순위·vol + fields(보이는 칸 — 문서번호·시행일자·수신·제목). 칸 값은 fields 아래라
    # 스키마 칸 이름이 순위·철 키를 덮지 않는다 (https://github.com/reddol18/docflow/issues/72)
    monkeypatch.setattr(main.llm, "embed", lambda vol, texts, **k: [[1.0, 0.0]])
    hits = main.hybrid("도로 점용", k=2)
    assert hits[0] == {"doc_id": "d1", "rrf": hits[0]["rrf"], "vector_rank": 1, "keyword_rank": 1, "vol": VOL,
                       "fields": {"docno": "가상 12345-1", "date": "1997-03-04", "recipient": "가상시장",
                                  "title": "가상 도로 점용 허가"}}


def test_migrate_legacy_columns(env):
    """예전 판 표(고정 열)를 흉내 내 옮긴다: 값은 fields로, 검색 열은 스키마로, 고정 열은 지운다. 두 번 불러도 그대로"""
    with store.pool.connection() as c:
        c.execute("ALTER TABLE documents ADD COLUMN docno text, ADD COLUMN serial text, ADD COLUMN date text, "
                  "ADD COLUMN recipient text, ADD COLUMN title text")
        c.execute("INSERT INTO documents (doc_id, vol, pages, docno, serial, date, recipient, title, flags) VALUES "
                  "('m1', %s, '{m1}', '가상 1-2', '2', '1995-05-06', '가상 수신', '가상 옛 제목', '{}'), "
                  "('m2', %s, '{m2}', NULL, NULL, NULL, NULL, '가상 제목만', '{}')", (VOL, VOL))
        assert store.migrate_documents(c) == 2
        assert store.migrate_documents(c) == 0
    rows = {d: r for (_, d), r in store.get_documents([(VOL, "m1"), (VOL, "m2")]).items()}
    assert rows["m1"]["fields"] == {"docno": "가상 1-2", "serial": "2", "date": "1995-05-06", "recipient": "가상 수신",
                                    "title": "가상 옛 제목"}
    assert rows["m1"]["year"] == 1995 and rows["m1"]["search_text"] == "가상 옛 제목 가상 수신 가상 1-2"
    assert rows["m2"]["fields"]["docno"] == "" and rows["m2"]["year"] is None
    assert "docno" not in rows["m1"]
    assert [r["doc_id"] for r in store.keyword_search(["옛"], 1995, 1995)] == ["m1"]
    assert fields.search_text(fields.SCHEMA, rows["m1"]["fields"]) == rows["m1"]["search_text"]
