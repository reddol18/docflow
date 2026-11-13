"""대기열 인터페이스 시험 (https://github.com/reddol18/docflow/issues/5 1단계).

같은 시험을 두 구현에 돌린다: Postgres(PGlite, TEST_PG_URL — scripts/check.ps1이 띄운다)와 Redis Streams(fakeredis + lupa —
이 PC에서 서버 없이 Streams·컨슈머 그룹·XAUTOCLAIM·Lua 스크립트를 흉내 낸다). 시간이 걸리는 값(유휴 시간)은 시험용으로 짧게 준다.
fakeredis가 없으면 건너뛰지 않고 실패한다 (두 구현 모두 돌았다는 근거).
"""
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import fakeredis
import pytest

from app import jobqueue
from app.jobqueue import ASK, GRAPH, OCR_PAGE, KindSpec, NewJob, QueueBusy, QueueConfig, QueueFull

IDLE = 0.6       # ocr_page 되찾기 유휴 시간(초)
G_IDLE = 0.8     # graph 유휴 시간
G_BEAT = 0.2     # graph 살아 있음 표시 간격
LIMIT = 5

CFG = QueueConfig(kinds={OCR_PAGE: KindSpec(idle=IDLE, limit=LIMIT),
                         GRAPH: KindSpec(idle=G_IDLE, heartbeat=G_BEAT),
                         ASK: KindSpec(idle=IDLE)},
                  retry_after=30, max_attempts=3, dead_keep=2, dead_days=14, lock_timeout=0.5)


def clean_pg(store):
    with store.pool.connection() as c:
        c.execute("DELETE FROM job_queue")
        c.execute("DELETE FROM job_queue_kind")


# 선택: 실제 Redis 7.4 관리자 연결 (예: WSL redis-server, redis://:pw@127.0.0.1:56379/0). 시험은 이 연결로 시험마다 앱 전용 ACL 사용자
# (app/jobqueue_redis.py acl_rules — 운영 k8s/docs.yaml과 같은 규칙)를 만들고, 대기열은 그 사용자로 붙는다 — Lua가 부르는 명령까지
# ACL에 다 들어 있는지 공통 시험 전체로 확인한다
REAL_REDIS = os.environ.get("TEST_REDIS_URL")
# 되찾기를 기다리는 시간: 실제 Redis(WSL)는 서버 시계·부하로 조금 흔들려 넉넉히 (그래도 유휴 시간 0.6초보다 길게만 기다림)
RECLAIM_WAIT = IDLE + (0.7 if REAL_REDIS else 0.3)


@pytest.fixture(params=["postgres", "redis", "redis-real"])
def q(request):
    if request.param == "postgres":
        store = request.getfixturevalue("pg_store")
        from app.jobqueue_pg import PgQueue
        clean_pg(store)
        return PgQueue(store.pool, CFG, poll=0.05)
    from app.jobqueue_redis import RedisQueue
    if request.param == "redis":
        return RedisQueue(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True), CFG,
                          prefix="t:q", poll=0.05)
    if not REAL_REDIS:
        pytest.skip("TEST_REDIS_URL 없음 — 실제 Redis 시험은 선택 (fakeredis 시험은 늘 돈다)")
    return acl_queue(request)


def acl_queue(request, cfg=None):
    import secrets

    import redis

    from app.jobqueue_redis import RedisQueue, acl_rules
    admin = redis.Redis.from_url(REAL_REDIS, decode_responses=True, db=0)  # 시험 전용: URL 비밀번호 허용
    prefix = f"t:{os.getpid()}:{time.monotonic_ns()}"  # 시험마다 새 키
    user, pw = f"t-docflow-{os.getpid()}-{time.monotonic_ns()}", secrets.token_hex(16)
    admin.execute_command("ACL", "SETUSER", user, "reset", "on", f">{pw}", *acl_rules(prefix))

    def cleanup():
        for k in admin.scan_iter(f"{{{prefix}}}:*"):
            admin.delete(k)
        admin.execute_command("ACL", "DELUSER", user)
    request.addfinalizer(cleanup)
    kw = admin.connection_pool.connection_kwargs
    client = redis.Redis(host=kw["host"], port=kw["port"], db=0, username=user, password=pw, decode_responses=True)
    return RedisQueue(client, cfg or CFG, prefix=prefix, poll=0.05)


