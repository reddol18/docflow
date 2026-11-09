"""칸 검증 규칙 — 문서 수정이 다른 문서 표시를 다시 쓸 때의 동시 수정 (https://github.com/reddol18/docflow/issues/87).
실제 Postgres(PGlite)에 돌린다. TEST_PG_URL이 없으면 건너뛴다. 표를 지우므로 운영 DB에 대고 돌리지 말 것. 값은 모두 지어낸 것.

김해 칸 + 제목 칸에 '철 안 중복 금지'를 단 스키마로: A가 d01 제목을 d02와 같게 고쳐 d02에 중복 표시가 붙는 동안 B가 d02를 고친다.
B의 값이 되돌아가지 않고(다른 문서는 표시만 쓴다), 늦게 끝난 저장이 낡은 표시를 쓰지 않아야 한다(쓰기는 철 잠금 안)."""
import os
import threading
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from app import auth, fields, store

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_PG_URL"), reason="TEST_PG_URL 없음")
USERS = {"w": (2, "reviewer"), "w2": (6, "reviewer")}
TABLES = ("inspection", "inspection_plan", "review_submitters", "audit", "events", "edits", "sessions", "review_time",
          "doc_stage", "stage_log", "stage_gen", "exports", "documents", "users")
DUP = "기계 검수 · 제목 · 철 안 중복"


def h(name):
    return {"authorization": f"Bearer {name}", "x-docflow-csrf": "1"}


def doc(did, title):
    return {"doc_id": did, "vol": "v1", "pages": [did], "flags": {},
            "fields": {"docno": "가상-1", "serial": "1", "date": "1999-01-01", "recipient": "가상시청", "title": title},
            "hw": {}, "reviewed": True}


@pytest.fixture
def main(pg_store, monkeypatch, tmp_path):
    from app import main
    raw = yaml.safe_load(fields.DEFAULT.read_text(encoding="utf-8"))
    next(f for f in raw["fields"] if f["name"] == "title")["validate"] = {"unique": "volume"}
    schema = fields.parse(raw, fields.load(fields.BASE))
    assert next(f for f in schema["fields"] if f["name"] == "title")["label"] == "제목"
    monkeypatch.setattr(fields, "SCHEMA", schema)  # main·store가 부를 때 읽는 스키마
    with pg_store.pool.connection() as c:
        for t in TABLES:
            c.execute(f"DELETE FROM {t}")  # noqa: S608 — 고정된 표 이름
        for name, (uid, role) in USERS.items():
            c.execute("INSERT INTO users (id, username, role) VALUES (%s, %s, %s)", (uid, name, role))
    monkeypatch.setattr(auth, "MODES", frozenset({"local"}))
    monkeypatch.setattr(auth, "session_user",
                        lambda t: auth.User(USERS[t][0], t, t, USERS[t][1]) if t in USERS else None)
    monkeypatch.setattr(store, "DATA", tmp_path)
    docs = [doc("d01", "가상 제목 1"), doc("d02", "가상 제목 2"), doc("d03", "가상 제목 3")]
    store.save_documents(docs)
    state = {"docs": docs, "indexed": len(docs)}
    snap = SimpleNamespace(next=(), interrupts=[], values=state)

    def update_state(cfg, v, as_node=None):
        new = {d["doc_id"]: d for d in v["docs"]}
        state["docs"] = [new.get(d["doc_id"], d) for d in state["docs"]]

    monkeypatch.setattr(main, "G", SimpleNamespace(get_state=lambda cfg: snap, update_state=update_state,
                                                    invoke=lambda arg, cfg: None))
    monkeypatch.setattr(main.llm, "embed", lambda vol, texts, **k: [[0.0]] * len(texts))
    monkeypatch.setattr(main.store, "upsert_vectors", lambda docs, vecs: None)
    monkeypatch.setattr(main.flow, "doc_text", lambda vol, d: "x")
    main._state = state
    return main


def rows():
    with store.pool.connection() as c:
        return {r["doc_id"]: r for r in c.execute("SELECT doc_id, fields, flags, reviewed FROM documents WHERE vol = 'v1'")}


