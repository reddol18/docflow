"""실행 대기열을 앱에 연결한 흐름 (https://github.com/reddol18/docflow/issues/5 2단계 측정 시험). 실제 Postgres(PGlite)와
Postgres 대기열(app/jobqueue_pg.py)로 돌린다 — API 경로(main) → 대기열 → 워커(app/worker.py) → 결과.
그래프는 호출만 적는 가짜(LLM·OCR 없음), 쪽 OCR도 가짜(conftest). 이름·값은 모두 지어낸 것. 표를 지우므로 운영 DB에 대지 말 것.
"""
import collections
import os
import time
from dataclasses import replace
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import auth, jobqueue, runs, store, worker
from app.jobqueue import ASK, GRAPH, OCR_PAGE, KindSpec, NewJob, QueueConfig
from app.jobqueue_pg import PgQueue

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_PG_URL"), reason="TEST_PG_URL 없음")
ADMIN = {"authorization": "Bearer a", "x-docflow-csrf": "1"}
VIEWER2 = {"authorization": "Bearer v", "x-docflow-csrf": "1"}
SESSIONS = {"a": auth.User(1, "a", "a", "admin"), "v": auth.User(2, "v", "v", "viewer")}
JPG = cv2.imencode(".jpg", np.zeros((8, 8, 3), np.uint8))[1].tobytes()
IDLE = 0.6


class FakeGraph:
    """LangGraph 대신: 받은 입력을 적고, 그 순간을 진행 이벤트로도 남긴다 (OCR 이벤트와 순서를 보려고)"""

    def __init__(self, interrupts=(), fail=0, nxt=()):
        self.calls, self.fail, self.state = [], fail, SimpleNamespace(next=nxt, interrupts=list(interrupts),
                                                                       values={"docs": []})

    def invoke(self, arg, cfg):
        vol = cfg["configurable"]["thread_id"]
        self.calls.append(arg)
        store.event(vol, "graph", "start", None)
        if self.fail:
            self.fail -= 1
            self.state.next = ("ocr",)  # 실패한 실행처럼 멈춘 곳이 남는다
            raise RuntimeError("가상 실패")
        self.state.next = ()
        self.state.values = {"docs": [], "indexed": 0}  # 색인까지 끝난 철

    def get_state(self, cfg):
        return self.state


def cfg(**kw):
    base = QueueConfig(kinds={OCR_PAGE: KindSpec(idle=IDLE, limit=kw.pop("limit", 2000)),
                              GRAPH: KindSpec(idle=IDLE, limit=200, heartbeat=0.2),
                              ASK: KindSpec(idle=IDLE, limit=100)},
                       retry_after=30, max_attempts=3, lock_timeout=2)
    return replace(base, **kw)


@pytest.fixture
def app(pg_store, monkeypatch, tmp_path):
    from app import main
    auth.audit_flush()
    with pg_store.pool.connection() as c:
        for t in ("audit", "events", "edits", "inspection_plan", "inspection", "users", "ask_request", "run_stop", "api_quota"):
            c.execute(f"DELETE FROM {t}")  # noqa: S608 — 고정된 표 이름
        c.execute("INSERT INTO users (id, username, role) VALUES (1, 'a', 'admin'), (2, 'v', 'viewer')")
    monkeypatch.setattr(auth, "MODES", frozenset({"local"}))
    monkeypatch.setattr(auth, "session_user", lambda t: SESSIONS.get(t))
    monkeypatch.setattr(auth, "_anon", collections.deque())
    monkeypatch.setattr(auth, "_anon_state", {"dropped": 0, "overflow": 0})
    monkeypatch.setattr(store, "DATA", tmp_path)
    monkeypatch.setattr(worker, "NOT_YET_WAIT", 0.01)
    q = PgQueue(pg_store.pool, cfg(), poll=0.05)
    runs.set_queue(q)
    g = FakeGraph()
    monkeypatch.setattr(main, "G", g)
    yield SimpleNamespace(main=main, q=q, g=g, c=TestClient(main.app))
    runs.set_queue(None)


def upload(a, vol, n):
    files = [("files", (f"p{i:02d}.jpg", JPG, "image/jpeg")) for i in range(1, n + 1)]
    assert a.c.post(f"/volumes/{vol}/pages", headers=ADMIN, files=files).status_code == 200


