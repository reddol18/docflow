"""시험 공통. TEST_PG_URL이 있으면 실제 Postgres 시험을 돌린다 (scripts/check.ps1이 PGlite를 띄워 넣어 준다).

app.store는 불러올 때 PG_URL로 연결 풀을 만든다 → 어떤 시험 모듈이 먼저 app을 불러오든 같은 주소를 쓰도록 여기서 먼저 맞춘다.
연결 풀은 한 번 닫으면 다시 열 수 없으므로 세션 전체에서 하나만 열고 끝에 닫는다.
"""
import os
import sys

import pytest

PG = os.environ.get("TEST_PG_URL")
if PG:
    os.environ["PG_URL"] = PG
# LLM 제공자는 openai 기본값으로 고정 — 셸의 LLM_PROVIDER·LLM_BASE_URL에 시험 결과가 흔들리지 않게 (app.llm은 불러올 때 읽는다)
os.environ["LLM_PROVIDER"] = "openai"


@pytest.fixture(autouse=True)
def llm_provider_default(monkeypatch):
    """시험마다 제공자를 openai 기본값(모델 환경변수 무시)으로. 바꾸는 시험은 그 안에서 다시 바꾼다"""
    from app import llm
    monkeypatch.setattr(llm, "PROVIDER", llm.load_provider({}))
    monkeypatch.setattr(llm, "_client", None)


@pytest.fixture(autouse=True)
def queue_isolated(monkeypatch):
    """실행 대기열(https://github.com/reddol18/docflow/issues/5): 시험마다 빈 대기열·실행 행에서 시작하고, 쪽 OCR은 가짜로
    (워커가 PaddleOCR을 부르지 않게 — 진짜 OCR을 보는 시험은 없다). 표는 DB가 열린 시험(_pg)에서만 비운다"""
    from app import ocr
    if ocr is not None:
        monkeypatch.setattr(ocr, "ocr_page", lambda path: {"lines": [path.stem], "items": []})
    store = sys.modules.get("app.store")
    if PG and store is not None and not store.pool.closed:
        with store.pool.connection() as c:
            c.execute("DELETE FROM job_queue")
            c.execute("DELETE FROM vol_run")
    runs = sys.modules.get("app.runs")
    if runs is not None:
        runs.set_queue(None)


@pytest.fixture
def ask_inline(monkeypatch):
    """DB·워커 없는 시험용 /ask: 요청 행을 메모리에 두고, 넣는 순간 워커의 ask 처리(app/worker.py Worker.ask)를 이 스레드에서
    돌린다 — 경로의 검증·호출 제한·예산 503·응답 모양을 그대로 본다. 실제 대기열·DB를 거치는 것은 test_queue_app_pg"""
    from types import SimpleNamespace

    from app import jobqueue, runs, worker
    rows = {}

    def new(req_id, user, q, yf, yt):
        rows[req_id] = {"id": req_id, "user_id": getattr(user, "id", None), "username": getattr(user, "username", None),
                        "q": q, "year_from": yf, "year_to": yt, "state": "queued", "result": None, "code": None}

    def done(req_id, result, code=None):
        if req_id in rows and rows[req_id]["state"] == "queued":
            rows[req_id].update(state="error" if code else "done", result=result, code=code, q="")

    fake_q = SimpleNamespace(ack=lambda job: True, fail=lambda job: None, cfg=jobqueue.QueueConfig.from_env({}))

    def put(jobs):
        for j in jobs:
            worker.Worker(q=fake_q, name="t").ask(jobqueue.Job(id="1", kind=j.kind, consumer="t", attempts=1, ref=j.ref))
        return ["1"] * len(jobs)
    monkeypatch.setattr(runs, "ask_new", new)
    monkeypatch.setattr(runs, "ask_get", lambda req_id: dict(rows[req_id]) if req_id in rows else None)
    monkeypatch.setattr(runs, "ask_done", done)
    monkeypatch.setattr(runs, "ask_forget", lambda req_id: rows.pop(req_id, None))
    monkeypatch.setattr(runs, "put", put)
    return rows


def drain():
    """시험용: 대기열 작업을 모두 처리한다 (워커 하나, 이 스레드). 처리한 작업 수"""
    from app import worker
    return worker.drain()


@pytest.fixture(scope="session")
def pg_store():
    if not PG:
        pytest.skip("TEST_PG_URL 없음")
    from app import store
    if os.environ.get("TEST_PG_NO_PREPARE"):  # PGlite 소켓 서버처럼 prepared statement에 약한 시험 서버용
        store.pool.kwargs["prepare_threshold"] = None
    store.qdrant = lambda: type("Q", (), {"collection_exists": lambda self, n: True,
                                          "scroll": lambda self, *a, **k: ([], None)})()
    store.init()
    yield store
    store.lock_pool.close()
    store.pool.close()
