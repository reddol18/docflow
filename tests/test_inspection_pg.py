"""2단계 검증 — 역할·교차검사·단계별 이력·추정 오류율 (https://github.com/reddol18/docflow/issues/10).
실제 Postgres(PGlite)에 돌린다. TEST_PG_URL이 없으면 건너뛴다. 표를 지우므로 운영 DB에 대고 돌리지 말 것."""
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import auth, store

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_PG_URL"), reason="TEST_PG_URL 없음")
USERS = {"r": (2, "reviewer"), "i": (3, "inspector"), "j": (4, "inspector"), "a": (1, "admin")}


def h(name):
    return {"authorization": f"Bearer {name}", "x-docflow-csrf": "1"}


def doc(did, flagged=False, title="제목"):
    return {"doc_id": did, "vol": "v1", "pages": [did], "flags": {"title": ["확신도"]} if flagged else {},
            "fields": {"docno": "가-1", "serial": "1", "date": "1999-01-01", "recipient": "시청", "title": title},
            "hw": {}, "reviewed": True}


@pytest.fixture
def main(pg_store, monkeypatch, tmp_path):
    from app import main
    with pg_store.pool.connection() as c:
        for t in ("inspection", "inspection_plan", "audit", "events", "edits", "sessions", "review_time", "users"):
            c.execute(f"DELETE FROM {t}")  # noqa: S608 — 고정된 표 이름
        for name, (uid, role) in USERS.items():
            c.execute("INSERT INTO users (id, username, role) VALUES (%s, %s, %s)", (uid, name, role))
    monkeypatch.setattr(auth, "MODES", frozenset({"local"}))
    monkeypatch.setattr(auth, "session_user",
                        lambda t: auth.User(USERS[t][0], t, t, USERS[t][1]) if t in USERS else None)
    monkeypatch.setattr(store, "DATA", tmp_path)
    docs = [doc("d01", True), doc("d02", True)] + [doc(f"d{i:02d}") for i in range(3, 13)]  # 표시 2, 표시 없음 10
    state = {"docs": docs, "indexed": len(docs)}
    snap = SimpleNamespace(next=(), interrupts=[], values=state)

    def update_state(cfg, v, as_node=None):
        new = {d["doc_id"]: d for d in v["docs"]}
        state["docs"] = [new.get(d["doc_id"], d) for d in state["docs"]]

    monkeypatch.setattr(main, "G", SimpleNamespace(get_state=lambda cfg: snap, update_state=update_state))
    monkeypatch.setattr(main.llm, "embed", lambda vol, texts: [[0.0]] * len(texts))
    monkeypatch.setattr(main.store, "upsert_vectors", lambda docs, vecs: None)
    monkeypatch.setattr(main.flow, "doc_text", lambda vol, d: "x")
    return main


def test_roles_cross_check_stage_and_error_rate(main):
    c = TestClient(main.app)
    # 입력자(reviewer)는 검사 목록을 못 만든다 / 검사자는 만든다
    assert c.post("/volumes/v1/inspection", headers=h("r"), json={"rate": 0.3, "seed": 5}).status_code == 403
    r = c.post("/volumes/v1/inspection", headers=h("i"), json={"rate": 0.3, "seed": 5})
    assert r.status_code == 200 and r.json()["plan"]["sample"] == 3  # ceil(10×0.3)
    assert c.post("/volumes/v1/inspection", headers=h("i"), json={"rate": 0.3}).status_code == 409  # 한 번만

    view = c.get("/volumes/v1/inspection", headers=h("i")).json()
    reasons = {d["doc_id"]: d["reason"] for d in view["docs"]}
    assert reasons["d01"] == reasons["d02"] == "flagged" and list(reasons.values()).count("sample") == 3
    assert next(d for d in view["docs"] if d["doc_id"] == "d01")["check_fields"] == ["title"]

    # 입력 단계: 입력자 r이 d01 제목을 고쳤고, j는 이 철의 검토를 제출했다
    store.add_edits("v1", [("d01", "title", "제목", "입력 제목")], 2, "review")
    store.audit(auth.User(4, "j", "j", "inspector"), "review", "v1", None, "ok")

    assert c.post("/inspection/v1/d01", headers=h("r"), json={}).status_code == 403  # 입력자 역할 → 검사 경로 403
    assert c.post("/inspection/v1/d01", headers=h("j"), json={}).status_code == 403  # 교차검사: 검토 제출자
    assert c.post("/inspection/v1/d01", headers=h("i"), json={"fields": {"title": "검사 제목"}}).status_code == 200
    assert c.post("/inspection/v1/d01", headers=h("a"), json={}).status_code == 409  # 이미 검사함

    hist = c.get("/documents/v1/d01/edits", headers=h("i")).json()
    assert [(e["stage"], e["username"], e["field"]) for e in hist] == [("entry", "r", "title"), ("inspect", "i", "title")]

    # 표본 3건 중 1건을 고치고 2건은 통과 → 추정 오류율 1/3
    sample = [d for d, rsn in reasons.items() if rsn == "sample"]
    assert c.post(f"/inspection/v1/{sample[0]}", headers=h("i"), json={"fields": {"recipient": "구청"}}).json() == \
        {"doc_id": sample[0], "fixed_fields": ["recipient"]}
    for d in sample[1:]:
        assert c.post(f"/inspection/v1/{d}", headers=h("i"), json={}).json()["fixed_fields"] == []
    stats = c.get("/report", headers=h("i")).json()["inspection"]
    assert [(s["vol"], s["sample"], s["inspected"], s["fixed"], s["error_rate"]) for s in stats] == [("v1", 3, 3, 1, 0.3333)]
    assert "추정 오류율" in c.get("/report.csv", headers=h("i")).text


def test_field100_lists_every_document(main):
    c = TestClient(main.app)
    r = c.post("/volumes/v1/inspection", headers=h("i"), json={"rate": 0.1, "fields100": ["title"], "seed": 1})
    assert r.json()["docs"] == 12
    view = c.get("/volumes/v1/inspection", headers=h("i")).json()["docs"]
    assert all("title" in d["check_fields"] for d in view)


def test_not_done_volume_rejected(main, monkeypatch):
    monkeypatch.setattr(main, "G", SimpleNamespace(get_state=lambda cfg: SimpleNamespace(
        next=("review",), interrupts=[1], values={"docs": []})))
    assert TestClient(main.app).post("/volumes/v1/inspection", headers=h("i"), json={}).status_code == 409