def inject_raw(q, kind, body: str) -> None:
    """약속(NewJob)을 거치지 않고 본문을 바로 넣는다 — 대기열에 잘못된 것이 들어온 경우"""
    from app.jobqueue_pg import PgQueue
    if isinstance(q, PgQueue):
        with q.pool.connection() as c:
            c.execute("INSERT INTO job_queue (kind, body) VALUES (%s, %s::jsonb)", (kind, body))
    else:
        q.r.xadd(q.key(kind), {"body": body, "a": 0})


def pages(n, vol="철1"):
    return [NewJob(OCR_PAGE, vol=vol, page=f"{i:03d}") for i in range(1, n + 1)]


def test_put_take_ack_in_order(q):
    ids = q.put(pages(3))
    assert len(ids) == 3
    got = [q.take(OCR_PAGE, "w1") for _ in range(3)]
    assert [j.page for j in got] == ["001", "002", "003"]
    assert [j.id for j in got] == ids
    assert all(j.vol == "철1" and j.attempts == 1 and j.consumer == "w1" for j in got)
    assert q.take(OCR_PAGE, "w1") is None
    assert q.stats(OCR_PAGE).running == 3
    assert all(q.ack(j) for j in got)
    assert not q.ack(got[0])  # 두 번째 완료는 없는 작업
    s = q.stats(OCR_PAGE)
    assert (s.waiting, s.running, s.length) == (0, 0, 0)


def test_empty_put(q):
    assert q.put([]) == []
    assert q.stats(OCR_PAGE).length == 0


def test_take_waits_then_gives_up(q):
    t = time.monotonic()
    assert q.take(OCR_PAGE, "w1", wait=0.3) is None
    assert time.monotonic() - t >= 0.25


def test_take_waits_for_new_job(q):
    threading.Timer(0.2, lambda: q.put(pages(1))).start()
    job = q.take(OCR_PAGE, "w1", wait=3)
    assert job and job.page == "001"


def test_stats_and_position(q):
    ids = q.put(pages(3))
    assert [q.position(OCR_PAGE, i) for i in ids] == [1, 2, 3]
    first = q.take(OCR_PAGE, "w1")
    assert q.position(OCR_PAGE, first.id) is None  # 쥐고 있는 작업은 순번 없음
    assert [q.position(OCR_PAGE, i) for i in ids[1:]] == [1, 2]
    s = q.stats(OCR_PAGE)
    assert (s.waiting, s.running) == (2, 1)
    q.ack(first)
    assert q.position(OCR_PAGE, first.id) is None  # 끝난 작업도
    assert q.position(OCR_PAGE, "999999") is None  # 없는 id
    assert q.stats(GRAPH).length == 0  # 종류마다 따로


def test_due_job_counts_as_waiting_and_first(q):
    """유휴 시간이 지난(되찾을 차례인) 작업은 기다림으로 세고, 새 작업보다 앞 순번"""
    q.put(pages(1))
    lost = q.take(OCR_PAGE, "dead-worker")
    assert q.stats(OCR_PAGE).running == 1
    time.sleep(RECLAIM_WAIT)
    s = q.stats(OCR_PAGE)
    assert (s.waiting, s.running) == (1, 0)
    assert q.position(OCR_PAGE, lost.id) == 1
    (new_id,) = q.put(pages(1, vol="철2"))
    assert q.position(OCR_PAGE, new_id) == 2


def test_release_goes_to_back_and_does_not_count_attempt(q):
    q.put(pages(2))
    a = q.take(OCR_PAGE, "w1")
    new_id = q.release(a)
    assert new_id
    assert q.position(OCR_PAGE, new_id) == 2
    b = q.take(OCR_PAGE, "w1")
    assert b.page == "002" and b.attempts == 1
    again = q.take(OCR_PAGE, "w2")
    assert again.page == "001" and again.id == new_id
    assert again.attempts == 1  # 자발적 되돌리기는 시도로 세지 않는다
    assert q.stats(OCR_PAGE).length == 2


def test_many_releases_never_go_dead(q):
    """max_attempts(3)보다 여러 번 되돌려도 죽은 작업이 되지 않는다 (실패와 구분)"""
    q.put(pages(1))
    for _ in range(CFG.max_attempts + 2):
        job = q.take(OCR_PAGE, "w1")
        assert job and job.attempts == 1
        assert q.release(job)
    s = q.stats(OCR_PAGE)
    assert (s.length, s.dead) == (1, 0)