def events(vol):
    return [(e["node"], e["phase"]) for e in store.events(vol)]


def test_run_splits_into_page_jobs_then_one_graph_after_last_page(app):
    """/run → 쪽 수만큼 ocr_page 작업. 쪽 작업을 하나씩 처리하는 동안 graph는 없고, 마지막 쪽 뒤에 딱 하나 —
    진행 이벤트도 run.start → ocr_page.end × 쪽 수 → 그래프 시작 순서"""
    upload(app, "v1", 3)
    r = app.c.post("/volumes/v1/run", headers=ADMIN)
    assert r.status_code == 200
    assert app.q.stats(OCR_PAGE).length == 3 and app.q.stats(GRAPH).length == 0
    assert app.c.get("/volumes/v1", headers=ADMIN).json()["stage"] == "queued"
    w = worker.Worker(q=app.q, name="w1")
    for _ in (2, 1):
        assert w.run_once(OCR_PAGE)
        assert app.q.stats(GRAPH).length == 0, "마지막 쪽 전에 graph가 들어감"
        assert app.c.get("/volumes/v1", headers=ADMIN).json()["stage"] == "running"
    assert w.run_once(OCR_PAGE)
    assert app.q.stats(GRAPH).length == 1
    assert w.run_once(GRAPH) and not w.run_once(GRAPH)
    assert app.g.calls == [{"vol": "v1", "review_mode": "human"}]
    ev = events("v1")
    assert ev[:5] == [("run", "start"), ("ocr_page", "end"), ("ocr_page", "end"), ("ocr_page", "end"), ("graph", "start")]
    assert ev[-1] == ("run", "end")
    s = app.c.get("/volumes/v1", headers=ADMIN).json()
    assert s["stage"] == "done" and s["running"] is None and s["ocr_done"] == 3
    assert runs.row("v1")["outcome"] == "ok" and runs.row("v1")["arg"] is None
    # 다 읽은 철을 다시 돌리면 OCR 작업 없이 graph 하나
    assert app.c.post("/volumes/v1/run", headers=ADMIN).status_code == 200
    assert app.q.stats(OCR_PAGE).length == 0 and app.q.stats(GRAPH).length == 1


def test_run_over_limit_429_changes_nothing(app, monkeypatch):
    """대기열 상한을 넘는 /run → 429 + Retry-After: 30. 실행 표시·대기열 길이·검사 목록·단계가 그대로"""
    app.q.cfg = cfg(limit=4)
    runs.set_queue(app.q)
    upload(app, "full", 1)
    app.q.put([NewJob(OCR_PAGE, vol="other", page=f"x{i}", ref="1") for i in range(3)])  # 다른 철 작업 3개
    upload(app, "v2", 2)
    plan = {"rate": 0.5, "fields100": [], "seed": 1, "flagged": 0, "unflagged": 1, "sample": 1}
    store.create_inspection("v2", plan, {"p01": "sample"}, 1)
    before = app.c.get("/volumes/v2", headers=ADMIN).json()
    r = app.c.post("/volumes/v2/run", headers=ADMIN)
    assert r.status_code == 429 and r.headers["retry-after"] == "30"
    after = app.c.get("/volumes/v2", headers=ADMIN).json()
    assert (after["stage"], after["running"]) == (before["stage"], before["running"]) == ("new", None)
    assert app.q.stats(OCR_PAGE).length == 3 and runs.row("v2") is None
    assert store.inspection_plan("v2") is not None  # 넣지 못했으면 검사 목록도 그대로
    assert ("run", "start") not in events("v2")
    assert app.main.running.get("v2") is None
    rows = [(r["action"], r["result"]) for r in store.audit_log() if r["vol"] == "v2"]
    assert ("run", "rejected_429") in rows


def test_queue_busy_is_503(app, monkeypatch):
    upload(app, "v3", 1)
    monkeypatch.setattr(runs, "put", lambda jobs: (_ for _ in ()).throw(jobqueue.QueueBusy(OCR_PAGE, 30)))
    r = app.c.post("/volumes/v3/run", headers=ADMIN)
    assert r.status_code == 503 and r.headers["retry-after"] == "30" and runs.row("v3") is None


