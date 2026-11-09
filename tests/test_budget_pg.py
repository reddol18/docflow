"""비용 한도 예약·정산과 /search·/ask 호출 제한을 실제 Postgres(PGlite)로 (https://github.com/reddol18/docflow/issues/7).
모델·API는 부르지 않는다 (가짜 클라이언트). 날짜는 지어낸 날(2001년)로 고정해 다른 시험·오늘 사용량과 섞이지 않게 한다."""
import datetime as dt
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import llm


@pytest.fixture
def db(pg_store, monkeypatch):
    monkeypatch.setattr(llm, "_key", lambda: "fake-key-for-test")
    return pg_store


def fixed_day(monkeypatch, store, day):
    start = dt.datetime.combine(day, dt.time(), store.KST)
    monkeypatch.setattr(store, "kst_day", lambda now=None: (day, start))
    return start


def fake_embed_client(monkeypatch, create):
    monkeypatch.setattr(llm, "client", lambda: SimpleNamespace(embeddings=SimpleNamespace(create=create)))


def usage_row(tokens):
    return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=tokens, completion_tokens=0),
                           data=[SimpleNamespace(embedding=[0.0])])


def ledger(store, scope, day):
    with store.pool.connection() as c:
        used = c.execute("SELECT used::float AS u FROM budget_day WHERE scope = %s AND day = %s", (scope, day)).fetchone()
        held = c.execute("SELECT count(*) AS n FROM budget_hold WHERE scope = %s AND day = %s", (scope, day)).fetchone()["n"]
    return (used["u"] if used else None), held


def test_concurrent_calls_only_one_fits(db, monkeypatch):
    """남은 한도가 호출 1건분일 때 4개를 동시에 부르면 1건만 통과, 정산 뒤 합계가 한도를 넘지 않는다"""
    store = db
    day = dt.date(2001, 1, 1)
    fixed_day(monkeypatch, store, day)
    text = "가상 질의 문장"
    est = llm.estimate_usd("text-embedding-3-small", llm.tokens_max(text), 0)
    limit = est * 1.5  # 한 건은 들어가고 두 건은 안 들어간다
    monkeypatch.setattr(llm, "LIMITS", {"index": limit, "query": 1.0})
    refused = []
    all_refused = threading.Event()

    def create(**a):  # 통과한 호출은 나머지 셋이 거절될 때까지 예약을 쥔 채 기다린다 (겹침을 확실히)
        assert all_refused.wait(20)
        return usage_row(5)
    fake_embed_client(monkeypatch, create)
    start = threading.Barrier(4)

    def call(i):
        start.wait()
        try:
            llm.embed("t7-concurrent", [text])
            return "ok"
        except llm.BudgetExceeded:
            refused.append(i)
            if len(refused) == 3:
                all_refused.set()
            return "budget"
    with ThreadPoolExecutor(4) as ex:
        out = list(ex.map(call, range(4)))
    assert sorted(out) == ["budget", "budget", "budget", "ok"]
    used, held = ledger(store, "index", day)
    assert held == 0 and used == pytest.approx(5 * 0.02 / 1e6) and used <= limit
    with store.pool.connection() as c:
        rows = c.execute("SELECT scope, usd::float AS usd FROM usage WHERE vol = 't7-concurrent'").fetchall()
    assert len(rows) == 1 and rows[0]["scope"] == "index" and sum(r["usd"] for r in rows) <= limit


