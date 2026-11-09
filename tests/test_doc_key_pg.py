"""문서 키 (vol, doc_id) — 다른 철의 같은 쪽 이름이 서로 덮어쓰지 않는다 (https://github.com/reddol18/docflow/issues/50).
실제 Postgres(PGlite)·Qdrant(메모리 모드). TEST_PG_URL이 없으면 건너뛴다. 표를 지우고 documents 표를 다시 만드므로
운영 DB에 대고 돌리지 말 것. 철·쪽 이름·값은 모두 지어낸 것."""
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import auth, store

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_PG_URL"), reason="TEST_PG_URL 없음")
VA, VB, PAGE = "vol-a", "vol-b", "p-0001"
USERS = {"w": (2, "reviewer"), "v": (8, "viewer")}
TABLES = ("documents", "edits", "events", "audit", "inspection", "inspection_plan", "review_submitters", "doc_stage",
          "stage_log", "stage_gen", "exports", "review_time", "sessions", "users")


def h(name):
    return {"authorization": f"Bearer {name}", "x-docflow-csrf": "1"}


def doc(vol, did, title, recipient="가상시장"):
    return {"doc_id": did, "vol": vol, "pages": [did], "flags": {}, "hw": {}, "reviewed": True,
            "fields": {"docno": "가상 1-1", "serial": "1", "date": "1999-01-01", "recipient": recipient, "title": title}}


@pytest.fixture
def env(pg_store, monkeypatch, tmp_path):
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams

    from app import main
    q = QdrantClient(":memory:")
    q.create_collection(store.COLLECTION, vectors_config=VectorParams(size=2, distance=Distance.COSINE))
    monkeypatch.setattr(store, "qdrant", lambda: q)
    with store.pool.connection() as c:
        for t in TABLES:
            c.execute(f"DELETE FROM {t}")  # noqa: S608 — 고정된 표 이름
        for name, (uid, role) in USERS.items():
            c.execute("INSERT INTO users (id, username, role) VALUES (%s, %s, %s)", (uid, name, role))
    monkeypatch.setattr(auth, "MODES", frozenset({"local"}))
    monkeypatch.setattr(auth, "session_user",
                        lambda t: auth.User(USERS[t][0], t, t, USERS[t][1]) if t in USERS else None)
    monkeypatch.setattr(store, "DATA", tmp_path)
    for v in (VA, VB):
        (tmp_path / "pages" / v).mkdir(parents=True)
    states = {}

    def get_state(cfg):
        return SimpleNamespace(next=(), interrupts=[], values=states.get(cfg["configurable"]["thread_id"], {}))

    def update_state(cfg, v, as_node=None):
        s = states[cfg["configurable"]["thread_id"]]
        new = {d["doc_id"]: d for d in v["docs"]}
        s["docs"] = [new.get(d["doc_id"], d) for d in s["docs"]]

    monkeypatch.setattr(main, "G", SimpleNamespace(get_state=get_state, update_state=update_state))
    monkeypatch.setattr(main.llm, "embed", lambda vol, texts: [[1.0, 0.0]] * len(texts))
    monkeypatch.setattr(main.flow, "doc_text", lambda vol, d: "x")
    yield SimpleNamespace(main=main, q=q, states=states)
    with store.pool.connection() as c:
        c.execute("DELETE FROM documents")


def process(env, vol, docs):
    """철 하나를 색인까지 끝낸 것처럼: 그래프의 index 노드를 그대로 부른다 (documents 행 + Qdrant 점)"""
    env.main.flow.index({"vol": vol, "docs": docs})
    env.states[vol] = {"docs": docs, "indexed": len(docs)}


def payload(env, vol, did):
    pts = env.q.retrieve(store.COLLECTION, [store.point_id(vol, did)])
    return pts[0].payload if pts else None


