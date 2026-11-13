"""대기열 Redis Streams 구현 — 클라우드 기준 (https://github.com/reddol18/docflow/issues/5). 약속은 app/jobqueue.py.

redis 클라이언트는 이 모듈만 불러온다. 앱은 QUEUE_BACKEND=redis일 때만 이 모듈을 불러온다 (app.jobqueue.open_queue)
— 이미지 의존성(requirements.txt·lock)에 redis를 넣는 것은 2단계 (지금은 시험 환경 requirements-test.txt에만).

키: <prefix>:<종류> 스트림 하나 + 컨슈머 그룹 "workers", 죽은 작업은 <prefix>:<종류>:dead 스트림.
  넣기    WATCH로 XLEN(=ack 전 전부, 완료하면 XDEL하므로)을 보고 상한 안이면 MULTI 안에서 XADD (동시에 넣어도 넘지 않게)
  꺼내기  XAUTOCLAIM(유휴 시간 지난 것을 이 컨슈머로)을 먼저, 없으면 XREADGROUP '>'
  완료    XACK + XDEL (한 트랜잭션)
  되돌리기 같은 본문을 맨 뒤에 XADD(지금까지 시도 횟수 a를 실어) + 옛 항목 XACK·XDEL
  살아 있음 XCLAIM JUSTID(자기 자신에게, 전달 횟수는 늘지 않고 유휴 시계만 0)
  시도 횟수 = 항목의 a + 그 항목의 전달 횟수(XPENDING times_delivered)
시간은 Redis 서버 시계만 쓴다 (유휴 시간 판단).
"""

from __future__ import annotations

import time
from typing import Sequence

import redis

from app.jobqueue import Job, NewJob, QueueConfig, QueueStats, check_limit, group_by_kind

GROUP = "workers"
DEAD_MAXLEN = 10_000


def _sid(s: str) -> tuple[int, int]:
    ms, _, seq = s.partition("-")
    return int(ms), int(seq or 0)