def test_query_budget_separate_from_index(db, monkeypatch):
    """검색·질문 예산을 다 써도 색인·수정 임베딩은 통과한다 (예산이 따로)"""
    store = db
    day = dt.date(2001, 1, 2)
    fixed_day(monkeypatch, store, day)
    est = llm.estimate_usd("text-embedding-3-small", llm.tokens_max("가상"), 0)
    monkeypatch.setattr(llm, "LIMITS", {"index": 1.0, "query": est * 1.5})
    fake_embed_client(monkeypatch, lambda **a: usage_row(llm.tokens_max("가상")))  # 실제 = 예상 (최악)
    llm.embed("-", ["가상"], scope="query", user_id=901)
    with pytest.raises(llm.BudgetExceeded):
        llm.embed("-", ["가상"], scope="query", user_id=902)
    with pytest.raises(llm.BudgetExceeded):  # 답변도 같은 query 예산
        llm.answer("가상", [], user_id=902)
    assert llm.embed("t7-index", ["가상"]) == [[0.0]]  # 색인·수정 (main._edit_document·graph.index는 scope 기본값 index)
    q_used, _ = ledger(store, "query", day)
    i_used, _ = ledger(store, "index", day)
    assert q_used == pytest.approx(est) and i_used == pytest.approx(est)
    with store.pool.connection() as c:
        r = c.execute("SELECT user_id FROM usage WHERE scope = 'query' AND user_id = 901").fetchone()
    assert r is not None  # 검색·질문 사용량에 부른 사람이 남는다


def test_legacy_usage_counts_toward_today(db, monkeypatch):
    """이 판 이전 행(scope 없음)의 그날 사용량이 예산 행을 처음 만들 때 들어간다 — vol '-'는 검색·질문"""
    store = db
    day = dt.date(2001, 1, 3)
    start = fixed_day(monkeypatch, store, day)
    with store.pool.connection() as c:
        for vol, usd in (("-", 0.5), ("t7-legacy", 0.1)):
            c.execute("INSERT INTO usage (ts, vol, kind, model, tin, tout, usd) VALUES (%s,%s,'embed','m',1,0,%s)",
                      (start + dt.timedelta(hours=1), vol, usd))
    assert store.reserve_budget("query", day, start, 0.2, 0.6) is None  # 0.5 + 0.2 > 0.6
    hold = store.reserve_budget("index", day, start, 0.2, 0.6)  # 0.1 + 0.2 ≤ 0.6
    assert hold is not None
    shown = store.budget_today({"index": 0.6})  # 화면용 (오늘 = 고정한 날)
    assert shown["index"]["used"] == pytest.approx(0.1) and shown["index"]["held"] == pytest.approx(0.2)
    store.release_budget(hold)
    assert ledger(store, "index", day) == (pytest.approx(0.1), 0)


def user(uid, name="가상"):
    from app import auth
    return auth.User(uid, f"{name}{uid}", "가상 사용자", "viewer")


def test_rate_limit_per_user(db, monkeypatch):
    """같은 사용자가 분당 상한을 넘기면 429, 다른 사용자는 그대로. 하루 쿼터도 같은 방식 (Postgres에 세어 재시작에도 이어짐)"""
    from app import main
    monkeypatch.setattr(main, "RATE", {"ask": {"min": 3, "day": 5}, "search": {"min": 0, "day": 2}})
    now = dt.datetime(2001, 2, 1, 10, 0, 30, tzinfo=db.KST)  # 분이 바뀌어 시험이 흔들리지 않게 시각을 고정
    windows = main._quota_windows
    monkeypatch.setattr(main, "_quota_windows", lambda route, at=None: windows(route, at or now))
    ask = main.rate_limit("ask")
    for _ in range(3):
        ask(user(7101))
    with pytest.raises(HTTPException) as e:
        ask(user(7101))
    assert e.value.status_code == 429 and e.value.headers["Retry-After"] == "60"
    ask(user(7102))  # 다른 사용자는 영향 없음
    # 하루 쿼터 (분당 0 = 끔): 2번 뒤 429, Retry-After는 다음 날 0시까지
    search = main.rate_limit("search")
    search(user(7101))
    search(user(7101))
    with pytest.raises(HTTPException) as e:
        search(user(7101))
    assert e.value.status_code == 429 and 0 < int(e.value.headers["Retry-After"]) <= 86400
    search(user(7102))
    # 막힌 호출은 세지 않는다: 분당 창이 바뀌면(다음 분) 하루 쿼터 안에서 다시 된다
    nxt = now + dt.timedelta(minutes=1)
    assert db.take_quota("7101", "ask", main._quota_windows("ask", nxt)) is None  # 하루 4번째
    assert db.take_quota("7101", "ask", main._quota_windows("ask", nxt)) is None  # 하루 5번째
    assert db.take_quota("7101", "ask", main._quota_windows("ask", nxt)) == "day"
