"""실행 대기열 인터페이스 (https://github.com/reddol18/docflow/issues/5 1단계).

구현 두 가지가 같은 약속을 지킨다 — 시험도 같은 것을 둘에 돌린다 (tests/test_jobqueue.py):
  - Redis Streams (app/jobqueue_redis.py): 종류마다 스트림 하나 + 컨슈머 그룹, 죽은 워커의 작업은 XAUTOCLAIM으로 되찾는다.
    클라우드 기준(관리형 Redis/Valkey). redis 클라이언트는 그 모듈 안에서만 불러온다 — 이 모듈·앱 기동 경로는 redis 없이 돈다.
  - Postgres (app/jobqueue_pg.py): SELECT … FOR UPDATE SKIP LOCKED, 유휴 시간이 지난 작업을 되찾는다. 로컬 k3s 1노드용.
    표는 app/store.py SCHEMA(init_db)가 만든다.

약속 (두 구현 공통):
  put(jobs)              넣기. 여러 개를 한꺼번에 — 상한을 넘으면 하나도 넣지 않고 QueueFull(retry_after). 빈 묶음은 []
  take(kind, consumer)   꺼내기. 되찾을 차례인(유휴 시간 동안 ack·살아 있음 표시가 없는) 작업을 먼저, 없으면 새 작업.
                         시도 횟수가 max_attempts를 넘었거나 본문이 약속(NewJob)에 맞지 않는 작업은 꺼내지 않고 죽은 작업으로 뺀다
  ack(job)               완료. 지금도 이 워커가 이 시도(attempts)로 쥐고 있을 때만 지운다 → True.
                         늦게 끝난 옛 워커는 새 워커가 되찾은 작업을 지우지 못한다 (False). 작업은 두 번 돌아도 같은 결과여야 한다
  release(job)           되돌리기(자발적). 맨 뒤로 다시 넣고, 이번 시도는 시도 횟수에서 뺀다 (실패·죽음과 구분). 주인·임대가 다르면 None
  fail(job)              실패. 맨 뒤로 다시 넣되 이번 시도는 센 채로 — max_attempts를 다 쓰면 다음 꺼냄에서 죽은 작업
  heartbeat(job)         살아 있음 표시. 유휴 시계를 0으로. 이미 다른 워커가 되찾았으면 False (그 작업은 그만둔다)
  on_dead                (속성) 죽은 작업으로 뺄 때 부르는 함수 on_dead(kind, 본문 dict|None, 사유 'attempts'|'invalid')
  stats(kind)            길이: waiting(꺼낼 수 있는 = 새 작업 + 되찾을 차례) · running(쥐고 있고 유휴 시간 안) · dead
  position(kind, id)     순번: 꺼낼 수 있는 작업이면 1부터 (되찾을 차례인 작업이 새 작업보다 앞), 쥐고 있거나 없으면 None
  주인 확인(ack·release·fail·heartbeat)은 job.consumer와 job.lease(임대 번호, 펜싱 토큰)를 함께 본다 — 확인과 동작은 원자적으로.
  lease는 꺼낼 때마다 새 값이다 (Postgres: 단조 증가 시퀀스, Redis: 시도 횟수 — Redis는 release가 새 항목 id를 만들어 겹치지 않는다).
  consumer 이름은 consumer_name()으로 프로세스마다 유일하게.

값 (통제자 결정 2026-11-13, https://github.com/reddol18/docflow/issues/5 댓글 — 모두 환경 변수로 바꾼다):
  QUEUE_LIMIT_OCR_PAGE=2000   쪽 OCR 작업 상한. 넘으면 429 + Retry-After: QUEUE_RETRY_AFTER(30)
  QUEUE_IDLE_OCR_PAGE=120     쪽 OCR 작업 되찾기 유휴 시간(초)
  QUEUE_IDLE_GRAPH=180        그래프 작업 되찾기 유휴 시간(초), QUEUE_HEARTBEAT_GRAPH=30마다 살아 있음 표시
  QUEUE_MAX_ATTEMPTS=3 · QUEUE_LIMIT_GRAPH=200 · QUEUE_LIMIT_ASK=100 · QUEUE_IDLE_ASK=60   ← 2단계 전 결정 (2026-11-13 이슈 댓글)
  QUEUE_DEAD_KEEP=1000 · QUEUE_DEAD_DAYS=14   죽은 작업 보존 (종류마다 최근 개수·기간, 넘으면 지운다)
  QUEUE_LOCK_TIMEOUT=5        (Postgres) 넣기가 상한 잠금을 기다리는 최대 초 — 넘으면 QueueBusy (pool 연결을 오래 쥐지 않게)
  상한 0 = 상한 없음.
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from dataclasses import dataclass
from typing import Protocol, Sequence

OCR_PAGE = "ocr_page"  # 쪽 하나 OCR — vol·page 필수
GRAPH = "graph"        # 철 하나의 LangGraph 그래프 (그 철의 쪽 OCR이 모두 끝난 뒤) — vol 필수
ASK = "ask"            # /ask 요청 하나 — ref(요청 id) 필수. 질문 본문은 DB에 두고 대기열에는 id만
FIELD_MAX = 256        # vol·page·ref 길이 상한
BODY_MAX = 1024        # 본문(JSON) 길이 상한 — 꺼낼 때 다시 본다


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
    max_attempts: int = 3
    dead_keep: int = 1000
    dead_days: float = 14.0
    lock_timeout: float = 5.0

    @classmethod
    def from_env(cls, env=os.environ) -> QueueConfig:
        kinds = {
            OCR_PAGE: KindSpec(idle=_num(env, "QUEUE_IDLE_OCR_PAGE", 120.0),
                               limit=_num(env, "QUEUE_LIMIT_OCR_PAGE", 2000, int)),
            GRAPH: KindSpec(idle=_num(env, "QUEUE_IDLE_GRAPH", 180.0),
                            limit=_num(env, "QUEUE_LIMIT_GRAPH", 200, int),
                            heartbeat=_num(env, "QUEUE_HEARTBEAT_GRAPH", 30.0)),
            # ask도 살아 있음 표시(20초): 답변 LLM이 늦어도(최악 시간 > 유휴 60초) 다른 워커가 같은 요청을 되찾지 않게
            ASK: KindSpec(idle=_num(env, "QUEUE_IDLE_ASK", 60.0), limit=_num(env, "QUEUE_LIMIT_ASK", 100, int),
                          heartbeat=_num(env, "QUEUE_HEARTBEAT_ASK", 20.0)),
        }
        for name, k in kinds.items():
            if k.idle <= 0:
                raise ValueError(f"{name} 유휴 시간은 0보다 커야 합니다")
            if k.heartbeat and k.heartbeat >= k.idle:
                raise ValueError(f"{name} 살아 있음 표시 간격({k.heartbeat})이 유휴 시간({k.idle})보다 짧아야 합니다")
        lock_timeout = _num(env, "QUEUE_LOCK_TIMEOUT", 5.0)
        if lock_timeout <= 0:
            raise ValueError("QUEUE_LOCK_TIMEOUT은 0보다 커야 합니다 (0이면 Postgres가 끝없이 기다린다)")
        return cls(kinds=kinds, retry_after=_num(env, "QUEUE_RETRY_AFTER", 30, int),
                   max_attempts=max(1, _num(env, "QUEUE_MAX_ATTEMPTS", 3, int)),
                   dead_keep=_num(env, "QUEUE_DEAD_KEEP", 1000, int), dead_days=_num(env, "QUEUE_DEAD_DAYS", 14.0),
                   lock_timeout=lock_timeout)

    def spec(self, kind: str) -> KindSpec:
        try:
            return self.kinds[kind]
        except (KeyError, TypeError):
            raise ValueError("모르는 작업 종류") from None


def consumer_name(role: str = "worker") -> str:
    """워커(프로세스)마다 유일한 consumer 이름 — Pod 이름(hostname) + 임의 값. 같은 이름을 두 프로세스가 쓰면 주인 확인이 무너진다"""
    host = "".join(ch for ch in socket.gethostname() if ch.isalnum() or ch in "-.")[:63] or "host"
    return f"{role}-{host}-{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class NewJob:
    """넣을 작업. 대기열에는 이 칸들만 JSON으로 들어간다 (파일·본문은 저장소·DB에 두고 이름·id만)"""
    kind: str
    vol: str | None = None
    page: str | None = None
    ref: str | None = None   # ask 요청 id 등. 개인정보·질문 본문을 넣지 않는다

    def validate(self) -> None:
        if self.kind not in (OCR_PAGE, GRAPH, ASK):
            raise ValueError("모르는 작업 종류")
        for v in (self.vol, self.page, self.ref):
            if v is not None and (not isinstance(v, str) or not v or len(v) > FIELD_MAX):
                raise ValueError(f"vol·page·ref는 1~{FIELD_MAX}자 문자열")
        if self.kind == OCR_PAGE and not (self.vol and self.page):
            raise ValueError("ocr_page 작업에는 vol·page가 필요합니다")
        if self.kind == GRAPH and not self.vol:
            raise ValueError("graph 작업에는 vol이 필요합니다")
        if self.kind == ASK and not self.ref:
            raise ValueError("ask 작업에는 ref가 필요합니다")

    def body(self) -> str:
        return json.dumps({k: v for k, v in (("vol", self.vol), ("page", self.page), ("ref", self.ref)) if v is not None},
                          ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class Job:
    """꺼낸 작업. id는 구현이 매긴다 (Redis 스트림 id / Postgres 행 id, 문자열).
    attempts는 이번 꺼냄을 포함한 시도 횟수. lease는 이번 꺼냄의 임대 번호(펜싱 토큰) — ack·release·fail·heartbeat가
    consumer와 함께 확인한다"""
    id: str
    kind: str
    consumer: str
    attempts: int
    vol: str | None = None
    page: str | None = None
    ref: str | None = None
    lease: str = ""


def notify_dead(fn, kind: str, body, reason: str) -> None:
    """죽은 작업 알림 (on_dead). 본문은 약속 칸(vol·page·ref)만 문자열일 때 넘긴다. 알림이 실패해도 꺼내기는 멈추지 않는다"""
    if fn is None:
        return
    if isinstance(body, str):
        try:
            body = json.loads(body) if len(body) <= BODY_MAX else None
        except json.JSONDecodeError:
            body = None
    if isinstance(body, dict):
        body = {k: v for k, v in body.items() if k in ("vol", "page", "ref") and isinstance(v, str) and len(v) <= FIELD_MAX}
    else:
        body = None
    try:
        fn(kind, body, reason)
    except Exception as e:  # noqa: BLE001 — 알림 실패가 대기열을 막지 않게 (상세는 서버 로그)
        print(f"[queue] on_dead 실패: {type(e).__name__}", flush=True)


def parse_job(job_id: str, kind: str, consumer: str, attempts: int, body, lease: str | None = None) -> Job:
    """꺼낸 본문을 다시 검사한다 — 대기열에 누가 무엇을 넣었든 약속(NewJob)에 맞는 것만 워커에 넘긴다. 아니면 ValueError"""
    if isinstance(body, str):
        if len(body) > BODY_MAX:
            raise ValueError("본문이 너무 김")
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            raise ValueError("본문이 JSON이 아님") from None
    if not isinstance(body, dict) or set(body) - {"vol", "page", "ref"}:
        raise ValueError("본문 칸이 약속과 다름")
    new = NewJob(kind=kind, vol=body.get("vol"), page=body.get("page"), ref=body.get("ref"))
    new.validate()
    return Job(id=job_id, kind=kind, consumer=consumer, attempts=attempts, vol=new.vol, page=new.page, ref=new.ref,
               lease=str(attempts) if lease is None else lease)


@dataclass(frozen=True)
class QueueStats:
    waiting: int
    running: int
    dead: int = 0

    @property
    def length(self) -> int:
        """상한과 비교하는 길이 = ack 전 전부 (꺼낼 수 있음 + 쥐고 있음). 죽은 작업은 세지 않는다"""
        return self.waiting + self.running


class QueueFull(Exception):
    """상한 초과 — API는 429 + Retry-After: retry_after로 바꾼다 (2단계). 넣은 것은 없다"""

    def __init__(self, kind: str, length: int, adding: int, limit: int, retry_after: int):
        super().__init__(f"{kind} 대기열이 가득 찼습니다 ({length}+{adding} > {limit})")
        self.kind, self.length, self.adding, self.limit, self.retry_after = kind, length, adding, limit, retry_after

    def headers(self) -> dict[str, str]:
        return {"Retry-After": str(self.retry_after)}


class QueueBusy(Exception):
    """넣기가 상한 잠금을 lock_timeout 안에 얻지 못함 (다른 넣기가 오래 쥠) — API는 503 + Retry-After로 바꾼다. 넣은 것은 없다"""

    def __init__(self, kind: str, retry_after: int):
        super().__init__(f"{kind} 대기열 잠금을 얻지 못했습니다")
        self.kind, self.retry_after = kind, retry_after

    def headers(self) -> dict[str, str]:
        return {"Retry-After": str(self.retry_after)}


def check_limit(cfg: QueueConfig, kind: str, length: int, adding: int) -> None:
    lim = cfg.spec(kind).limit
    if lim and length + adding > lim:
        raise QueueFull(kind, length, adding, lim, cfg.retry_after)


def group_by_kind(jobs: Sequence[NewJob], cfg: QueueConfig) -> dict[str, list[NewJob]]:
    out: dict[str, list[NewJob]] = {}
    for j in jobs:
        j.validate()
        cfg.spec(j.kind)
        out.setdefault(j.kind, []).append(j)
    return out


class JobQueue(Protocol):
    cfg: QueueConfig

    def put(self, jobs: Sequence[NewJob]) -> list[str]: ...
    def take(self, kind: str, consumer: str, wait: float = 0.0) -> Job | None: ...
    def ack(self, job: Job) -> bool: ...
    def release(self, job: Job) -> str | None: ...  # 새 id. 이미 끝났거나 다른 워커가 되찾았으면 None
    def fail(self, job: Job) -> str | None: ...     # 같음, 시도로 센다
    def heartbeat(self, job: Job) -> bool: ...
    def stats(self, kind: str) -> QueueStats: ...
    def position(self, kind: str, job_id: str) -> int | None: ...
    def count_for(self, vol: str) -> int: ...  # 그 철의 작업 수 (죽은 것 빼고, 모든 종류) — 감시(app/runs.py sweep)용


def open_queue(env=os.environ) -> JobQueue:
    """QUEUE_BACKEND=postgres(기본)|redis. redis는 여기서 고를 때만 불러온다 (이미지에 redis가 없어도 앱이 뜬다).
    Postgres 표는 store.init()(init_db)이 만든다 — 앱·워커 기동이 부른다"""
    cfg = QueueConfig.from_env(env)
    backend = env.get("QUEUE_BACKEND", "postgres")
    if backend == "redis":
        from app import jobqueue_redis
        return jobqueue_redis.RedisQueue.connect(env, cfg)
    if backend == "postgres":
        from app import jobqueue_pg, store
        return jobqueue_pg.PgQueue(store.pool, cfg)
    raise ValueError(f"QUEUE_BACKEND는 postgres 또는 redis: {backend!r}")
