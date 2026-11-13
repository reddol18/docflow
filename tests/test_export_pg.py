"""납품 형식 출력·일괄 삭제 (https://github.com/reddol18/docflow/issues/12).
실제 Postgres(PGlite)·LangGraph 체크포인트·Qdrant(메모리 모드)에 돌린다. TEST_PG_URL이 없으면 건너뛴다.
표를 지우므로 운영 DB에 대고 돌리지 말 것. 값은 모두 지어낸 것."""
import io
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app import auth, store

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_PG_URL"), reason="TEST_PG_URL 없음")
USERS = {"v": (2, "viewer"), "a": (1, "admin")}
VOL, OTHER = "1234567-1995-0001", "1234567-1995-0002"
CHECKPOINT_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")  # 확인용 — 지우기는 PostgresSaver.delete_thread


def h(name):
    return {"authorization": f"Bearer {name}", "x-docflow-csrf": "1"}


def doc(did, pages, vol=VOL, **f):
    fields = {"docno": "가나 100-1", "serial": "1", "date": "1995-03-07", "recipient": "가상시장", "title": "가상 제목"} | f
    return {"doc_id": did, "vol": vol, "pages": pages, "flags": {}, "reviewed": False, "fields": fields, "hw": {}}


@pytest.fixture
def env(pg_store, monkeypatch, tmp_path):
    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.graph import END, START, StateGraph
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams

    from app import main
    saver = PostgresSaver(store.pool)
    saver.setup()
    with store.pool.connection() as c:
        for t in store.PURGE_TABLES + CHECKPOINT_TABLES + ("exports", "usage", "audit", "sessions", "review_time", "users"):
            c.execute(f"DELETE FROM {t}")  # noqa: S608 — 고정된 표 이름
        for name, (uid, role) in USERS.items():
            c.execute("INSERT INTO users (id, username, role) VALUES (%s, %s, %s)", (uid, name, role))
    monkeypatch.setattr(auth, "MODES", frozenset({"local"}))
    monkeypatch.setattr(auth, "session_user",
                        lambda t: auth.User(USERS[t][0], t, t, USERS[t][1]) if t in USERS else None)
    monkeypatch.setattr(store, "DATA", tmp_path)

    # 두 철: 파일·문서 행·이력·이벤트·검사·체크포인트·벡터를 모두 만든다 (OTHER는 지우면 안 되는 쪽)
    q = QdrantClient(":memory:")
    q.create_collection(store.COLLECTION, vectors_config=VectorParams(size=2, distance=Distance.COSINE))
    monkeypatch.setattr(store, "qdrant", lambda: q)
    g = StateGraph(dict)
    g.add_node("n", lambda s: {"x": 1})
    g.add_edge(START, "n")
    g.add_edge("n", END)
    graph = g.compile(checkpointer=saver)
    states = {}
    for i, vol in enumerate((VOL, OTHER)):
        pages = ["p01", "p02", "p03"] if vol == VOL else ["q01", "q02", "q03"]  # doc_id(첫 쪽 이름)는 철마다 다르다
        docs = [doc(pages[2], [pages[2]], vol, title=""), doc(pages[0], pages[:2], vol)]  # 쪽 순서와 다르게 둠, 제목 빈칸 1
        states[vol] = {"pages": pages, "docs": docs, "indexed": 2}
        for kind, name in (("pages", f"{pages[0]}.jpg"), ("ocr", f"{pages[0]}.json"), ("crops", f"{pages[0]}_docno.png")):
            store.under(kind, vol, name).parent.mkdir(parents=True, exist_ok=True)
            store.under(kind, vol, name).write_bytes(b"x")
        store.save_documents(docs)
        store.add_edits(vol, [(pages[0], "title", "a", "b")], 1, "edit")
        store.event(vol, "run", "end")
        store.add_usage(vol, "embed", "m", 1, 0, 0.001)
        store.add_review_submitter(vol, 1)
        plan = {"rate": 0.5, "fields100": [], "seed": 1, "flagged": 0, "unflagged": 2, "sample": 1}
        store.create_inspection(vol, plan, {pages[0]: "sample"}, 1)
        # 검토 단계 위치·기록 (https://github.com/reddol18/docflow/issues/86) — 반려 사유가 들어 있으므로 일괄 삭제 대상
        store.ensure_stage(vol, pages[0], "entry")
        store.add_stage_log(vol, pages[0], "qc", 1, "reject", "entry", 1, reason="가상 사유")
        # 끝난 실행 행 (https://github.com/reddol18/docflow/issues/5) — 일괄 삭제 대상
        from app import runs
        runs.commit(vol, runs.next_run(), "start", {"review_mode": "human"})
        runs.end(vol, runs.row(vol)["run"], "ok")
        q.upsert(store.COLLECTION, [PointStruct(id=i * 10 + j, vector=[1.0, float(j)], payload={"vol": vol, "doc_id": d})
                                    for j, d in enumerate((pages[0], pages[2]))])
        graph.invoke({"x": 0}, {"configurable": {"thread_id": vol}})

    monkeypatch.setattr(main, "G", SimpleNamespace(checkpointer=saver, get_state=lambda cfg: SimpleNamespace(
        next=(), interrupts=[], values=states[cfg["configurable"]["thread_id"]])))
    return SimpleNamespace(main=main, q=q, states=states)


