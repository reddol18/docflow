"""감사 로그·오류 노출·로그 가림 (https://github.com/reddol18/docflow/issues/28). 실제 Postgres(PGlite)에 돌린다.
TEST_PG_URL이 없으면 건너뛴다. 표를 지우므로 운영 DB에 대고 돌리지 말 것. 이름·키는 모두 가상의 값."""
import json
import os
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import auth, errors, store

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_PG_URL"), reason="TEST_PG_URL 없음")
ADMIN = {"authorization": "Bearer a", "x-docflow-csrf": "1"}
FAKE_KEY = "sk-proj-FAKEkey0000000000000000TEST"
FAKE_NAME = "홍길순가상"
JPG = b"\xff\xd8\xff\xe0" + b"0" * 64


@pytest.fixture
def main(pg_store, monkeypatch, tmp_path):
    from app import main
    with pg_store.pool.connection() as c:
        for t in ("audit", "events", "edits", "sessions", "review_time", "doc_stage", "stage_log", "stage_gen", "users"):
            c.execute(f"DELETE FROM {t}")  # noqa: S608 — 고정된 표 이름
        c.execute("INSERT INTO users (id, username, role) VALUES (1, 'a', 'admin')")
    monkeypatch.setattr(auth, "MODES", frozenset({"local"}))
    monkeypatch.setattr(auth, "session_user", lambda t: auth.User(1, "a", "a", "admin") if t == "a" else None)
    monkeypatch.setattr(store, "DATA", tmp_path)
    return main


def wait_idle(main, vol):
    for _ in range(100):
        if vol not in main.running:
            return
        time.sleep(0.05)
    raise AssertionError("실행이 끝나지 않음")


def fake_graph(*, interrupts=(), docs=(), invoke=None):
    snap = SimpleNamespace(next=(), interrupts=list(interrupts), values={"docs": list(docs)})
    return SimpleNamespace(get_state=lambda cfg: snap, invoke=invoke or (lambda arg, cfg: None),
                           update_state=lambda cfg, v, as_node=None: None)


def test_audit_four_actions_and_no_leak(main, monkeypatch, capsys):
    c = TestClient(main.app)
    doc = {"doc_id": "d1", "vol": "v1", "pages": ["p1"], "flags": {"title": ["확신도"]},
           "fields": {"docno": "가-1", "serial": "1", "date": "1999-01-01", "recipient": "시청", "title": "옛 제목"},
           "hw": {}, "reviewed": True}

    # 1) 업로드
    assert c.post("/volumes/v1/pages", headers=ADMIN, files=[("files", ("p1.jpg", JPG, "image/jpeg"))]).status_code == 200

    # 2) 실행 — 흐름이 키 꼴 문자열이 든 예외로 실패한다
    def boom(arg, cfg):
        raise RuntimeError(f"upstream said: Incorrect API key provided: {FAKE_KEY} at /app/app/llm.py")
    monkeypatch.setattr(main, "G", fake_graph(invoke=boom))
    assert c.post("/volumes/v1/run", headers=ADMIN).status_code == 200
    wait_idle(main, "v1")

    # 3) 검토 저장 — 고친 값에 가상 실명
    monkeypatch.setattr(main, "G", fake_graph(interrupts=[SimpleNamespace(value={"flagged": [doc]})]))
    r = c.post("/review/v1/json", headers=ADMIN, json={"d1": {"title": f"{FAKE_NAME} 민원 회신"}})
    assert r.status_code == 200, r.text
    wait_idle(main, "v1")

    # 4) 문서 수정 — 수신에 가상 실명
    monkeypatch.setattr(main, "G", fake_graph(docs=[doc]))
    monkeypatch.setattr(main.llm, "embed", lambda vol, texts: [[0.0]] * len(texts))
    monkeypatch.setattr(main.store, "upsert_vectors", lambda docs, vecs: None)
    monkeypatch.setattr(main.flow, "doc_text", lambda vol, d: "x")
    assert c.patch("/documents/v1/d1", headers=ADMIN, json={"recipient": FAKE_NAME}).status_code == 200

    rows = store.audit_log()
    assert sorted(r["action"] for r in rows) == ["edit", "review", "run", "run_done", "run_done", "upload"]
    # 실행 실패, 검토 뒤 이어 간 실행(가짜 그래프가 여전히 검토 대기를 돌려줌)
    assert {r["result"] for r in rows if r["action"] == "run_done"} == {"error", "review_waiting"}
    rows = [r for r in rows if r["action"] != "run_done"]
    for r in rows:  # 각 행에 시각·사용자·행위·대상·결과
        assert r["ts"] and r["user_id"] == 1 and r["username"] == "a" and r["vol"] == "v1" and r["result"] == "ok"
    assert next(r for r in rows if r["action"] == "edit")["doc_id"] == "d1"
    assert next(r for r in rows if r["action"] == "edit")["info"]["fields"] == ["recipient"]

    ev = c.get("/volumes/v1/events", headers=ADMIN).json()
    err = [e for e in ev if e["phase"] == "error"]
    assert err and err[0]["info"]["code"] == "internal" and "message" in err[0]["info"]
    ev_text = json.dumps(ev, ensure_ascii=False)
    for bad in ("RuntimeError", "upstream said", "/app/", FAKE_KEY, FAKE_NAME):
        assert bad not in ev_text  # 화면엔 예외 이름·메시지·경로가 없다

    log = capsys.readouterr()
    server_log = log.out + log.err
    assert "Traceback" in server_log and "RuntimeError" in server_log  # 상세는 서버 로그에
    audit_text = json.dumps(rows, ensure_ascii=False, default=str)
    for text in (server_log, audit_text, ev_text):
        assert FAKE_KEY not in text and FAKE_NAME not in text