def test_dead_worker_job_reclaimed_after_idle(q):
    """꺼낸 워커가 ack 없이 죽으면 유휴 시간 뒤 다른 워커가 되찾아 끝낸다. 늦게 살아난 옛 워커는 아무것도 못 한다"""
    q.put(pages(1))
    lost = q.take(OCR_PAGE, "dead-worker")
    assert q.take(OCR_PAGE, "w2") is None  # 유휴 시간 전에는 못 가져간다
    time.sleep(RECLAIM_WAIT)
    got = q.take(OCR_PAGE, "w2")
    assert got and got.id == lost.id and got.page == "001"
    assert got.attempts == 2 and got.consumer == "w2"
    assert not q.heartbeat(lost)       # 옛 워커는 자기 것이 아님을 안다
    assert q.release(lost) is None     # 되돌리지도 못한다
    assert not q.ack(lost)             # 늦게 끝내도 새 워커의 작업을 지우지 못한다
    assert q.stats(OCR_PAGE).running == 1
    assert q.ack(got)
    assert q.stats(OCR_PAGE).length == 0


def test_release_then_retake_old_lease_is_fenced(q):
    """같은 consumer가 되돌린 뒤 다시 꺼내면 attempts는 같아도(되돌림은 시도로 안 셈) 옛 Job으로는 ack·heartbeat가 안 된다 (ABA)"""
    q.put(pages(1))
    first = q.take(OCR_PAGE, "w1")
    assert q.release(first)
    second = q.take(OCR_PAGE, "w1")
    assert second.attempts == first.attempts == 1
    assert not q.ack(first) and not q.heartbeat(first) and q.release(first) is None and q.fail(first) is None
    assert q.heartbeat(second) and q.ack(second)
    assert q.stats(OCR_PAGE).length == 0


def test_fail_counts_attempts_then_dead(q):
    """실패(fail)는 맨 뒤로 되돌리되 시도로 센다 — max_attempts(3)번 실패하면 네 번째로 꺼내지 않고 죽은 작업, on_dead 알림"""
    seen = []
    q.on_dead = lambda kind, body, reason: seen.append((kind, body, reason))
    q.put(pages(1))
    for n in (1, 2, 3):
        job = q.take(OCR_PAGE, "w1")
        assert job and job.attempts == n
        assert q.fail(job)
    assert q.take(OCR_PAGE, "w1") is None
    s = q.stats(OCR_PAGE)
    assert (s.length, s.dead) == (0, 1)
    assert seen == [(OCR_PAGE, {"vol": "철1", "page": "001"}, "attempts")]


def test_on_dead_failure_does_not_block(q):
    def boom(*_):
        raise RuntimeError("x")
    q.on_dead = boom
    inject_raw(q, OCR_PAGE, '{"bad": 1}')
    q.put(pages(1))
    assert q.take(OCR_PAGE, "w1").page == "001"


def test_same_consumer_old_attempt_is_fenced(q):
    """같은 consumer 이름이 다시 되찾아도 옛 시도(attempts)로는 ack·heartbeat가 안 된다 (펜싱 토큰)"""
    q.put(pages(1))
    first = q.take(OCR_PAGE, "w1")
    time.sleep(RECLAIM_WAIT)
    second = q.take(OCR_PAGE, "w1")
    assert second.id == first.id and second.attempts == 2
    assert not q.ack(first) and not q.heartbeat(first)
    assert q.heartbeat(second) and q.ack(second)


def test_reclaim_comes_before_new_jobs(q):
    q.put(pages(1))
    q.take(OCR_PAGE, "dead-worker")
    time.sleep(RECLAIM_WAIT)
    q.put(pages(1, vol="철2"))
    assert q.take(OCR_PAGE, "w2").vol == "철1"
    assert q.take(OCR_PAGE, "w2").vol == "철2"


def test_heartbeat_keeps_long_job(q):
    """그래프 작업: 살아 있음 표시를 하는 동안은 유휴 시간이 지나도 되찾기지 않는다, 멈추면 되찾긴다"""
    q.put([NewJob(GRAPH, vol="철1")])
    job = q.take(GRAPH, "w1")
    end = time.monotonic() + G_IDLE * 2
    while time.monotonic() < end:
        time.sleep(G_BEAT)
        assert q.heartbeat(job)
        assert q.take(GRAPH, "w2") is None
    time.sleep(G_IDLE + 0.3)
    got = q.take(GRAPH, "w2")
    assert got and got.id == job.id and got.attempts == 2