def counts(vol):
    with store.pool.connection() as c:
        out = {t: c.execute(f"SELECT count(*) AS n FROM {t} WHERE vol = %s", (vol,)).fetchone()["n"]  # noqa: S608
               for t in store.PURGE_TABLES + ("usage", "audit")}
        out |= {t: c.execute(f"SELECT count(*) AS n FROM {t} WHERE thread_id = %s", (vol,)).fetchone()["n"]  # noqa: S608
                for t in CHECKPOINT_TABLES}
    out["vectors"] = store.count_vectors(vol)
    out["files"] = sum(1 for k in ("pages", "ocr", "crops") if store.under(k, vol).exists())
    return out


def test_check_export_then_purge(env):
    c = TestClient(env.main.app)
    chk = c.get(f"/export/check?vol={VOL}", headers=h("v")).json()
    assert (chk["required_blank"], chk["format"], chk["not_reviewed"]) == (1, 0, 0)
    assert c.get(f"/export.xlsx?vol={VOL}", headers=h("v")).status_code == 403  # 엑셀 출력은 관리자만
    assert c.delete(f"/volumes/{VOL}?confirm={VOL}", headers=h("a")).status_code == 409  # 내보내기 전에는 못 지움

    r = c.get(f"/export.xlsx?vol={VOL}&vol={VOL}", headers=h("a"))  # 같은 철을 두 번 줘도 한 번만
    assert r.status_code == 200 and r.headers["content-disposition"].startswith("attachment")
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.worksheets[0].max_row == 2
    docs = [list(x) for x in wb.worksheets[1].iter_rows(values_only=True)][1:]
    assert [(x[1], x[6]) for x in docs] == [(1, "p01"), (2, "p03")]  # 쪽 순서

    assert c.delete(f"/volumes/{VOL}?confirm=다른철", headers=h("a")).status_code == 400
    assert c.delete(f"/volumes/{VOL}?confirm={VOL}", headers=h("a")).status_code == 409  # 출력에 빈칸 1건이 있었다
    store.save_documents(env.states[VOL]["docs"][:1])  # 내보낸 뒤 고침 (updated_at)
    assert "바뀐" in c.delete(f"/volumes/{VOL}?confirm={VOL}&allow_problems=true", headers=h("a")).json()["detail"]
    assert c.get(f"/export.xlsx?vol={VOL}", headers=h("a")).status_code == 200  # 다시 내보냄
    before = counts(VOL)
    assert all(before[k] > 0 for k in before)
    r = c.delete(f"/volumes/{VOL}?confirm={VOL}&allow_problems=true", headers=h("a"))
    assert r.status_code == 200, r.text
    after = counts(VOL)
    gone = {k: v for k, v in after.items() if k not in ("usage", "audit")}
    assert gone == {k: 0 for k in gone}  # 파일·문서·이력·이벤트·검사·체크포인트·벡터 0
    assert after["usage"] == before["usage"] and after["audit"] > before["audit"]  # 비용 장부·감사 로그는 남김
    other = counts(OTHER)
    assert all(v > 0 for k, v in other.items() if k != "audit"), other  # 다른 철은 그대로
    with store.pool.connection() as cn:
        rows = cn.execute("SELECT result, info FROM audit WHERE action = 'purge' AND vol = %s ORDER BY id", (VOL,)).fetchall()
    assert [r["result"] for r in rows] == ["rejected_409", "rejected_400", "rejected_409", "rejected_409", "ok"]
    assert rows[-1]["info"]["vectors"] == 2 and rows[-1]["info"]["documents"] == 2


def test_export_refuses_unfinished_volume(env, monkeypatch):
    waiting = {"pages": ["p01"], "docs": [doc("p01", ["p01"]) | {"flags": {"title": ["x"]}}]}
    monkeypatch.setattr(env.main, "G", SimpleNamespace(get_state=lambda cfg: SimpleNamespace(
        next=("review",), interrupts=[1], values=waiting)))
    c = TestClient(env.main.app)
    assert c.get(f"/export.xlsx?vol={VOL}", headers=h("a")).status_code == 409
    chk = c.get(f"/export/check?vol={VOL}", headers=h("v")).json()  # 검토 대기 철도 검증은 한다
    assert chk["not_reviewed"] == 1 and chk["volumes"][0]["stage"] == "review"


def test_check_response_counts_all_three(env, monkeypatch):
    """빈칸·형식 위반·검토 대기를 한 철에 넣고 /export/check 응답의 세 건수를 본다"""
    vals = {"pages": ["p1", "p2", "p3"], "indexed": 3,
            "docs": [doc("p1", ["p1"], title=""), doc("p2", ["p2"], date="1997-03"),
                     doc("p3", ["p3"]) | {"flags": {"title": ["확신도"]}}]}
    monkeypatch.setattr(env.main, "G", SimpleNamespace(get_state=lambda cfg: SimpleNamespace(
        next=(), interrupts=[], values=vals)))
    chk = TestClient(env.main.app).get(f"/export/check?vol={VOL}", headers=h("v")).json()
    assert (chk["required_blank"], chk["format"], chk["not_reviewed"]) == (1, 1, 1)


def test_audit_older_than_a_year_pruned(env):
    with store.pool.connection() as c:
        c.execute("INSERT INTO audit (ts, action, result) VALUES (now() - interval '366 days', 'old', 'ok'), "
                  "(now() - interval '300 days', 'kept', 'ok')")
    assert store.prune_audit() == 1
    with store.pool.connection() as c:
        assert [r["action"] for r in c.execute("SELECT action FROM audit WHERE action IN ('old','kept')").fetchall()] == ["kept"]


def test_bad_volume_name_rejected(env):
    assert TestClient(env.main.app).get("/export/check?vol=../x", headers=h("v")).status_code == 400