def test_same_page_name_in_two_volumes(env):
    """두 철에 같은 쪽 이름 → 둘 다 목록·조회·검색(벡터·낱말)·수정 이력에 따로 남고, 한쪽 PATCH가 다른 쪽을 건드리지 않는다"""
    process(env, VA, [doc(VA, PAGE, "가상 도로 허가 갑")])
    process(env, VB, [doc(VB, PAGE, "가상 도로 허가 을")])
    c = TestClient(env.main.app)
    assert [d["fields"]["title"] for d in c.get(f"/volumes/{VA}/documents", headers=h("v")).json()] == ["가상 도로 허가 갑"]
    assert [d["fields"]["title"] for d in c.get(f"/volumes/{VB}/documents", headers=h("v")).json()] == ["가상 도로 허가 을"]
    rows = store.get_documents([(VA, PAGE), (VB, PAGE)])
    assert {k: r["fields"]["title"] for k, r in rows.items()} == {(VA, PAGE): "가상 도로 허가 갑", (VB, PAGE): "가상 도로 허가 을"}
    assert env.q.count(store.COLLECTION, exact=True).count == 2
    assert payload(env, VA, PAGE)["vol"] == VA and payload(env, VB, PAGE)["vol"] == VB
    # 검색: 벡터·낱말 둘 다 두 철의 문서를 따로 낸다
    assert {(r["vol"], r["doc_id"]) for r in store.vector_search([1.0, 0.0])} == {(VA, PAGE), (VB, PAGE)}
    assert {(r["vol"], r["doc_id"]) for r in store.keyword_search(["도로"])} == {(VA, PAGE), (VB, PAGE)}
    hits = c.get("/search", params={"q": "도로 허가"}, headers=h("v"))
    assert hits.status_code == 200
    got = {(x["vol"], x["doc_id"]): x for x in hits.json()}
    assert set(got) == {(VA, PAGE), (VB, PAGE)}
    assert got[(VA, PAGE)]["fields"]["title"] == "가상 도로 허가 갑" and got[(VB, PAGE)]["fields"]["title"] == "가상 도로 허가 을"
    assert all(x["vector_rank"] and x["keyword_rank"] for x in got.values())

    # 한쪽(A)만 고친다
    before_b = (dict(rows[(VB, PAGE)]), payload(env, VB, PAGE), env.states[VB]["docs"][0])
    r = c.patch(f"/documents/{VA}/{PAGE}", headers=h("w"), json={"title": "가상 도로 허가 갑 고침"})
    assert r.status_code == 200, r.text
    rows = store.get_documents([(VA, PAGE), (VB, PAGE)])
    assert rows[(VA, PAGE)]["fields"]["title"] == "가상 도로 허가 갑 고침"
    assert rows[(VB, PAGE)] == before_b[0]  # updated_at까지 그대로
    assert payload(env, VA, PAGE)["fields"]["title"] == "가상 도로 허가 갑 고침"
    assert payload(env, VB, PAGE) == before_b[1] and env.states[VB]["docs"][0] == before_b[2]
    # 수정 이력도 철마다
    hist_a = c.get(f"/documents/{VA}/{PAGE}/edits", headers=h("v")).json()
    assert [(e["field"], e["new"]) for e in hist_a] == [("title", "가상 도로 허가 갑 고침")]
    assert c.get(f"/documents/{VB}/{PAGE}/edits", headers=h("v")).json() == []
    # 벡터 지우기(LLM 꺼짐 경로)도 그 철 것만
    store.delete_doc_vectors(VA, [PAGE])
    assert payload(env, VA, PAGE) is None and payload(env, VB, PAGE) == before_b[1]