def test_run_while_running_409_and_upload_blocked(app):
    upload(app, "v4", 1)
    assert app.c.post("/volumes/v4/run", headers=ADMIN).status_code == 200
    assert app.c.post("/volumes/v4/run", headers=ADMIN).status_code == 409
    files = [("files", ("p09.jpg", JPG, "image/jpeg"))]
    assert app.c.post("/volumes/v4/pages", headers=ADMIN, files=files).status_code == 409
    worker.drain(worker.Worker(q=app.q))
    assert app.c.post("/volumes/v4/run", headers=ADMIN).status_code == 200


def test_dead_worker_page_job_reclaimed(app):
    """쪽 작업을 꺼낸 워커가 죽으면(ack 없음) 유휴 시간 뒤 다른 워커가 되찾아 끝내고 graph까지 간다"""
    upload(app, "v5", 2)
    assert app.c.post("/volumes/v5/run", headers=ADMIN).status_code == 200
    lost = app.q.take(OCR_PAGE, "dead-worker")
    assert lost is not None
    w = worker.Worker(q=app.q, name="w2")
    assert w.run_once(OCR_PAGE)  # 나머지 쪽
    assert not w.run_once(OCR_PAGE) and app.q.stats(GRAPH).length == 0  # 죽은 워커의 쪽은 아직
    time.sleep(IDLE + 0.3)
    worker.drain(w)
    assert app.g.calls and runs.row("v5")["outcome"] == "ok"
    assert not app.q.ack(lost)  # 늦게 살아난 옛 워커는 아무것도 못 한다


def test_dead_graph_worker_reclaimed_resumes_from_checkpoint(app):
    """그래프를 돌리던 워커가 죽으면 되찾은 워커가 체크포인트에서 이어 간다 (처음 입력을 다시 넣지 않음)"""
    upload(app, "v6", 1)
    assert app.c.post("/volumes/v6/run", headers=ADMIN).status_code == 200
    w = worker.Worker(q=app.q, name="w1")
    assert w.run_once(OCR_PAGE)
    job = app.q.take(GRAPH, "dead-worker")
    claim = w._claim_graph(job, int(job.ref))
    w._input(job, int(job.ref), claim)  # 시작 표시까지 하고 죽음
    app.g.state.next = ("doc",)
    time.sleep(IDLE + 0.3)
    assert w.run_once(GRAPH)
    assert app.g.calls == [None] and runs.row("v6")["outcome"] == "ok"


def test_graph_failure_retries_then_error(app):
    """그래프 실패는 시도로 센다 — 두 번 실패 뒤 세 번째에 끝나면 ok, 세 번 다 실패하면 오류로 닫고 감사·이벤트"""
    upload(app, "v7", 1)
    app.g.fail = 2
    assert app.c.post("/volumes/v7/run", headers=ADMIN).status_code == 200
    worker.drain(worker.Worker(q=app.q))
    assert runs.row("v7")["outcome"] == "ok" and len(app.g.calls) == 3
    assert app.g.calls[1:] == [None, None]  # 다시 할 때는 체크포인트에서
    app.g.fail = 9
    assert app.c.post("/volumes/v7/run", headers=ADMIN).status_code == 200
    worker.drain(worker.Worker(q=app.q))
    r = runs.row("v7")
    assert r["state"] == "ended" and r["outcome"] == "error"
    assert app.c.get("/volumes/v7", headers=ADMIN).json()["running"] is None
    assert [x["result"] for x in store.audit_log() if x["action"] == "run_done"][0] == "error"
    assert app.q.stats(GRAPH).length == 0


def test_page_job_dead_closes_run(app, monkeypatch):
    """쪽 OCR이 max_attempts번 실패하면 죽은 작업 → 실행을 오류로 닫는다 (영원히 '실행 중'으로 남지 않게)"""
    upload(app, "v8", 1)
    from app import graph as flow
    monkeypatch.setattr(flow, "ocr_one", lambda vol, p: (_ for _ in ()).throw(OSError("가상 디스크 오류")))
    assert app.c.post("/volumes/v8/run", headers=ADMIN).status_code == 200
    worker.drain(worker.Worker(q=app.q))
    assert runs.row("v8")["outcome"] == "error" and app.q.stats(OCR_PAGE).dead == 1
    assert ("run", "error") in events("v8")
    assert app.c.get("/volumes/v8", headers=ADMIN).json()["running"] is None