class RedisQueue:
    def __init__(self, client: redis.Redis, cfg: QueueConfig, prefix: str = "docflow:q", poll: float = 1.0):
        """client는 decode_responses=True"""
        self.r, self.cfg, self.prefix, self.poll = client, cfg, prefix, poll
        self._ready: set[str] = set()

    @classmethod
    def from_url(cls, url: str, cfg: QueueConfig, prefix: str = "docflow:q") -> RedisQueue:
        # socket_timeout은 XREADGROUP BLOCK(poll, 1초)보다 길게
        return cls(redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=5, socket_timeout=10,
                                        health_check_interval=30), cfg, prefix)

    def key(self, kind: str) -> str:
        self.cfg.spec(kind)
        return f"{self.prefix}:{kind}"

    def _ensure(self, kind: str, force: bool = False) -> str:
        k = self.key(kind)
        if force or k not in self._ready:
            try:
                self.r.xgroup_create(k, GROUP, id="0", mkstream=True)
            except redis.ResponseError as e:
                if "BUSYGROUP" not in str(e):
                    raise
            self._ready.add(k)
        return k

    def put(self, jobs: Sequence[NewJob]) -> list[str]:
        groups = group_by_kind(jobs, self.cfg)
        keys = {kind: self._ensure(kind) for kind in sorted(groups)}
        with self.r.pipeline() as p:
            while True:
                try:
                    p.watch(*keys.values())
                    for kind, k in keys.items():
                        check_limit(self.cfg, kind, p.xlen(k), len(groups[kind]))  # 넘으면 예외 — 아무것도 넣지 않음
                    p.multi()
                    for j in jobs:
                        p.xadd(keys[j.kind], {"body": j.body(), "a": 0})
                    return [str(x) for x in p.execute()]
                except redis.WatchError:
                    continue  # 그 사이 다른 넣기·완료가 있었다 — 다시 센다

    def _delivered(self, k: str, msg_id: str) -> int:
        rows = self.r.xpending_range(k, GROUP, min=msg_id, max=msg_id, count=1)
        return int(rows[0]["times_delivered"]) if rows else 1

    def _owner(self, client, k: str, msg_id: str) -> str | None:
        rows = client.xpending_range(k, GROUP, min=msg_id, max=msg_id, count=1)
        return rows[0]["consumer"] if rows else None

    def _to_dead(self, kind: str, msg_id: str, fields: dict) -> None:
        k = self.key(kind)
        with self.r.pipeline(transaction=True) as p:
            p.xadd(f"{k}:dead", {**fields, "id": msg_id}, maxlen=DEAD_MAXLEN, approximate=True)
            p.xack(k, GROUP, msg_id)
            p.xdel(k, msg_id)
            p.execute()

    def _job(self, kind: str, consumer: str, msg_id: str, fields: dict) -> Job | None:
        attempts = int(fields.get("a", 0)) + self._delivered(self.key(kind), msg_id)
        if attempts > self.cfg.max_attempts:  # 여러 번 워커를 죽인 작업 — 다시 돌리지 않는다
            self._to_dead(kind, msg_id, fields)
            return None
        return Job.from_body(msg_id, kind, consumer, attempts, fields["body"])

    def _reclaim(self, kind: str, consumer: str) -> Job | None:
        k = self.key(kind)
        idle_ms = int(self.cfg.spec(kind).idle * 1000)
        cursor = "0-0"
        while True:
            # count=1: 한 번에 하나만 되찾는다 (쥐고 못 돌릴 것을 가져오지 않게). 한 번에 PEL을 10개까지 훑으므로 커서로 이어 본다
            res = self.r.xautoclaim(k, GROUP, consumer, min_idle_time=idle_ms, start_id=cursor, count=1)
            cursor, msgs = res[0], res[1]
            for msg_id, fields in msgs:
                if not fields:  # 지워진 항목 (Redis 7은 세 번째 칸으로 따로 준다)
                    continue
                job = self._job(kind, consumer, msg_id, fields)
                if job:
                    return job
            if cursor == "0-0":
                return None

    def _take_once(self, kind: str, consumer: str, block_ms: int | None) -> Job | None:
        k = self._ensure(kind)
        job = self._reclaim(kind, consumer)
        if job:
            return job
        while True:
            got = self.r.xreadgroup(GROUP, consumer, {k: ">"}, count=1, block=block_ms)
            if not got or not got[0][1]:
                return None
            msg_id, fields = got[0][1][0]
            job = self._job(kind, consumer, msg_id, fields)
            if job:
                return job
            block_ms = None  # 죽은 작업으로 뺐으면 기다리지 않고 다음 것을 본다

    def take(self, kind: str, consumer: str, wait: float = 0.0) -> Job | None:
        end = time.monotonic() + wait
        while True:
            left = end - time.monotonic()
            block = int(min(self.poll, left) * 1000) if left > 0 else None
            try:
                job = self._take_once(kind, consumer, block or None)
            except redis.ResponseError as e:
                if "NOGROUP" not in str(e):
                    raise
                self._ensure(kind, force=True)  # 스트림이 지워졌다(Redis 초기화 등) — 그룹을 다시 만든다
                continue
            if job or end - time.monotonic() <= 0:
                return job

    def ack(self, job: Job) -> bool:
        k = self.key(job.kind)
        with self.r.pipeline(transaction=True) as p:
            p.xack(k, GROUP, job.id)
            p.xdel(k, job.id)
            _, deleted = p.execute()
        return deleted == 1

    def release(self, job: Job) -> str | None:
        k = self.key(job.kind)
        with self.r.pipeline() as p:
            while True:
                try:
                    p.watch(k)
                    entry = p.xrange(k, min=job.id, max=job.id, count=1)
                    if not entry or self._owner(p, k, job.id) != job.consumer:
                        return None  # 이미 끝났거나 다른 워커가 되찾았다
                    p.multi()
                    p.xadd(k, {"body": entry[0][1]["body"], "a": job.attempts})
                    p.xack(k, GROUP, job.id)
                    p.xdel(k, job.id)
                    return str(p.execute()[0])
                except redis.WatchError:
                    continue

    def heartbeat(self, job: Job) -> bool:
        k = self.key(job.kind)
        with self.r.pipeline() as p:
            while True:
                try:
                    p.watch(k)
                    if self._owner(p, k, job.id) != job.consumer:
                        return False  # 다른 워커가 되찾았거나 이미 끝났다 — 이 작업은 그만둔다
                    p.multi()
                    p.xclaim(k, GROUP, job.consumer, min_idle_time=0, message_ids=[job.id], justid=True)
                    return bool(p.execute()[0])
                except redis.WatchError:
                    continue

    def stats(self, kind: str) -> QueueStats:
        k = self._ensure(kind)
        total = self.r.xlen(k)
        running = int(self.r.xpending(k, GROUP)["pending"])
        return QueueStats(waiting=total - running, running=running, dead=self.r.xlen(f"{k}:dead"))

    def position(self, kind: str, job_id: str) -> int | None:
        k = self._ensure(kind)
        if not self.r.xrange(k, min=job_id, max=job_id, count=1):
            return None
        last = next((g["last-delivered-id"] for g in self.r.xinfo_groups(k) if g["name"] == GROUP), "0-0")
        if _sid(job_id) <= _sid(last):
            return None  # 이미 꺼냈다 (진행 중이거나 되찾기를 기다림)
        return len(self.r.xrange(k, min=f"({last}", max=job_id))
