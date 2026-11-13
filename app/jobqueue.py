"""실행 대기열 인터페이스 (https://github.com/reddol18/docflow/issues/5 1단계).

구현 두 가지가 같은 약속을 지킨다 — 시험도 같은 것을 둘에 돌린다 (tests/test_jobqueue.py):
  - Redis Streams (app/jobqueue_redis.py): 종류마다 스트림 하나 + 컨슈머 그룹, 죽은 워커의 작업은 XAUTOCLAIM으로 되찾는다.
    클라우드 기준(관리형 Redis/Valkey). redis 클라이언트는 그 모듈 안에서만 불러온다 — 이 모듈·앱 기동 경로는 redis 없이 돈다.
  - Postgres (app/jobqueue_pg.py): SELECT … FOR UPDATE SKIP LOCKED, 유휴 시간이 지난 작업을 되찾는다. 로컬 k3s 1노드용.

약속 (두 구현 공통):
  put(jobs)              넣기. 여러 개를 한꺼번에 — 상한을 넘으면 하나도 넣지 않고 QueueFull(retry_after)
  take(kind, consumer)   꺼내기. 유휴 시간이 지난(ack도 살아 있음 표시도 없는) 작업을 먼저 되찾고, 없으면 새 작업.
                         시도 횟수가 max_attempts를 넘은 작업은 꺼내지 않고 죽은 작업으로 뺀다 (dead로 셈)
  ack(job)               완료. 대기열에서 지운다 (누가 쥐고 있든 — 작업은 두 번 돌아도 같은 결과여야 한다: 최소 한 번 전달)
  release(job)           되돌리기. 맨 뒤로 다시 넣는다 (시도 횟수는 그대로 이어 센다)
  heartbeat(job)         살아 있음 표시. 유휴 시계를 0으로. 이미 다른 워커가 되찾았으면 False (그 작업은 그만둔다)
  stats(kind)            길이: waiting(아직 안 꺼낸) · running(꺼내서 ack 전) · dead
  position(job_id)       순번: 아직 안 꺼낸 작업이면 1부터(앞에 기다리는 작업 수 + 1), 꺼냈거나 없으면 None

값 (통제자 결정 2026-11-13, https://github.com/reddol18/docflow/issues/5 댓글 — 모두 환경 변수로 바꾼다):
  QUEUE_LIMIT_OCR_PAGE=2000   쪽 OCR 작업 상한. 넘으면 429 + Retry-After: QUEUE_RETRY_AFTER(30)
  QUEUE_IDLE_OCR_PAGE=120     쪽 OCR 작업 되찾기 유휴 시간(초)
  QUEUE_IDLE_GRAPH=180        그래프 작업 되찾기 유휴 시간(초), QUEUE_HEARTBEAT_GRAPH=30마다 살아 있음 표시
  QUEUE_IDLE_ASK=60 · QUEUE_LIMIT_ASK=0(없음) · QUEUE_MAX_ATTEMPTS=5   ← 결정 전 임시값 (2단계에서 정한다)
  상한 0 = 상한 없음.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Protocol, Sequence

OCR_PAGE = "ocr_page"  # 쪽 하나 OCR — vol·page 필수
GRAPH = "graph"        # 철 하나의 LangGraph 그래프 (그 철의 쪽 OCR이 모두 끝난 뒤) — vol 필수
ASK = "ask"            # /ask 요청 하나 — ref(요청 id) 필수. 질문 본문은 DB에 두고 대기열에는 id만


@dataclass(frozen=True)
class KindSpec:
    idle: float              # 이 초 동안 ack·살아 있음 표시가 없으면 다른 워커가 되찾는다
    limit: int = 0           # 대기열(ack 전 전부) 상한. 0 = 없음
    heartbeat: float = 0.0   # 워커가 이 초마다 heartbeat()를 부른다 (0 = 부르지 않음 — idle 안에 끝나는 작업)


def _num(env, name, default, cast=float):
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    v = cast(raw)
    if v < 0:
        raise ValueError(f"{name}는 0 이상이어야 합니다")
    return v


@dataclass(frozen=True)
class QueueConfig:
    kinds: dict[str, KindSpec]
    retry_after: int = 30
    max_attempts: int = 5

    @classmethod
    def from_env(cls, env=os.environ) -> QueueConfig:
        kinds = {
            OCR_PAGE: KindSpec(idle=_num(env, "QUEUE_IDLE_OCR_PAGE", 120.0),
                               limit=_num(env, "QUEUE_LIMIT_OCR_PAGE", 2000, int)),
            GRAPH: KindSpec(idle=_num(env, "QUEUE_IDLE_GRAPH", 180.0),
                            limit=_num(env, "QUEUE_LIMIT_GRAPH", 0, int),
                            heartbeat=_num(env, "QUEUE_HEARTBEAT_GRAPH", 30.0)),
            ASK: KindSpec(idle=_num(env, "QUEUE_IDLE_ASK", 60.0), limit=_num(env, "QUEUE_LIMIT_ASK", 0, int)),
        }
        for name, k in kinds.items():
            if k.idle <= 0:
                raise ValueError(f"{name} 유휴 시간은 0보다 커야 합니다")
            if k.heartbeat and k.heartbeat >= k.idle:
                raise ValueError(f"{name} 살아 있음 표시 간격({k.heartbeat})이 유휴 시간({k.idle})보다 짧아야 합니다")
        return cls(kinds=kinds, retry_after=_num(env, "QUEUE_RETRY_AFTER", 30, int),
                   max_attempts=max(1, _num(env, "QUEUE_MAX_ATTEMPTS", 5, int)))

    def spec(self, kind: str) -> KindSpec:
        try:
            return self.kinds[kind]
        except KeyError:
            raise ValueError(f"모르는 작업 종류: {kind!r}") from None


@dataclass(frozen=True)
class NewJob:
    """넣을 작업. 대기열에는 이 칸들만 JSON으로 들어간다 (파일·본문은 저장소·DB에 두고 이름·id만)"""
    kind: str
    vol: str | None = None
    page: str | None = None
    ref: str | None = None   # ask 요청 id 등. 개인정보·질문 본문을 넣지 않는다

    def validate(self) -> None:
        if self.kind == OCR_PAGE and not (self.vol and self.page):
            raise ValueError("ocr_page 작업에는 vol·page가 필요합니다")
        if self.kind == GRAPH and not self.vol:
            raise ValueError("graph 작업에는 vol이 필요합니다")
        if self.kind == ASK and not self.ref:
            raise ValueError("ask 작업에는 ref가 필요합니다")
        for v in (self.vol, self.page, self.ref):
            if v is not None and (not isinstance(v, str) or len(v) > 256):
                raise ValueError("vol·page·ref는 256자 이하 문자열")

    def body(self) -> str:
        return json.dumps({k: v for k, v in (("vol", self.vol), ("page", self.page), ("ref", self.ref)) if v is not None},
                          ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class Job:
    """꺼낸 작업. id는 구현이 매긴다 (Redis 스트림 id / Postgres 행 id, 문자열). attempts는 이번 꺼냄을 포함한 시도 횟수"""
    id: str
    kind: str
    consumer: str
    attempts: int
    vol: str | None = None
    page: str | None = None
    ref: str | None = None

    @classmethod
    def from_body(cls, id: str, kind: str, consumer: str, attempts: int, body: str | dict) -> Job:
        d = json.loads(body) if isinstance(body, str) else dict(body)
        return cls(id=id, kind=kind, consumer=consumer, attempts=attempts,
                   vol=d.get("vol"), page=d.get("page"), ref=d.get("ref"))

    def as_new(self) -> NewJob:
        return NewJob(kind=self.kind, vol=self.vol, page=self.page, ref=self.ref)


@dataclass(frozen=True)
class QueueStats:
    waiting: int
    running: int
    dead: int = 0

    @property
    def length(self) -> int:
        """상한과 비교하는 길이 = ack 전 전부 (기다림 + 꺼냄). 죽은 작업은 세지 않는다"""
        return self.waiting + self.running


class QueueFull(Exception):
    """상한 초과 — API는 429 + Retry-After: retry_after로 바꾼다 (2단계). 넣은 것은 없다"""

    def __init__(self, kind: str, length: int, adding: int, limit: int, retry_after: int):
        super().__init__(f"{kind} 대기열이 가득 찼습니다 ({length}+{adding} > {limit})")
        self.kind, self.length, self.adding, self.limit, self.retry_after = kind, length, adding, limit, retry_after

    def headers(self) -> dict[str, str]:
        return {"Retry-After": str(self.retry_after)}


def check_limit(cfg: QueueConfig, kind: str, length: int, adding: int) -> None:
    lim = cfg.spec(kind).limit
    if lim and length + adding > lim:
        raise QueueFull(kind, length, adding, lim, cfg.retry_after)


def group_by_kind(jobs: Sequence[NewJob], cfg: QueueConfig) -> dict[str, list[NewJob]]:
    out: dict[str, list[NewJob]] = {}
    for j in jobs:
        cfg.spec(j.kind)
        j.validate()
        out.setdefault(j.kind, []).append(j)
    return out


class JobQueue(Protocol):
    cfg: QueueConfig

    def put(self, jobs: Sequence[NewJob]) -> list[str]: ...
    def take(self, kind: str, consumer: str, wait: float = 0.0) -> Job | None: ...
    def ack(self, job: Job) -> bool: ...
    def release(self, job: Job) -> str | None: ...  # 새 id. 이미 끝났거나 다른 워커가 되찾았으면 None
    def heartbeat(self, job: Job) -> bool: ...
    def stats(self, kind: str) -> QueueStats: ...
    def position(self, kind: str, job_id: str) -> int | None: ...


def open_queue(env=os.environ) -> JobQueue:
    """QUEUE_BACKEND=postgres(기본)|redis. redis는 여기서 고를 때만 불러온다 (이미지에 redis가 없어도 앱이 뜬다)"""
    cfg = QueueConfig.from_env(env)
    backend = env.get("QUEUE_BACKEND", "postgres")
    if backend == "redis":
        from app import jobqueue_redis
        return jobqueue_redis.RedisQueue.from_url(env["REDIS_URL"], cfg, prefix=env.get("QUEUE_PREFIX", "docflow:q"))
    if backend == "postgres":
        from app import jobqueue_pg, store
        return jobqueue_pg.PgQueue(store.pool, cfg)
    raise ValueError(f"QUEUE_BACKEND는 postgres 또는 redis: {backend!r}")