def test_bad_names_and_stale_jobs_dropped(app):
    """대기열 본문은 믿지 않는다: 철·쪽 이름이 규칙에 안 맞으면 경로에 쓰지 않고 버린다. 옛 실행 번호의 작업도 버린다"""
    seen = []
    from app import graph as flow
    real = flow.ocr_one
    flow.ocr_one = lambda vol, p: seen.append((vol, p))
    try:
        app.q.put([NewJob(OCR_PAGE, vol="..", page="p01", ref="1"), NewJob(OCR_PAGE, vol="v9", page="../x", ref="1"),
                   NewJob(GRAPH, vol="a/b", ref="1"), NewJob(GRAPH, vol="v9", ref="x")])
        upload(app, "v9", 1)
        assert app.c.post("/volumes/v9/run", headers=ADMIN).status_code == 200
        app.q.put([NewJob(OCR_PAGE, vol="v9", page="p01", ref="1")])  # 옛 실행
        worker.drain(worker.Worker(q=app.q))
    finally:
        flow.ocr_one = real
    assert seen == [("v9", "p01")]  # 지금 실행의 쪽 하나만
    assert app.q.stats(OCR_PAGE).length == 0 and app.q.stats(GRAPH).length == 0


def test_duplicate_graph_job_runs_once(app):
    upload(app, "va", 1)
    assert app.c.post("/volumes/va/run", headers=ADMIN).status_code == 200
    w = worker.Worker(q=app.q, name="w1")
    assert w.run_once(OCR_PAGE)
    run = runs.row("va")["run"]
    app.q.put([NewJob(GRAPH, vol="va", ref=str(run))])  # 넣기 재시도로 생긴 겹침
    first = app.q.take(GRAPH, "w1")
    assert w._claim_graph(first, run) is not None
    worker.drain(w)  # 두 번째 graph 작업은 버려진다
    assert app.g.calls == []
    w.handle(first)
    assert len(app.g.calls) == 1 and runs.row("va")["outcome"] == "ok"


def test_review_submit_goes_through_queue(app):
    """검토 제출 → graph 작업(검토 값) → 워커가 Command(resume)로 잇는다. 대기열이 가득 차면 429이고 이력도 남지 않는다"""
    intr = SimpleNamespace(value={"flagged": [{"doc_id": "d1", "pages": ["p01"], "fields": {"title": "옛"}, "flags":
                                               {"title": ["x"]}, "hw": {}}]}, id="7" * 32)
    app.g.state.interrupts = [intr]
    app.q.cfg = replace(app.q.cfg, kinds={**app.q.cfg.kinds, GRAPH: KindSpec(idle=IDLE, limit=1)})
    app.q.put([NewJob(GRAPH, vol="zz", ref="1")])
    r = app.c.post("/review/vb/json", headers=ADMIN, json={"d1": {"title": "가상 새 제목"}})
    assert r.status_code == 429 and r.headers["retry-after"] == "30"
    with store.pool.connection() as c:
        assert c.execute("SELECT count(*) AS n FROM edits WHERE vol = 'vb'").fetchone()["n"] == 0
    c_ = app.q.take(GRAPH, "x")
    app.q.ack(c_)
    r = app.c.post("/review/vb/json", headers=ADMIN, json={"d1": {"title": "가상 새 제목"}})
    assert r.status_code == 200
    assert app.c.post("/review/vb/json", headers=ADMIN, json={}).status_code == 409  # 넣은 제출이 돌기 전 두 번째
    app.g.state.interrupts = []

    def resume(arg, cfg_):
        app.g.calls.append(arg)
    app.g.invoke = resume
    worker.drain(worker.Worker(q=app.q))
    assert len(app.g.calls) == 1 and app.g.calls[0].resume == {"7" * 32: {"d1": {"title": "가상 새 제목"}}}
    assert runs.row("vb")["arg"] is None  # 검토 값은 끝나면 지운다


