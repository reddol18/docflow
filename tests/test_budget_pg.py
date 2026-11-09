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
        # 다음 날 0시 정각 행은 그날 몫이 아니다 — 예약과 화면이 같은 범위 [0시, +1일)
        c.execute("INSERT INTO usage (ts, vol, kind, model, tin, tout, usd) VALUES (%s,'t7-legacy','embed','m',1,0,0.3)",
                  (start + dt.timedelta(days=1),))
    shown = store.budget_today({"index": 0.6, "query": 0.6})  # 예산 행을 만들기 전 (예전 사용량으로)
    assert shown["index"]["used"] == pytest.approx(0.1) and shown["query"]["used"] == pytest.approx(0.5)
    assert store.reserve_budget("query", day, start, 0.2, 0.6) is None  # 0.5 + 0.2 > 0.6
    hold = store.reserve_budget("index", day, start, 0.2, 0.6)  # 0.1 + 0.2 ≤ 0.6
    assert hold is not None
    shown = store.budget_today({"index": 0.6})  # 화면용 (오늘 = 고정한 날)
    assert shown["index"]["used"] == pytest.approx(0.1) and shown["index"]["held"] == pytest.approx(0.2)
    store.release_budget(hold)
    assert ledger(store, "index", day) == (pytest.approx(0.1), 0)
    # 예산 행이 생긴 뒤에는 예전 사용량을 다시 읽지 않는다 (그 뒤 사용량은 정산으로만 늘어난다)
    with store.pool.connection() as c:
        c.execute("INSERT INTO usage (ts, vol, kind, model, tin, tout, usd) VALUES (%s,'t7-legacy','embed','m',1,0,0.4)",
                  (start + dt.timedelta(hours=2),))
    hold = store.reserve_budget("index", day, start, 0.2, 0.6)
    assert hold is not None and ledger(store, "index", day)[0] == pytest.approx(0.1)
    store.release_budget(hold)


def test_unsettled_hold_settled_at_estimate(db, monkeypatch, capsys):
    """정산 쓰기가 실패해도 호출 결과는 돌려주고 예약은 남는다 → 만료되면 예상 비용 그대로 used·usage에 들어간다
    (프로세스가 호출 중에 죽은 경우와 같은 길). 늦게 온 정산은 두 번 세지 않는다"""
    store = db
    day = dt.date(2001, 1, 14)
    fixed_day(monkeypatch, store, day)
    monkeypatch.setattr(llm, "LIMITS", {"index": 1.0, "query": 1.0})
    fake_embed_client(monkeypatch, lambda **a: usage_row(3))
    est = llm.estimate_usd("text-embedding-3-small", llm.tokens_max("가상"), 0)
    real_settle = store.settle_budget

    def broken(*a, **k):
        raise TimeoutError("가상 DB 끊김")
    monkeypatch.setattr(store, "settle_budget", broken)
    assert llm.embed("t7-unsettled", ["가상"]) == [[0.0]]  # 결과는 돌려준다
    assert "정산 실패" in capsys.readouterr().err
    assert ledger(store, "index", day) == (0.0, 1)  # 예약이 남아 한도를 잡고 있다
    with store.pool.connection() as c:
        hold = c.execute("SELECT id FROM budget_hold WHERE scope = 'index' AND day = %s", (day,)).fetchone()["id"]
    monkeypatch.setattr(store, "HOLD_TTL", "0 seconds")  # 만료
    monkeypatch.setattr(store, "settle_budget", real_settle)
    other = store.reserve_budget("index", day, store.kst_day()[1], 0.001, 1.0)  # 다음 예약이 만료 예약을 보수 정산
    used, held = ledger(store, "index", day)
    assert used == pytest.approx(est) and held == 1
    with store.pool.connection() as c:
        rows = c.execute("SELECT kind, usd::float AS usd, tin FROM usage WHERE vol = 't7-unsettled'").fetchall()
    assert rows == [{"kind": "embed" + store.UNSETTLED, "usd": pytest.approx(est), "tin": 0}]
    store.settle_budget(hold, "index", day, 1e-9, ("t7-unsettled", "embed", "m", 3, 0, None))  # 늦은 정산 — 무시
    assert ledger(store, "index", day)[0] == pytest.approx(est)
    store.release_budget(other)


def test_prune_settles_expired_holds(db, monkeypatch):
    """시작할 때 만료 예약은 지우지 않고 예상 비용으로 정산한다 (지난 날 것은 그날 used에)"""
    store = db
    day = dt.date(2001, 1, 15)
    start = fixed_day(monkeypatch, store, day)
    hold = store.reserve_budget("index", day, start, 0.25, 1.0, ("t7-prune", "extract", "gpt-4.1-mini", None))
    assert hold is not None
    monkeypatch.setattr(store, "HOLD_TTL", "0 seconds")
    store.prune_budget()
    assert ledger(store, "index", day) == (pytest.approx(0.25), 0)
    with store.pool.connection() as c:
        r = c.execute("SELECT kind, model FROM usage WHERE vol = 't7-prune'").fetchone()
    assert r == {"kind": "extract" + store.UNSETTLED, "model": "gpt-4.1-mini"}


def user(uid, name="가상"):
    from app import auth
    return auth.User(uid, f"{name}{uid}", "가상 사용자", "viewer")


def test_rate_limit_per_user(db, monkeypatch):
    """같은 사용자가 분당 상한을 넘기면 429, 다른 사용자는 그대로. 하루 쿼터도 같은 방식 (Postgres에 세어 재시작에도 이어짐)"""
    from app import main
    monkeypatch.setattr(main, "RATE", {"ask": {"min": 3, "day": 5}, "search": {"min": 0, "day": 2}})
    now = dt.datetime(2001, 2, 1, 10, 0, 30, tzinfo=db.KST)  # 분이 바뀌어 시험이 흔들리지 않게 시각을 고정
    monkeypatch.setattr(main, "_now", lambda: now)

    def ask(u):
        with main.rate_limited("ask", u):
            pass

    def search(u):
        with main.rate_limited("search", u):
            pass
    for _ in range(3):
        ask(user(7101))
    with pytest.raises(HTTPException) as e:
        ask(user(7101))
    assert e.value.status_code == 429 and e.value.headers["Retry-After"] == "30"  # 이 분이 끝날 때까지
    ask(user(7102))  # 다른 사용자는 영향 없음
    # 하루 쿼터 (분당 0 = 끔): 2번 뒤 429, Retry-After는 다음 날 0시까지
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


def test_rate_refunded_on_query_budget(db, monkeypatch):
    """검색·질문 예산이 차서 503이면 그 호출 1회를 되돌린다 — 쿼터가 사용자 탓 아닌 거절로 줄지 않는다"""
    from app import main
    monkeypatch.setattr(main, "RATE", {"ask": {"min": 2, "day": 2}, "search": {"min": 0, "day": 0}})
    monkeypatch.setattr(main, "_now", lambda: dt.datetime(2001, 2, 2, 9, 0, 10, tzinfo=db.KST))
    for _ in range(3):  # 예산 거절 3번 — 모두 되돌려져 쿼터가 그대로
        with pytest.raises(llm.BudgetExceeded), main.rate_limited("ask", user(7201)):
            raise llm.BudgetExceeded("query 예산 한도 (가상)")
    for _ in range(2):  # 상한 2번은 그대로 쓸 수 있다
        with main.rate_limited("ask", user(7201)):
            pass
    with pytest.raises(HTTPException) as e, main.rate_limited("ask", user(7201)):
        pass
    assert e.value.status_code == 429
