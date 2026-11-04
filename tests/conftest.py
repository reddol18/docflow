"""시험 공통. TEST_PG_URL이 있으면 실제 Postgres 시험을 돌린다 (scripts/check.ps1이 PGlite를 띄워 넣어 준다).

app.store는 불러올 때 PG_URL로 연결 풀을 만든다 → 어떤 시험 모듈이 먼저 app을 불러오든 같은 주소를 쓰도록 여기서 먼저 맞춘다.
연결 풀은 한 번 닫으면 다시 열 수 없으므로 세션 전체에서 하나만 열고 끝에 닫는다.
"""
import os

import pytest

PG = os.environ.get("TEST_PG_URL")
if PG:
    os.environ["PG_URL"] = PG

@pytest.fixture(scope="session")
def pg_store():
    if not PG:
        pytest.skip("TEST_PG_URL 없음")
    from app import store
    if os.environ.get("TEST_PG_NO_PREPARE"):  # PGlite 소켓 서버처럼 prepared statement에 약한 시험 서버용
        store.pool.kwargs["prepare_threshold"] = None
    store.qdrant = lambda: type("Q", (), {"collection_exists": lambda self, n: True})()
    store.init()
    yield store
    store.pool.close()