def test_ask_through_queue(app, monkeypatch):
    """/ask → 요청 행(질문 본문) + 대기열(id만) → 워커 → 결과 조회. 기다림 안에 못 끝나면 202 {id} → /ask/{id}.
    다른 사람은 남의 요청을 못 본다. 대기열이 가득 차면 429"""
    monkeypatch.setattr(app.main, "hybrid", lambda q, yf, yt, k, user=None: [])
    monkeypatch.setattr(app.main.llm, "answer", lambda q, hits, user_id=None: "가상 답")
    monkeypatch.setattr(app.main, "ASK_WAIT", 0)
    r = app.c.get("/ask", params={"q": "가상 질문"}, headers=ADMIN)
    assert r.status_code == 202
    req = r.json()["id"]
    with store.pool.connection() as c:
        qb = c.execute("SELECT body FROM job_queue WHERE kind = 'ask'").fetchone()["body"]
    assert qb == {"ref": req}  # 대기열에는 id만 (질문 본문은 ask_request)
    assert app.c.get(f"/ask/{req}", headers=ADMIN).status_code == 202
    worker.drain(worker.Worker(q=app.q))
    assert app.c.get(f"/ask/{req}", headers=VIEWER2).status_code == 404  # 남의 요청
    r = app.c.get(f"/ask/{req}", headers=ADMIN)
    assert r.status_code == 200 and r.json() == {"answer": "가상 답", "sources": []}
    assert app.c.get(f"/ask/{req}", headers=ADMIN).status_code == 404  # 한 번 돌려주면 지운다
    assert app.c.get("/ask/..%2F..", headers=ADMIN).status_code == 404
    # 기다림 안에 끝나면 예전처럼 바로 답 (워커가 다른 스레드에서 돈다)
    import threading
    monkeypatch.setattr(app.main, "ASK_WAIT", 10)
    stop = threading.Event()
    t = threading.Thread(target=worker.Worker(q=app.q).loop, args=(ASK, stop), daemon=True)
    t.start()
    try:
        r = app.c.get("/ask", params={"q": "가상 질문 둘"}, headers=ADMIN)
    finally:
        stop.set()
        t.join(5)
    assert r.status_code == 200 and r.json()["answer"] == "가상 답"
    # 예산이 차면 503 (워커가 남긴 코드 → API가 예전과 같은 처리기로)
    monkeypatch.setattr(app.main, "hybrid", lambda *a, **k: (_ for _ in ()).throw(app.main.llm.BudgetExceeded("가상")))
    monkeypatch.setattr(app.main, "ASK_WAIT", 0)
    req = app.c.get("/ask", params={"q": "가상 셋"}, headers=ADMIN).json()["id"]
    worker.drain(worker.Worker(q=app.q))
    r = app.c.get(f"/ask/{req}", headers=ADMIN)
    assert r.status_code == 503 and r.json()["code"] == "budget"
    with store.pool.connection() as c:  # 질문 본문은 끝나면 지운다
        assert c.execute("SELECT count(*) AS n FROM ask_request WHERE q <> ''").fetchone()["n"] == 0
    app.q.cfg = replace(app.q.cfg, kinds={**app.q.cfg.kinds, ASK: KindSpec(idle=IDLE, limit=1)})
    app.q.put([NewJob(ASK, ref="x" * 20)])
    r = app.c.get("/ask", params={"q": "가상 넷"}, headers=ADMIN)
    assert r.status_code == 429 and r.headers["retry-after"] == "30"


def test_resume_unfinished_on_worker_start(app):
    """워커 기동 때: 그래프가 중간에 멈춘 철(예전 판에서 Pod 재시작으로 끊김, 실행 행 없음)을 이어 간다.
    검토 대기·실행 중·마지막 실행 오류·한도 멈춤인 철은 건드리지 않는다. 두 워커가 함께 떠도 한 번만"""
    for v in ("s1", "s2", "s3", "s4"):
        (store.pages_dir(v)).mkdir(parents=True)
    app.g.state.next = ("doc",)
    runs.commit("s2", runs.next_run(), "start", {})
    runs.end("s2", runs.row("s2")["run"], "error")  # 마지막 실행이 오류 — 사람이 /resume
    store.set_run_stop("s3", "budget")
    runs.commit("s4", runs.next_run(), "start", {})  # 실행 중 — 대기열 작업이 잇는다
    assert worker.resume_unfinished(app.main) == ["s1"]
    assert worker.resume_unfinished(app.main) == []  # 두 번째 워커
    assert app.q.stats(GRAPH).length == 1
    worker.drain(worker.Worker(q=app.q), kinds=(GRAPH,))
    assert app.g.calls == [None] and runs.row("s1")["outcome"] in ("ok", "error")
    assert ("run", "start") in events("s1")
