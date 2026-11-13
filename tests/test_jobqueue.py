"""대기열 인터페이스 시험 (https://github.com/reddol18/docflow/issues/5 1단계).

같은 시험을 두 구현에 돌린다: Postgres(PGlite, TEST_PG_URL — scripts/check.ps1이 띄운다)와 Redis Streams(fakeredis — 이 PC에서
서버 없이 Streams·컨슈머 그룹·XAUTOCLAIM·XCLAIM을 흉내 낸다). 시간이 걸리는 값(유휴 시간)은 시험용으로 짧게 준다.
"""
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app import jobqueue
from app.jobqueue import ASK, GRAPH, OCR_PAGE, KindSpec, NewJob, QueueConfig, QueueFull

IDLE = 0.6       # ocr_page 되찾기 유휴 시간(초)
G_IDLE = 0.8     # graph 유휴 시간
G_BEAT = 0.2     # graph 살아 있음 표시 간격
LIMIT = 5

CFG = QueueConfig(kinds={OCR_PAGE: KindSpec(idle=IDLE, limit=LIMIT),
                         GRAPH: KindSpec(idle=G_IDLE, heartbeat=G_BEAT),
                         ASK: KindSpec(idle=IDLE)},
                  retry_after=30, max_attempts=3)


@pytest.fixture(params=["postgres", "redis"])
def q(request):
    if request.param == "postgres":
        store = request.getfixturevalue("pg_store")
        from app.jobqueue_pg import PgQueue
        queue = PgQueue(store.pool, CFG, poll=0.05)
        queue.init_schema()
        with store.pool.connection() as c:
            c.execute("DELETE FROM job_queue")
            c.execute("DELETE FROM job_queue_kind")
        return queue
    fakeredis = pytest.importorskip("fakeredis")
    from app.jobqueue_redis import RedisQueue
    return RedisQueue(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True), CFG,
                      prefix="t:q", poll=0.05)


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
    assert q.position(OCR_PAGE, first.id) is None  # 꺼낸 작업은 순번 없음
    assert [q.position(OCR_PAGE, i) for i in ids[1:]] == [1, 2]
    s = q.stats(OCR_PAGE)
    assert (s.waiting, s.running) == (2, 1)
    q.ack(first)
    assert q.position(OCR_PAGE, first.id) is None  # 끝난 작업도
    assert q.stats(GRAPH).length == 0  # 종류마다 따로


def test_release_goes_to_back_and_keeps_attempts(q):
    q.put(pages(2))
    a = q.take(OCR_PAGE, "w1")
    new_id = q.release(a)
    assert new_id
    assert q.position(OCR_PAGE, new_id) == 2
    b = q.take(OCR_PAGE, "w1")
    assert b.page == "002" and b.attempts == 1
    again = q.take(OCR_PAGE, "w2")
    assert again.page == "001" and again.attempts == 2 and again.id == new_id
    assert q.stats(OCR_PAGE).length == 2


def test_dead_worker_job_reclaimed_after_idle(q):
    """꺼낸 워커가 ack 없이 죽으면 유휴 시간 뒤 다른 워커가 되찾아 끝낸다"""
    q.put(pages(1))
    lost = q.take(OCR_PAGE, "dead-worker")
    assert q.take(OCR_PAGE, "w2") is None  # 유휴 시간 전에는 못 가져간다
    time.sleep(IDLE + 0.3)
    got = q.take(OCR_PAGE, "w2")
    assert got and got.id == lost.id and got.page == "001"
    assert got.attempts == 2 and got.consumer == "w2"
    assert not q.heartbeat(lost)       # 옛 워커는 살아나도 자기 것이 아님을 안다
    assert q.release(lost) is None     # 되돌리지도 못한다
    assert q.ack(got)
    assert q.stats(OCR_PAGE).length == 0


def test_reclaim_comes_before_new_jobs(q):
    q.put(pages(1))
    q.take(OCR_PAGE, "dead-worker")
    time.sleep(IDLE + 0.3)
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
    assert (e.value.length, e.value.adding, e.value.limit) == (4, 2, LIMIT)
    assert q.stats(OCR_PAGE).length == 4
    q.put(pages(1, vol="철2"))  # 딱 상한
    job = q.take(OCR_PAGE, "w1")
    with pytest.raises(QueueFull):
        q.put(pages(1, vol="철3"))  # 꺼낸 작업도 ack 전이면 센다
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
        time.sleep(IDLE + 0.2)
    assert q.take(OCR_PAGE, "w4") is None
    s = q.stats(OCR_PAGE)
    assert (s.length, s.dead) == (0, 1)


def test_ask_job_round_trip(q):
    q.put([NewJob(ASK, ref="ask-123")])
    job = q.take(ASK, "w1")
    assert job.ref == "ask-123" and job.vol is None and job.page is None
    assert q.ack(job)


@pytest.mark.parametrize("bad", [NewJob(OCR_PAGE, vol="철1"), NewJob(GRAPH), NewJob(ASK), NewJob("nope", vol="철1"),
                                 NewJob(OCR_PAGE, vol="x" * 300, page="1")])
def test_invalid_job_rejected(q, bad):
    with pytest.raises(ValueError):
        q.put([pages(1)[0], bad])
    assert q.stats(OCR_PAGE).length == 0  # 앞의 정상 작업도 넣지 않는다


def test_config_defaults_match_decision():
    """구현 전 결정(2026-11-13 이슈 댓글)의 값이 기본값"""
    cfg = QueueConfig.from_env({})
    assert cfg.spec(OCR_PAGE) == KindSpec(idle=120.0, limit=2000)
    assert cfg.spec(GRAPH) == KindSpec(idle=180.0, limit=0, heartbeat=30.0)
    assert cfg.retry_after == 30


def test_config_from_env():
    cfg = QueueConfig.from_env({"QUEUE_LIMIT_OCR_PAGE": "10", "QUEUE_IDLE_OCR_PAGE": "5", "QUEUE_RETRY_AFTER": "7",
                                "QUEUE_IDLE_GRAPH": "60", "QUEUE_HEARTBEAT_GRAPH": "10", "QUEUE_MAX_ATTEMPTS": "2"})
    assert cfg.spec(OCR_PAGE) == KindSpec(idle=5.0, limit=10)
    assert cfg.spec(GRAPH).heartbeat == 10.0 and cfg.retry_after == 7 and cfg.max_attempts == 2
    with pytest.raises(ValueError):
        QueueConfig.from_env({"QUEUE_IDLE_GRAPH": "20", "QUEUE_HEARTBEAT_GRAPH": "30"})  # 표시 간격 ≥ 유휴 시간
    with pytest.raises(ValueError):
        QueueConfig.from_env({"QUEUE_LIMIT_OCR_PAGE": "-1"})
    with pytest.raises(ValueError):
        jobqueue.open_queue({"QUEUE_BACKEND": "kafka"})


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