def test_patch_rewrites_other_docs_flags_only(main):
    c = TestClient(main.app)
    r = c.patch("/documents/v1/d01", headers=h("w"), json={"title": "가상 제목 2"})
    assert r.status_code == 200 and r.json()["reflagged"] == ["d02"] and r.json()["flags"] == {"title": [DUP]}
    db = rows()
    assert db["d01"]["flags"] == {"title": [DUP]} and db["d02"]["flags"] == {"title": [DUP]} and db["d03"]["flags"] == {}
    assert db["d02"]["fields"]["title"] == "가상 제목 2" and db["d02"]["reviewed"] is True
    # 중복을 풀면 두 문서 모두 풀린다
    r = c.patch("/documents/v1/d01", headers=h("w"), json={"title": "가상 제목 1"})
    assert r.json()["reflagged"] == ["d02"]
    assert {k: v["flags"] for k, v in rows().items()} == {"d01": {}, "d02": {}, "d03": {}}
    # 표시가 안 바뀌면 다른 문서는 쓰지 않는다
    assert c.patch("/documents/v1/d03", headers=h("w"), json={"title": "가상 제목 9"}).json()["reflagged"] == []


def test_concurrent_edit_not_reverted_and_no_stale_flags(main, monkeypatch):
    """A(d01 → d02와 같은 제목)가 저장하는 중에 B가 d02 제목을 고친다. A의 저장 지점에서 B를 시작하고 잠깐 멈춘다 —
    쓰기가 철 잠금 밖이었으면 B가 그 사이 끝나고 A가 낡은 d02(값·표시)로 덮었다"""
    c = TestClient(main.app)
    real_docs, real_edit = store.save_documents, getattr(store, "save_edit", None)
    b_result, started = {}, []

    def run_b():
        b_result["res"] = c.patch("/documents/v1/d02", headers=h("w2"), json={"title": "가상 제목 B"})

    b = threading.Thread(target=run_b)

    def interleave(first_id):
        # A가 d01을 저장하는 순간 B를 시작하고 멈춘다. 저장 방식(통째·표시만·한 트랜잭션)과 상관없이 첫 문서로 고른다 —
        # 쓰기가 철 잠금 밖이던 반영 전 코드(save_documents([d01, d02]))에서도 같은 지점에서 끼어들어 lost update로 실패한다
        if first_id == "d01" and not started:
            started.append(1)
            b.start()
            # B가 끼어들 틈: B가 끝나거나(잠금 밖 저장이면 여기서 끝난다) 막힐 때까지(철 잠금 안 저장이면 A를 기다린다).
            # 다만 PGlite(연결 4개)에서는 B가 잠금 연결을 못 얻어 A 뒤로 밀릴 수도 있다 — 그래서 아래 '쓰기 때 철 잠금을 쥐었나'를
            # 따로 확인한다 (시간과 상관없이 반영 전 코드에서 실패)
            b.join(timeout=3)

    vol_stripe = store._STRIPES[store._lock_key("docflow-vol/v1") % len(store._STRIPES)]
    held = []  # A·B의 documents 쓰기마다 그 스레드가 철 잠금(프로세스 안 RLock)을 쥐고 있었나

    def slow_docs(docs, schema=None):
        held.append(vol_stripe._is_owned())
        if docs:
            interleave(docs[0]["doc_id"])
        return real_docs(docs, schema)

    def slow_edit(vol, d, items, schema=None):
        held.append(vol_stripe._is_owned())
        interleave(d["doc_id"])
        return real_edit(vol, d, items, schema)

    monkeypatch.setattr(store, "save_documents", slow_docs)
    if real_edit is not None:
        monkeypatch.setattr(store, "save_edit", slow_edit)
    ra = c.patch("/documents/v1/d01", headers=h("w"), json={"title": "가상 제목 2"})
    assert started, "B 수정이 시작되지 않음 — A의 d01 저장 지점을 못 찾았다 (시험이 아무것도 재지 않음)"
    b.join(timeout=30)
    assert not b.is_alive(), "B 수정이 30초 안에 끝나지 않음 (잠금 교착?)"
    assert ra.status_code == 200 and b_result["res"].status_code == 200
    # Postgres 쓰기는 모두 철 잠금 안에서 (그래프 상태 읽기·쓰기와 같은 구간)
    assert held and all(held), f"철 잠금 밖에서 documents를 썼다: {held}"
    db = rows()
    # B의 값이 남는다 (A가 d02 칸 값을 낡은 값으로 덮지 않음)
    assert db["d02"]["fields"]["title"] == "가상 제목 B" and db["d01"]["fields"]["title"] == "가상 제목 2"
    # 늦게 끝난 쪽이 낡은 표시를 쓰지 않는다: 지금 값으로는 중복이 없다
    assert {k: v["flags"] for k, v in db.items()} == {"d01": {}, "d02": {}, "d03": {}}
    # 그래프 상태도 같다
    st = {d["doc_id"]: d for d in main._state["docs"]}
    assert st["d02"]["fields"]["title"] == "가상 제목 B" and all(d["flags"] == {} for d in st.values())