def test_limit_rejects_whole_batch(q):
    """상한까지 채운 뒤 다음 넣기는 QueueFull(Retry-After 30) — 넣은 것은 없다. 꺼낸(ack 전) 작업도 센다"""
    q.put(pages(4))
    with pytest.raises(QueueFull) as e:
        q.put(pages(2, vol="철2"))  # 4 + 2 > 5: 한 개도 안 넣는다
    assert e.value.retry_after == 30 and e.value.headers() == {"Retry-After": "30"}
    assert (e.value.kind, e.value.length, e.value.adding, e.value.limit) == (OCR_PAGE, 4, 2, LIMIT)
    assert q.stats(OCR_PAGE).length == 4
    q.put(pages(1, vol="철2"))  # 딱 상한
    job = q.take(OCR_PAGE, "w1")
    with pytest.raises(QueueFull):
        q.put(pages(1, vol="철3"))  # 꺼낸 작업도 ack 전이면 센다
    with pytest.raises(QueueFull):
        q.put([NewJob(GRAPH, vol="철9"), *pages(1, vol="철3")])  # 섞인 묶음도 통째로 거절
    assert q.stats(GRAPH).length == 0
    q.ack(job)
    q.put(pages(1, vol="철3"))
    q.put([NewJob(GRAPH, vol=f"철{i}") for i in range(10)])  # 그래프는 상한 없음 (쪽 OCR 상한과 따로)
    assert q.stats(OCR_PAGE).length == LIMIT


