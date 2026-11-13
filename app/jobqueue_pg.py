"""대기열 Postgres 구현 — 로컬 k3s 1노드용 (https://github.com/reddol18/docflow/issues/5). 약속은 app/jobqueue.py.

꺼내기: SELECT … FOR UPDATE SKIP LOCKED로 행 하나를 잡아 owner·taken_at을 쓴다 (워커 여럿이 같은 행을 잡지 않는다).
되찾기: owner가 있어도 taken_at(꺼낸 때·마지막 살아 있음 표시)이 유휴 시간보다 오래면 다시 꺼낼 수 있다 — 되찾을 것을 먼저.
상한: 종류별 잠금 행(job_queue_kind)을 FOR UPDATE로 잡고 센 뒤 넣는다 (동시에 넣어도 상한을 넘지 않게).
완료한 행은 지운다. 죽은 작업(시도 횟수 초과)은 dead=true로 남긴다 (상한에 세지 않음).
시간은 DB 서버 시계(clock_timestamp)만 쓴다 — 워커 Pod 시계가 달라도 되찾기 판단이 같다.
"""

from __future__ import annotations

import time
from typing import Sequence

from app.jobqueue import Job, NewJob, QueueConfig, QueueStats, check_limit, group_by_kind

SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS job_queue_seq;
CREATE TABLE IF NOT EXISTS job_queue (
    id bigserial PRIMARY KEY,
    seq bigint NOT NULL DEFAULT nextval('job_queue_seq'),
    kind text NOT NULL,
    body jsonb NOT NULL,
    attempts int NOT NULL DEFAULT 0,
    owner text,
    taken_at timestamptz,
    dead boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS job_queue_take ON job_queue (kind, seq) WHERE NOT dead;
CREATE TABLE IF NOT EXISTS job_queue_kind (kind text PRIMARY KEY);
"""

TAKE = """
WITH c AS (
    SELECT id FROM job_queue
    WHERE kind = %(kind)s AND NOT dead
      AND (owner IS NULL OR taken_at < clock_timestamp() - make_interval(secs => %(idle)s))
    ORDER BY owner IS NULL, seq
    LIMIT 1
    FOR UPDATE SKIP LOCKED)
UPDATE job_queue j SET owner = %(me)s, taken_at = clock_timestamp(), attempts = j.attempts + 1
FROM c WHERE j.id = c.id
RETURNING j.id, j.body, j.attempts
"""


class PgQueue:
    def __init__(self, pool, cfg: QueueConfig, poll: float = 1.0):
        """pool: psycopg_pool.ConnectionPool (autocommit, dict_row — app.store.pool과 같은 설정)"""
        self.pool, self.cfg, self.poll = pool, cfg, poll

    def init_schema(self) -> None:
        with self.pool.connection() as c:
            for stmt in filter(str.strip, SCHEMA.split(";")):
                c.execute(stmt)

    def put(self, jobs: Sequence[NewJob]) -> list[str]:
        groups = group_by_kind(jobs, self.cfg)
        ids: list[str] = []
        with self.pool.connection() as c, c.transaction():
            for kind in sorted(groups):  # 잠금 순서를 늘 같게 (교착 방지)
                c.execute("INSERT INTO job_queue_kind (kind) VALUES (%s) ON CONFLICT DO NOTHING", (kind,))
                c.execute("SELECT kind FROM job_queue_kind WHERE kind = %s FOR UPDATE", (kind,))
                n = c.execute("SELECT count(*) AS n FROM job_queue WHERE kind = %s AND NOT dead", (kind,)).fetchone()["n"]
                check_limit(self.cfg, kind, n, len(groups[kind]))  # 넘으면 예외 → 트랜잭션 되돌림 (하나도 안 넣음)
            for j in jobs:
                row = c.execute("INSERT INTO job_queue (kind, body) VALUES (%s, %s::jsonb) RETURNING id",
                                (j.kind, j.body())).fetchone()
                ids.append(str(row["id"]))
        return ids

    def _take_once(self, kind: str, consumer: str) -> Job | None:
        idle = self.cfg.spec(kind).idle
        with self.pool.connection() as c:
            while True:
                row = c.execute(TAKE, {"kind": kind, "idle": idle, "me": consumer}).fetchone()
                if not row:
                    return None
                if row["attempts"] > self.cfg.max_attempts:  # 여러 번 워커를 죽인 작업 — 다시 돌리지 않는다
                    c.execute("UPDATE job_queue SET dead = true, owner = NULL WHERE id = %s", (row["id"],))
                    continue
                return Job.from_body(str(row["id"]), kind, consumer, row["attempts"], row["body"])

    def take(self, kind: str, consumer: str, wait: float = 0.0) -> Job | None:
        end = time.monotonic() + wait
        while True:
            job = self._take_once(kind, consumer)
            left = end - time.monotonic()
            if job or left <= 0:
                return job
            time.sleep(min(self.poll, left))

    def ack(self, job: Job) -> bool:
        with self.pool.connection() as c:
            return c.execute("DELETE FROM job_queue WHERE id = %s AND kind = %s", (int(job.id), job.kind)).rowcount == 1

    def release(self, job: Job) -> str | None:
        with self.pool.connection() as c:
            row = c.execute("""UPDATE job_queue SET owner = NULL, taken_at = NULL, seq = nextval('job_queue_seq')
                               WHERE id = %s AND kind = %s AND owner = %s AND NOT dead RETURNING id""",
                            (int(job.id), job.kind, job.consumer)).fetchone()
        return str(row["id"]) if row else None

    def heartbeat(self, job: Job) -> bool:
        with self.pool.connection() as c:
            return c.execute("""UPDATE job_queue SET taken_at = clock_timestamp()
                                WHERE id = %s AND kind = %s AND owner = %s AND NOT dead""",
                             (int(job.id), job.kind, job.consumer)).rowcount == 1

    def stats(self, kind: str) -> QueueStats:
        self.cfg.spec(kind)
        with self.pool.connection() as c:
            r = c.execute("""SELECT count(*) FILTER (WHERE owner IS NULL AND NOT dead) AS waiting,
                                    count(*) FILTER (WHERE owner IS NOT NULL AND NOT dead) AS running,
                                    count(*) FILTER (WHERE dead) AS dead
                             FROM job_queue WHERE kind = %s""", (kind,)).fetchone()
        return QueueStats(r["waiting"], r["running"], r["dead"])

    def position(self, kind: str, job_id: str) -> int | None:
        with self.pool.connection() as c:
            r = c.execute("""SELECT count(*) AS n FROM job_queue a
                             JOIN job_queue me ON me.id = %s AND me.kind = %s AND me.owner IS NULL AND NOT me.dead
                             WHERE a.kind = me.kind AND a.owner IS NULL AND NOT a.dead AND a.seq <= me.seq""",
                          (int(job_id), kind)).fetchone()
        return r["n"] or None
