"""2단계 검증 — 역할·교차검사·단계별 이력·추정 오류율 (https://github.com/reddol18/docflow/issues/10).
실제 Postgres(PGlite)에 돌린다. TEST_PG_URL이 없으면 건너뛴다. 표를 지우므로 운영 DB에 대고 돌리지 말 것."""
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import auth, store

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_PG_URL"), reason="TEST_PG_URL 없음")
USERS = {"r": (2, "reviewer"), "i": (3, "inspector"), "j": (4, "inspector"), "k": (5, "inspector"), "a": (1, "admin")}


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
        for t in ("inspection", "inspection_plan", "review_submitters", "audit", "events", "edits", "sessions",
                  "review_time", "users"):
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

    monkeypatch.setattr(main, "G", SimpleNamespace(get_state=lambda cfg: snap, update_state=update_state,
                                                    invoke=lambda arg, cfg: None))
    monkeypatch.setattr(main.llm, "embed", lambda vol, texts: [[0.0]] * len(texts))
    monkeypatch.setattr(main.store, "upsert_vectors", lambda docs, vecs: None)
    monkeypatch.setattr(main.flow, "doc_text", lambda vol, d: "x")
    return main


def plan(c, who="a", **body):
    return c.post("/volumes/v1/inspection", headers=h(who), json={"rate": 0.3, "seed": 5, **body})


def test_roles_cross_check_stage_and_error_rate(main):
    c = TestClient(main.app)
    assert plan(c, "r").status_code == 403  # 입력자(reviewer)는 검사 목록을 못 만든다
    assert plan(c, "i").status_code == 403  # 시드 지정은 관리자만
    assert plan(c, "i", seed=None, rate=0.01).status_code == 403  # INSPECT_RATE 아래로는 관리자만
    r = plan(c)
    assert r.status_code == 200 and r.json()["plan"]["sample"] == 3 and "seed" not in r.json()["plan"]  # ceil(10×0.3)
    assert plan(c).status_code == 409  # 철마다 한 번

    view = c.get("/volumes/v1/inspection", headers=h("i")).json()
    assert "seed" not in view["plan"] and "created_by" not in view["plan"]
    reasons = {d["doc_id"]: d["reason"] for d in view["docs"]}
    assert reasons["d01"] == reasons["d02"] == "flagged" and list(reasons.values()).count("sample") == 3
    assert next(d for d in view["docs"] if d["doc_id"] == "d01")["check_fields"] == ["title"]

    # 입력 단계: 입력자 r이 d01 제목을 고쳤고, j는 이 철의 검토를 제출했고, k는 d02를 색인 뒤 직접 고쳤다
    store.add_edits("v1", [("d01", "title", "제목", "입력 제목")], 2, "review")
    store.add_review_submitter("v1", 4)
    assert c.patch("/documents/v1/d02", headers=h("k"), json={"recipient": "읍사무소"}).status_code == 200

    assert c.post("/inspection/v1/d01", headers=h("r"), json={}).status_code == 403  # 입력자 역할 → 검사 경로 403
    assert c.post("/inspection/v1/d01", headers=h("j"), json={}).status_code == 403  # 교차검사: 검토 제출자
    assert c.post("/inspection/v1/d02", headers=h("k"), json={}).status_code == 403  # 교차검사: 직접 고친 사람
    can = {d["doc_id"]: d["can_inspect"] for d in c.get("/volumes/v1/inspection", headers=h("k")).json()["docs"]}
    assert can["d02"] is False and can["d01"] is True
    assert c.post("/inspection/v1/d01", headers=h("i"), json={"fields": {"title": "검사 제목"}}).status_code == 200
    assert c.post("/inspection/v1/d01", headers=h("a"), json={}).status_code == 409  # 이미 검사함
    assert c.patch("/documents/v1/d01", headers=h("a"), json={"title": "나중 수정"}).status_code == 409  # 검사 끝난 문서 잠금

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
    old = c.get("/report?day_from=2020-01-01&day_to=2020-01-02", headers=h("i")).json()["inspection"]
    assert old[0]["inspected"] == 0  # 리포트 기간 밖의 검사는 세지 않는다
    assert "추정 오류율" in c.get("/report.csv", headers=h("i")).text


def test_failed_fix_releases_and_still_counts(main, monkeypatch):
    """고치다 임베딩에서 실패하면 차지를 풀고, 다시 낸 제출에 차이가 없어도 앞서 저장된 수정을 고친 것으로 센다"""
    c = TestClient(main.app, raise_server_exceptions=False)
    plan(c)
    sample = next(d["doc_id"] for d in c.get("/volumes/v1/inspection", headers=h("i")).json()["docs"]
                  if d["reason"] == "sample")
    monkeypatch.setattr(main.llm, "embed", lambda vol, texts: (_ for _ in ()).throw(TimeoutError("embed")))
    assert c.post(f"/inspection/v1/{sample}", headers=h("i"), json={"fields": {"title": "새 제목"}}).status_code == 500
    assert store.inspection_row("v1", sample)["inspected_at"] is None  # 차지가 풀림
    monkeypatch.setattr(main.llm, "embed", lambda vol, texts: [[0.0]] * len(texts))
    r = c.post(f"/inspection/v1/{sample}", headers=h("i"), json={"fields": {"title": "새 제목"}})
    assert r.json()["fixed_fields"] == ["title"]


def test_plan_blocked_for_review_submitter_and_reset_on_rerun(main, tmp_path):
    c = TestClient(main.app)
    store.add_review_submitter("v1", 3)
    assert c.post("/volumes/v1/inspection", headers=h("i"), json={}).status_code == 403  # 자기가 검토한 철
    assert c.post("/volumes/v1/inspection", headers=h("j"), json={}).status_code == 200  # 다른 검사자, 시드 없이 기본 비율
    (store.pages_dir("v1")).mkdir(parents=True)
    (store.pages_dir("v1") / "p.jpg").write_bytes(b"\xff\xd8\xff")
    assert c.post("/volumes/v1/run", headers=h("a")).status_code == 200
    assert store.inspection_plan("v1") is None  # 처음부터 다시 돌리면 예전 검사 목록은 버린다


def test_field100_lists_every_document(main):
    c = TestClient(main.app)
    assert plan(c, rate=0.1, fields100=["title"], seed=1).json()["docs"] == 12
    view = c.get("/volumes/v1/inspection", headers=h("i")).json()["docs"]
    assert all("title" in d["check_fields"] for d in view)


def test_not_done_volume_rejected(main, monkeypatch):
    monkeypatch.setattr(main, "G", SimpleNamespace(get_state=lambda cfg: SimpleNamespace(
        next=("review",), interrupts=[1], values={"docs": []})))
    assert TestClient(main.app).post("/volumes/v1/inspection", headers=h("a"), json={}).status_code == 409