def _old_shape(env):
    """#50 이전(첫 판 꼴) documents 표: 기본 키 doc_id 하나 + 예전 고정 칸. 예전 키 충돌을 흉내 낸다 —
    VA를 먼저, VB를 나중에 처리해 (VA, p-0001) 행에 VB의 칸 값이 들어가 있고(ON CONFLICT가 vol을 안 바꿨다),
    Qdrant 옛 점 하나(VB 것)만 남았다. VA의 p-0002는 충돌 없는 문서 (사람 수정 이력·검사 기록이 있다)"""
    with store.pool.connection() as c:
        c.execute("DROP TABLE documents")
        c.execute("CREATE TABLE documents (doc_id text PRIMARY KEY, vol text NOT NULL, pages text[] NOT NULL, "
                  "docno text, serial text, date text, recipient text, title text, flags jsonb, "
                  "reviewed boolean DEFAULT false, updated_at timestamptz DEFAULT now())")
        c.execute("INSERT INTO documents (doc_id, vol, pages, docno, serial, date, recipient, title, flags, reviewed) VALUES "
                  "('p-0001', %s, '{p-0001}', '가상 1-1', '1', '1999-01-01', '가상시장', '가상 도로 허가 을', '{}', true), "
                  "('p-0002', %s, '{p-0002}', '가상 1-2', '2', '1998-05-06', '가상군수', '가상 하천 점용 사람 고침', '{}', true)",
                  (VA, VA))
        c.execute("INSERT INTO edits (vol, doc_id, field, old, new, user_id, source) VALUES "
                  "(%s, 'p-0002', 'title', '가상 하천 점용', '가상 하천 점용 사람 고침', 2, 'edit')", (VA,))
        c.execute("INSERT INTO inspection_plan (vol, rate, fields100, seed, flagged, unflagged, sample) "
                  "VALUES (%s, 0.5, '{}', 1, 0, 2, 1)", (VA,))
        c.execute("INSERT INTO inspection (vol, doc_id, reason) VALUES (%s, 'p-0002', 'sample')", (VA,))
        for v, d in ((VA, "p-0001"), (VA, "p-0002"), (VB, "p-0001")):
            c.execute("INSERT INTO events (vol, node, phase, doc_id) VALUES (%s, 'merge', 'end', %s)", (v, d))
    from qdrant_client.models import PointStruct
    env.q.upsert(store.COLLECTION, points=[
        PointStruct(id=store.legacy_point_id("p-0001"), vector=[1.0, 0.0],
                    payload={"doc_id": "p-0001", "vol": VB, "year": 1999, "fields": {"title": "가상 도로 허가 을"}}),
        PointStruct(id=store.legacy_point_id("p-0002"), vector=[0.0, 1.0],
                    payload={"doc_id": "p-0002", "vol": VA, "year": 1998, "fields": {"title": "가상 하천 점용 사람 고침"}})])
    a1 = doc(VA, "p-0001", "가상 도로 허가 갑")
    a2 = dict(doc(VA, "p-0002", "가상 하천 점용 사람 고침", recipient="가상군수"), edited=True)
    a2["fields"] = {**a2["fields"], "docno": "가상 1-2", "serial": "2", "date": "1998-05-06"}
    env.states[VA] = {"docs": [a1, a2], "indexed": 2}
    env.states[VB] = {"docs": [doc(VB, "p-0001", "가상 도로 허가 을")], "indexed": 1}


def _start(env):
    store.init()  # 앱 시작과 같은 길 (lifespan: store.init → startup_migrate)
    env.main.startup_migrate()