def test_rejected_request_is_audited(main, monkeypatch):
    monkeypatch.setattr(main, "G", fake_graph())
    c = TestClient(main.app)
    assert c.post("/volumes/v2/run", headers=ADMIN).status_code == 404  # 올린 쪽이 없음
    assert c.post("/review/v2/json", headers=ADMIN, json={}).status_code == 409
    got = {(r["action"], r["result"]) for r in store.audit_log()}
    assert got == {("run", "rejected_404"), ("review", "rejected_409")}


def test_legacy_error_events_hidden(main):
    store.event("v3", "run", "error", None, error="OperationalError: connection to /var/run/x failed")
    assert store.events("v3")[0]["info"] == {"code": "internal"}  # 읽을 때도 거르고
    store.init_db()  # 시작할 때 표에서 지운다
    with store.pool.connection() as c:
        raw = c.execute("SELECT info FROM events WHERE vol = 'v3'").fetchone()["info"]
    assert raw == {"code": "internal"}


def test_audit_failure_does_not_break_request(main, monkeypatch):
    monkeypatch.setattr(main, "G", fake_graph())
    monkeypatch.setattr(store, "audit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("pool timeout")))
    c = TestClient(main.app)
    assert c.post("/volumes/v4/run", headers=ADMIN).status_code == 404  # 원래 거절이 그대로 (500이 아니라)


def test_run_outcome_audited(main, monkeypatch):
    (store.pages_dir("v5")).mkdir(parents=True)
    (store.pages_dir("v5") / "p.jpg").write_bytes(JPG)
    monkeypatch.setattr(main, "G", fake_graph(invoke=lambda arg, cfg: (_ for _ in ()).throw(KeyError("x"))))
    c = TestClient(main.app)
    assert c.post("/volumes/v5/run?review=bogus-value-xyz", headers=ADMIN).status_code == 400
    assert c.post("/volumes/v5/run", headers=ADMIN).status_code == 200
    wait_idle(main, "v5")
    rows = {(r["action"], r["result"]): r for r in store.audit_log()}
    assert rows[("run", "rejected_400")]["info"]["review"] == "invalid"  # 사용자가 넣은 문자열은 남기지 않음
    assert rows[("run_done", "error")]["info"] == {"code": "internal"} and rows[("run_done", "error")]["username"] == "a"


def test_error_codes_and_mask():
    from app.llm import BudgetExceeded
    assert errors.code_of(BudgetExceeded("x")) == "budget"
    assert errors.code_of(FileNotFoundError("x")) == "storage"
    assert errors.code_of(ConnectionRefusedError("x")) == "network"
    assert errors.code_of(KeyError("x")) == "internal"
    assert set(errors.public(KeyError("x"))) == {"code", "message"}
    s = errors.mask(f"key {FAKE_KEY}; Authorization: Bearer abcdefgh12345678; password=hunter2xyz api_key='zzz' "
                    "client_secret=cs111 access_token: at222 openai_api_key=ok333 "
                    "postgresql://docflow:pgpw444@postgres:5432/docflow jwt eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJl")
    for bad in (FAKE_KEY, "abcdefgh12345678", "hunter2xyz", "zzz", "cs111", "at222", "ok333", "pgpw444", "c2lnbmF0dXJl"):
        assert bad not in s
    assert "postgresql://docflow:***@postgres" in s and "client_secret=***" in s
    s = errors.mask("Authorization: Basic dXNlcjpwYXNzd29yZA== {'password': 'a b c'} PGPASSWORD=hunter2 QDRANT_API_KEY=qd-1")
    for bad in ("dXNlcjpwYXNzd29yZA", "a b c", "b c", "hunter2", "qd-1"):
        assert bad not in s
