"""대기열 Postgres 구현 — 로컬 k3s 1노드용 (https://github.com/reddol18/docflow/issues/5). 약속은 app/jobqueue.py.

표: app/store.py SCHEMA의 job_queue·job_queue_kind (store.init()이 만든다).
꺼내기: 되찾을 차례인 작업(owner가 있고 taken_at이 유휴 시간보다 오래됨)을 먼저, 없으면 새 작업(owner 없음).
  둘 다 SELECT … ORDER BY seq LIMIT 1 FOR UPDATE SKIP LOCKED — 부분 인덱스(job_queue_taken·job_queue_new) 순서 그대로 훑는다.
주인 확인: ack·release·fail·heartbeat는 WHERE owner = consumer AND lease = 임대 번호 한 문장 — 확인과 동작이 원자적.
  lease는 꺼낼 때마다 시퀀스(job_queue_lease_seq)에서 새로 받는 단조 증가 번호다. attempts는 release가 되돌리므로(시도로 세지
  않음) 같은 consumer가 같은 attempts로 다시 꺼낼 수 있다 — attempts를 토큰으로 쓰면 옛 Job이 새 임대를 ack하는 ABA가 난다.
상한: 종류별 잠금 행(job_queue_kind)을 FOR UPDATE로 잡고 센 뒤 넣는다 (동시에 넣어도 넘지 않게).
  잠금 대기는 lock_timeout(QUEUE_LOCK_TIMEOUT, 5초)까지 — 넘으면 QueueBusy. pool 연결(최대 8)을 잠금 대기로 오래 쥐지 않는다.
완료한 행은 지운다. 죽은 작업은 dead=true로 남기고 종류마다 최근 dead_keep개·dead_days일만 둔다.
시간은 DB 서버 시계(clock_timestamp)만 쓴다 — 워커 Pod 시계가 달라도 되찾기 판단이 같다.
"""

from __future__ import annotations

import time
from typing import Sequence

from psycopg import errors as pg_errors

from app.jobqueue import Job, NewJob, QueueBusy, QueueConfig, QueueStats, check_limit, group_by_kind, notify_dead, parse_job

# 되찾기: 쥔 작업(워커 수만큼, 작다) 가운데 유휴 시간이 지난 것 — job_queue_taken (kind, seq)
RECLAIM = """
WITH c AS (
    SELECT id FROM job_queue
    WHERE kind = %(kind)s AND owner IS NOT NULL AND NOT dead AND taken_at < clock_timestamp() - make_interval(secs => %(idle)s)
    ORDER BY seq LIMIT 1
    FOR UPDATE SKIP LOCKED)
UPDATE job_queue j SET owner = %(me)s, taken_at = clock_timestamp(), attempts = j.attempts + 1,
                       lease = nextval('job_queue_lease_seq')
FROM c WHERE j.id = c.id
RETURNING j.id, j.body, j.attempts, j.lease
"""

# 새 작업: job_queue_new (kind, seq) 맨 앞
TAKE_NEW = """
WITH c AS (
    SELECT id FROM job_queue
    WHERE kind = %(kind)s AND owner IS NULL AND NOT dead
    ORDER BY seq LIMIT 1
    FOR UPDATE SKIP LOCKED)
UPDATE job_queue j SET owner = %(me)s, taken_at = clock_timestamp(), attempts = j.attempts + 1,
                       lease = nextval('job_queue_lease_seq')
FROM c WHERE j.id = c.id
RETURNING j.id, j.body, j.attempts, j.lease
"""

POSITION = """
WITH me AS (SELECT seq, owner IS NOT NULL AS taken, taken_at FROM job_queue WHERE id = %(id)s AND kind = %(kind)s AND NOT dead),
     t AS (SELECT clock_timestamp() - make_interval(secs => %(idle)s) AS cut),
     due AS (SELECT a.seq FROM job_queue a, t WHERE a.kind = %(kind)s AND a.owner IS NOT NULL AND NOT a.dead AND a.taken_at < t.cut)
SELECT CASE
    WHEN NOT me.taken THEN (SELECT count(*) FROM due)
        + (SELECT count(*) FROM job_queue a WHERE a.kind = %(kind)s AND a.owner IS NULL AND NOT a.dead AND a.seq <= me.seq)
    WHEN me.taken_at < (SELECT cut FROM t) THEN (SELECT count(*) FROM due WHERE due.seq <= me.seq)
    END AS n
FROM me
"""