def test_old_database_migrates_in_place(env, capsys):
    """예전 판 DB·컬렉션에서 시작: 다시 처리하지 않고 키를 옮긴다. 사람 수정·검사 기록이 남고, 옛 철이 목록·검색에 그대로 나온다"""
    _old_shape(env)
    _start(env)
    log = capsys.readouterr().out
    assert "기본 키 ['doc_id'] → [vol, doc_id]" in log and "점 2개 id를 (vol, doc_id)로 옮김" in log
    with store.pool.connection() as c:
        assert store._pk_columns(c, "documents")[1] == ["vol", "doc_id"]
        assert c.execute("SELECT count(*) AS n FROM edits").fetchone()["n"] == 1
        assert c.execute("SELECT count(*) AS n FROM inspection").fetchone()["n"] == 1
    rows = store.get_documents([(VA, "p-0001"), (VA, "p-0002"), (VB, "p-0001")])
    # 섞여 있던 (VA, p-0001) 행은 VA 상태대로 바로잡고, 잃었던 (VB, p-0001) 행은 VB 상태로 다시 만든다
    assert rows[(VA, "p-0001")]["fields"]["title"] == "가상 도로 허가 갑"
    assert rows[(VB, "p-0001")]["fields"]["title"] == "가상 도로 허가 을"
    # 충돌 없던 문서는 손대지 않음 (예전 고정 칸 → fields, 사람이 고친 값 그대로)
    assert rows[(VA, "p-0002")]["fields"]["title"] == "가상 하천 점용 사람 고침" and rows[(VA, "p-0002")]["year"] == 1998
    # Qdrant: 옛 id는 없어지고 새 id에 같은 벡터·payload (다시 임베딩하지 않음)
    assert env.q.retrieve(store.COLLECTION, [store.legacy_point_id("p-0001"), store.legacy_point_id("p-0002")]) == []
    assert payload(env, VB, "p-0001")["fields"]["title"] == "가상 도로 허가 을"
    assert payload(env, VA, "p-0002")["vol"] == VA and payload(env, VA, "p-0001") is None
    assert "벡터 없는 문서 1개" in log  # 예전 키로 잃은 VA p-0001 벡터 — 낱말 검색으로만 찾힌다
    c = TestClient(env.main.app)
    assert {v["vol"] for v in c.get("/volumes", headers=h("v")).json()} == {VA, VB}
    hits = {(x["vol"], x["doc_id"]) for x in c.get("/search", params={"q": "가상 도로 하천"}, headers=h("v")).json()}
    assert hits == {(VA, "p-0001"), (VA, "p-0002"), (VB, "p-0001")}
    assert [e["new"] for e in c.get(f"/documents/{VA}/p-0002/edits", headers=h("v")).json()] == ["가상 하천 점용 사람 고침"]

    # 다시 시작해도 같다 (옮길 것·바로잡을 것 없음)
    snapshot = store.get_documents([(VA, "p-0001"), (VA, "p-0002"), (VB, "p-0001")])
    _start(env)
    log = capsys.readouterr().out
    assert "기본 키" not in log and "옮김" not in log and "바로잡음" not in log
    assert store.get_documents(list(snapshot)) == snapshot
    assert env.q.count(store.COLLECTION, exact=True).count == 2


def test_vector_migration_resumes_after_partial_run(env):
    """새 id 넣기 뒤 옛 id 지우기 전에 멈춘 경우: 다시 돌리면 옛 id만 지워 끝난다 (점이 늘거나 줄지 않는다)"""
    from qdrant_client.models import PointStruct
    pl = {"doc_id": PAGE, "vol": VA, "year": 1999, "fields": {}}
    env.q.upsert(store.COLLECTION, points=[PointStruct(id=store.legacy_point_id(PAGE), vector=[1.0, 0.0], payload=pl),
                                          PointStruct(id=store.point_id(VA, PAGE), vector=[1.0, 0.0], payload=pl)])
    assert store.migrate_vectors() == 1
    assert [str(p.id) for p in env.q.scroll(store.COLLECTION)[0]] == [store.point_id(VA, PAGE)]
    assert store.migrate_vectors() == 0


def test_vector_migration_refuses_points_without_key(env):
    """payload에 vol이 없는 점이 있으면 아무것도 옮기지 않고 멈춘다 (조용히 지우지 않는다)"""
    from qdrant_client.models import PointStruct
    env.q.upsert(store.COLLECTION, points=[
        PointStruct(id=store.legacy_point_id("p-0009"), vector=[1.0, 0.0], payload={"doc_id": "p-0009"}),
        PointStruct(id=store.legacy_point_id(PAGE), vector=[1.0, 0.0], payload={"doc_id": PAGE, "vol": VA})])
    with pytest.raises(store.VectorMigrationError, match="vol·doc_id payload가 없는 점 1개"):
        store.migrate_vectors()
    assert env.q.count(store.COLLECTION, exact=True).count == 2
    assert env.q.retrieve(store.COLLECTION, [store.legacy_point_id(PAGE)])  # 옮기지 않았다