def test_limit_holds_under_concurrent_puts(q):
    ok, full = [], []
    start = threading.Barrier(8)

    def put(i):
        start.wait()
        try:
            q.put(pages(1, vol=f"철{i}"))
            ok.append(i)
        except QueueFull:
            full.append(i)
    ts = [threading.Thread(target=put, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert len(ok) == LIMIT and len(full) == 3
    assert q.stats(OCR_PAGE).length == LIMIT


def test_concurrent_take_no_duplicates(q):
    q.put(pages(5))
    seen, lock = [], threading.Lock()

    def work(name):
        while (job := q.take(OCR_PAGE, name)) is not None:
            with lock:
                seen.append(job.id)
            q.ack(job)
    ts = [threading.Thread(target=work, args=(f"w{i}",)) for i in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert len(seen) == 5 and len(set(seen)) == 5


def test_too_many_attempts_goes_dead(q):
    """max_attempts(3)번 워커를 잃은 작업은 네 번째로 꺼내지 않고 죽은 작업으로 뺀다 (상한에 세지 않음)"""
    q.put(pages(1))
    for n in (1, 2, 3):
        job = q.take(OCR_PAGE, f"w{n}")
        assert job and job.attempts == n
        time.sleep(RECLAIM_WAIT)  # 실제 Redis(WSL)는 서버 시계가 조금 흔들린다 — 다른 되찾기 시험처럼 넉넉히
    assert q.take(OCR_PAGE, "w4") is None
    s = q.stats(OCR_PAGE)
    assert (s.length, s.dead) == (0, 1)


@pytest.mark.parametrize("body", ['{"vol": 5, "page": "1"}', '{"vol": "철1", "page": "1", "cmd": "rm"}', '["x"]',
                                  '{"vol": "철1"}', '{"vol": "' + "x" * 300 + '", "page": "1"}'])
def test_poison_body_goes_dead_and_does_not_block(q, body):
    """약속에 맞지 않는 본문은 꺼낼 때 다시 검사해 워커에 넘기지 않고 죽은 작업으로 — 뒤 작업은 그대로 꺼낸다"""
    inject_raw(q, OCR_PAGE, body)
    q.put(pages(1))
    job = q.take(OCR_PAGE, "w1")
    assert job and job.page == "001" and job.vol == "철1"
    s = q.stats(OCR_PAGE)
    assert (s.length, s.dead) == (1, 1)


def test_dead_days_removes_old(q):
    """죽은 작업은 dead_days보다 오래되면 지운다 — 다음 죽은 작업을 뺄 때 정리"""
    from dataclasses import replace
    q.cfg = replace(CFG, dead_days=0.5 / 86400, dead_keep=0)  # 0.5초, 개수 제한 없음
    inject_raw(q, OCR_PAGE, '{"bad": 1}')
    assert q.take(OCR_PAGE, "w1") is None
    assert q.stats(OCR_PAGE).dead == 1
    time.sleep(1.2)
    inject_raw(q, OCR_PAGE, '{"bad": 2}')
    assert q.take(OCR_PAGE, "w1") is None
    assert q.stats(OCR_PAGE).dead == 1  # 앞의 것은 지워지고 방금 것만


def test_dead_keeps_only_recent(q):
    """죽은 작업은 종류마다 dead_keep(2)개만 남긴다"""
    for i in range(4):
        inject_raw(q, OCR_PAGE, f'{{"bad": {i}}}')
    assert q.take(OCR_PAGE, "w1") is None
    assert q.stats(OCR_PAGE).dead == 2


def test_ask_job_round_trip(q):
    q.put([NewJob(ASK, ref="ask-123")])
    job = q.take(ASK, "w1")
    assert job.ref == "ask-123" and job.vol is None and job.page is None
    assert q.ack(job)


@pytest.mark.parametrize("bad", [NewJob(OCR_PAGE, vol="철1"), NewJob(GRAPH), NewJob(ASK), NewJob("nope", vol="철1"),
                                 NewJob(OCR_PAGE, vol="x" * 300, page="1"), NewJob(OCR_PAGE, vol="철1", page="")])
def test_invalid_job_rejected(q, bad):
    with pytest.raises(ValueError):
        q.put([pages(1)[0], bad])
    assert q.stats(OCR_PAGE).length == 0  # 앞의 정상 작업도 넣지 않는다


def test_stream_vanished_recreates_group():
    """Redis: 스트림이 지워져도(초기화 등) 길이·순번·꺼내기·넣기가 NOGROUP 없이 다시 그룹을 만든다"""
    from app.jobqueue_redis import RedisQueue
    q = RedisQueue(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True), CFG, prefix="t:q", poll=0.05)
    (i,) = q.put(pages(1))
    q.r.delete(q.key(OCR_PAGE))
    assert q.stats(OCR_PAGE).length == 0
    assert q.position(OCR_PAGE, i) is None
    assert q.take(OCR_PAGE, "w1") is None
    q.r.delete(q.key(OCR_PAGE))
    q.put(pages(1))
    assert q.take(OCR_PAGE, "w1").page == "001"


def test_pg_put_lock_timeout_raises_busy():
    """Postgres: 넣기는 트랜잭션 첫 문장에서 lock_timeout(QUEUE_LOCK_TIMEOUT)을 걸고, 잠금을 못 얻으면(LockNotAvailable) QueueBusy.
    실제로 기다리게 하는 시험은 PGlite로 못 한다 — PGlite 소켓 서버는 백엔드 하나라 열린 트랜잭션이 있으면 다른 연결의 문장을
    아예 보내지 않고 줄 세운다(잠금 대기가 생기지 않음). 그래서 연결을 흉내 내 문장 순서와 예외 변환을 본다"""
    from contextlib import contextmanager

    from psycopg import errors as pg_errors

    from app.jobqueue_pg import PgQueue
    seen = []

    class Conn:
        def execute(self, sql, params=None):
            seen.append((" ".join(sql.split()), params))
            if "FOR UPDATE" in sql:
                raise pg_errors.LockNotAvailable("canceling statement due to lock timeout")
            return self

        def fetchone(self):
            return {"n": 0}

        @contextmanager
        def transaction(self):
            yield

    class Pool:
        @contextmanager
        def connection(self):
            yield Conn()

    q = PgQueue(Pool(), CFG)
    with pytest.raises(QueueBusy) as e:
        q.put(pages(1))
    assert e.value.headers() == {"Retry-After": "30"} and e.value.kind == OCR_PAGE
    assert seen[0] == ("SELECT set_config('lock_timeout', %s, true)", ("500ms",))  # CFG.lock_timeout 0.5초, 이 트랜잭션에만
    assert not any(s.startswith("INSERT INTO job_queue ") for s, _ in seen)


def test_pg_schema_created_by_store_init(pg_store):
    """표는 store.init()(init_db)이 만든다 — 운영 기동에서도 생긴다"""
    with pg_store.pool.connection() as c:
        names = {r["n"] for r in c.execute(
            "SELECT to_regclass('job_queue')::text AS n UNION ALL SELECT to_regclass('job_queue_kind')::text "
            "UNION ALL SELECT to_regclass('job_queue_new')::text UNION ALL SELECT to_regclass('job_queue_taken')::text")}
    assert names == {"job_queue", "job_queue_kind", "job_queue_new", "job_queue_taken"}
    assert "job_queue" in pg_store.SCHEMA


def test_pg_old_dev_table_upgraded(pg_store):
    """1단계 첫 판(daa3417)으로 만든 개발 DB: dead_at·dead_reason·lease 열이 없고 옛 인덱스 job_queue_take가 있다 —
    store.init()이 열을 더하고 옛 인덱스를 지운 뒤 대기열이 그대로 돈다"""
    from app.jobqueue_pg import PgQueue
    with pg_store.pool.connection() as c:
        c.execute("DROP TABLE job_queue")
        c.execute("""CREATE TABLE job_queue (id bigserial PRIMARY KEY, seq bigint NOT NULL DEFAULT nextval('job_queue_seq'),
                     kind text NOT NULL, body jsonb NOT NULL, attempts int NOT NULL DEFAULT 0, owner text, taken_at timestamptz,
                     dead boolean NOT NULL DEFAULT false, created_at timestamptz NOT NULL DEFAULT clock_timestamp())""")
        c.execute("CREATE INDEX job_queue_take ON job_queue (kind, seq) WHERE NOT dead")
        c.execute("INSERT INTO job_queue (kind, body, dead) VALUES ('ocr_page', '{}'::jsonb, true)")  # 옛 판의 죽은 작업
    pg_store.init()
    with pg_store.pool.connection() as c:
        cols = {r["column_name"] for r in c.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'job_queue'")}
        assert c.execute("SELECT to_regclass('job_queue_take')::text AS n").fetchone()["n"] is None
    assert {"dead_at", "dead_reason", "lease"} <= cols
    q = PgQueue(pg_store.pool, CFG, poll=0.05)
    q.put(pages(1))
    job = q.take(OCR_PAGE, "w1")
    assert job.lease and q.ack(job)
    for i in range(3):  # dead_keep 2: dead_at 없는 옛 행(NULLS LAST → 가장 오래된 것)부터 지운다
        inject_raw(q, OCR_PAGE, f'{{"bad": {i}}}')
    assert q.take(OCR_PAGE, "w1") is None
    with pg_store.pool.connection() as c:
        assert c.execute("SELECT count(*) AS n FROM job_queue WHERE dead AND dead_at IS NULL").fetchone()["n"] == 0
    assert q.stats(OCR_PAGE).dead == 2


def test_consumer_name_unique():
    a, b = jobqueue.consumer_name(), jobqueue.consumer_name()
    assert a != b and a.startswith("worker-") and len(a) < 100


def test_config_defaults_match_decision():
    """구현 전 결정(2026-11-13 이슈 댓글)의 값이 기본값"""
    cfg = QueueConfig.from_env({})
    assert cfg.spec(OCR_PAGE) == KindSpec(idle=120.0, limit=2000)
    assert cfg.spec(GRAPH) == KindSpec(idle=180.0, limit=200, heartbeat=30.0)
    assert cfg.spec(ASK) == KindSpec(idle=60.0, limit=100, heartbeat=20.0)  # 살아 있음 표시는 2차 리뷰 반영(결정값 60은 그대로)
    assert cfg.retry_after == 30 and cfg.max_attempts == 3  # 2단계 전 결정 (2026-11-13)
    assert (cfg.dead_keep, cfg.dead_days, cfg.lock_timeout) == (1000, 14.0, 5.0)


def test_config_from_env():
    cfg = QueueConfig.from_env({"QUEUE_LIMIT_OCR_PAGE": "10", "QUEUE_IDLE_OCR_PAGE": "5", "QUEUE_RETRY_AFTER": "7",
                                "QUEUE_IDLE_GRAPH": "60", "QUEUE_HEARTBEAT_GRAPH": "10", "QUEUE_MAX_ATTEMPTS": "2",
                                "QUEUE_DEAD_KEEP": "50", "QUEUE_DEAD_DAYS": "3", "QUEUE_LOCK_TIMEOUT": "2"})
    assert cfg.spec(OCR_PAGE) == KindSpec(idle=5.0, limit=10)
    assert cfg.spec(GRAPH).heartbeat == 10.0 and cfg.retry_after == 7 and cfg.max_attempts == 2
    assert (cfg.dead_keep, cfg.dead_days, cfg.lock_timeout) == (50, 3.0, 2.0)
    for bad in ({"QUEUE_IDLE_GRAPH": "20", "QUEUE_HEARTBEAT_GRAPH": "30"},  # 표시 간격 ≥ 유휴 시간
                {"QUEUE_LIMIT_OCR_PAGE": "-1"}, {"QUEUE_LOCK_TIMEOUT": "0"}):
        with pytest.raises(ValueError):
            QueueConfig.from_env(bad)
    with pytest.raises(ValueError):
        jobqueue.open_queue({"QUEUE_BACKEND": "kafka"})


def test_redis_connect_password_file_and_ca(tmp_path):
    """비밀번호는 파일(Secret 마운트)에서, URL에는 호스트만. CA는 rediss일 때만. 연결은 하지 않는다"""
    from app.jobqueue_redis import RedisQueue
    pw = tmp_path / "pw"
    pw.write_text("s3cret-for-test\n", encoding="utf-8")
    ca = tmp_path / "ca.crt"
    ca.write_text("x", encoding="utf-8")
    q = RedisQueue.connect({"REDIS_URL": "rediss://redis.docs:6379/0", "REDIS_PASSWORD_FILE": str(pw),
                            "REDIS_CA_FILE": str(ca)}, CFG)
    kw = q.r.connection_pool.connection_kwargs
    assert kw["password"] == "s3cret-for-test" and kw["ssl_ca_certs"] == str(ca) and kw["ssl_cert_reqs"] == "required"
    with pytest.raises(ValueError) as e:
        RedisQueue.connect({"REDIS_URL": "redis://:leakme@redis:6379/0"}, CFG)
    assert "leakme" not in str(e.value)
    with pytest.raises(ValueError):
        RedisQueue.connect({"REDIS_URL": "redis://redis:6379/0", "REDIS_CA_FILE": str(ca)}, CFG)  # 평문에 CA
    with pytest.raises(ValueError):
        RedisQueue.connect({"REDIS_URL": "http://redis:6379"}, CFG)
    for url in ("redis://redis:6379/0?password=leakme", "redis://redis:6379/0?PassWord=leakme"):  # redis-py가 쓰는 쿼리
        with pytest.raises(ValueError) as e:
            RedisQueue.connect({"REDIS_URL": url}, CFG)
        assert "leakme" not in str(e.value)
    up = RedisQueue.connect({"REDIS_URL": "REDISS://redis.docs:6379/0", "REDIS_CA_FILE": str(ca)}, CFG)  # 대문자 scheme
    assert up.r.connection_pool.connection_kwargs["ssl_ca_certs"] == str(ca)
    with pytest.raises(ValueError):
        RedisQueue(up.r, CFG, prefix="a{b}")  # 해시 태그를 깨는 prefix


def test_redis_keys_share_one_cluster_slot():
    """여러 종류를 한 스크립트로 넣고(상한 확인), 스트림과 dead 스트림을 한 스크립트로 만진다 — 모든 키가 한 슬롯이어야
    클러스터에서 CROSSSLOT이 나지 않는다 (해시 태그 {prefix})"""
    from redis.crc import key_slot

    from app.jobqueue_redis import RedisQueue
    q = RedisQueue(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True), CFG)
    keys = [q.key(k) for k in (OCR_PAGE, GRAPH, ASK)]
    keys += [f"{k}:dead" for k in keys]
    assert keys[0] == "{docflow:q}:ocr_page"
    assert len({key_slot(k.encode()) for k in keys}) == 1


def test_check_transport_requires_rediss():
    from app import store
    safe = {"DOCFLOW_REQUIRE_TLS": "1", "PG_URL": "postgresql://docflow@h/db?sslmode=verify-full",
            "QDRANT_URL": "https://qdrant:6333", "QDRANT_API_KEY_FILE": "/run/k"}
    store.check_transport(safe)  # Postgres 대기열(기본)은 Redis를 보지 않는다
    store.check_transport({**safe, "QUEUE_BACKEND": "redis", "REDIS_URL": "rediss://redis:6379/0", "REDIS_PASSWORD_FILE": "/p"})
    store.check_transport({**safe, "QUEUE_BACKEND": "redis", "REDIS_URL": "REDISS://redis:6379/0", "REDIS_PASSWORD_FILE": "/p"})
    for bad in ({"REDIS_URL": "redis://redis-host-xyz:6379/0", "REDIS_PASSWORD_FILE": "/p"},
                {"REDIS_URL": "rediss://redis:6379/0"}, {"REDIS_URL": "rediss", "REDIS_PASSWORD_FILE": "/p"}):
        with pytest.raises(RuntimeError) as e:
            store.check_transport({**safe, "QUEUE_BACKEND": "redis", **bad})
        assert "redis-host-xyz" not in str(e.value)  # 주소는 싣지 않는다


def test_app_starts_without_redis_installed():
    """이미지에는 아직 redis가 없다(2단계에서 lock에 넣음) — redis를 막은 프로세스에서 앱을 불러오고 Postgres 대기열을 열 수 있어야 한다"""
    code = """
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name == "redis" or name.startswith("redis.") or name == "fakeredis":
            raise ModuleNotFoundError(name)
sys.meta_path.insert(0, Block())
import app.main
from app import jobqueue
q = jobqueue.open_queue({"QUEUE_BACKEND": "postgres"})
assert type(q).__name__ == "PgQueue"
assert not [m for m in sys.modules if m == "redis" or m.startswith("redis.") or m == "app.jobqueue_redis"]
try:
    jobqueue.open_queue({"QUEUE_BACKEND": "redis", "REDIS_URL": "redis://127.0.0.1:1"})
except ModuleNotFoundError:
    print("redis-blocked-ok")
"""
    root = Path(__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONUTF8": "1"}
    r = subprocess.run([sys.executable, "-c", code], cwd=root, env=env, capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr[-3000:]
    assert "redis-blocked-ok" in r.stdout


def test_real_redis_acl_user_is_narrow(request):
    """실제 Redis: 앱 전용 ACL 사용자는 대기열 키(접두어) 밖·위험 명령을 못 한다 (선택 시험)"""
    if not REAL_REDIS:
        pytest.skip("TEST_REDIS_URL 없음")
    import redis
    q = acl_queue(request)
    q.put(pages(1))
    for bad in (lambda: q.r.set("other-key", "x"), lambda: q.r.flushall(), lambda: q.r.keys("*"),
                lambda: q.r.config_get("*"), lambda: q.r.xadd("{other:q}:ocr_page", {"a": "1"}),
                lambda: q.r.execute_command("ACL", "LIST"), lambda: q.r.execute_command("SELECT", 1)):
        with pytest.raises(redis.exceptions.NoPermissionError):
            bad()


def test_manifest_redis_acl_matches_code():
    """맥 Redis 매니페스트가 코드와 같은 ACL 규칙을 쓰고 default 사용자를 끈다 (k8s/docs.yaml redis 초기화 컨테이너)"""
    from pathlib import Path

    from app.jobqueue_redis import ACL_COMMANDS, acl_rules
    text = (Path(__file__).resolve().parents[1] / "k8s" / "docs.yaml").read_text(encoding="utf-8")
    assert "user docflow on >%s " + " ".join(acl_rules()) in text
    # default 줄: 꺼져 있고(비밀번호 없음), 권한은 +@all — 리셋 권한(-@all)이면 재시작 때 AOF 적재기가 Lua 효과의 MULTI/EXEC를
    # NOPERM으로 버린다 (https://github.com/reddol18/docflow/issues/5, scripts/redis_aof_check.sh)
    default = re.findall(r'printf "user default ([^"\\]*)\\n', text)
    assert len(default) == 1, default
    words = default[0].split()
    assert words[0] == "off" and "on" not in words and not any(w.startswith((">", "#", "<", "!")) for w in words)
    assert {"~*", "&*", "+@all"} <= set(words) and not any(w.startswith("-") or w in ("reset", "nocommands") for w in words)
    assert "requirepass" not in text
    assert all(c in ("ping", "client|setinfo", "time") or c.startswith(("x", "eval", "script")) for c in ACL_COMMANDS)