_FENCE = "id = %s AND kind = %s AND owner = %s AND lease = %s AND NOT dead"


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class PgQueue:
    def __init__(self, pool, cfg: QueueConfig, poll: float = 1.0):
        """pool: psycopg_pool.ConnectionPool (autocommit, dict_row — app.store.pool과 같은 설정)"""
        self.pool, self.cfg, self.poll = pool, cfg, poll
        self.on_dead = None  # 죽은 작업으로 뺄 때 부른다: on_dead(kind, 본문 dict|None, 사유) — app.worker가 철 실행을 멈춤으로

    def put(self, jobs: Sequence[NewJob]) -> list[str]:
        if not jobs:
            return []
        groups = group_by_kind(jobs, self.cfg)
        ids: list[str] = []
        kind = None
        try:
            with self.pool.connection() as c, c.transaction():
                # 이 트랜잭션에서만: 잠금을 lock_timeout보다 오래 기다리지 않는다 (LockNotAvailable → 되돌림)
                c.execute("SELECT set_config('lock_timeout', %s, true)", (f"{int(self.cfg.lock_timeout * 1000)}ms",))
                for kind in sorted(groups):  # 잠금 순서를 늘 같게 (교착 방지)
                    c.execute("INSERT INTO job_queue_kind (kind) VALUES (%s) ON CONFLICT DO NOTHING", (kind,))
                    c.execute("SELECT kind FROM job_queue_kind WHERE kind = %s FOR UPDATE", (kind,))
                    n = c.execute("SELECT count(*) AS n FROM job_queue WHERE kind = %s AND NOT dead", (kind,)).fetchone()["n"]
                    check_limit(self.cfg, kind, n, len(groups[kind]))  # 넘으면 예외 → 트랜잭션 되돌림 (하나도 안 넣음)
                for j in jobs:
                    row = c.execute("INSERT INTO job_queue (kind, body) VALUES (%s, %s::jsonb) RETURNING id",
                                    (j.kind, j.body())).fetchone()
                    ids.append(str(row["id"]))
        except pg_errors.LockNotAvailable:
            raise QueueBusy(kind or "?", self.cfg.retry_after) from None
        return ids

    def _to_dead(self, c, kind: str, job_id: int, reason: str) -> None:
        c.execute("""UPDATE job_queue SET dead = true, owner = NULL, taken_at = NULL, dead_at = clock_timestamp(), dead_reason = %s
                     WHERE id = %s""", (reason, job_id))
        if self.cfg.dead_days:
            c.execute("DELETE FROM job_queue WHERE kind = %s AND dead AND dead_at < clock_timestamp() - make_interval(secs => %s)",
                      (kind, self.cfg.dead_days * 86400))
        if self.cfg.dead_keep:
            # dead_at이 없는 행(옛 판이 남긴 죽은 작업)은 가장 오래된 것으로 — 먼저 지운다
            c.execute("""DELETE FROM job_queue WHERE id IN (
                             SELECT id FROM job_queue WHERE kind = %s AND dead
                             ORDER BY dead_at DESC NULLS LAST, id DESC OFFSET %s)""",
                      (kind, self.cfg.dead_keep))

    def _take_once(self, kind: str, consumer: str) -> Job | None:
        args = {"kind": kind, "idle": self.cfg.spec(kind).idle, "me": consumer}
        dead: list[tuple] = []
        try:
            with self.pool.connection() as c:
                while True:
                    row = c.execute(RECLAIM, args).fetchone() or c.execute(TAKE_NEW, args).fetchone()
                    if not row:
                        return None
                    if row["attempts"] > self.cfg.max_attempts:  # 실패·워커 죽음으로 max_attempts번 다 쓴 작업
                        self._to_dead(c, kind, row["id"], "attempts")
                        dead.append((row["body"], "attempts"))
                        continue
                    try:
                        return parse_job(str(row["id"]), kind, consumer, row["attempts"], row["body"], lease=str(row["lease"]))
                    except ValueError:  # 약속에 맞지 않는 본문 — 워커에 넘기지 않고 빼서 뒤 작업을 막지 않는다
                        self._to_dead(c, kind, row["id"], "invalid")
                        dead.append((None, "invalid"))
        finally:
            for body, reason in dead:  # 연결을 돌려준 뒤에 (알림이 같은 pool을 쓴다)
                notify_dead(self.on_dead, kind, body, reason)

    def take(self, kind: str, consumer: str, wait: float = 0.0) -> Job | None:
        end = time.monotonic() + wait
        while True:
            job = self._take_once(kind, consumer)
            left = end - time.monotonic()
            if job or left <= 0:
                return job
            time.sleep(min(self.poll, left))

    def _fence(self, job: Job):
        i, lease = _int(job.id), _int(job.lease)
        return None if i is None or lease is None else (i, job.kind, job.consumer, lease)

    def ack(self, job: Job) -> bool:
        if (f := self._fence(job)) is None:
            return False
        with self.pool.connection() as c:
            return c.execute(f"DELETE FROM job_queue WHERE {_FENCE}", f).rowcount == 1  # noqa: S608 — 고정 문자열만 붙인다

    def _back(self, job: Job, counted: bool) -> str | None:
        if (f := self._fence(job)) is None:
            return None
        # 자발적 되돌리기(release)는 이번 시도를 빼고, 실패(fail)는 센 채로 둔다 → 다음 꺼냄이 max_attempts를 넘으면 죽은 작업
        minus = "" if counted else ", attempts = attempts - 1"
        with self.pool.connection() as c:
            row = c.execute(f"""UPDATE job_queue SET owner = NULL, taken_at = NULL, seq = nextval('job_queue_seq'){minus}
                                WHERE {_FENCE} RETURNING id""", f).fetchone()  # noqa: S608 — 고정 문자열만 붙인다
        return str(row["id"]) if row else None

    def release(self, job: Job) -> str | None:
        return self._back(job, counted=False)

    def fail(self, job: Job) -> str | None:
        return self._back(job, counted=True)

    def heartbeat(self, job: Job) -> bool:
        if (f := self._fence(job)) is None:
            return False
        with self.pool.connection() as c:
            return c.execute(f"UPDATE job_queue SET taken_at = clock_timestamp() WHERE {_FENCE}",  # noqa: S608
                             f).rowcount == 1

    def stats(self, kind: str) -> QueueStats:
        idle = self.cfg.spec(kind).idle
        with self.pool.connection() as c:
            r = c.execute("""WITH t AS (SELECT clock_timestamp() - make_interval(secs => %(idle)s) AS cut)
                              SELECT count(*) FILTER (WHERE NOT dead AND (owner IS NULL OR taken_at < t.cut)) AS waiting,
                                     count(*) FILTER (WHERE NOT dead AND owner IS NOT NULL AND taken_at >= t.cut) AS running,
                                     count(*) FILTER (WHERE dead) AS dead
                              FROM job_queue, t WHERE kind = %(kind)s""", {"kind": kind, "idle": idle}).fetchone()
        return QueueStats(r["waiting"], r["running"], r["dead"])

    def count_for(self, vol: str) -> int:
        with self.pool.connection() as c:
            return c.execute("SELECT count(*) AS n FROM job_queue WHERE NOT dead AND body->>'vol' = %s",
                             (vol,)).fetchone()["n"]

    def position(self, kind: str, job_id: str) -> int | None:
        idle = self.cfg.spec(kind).idle
        if (i := _int(job_id)) is None:
            return None
        with self.pool.connection() as c:
            r = c.execute(POSITION, {"id": i, "kind": kind, "idle": idle}).fetchone()
        return r["n"] if r and r["n"] else None
