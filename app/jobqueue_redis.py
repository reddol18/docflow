"""대기열 Redis Streams 구현 — 클라우드 기준 (https://github.com/reddol18/docflow/issues/5). 약속은 app/jobqueue.py.

redis 클라이언트는 이 모듈만 불러온다. 앱은 QUEUE_BACKEND=redis일 때만 이 모듈을 불러온다 (app.jobqueue.open_queue)
— 이미지 의존성(requirements.txt·lock)에도 redis가 들어 있다 (QUEUE_BACKEND=redis로 바꾸기만 하면 되게).

키: {<prefix>}:<종류> 스트림 하나 + 컨슈머 그룹 "workers", 죽은 작업은 {<prefix>}:<종류>:dead 스트림 (기본 prefix docflow:q).
  {…}는 Redis 클러스터 해시 태그 — 대기열 키가 모두 한 슬롯에 있다. 넣기 스크립트가 여러 종류 스트림을, 죽은 작업 스크립트가
  스트림과 dead 스트림을 함께 만지므로 한 슬롯이 아니면 클러스터에서 CROSSSLOT으로 거절된다. 대기열 처리량(분당 수백 건)은
  한 샤드로 충분하다. 클러스터가 아닌 Redis(맥 docs·관리형 단일 노드)에서는 해시 태그가 아무 일도 하지 않는다.
항목 칸: body(JSON) · a(이 항목 전에 쓴 시도 횟수). 시도 횟수 = a + 그 항목의 전달 횟수(XPENDING times_delivered) = 펜싱 토큰
  (Job.lease). 되돌리기·실패는 새 항목(새 id)으로 넣으므로 같은 (id, 토큰)이 다시 나오지 않는다.
확인과 동작이 갈라지면 안 되는 것은 모두 Lua 스크립트 하나로 (Redis가 스크립트를 한 번에 돌린다 — 사이에 다른 명령이 끼지 않음):
  넣기     종류마다 XLEN(=ack 전 전부, 완료하면 XDEL하므로)을 보고 상한 안이면 XADD
  되찾기   XAUTOCLAIM(count 1, 커서로 PEL 끝까지) + 그 항목의 전달 횟수를 한 번에. 없으면 XREADGROUP '>'(BLOCK, 스크립트 밖)
  완료·되돌리기·실패·살아 있음   XPENDING으로 주인(consumer)·시도(a + 전달 횟수)가 맞는지 본 뒤 XACK+XDEL / XADD+XACK+XDEL /
           XCLAIM JUSTID
  죽은 작업 XADD dead(MAXLEN dead_keep, MINID dead_days) + XACK + XDEL
  길이·순번 XPENDING IDLE로 되찾을 차례를 세고, 개수만 돌려준다 (본문은 Redis 밖으로 나오지 않음)
스트림이 사라지면(Redis 초기화 등) NOGROUP — 어느 경로든 그룹을 다시 만들고 한 번 더 한다.
시간은 Redis 서버 시계만 쓴다 (유휴 시간 판단).
연결: REDIS_URL(호스트만, 비밀번호 없이) + REDIS_USERNAME(ACL 사용자) + REDIS_PASSWORD_FILE(Secret 마운트) + REDIS_CA_FILE(rediss일 때 CA,
  호스트 이름까지 확인). Redis 쪽은 default 사용자를 끄고 앱 전용 ACL 사용자만 둔다 — 허용 명령은 ACL_COMMANDS(아래, Lua 스크립트가
  부르는 명령 포함)뿐, 키는 {prefix}:* 만 (acl_rules, k8s/docs.yaml이 같은 규칙을 쓰는지 시험이 본다).
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Callable, Sequence, TypeVar
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import redis

from app.jobqueue import BODY_MAX, Job, NewJob, QueueConfig, QueueFull, QueueStats, group_by_kind, notify_dead, parse_job

GROUP = "workers"
# 앱 전용 ACL 사용자에 여는 명령 — 이 모듈과 Lua 스크립트가 부르는 것만 (실제 Redis 7.4 시험이 이 규칙의 사용자로 공통 시험을 돈다).
# client|setinfo: redis-py가 연결할 때 라이브러리 이름을 알린다. time: 죽은 작업 정리(Lua). script|load: EVALSHA가 NOSCRIPT일 때
ACL_COMMANDS = ("ping", "client|setinfo", "xadd", "xlen", "xrange", "xpending", "xautoclaim", "xclaim", "xack", "xdel",
                "xtrim", "xreadgroup", "xgroup|create", "evalsha", "eval", "script|load", "time")


def acl_rules(prefix: str = "docflow:q") -> list[str]:
    """ACL SETUSER 규칙 (켜기·비밀번호는 따로): 키 {prefix}:*만, 채널 없음, 명령은 ACL_COMMANDS만"""
    return [f"~{{{prefix}}}:*", "resetchannels", "-@all", *(f"+{c}" for c in ACL_COMMANDS)]


def aof_loader_rules(prefix: str = "docflow:q") -> list[str]:
    """default 사용자 규칙 (설정의 "user default ..." 뒤): 꺼 두고(비밀번호 없음 → 누구도 default로 인증 못 함) 권한은 AOF 적재기 몫만.
    설정의 user default 줄은 default를 먼저 리셋(-@all)하는데, 재시작 때 AOF 적재기는 Lua 효과의 MULTI/EXEC 묶음을 default
    권한으로 다시 검사해 NOPERM이면 버린다 (https://github.com/reddol18/docflow/issues/5, XLEN 3 → 0). 그래서 대기열 명령·Lua 효과가
    AOF에 남기는 명령(AOF_LOADER_COMMANDS — 실제 Redis 7.4 AOF에서 확인) + MULTI·EXEC·SELECT만 연다, 키는 {prefix}:*.
    +@all이 아닌 까닭: 실수로 on이 되어도 비밀번호 없는 전권 사용자가 되지 않게 (tests/test_jobqueue.py AOF 재시작 시험).
    QUEUE_PREFIX를 바꾸면 그 접두사로 aof_loader_rules(QUEUE_PREFIX)를 쓴다 (AOF_LOADER_ACL은 기본 접두사 docflow:q 몫)"""
    return ["off", "resetchannels", f"~{{{prefix}}}:*", *(f"+{c}" for c in AOF_LOADER_COMMANDS)]


# AOF에 남는 명령 (MULTI/EXEC 안의 것을 AOF 적재기가 default 권한으로 다시 검사한다). 늘리거나 줄일 때는 AOF 재시작 시험으로 확인
AOF_LOADER_COMMANDS = ("multi", "exec", "select", "xadd", "xack", "xdel", "xclaim", "xgroup|setid", "xtrim")
AOF_LOADER_ACL = " ".join(aof_loader_rules())  # k8s/docs.yaml redis 초기화 컨테이너의 "user default " 뒤와 같다 (시험이 본다)
PREFIX_RX = re.compile(r"[A-Za-z0-9:_-]{1,64}")
T = TypeVar("T")

# 항목 칸 읽기 + 주인·시도 확인 (스크립트마다 앞에 붙인다)
_LUA_LIB = """
local function entry(k, id)
  local e = redis.call('XRANGE', k, id, id)
  if #e == 0 then return nil end
  local f, a, body = e[1][2], 0, ''
  for i = 1, #f, 2 do
    if f[i] == 'a' then a = tonumber(f[i + 1]) or 0 elseif f[i] == 'body' then body = f[i + 1] end
  end
  return a, body
end
local function owned(k, g, id, me, token)
  local p = redis.call('XPENDING', k, g, id, id, 1)
  if #p == 0 or p[1][2] ~= me then return nil end
  local a, body = entry(k, id)
  if a == nil or a + tonumber(p[1][4]) ~= tonumber(token) then return nil end
  return a, body
end
"""

# KEYS: 스트림들. ARGV: m, (상한, 넣을 수)×m, (KEYS 번호, 본문)…  → 넣은 id들, 또는 {'full', 번호, 길이}
_PUT = """
local m = tonumber(ARGV[1])
for i = 1, m do
  local lim, n = tonumber(ARGV[2 * i]), tonumber(ARGV[2 * i + 1])
  local len = redis.call('XLEN', KEYS[i])
  if lim > 0 and len + n > lim then return {'full', i, len} end
end
local ids = {}
for j = 2 * m + 2, #ARGV, 2 do
  ids[#ids + 1] = redis.call('XADD', KEYS[tonumber(ARGV[j])], '*', 'body', ARGV[j + 1], 'a', '0')
end
return ids
"""

# KEYS[1] 스트림. ARGV: 그룹, 나, 유휴 ms → {id, a, body, 전달 횟수} 또는 {}
_RECLAIM = _LUA_LIB + """
local k, g = KEYS[1], ARGV[1]
local cursor = '0-0'
repeat
  local r = redis.call('XAUTOCLAIM', k, g, ARGV[2], ARGV[3], cursor, 'COUNT', 1)
  cursor = r[1]
  for _, m in ipairs(r[2]) do
    if m and m[2] then
      local a, body = entry(k, m[1])
      local p = redis.call('XPENDING', k, g, m[1], m[1], 1)
      return {m[1], tostring(a or 0), body or '', tostring(p[1][4])}
    end
  end
until cursor == '0-0'
return {}
"""

# KEYS[1] 스트림. ARGV: 그룹, id, 나, 토큰
_ACK = _LUA_LIB + """
if owned(KEYS[1], ARGV[1], ARGV[2], ARGV[3], ARGV[4]) == nil then return 0 end
redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
return redis.call('XDEL', KEYS[1], ARGV[2])
"""

# 자발적 되돌리기는 이번 시도를 빼고(a = 토큰 - 1) 맨 뒤에 다시 넣는다
_RELEASE = _LUA_LIB + """
local a, body = owned(KEYS[1], ARGV[1], ARGV[2], ARGV[3], ARGV[4])
if a == nil then return false end
local id = redis.call('XADD', KEYS[1], '*', 'body', body, 'a', tostring(tonumber(ARGV[4]) - 1))
redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
redis.call('XDEL', KEYS[1], ARGV[2])
return id
"""

# 실패: 되돌리기와 같되 이번 시도를 센 채로 (a = 토큰) — 다음 전달이 max_attempts를 넘으면 죽은 작업
_FAIL = _LUA_LIB + """
local a, body = owned(KEYS[1], ARGV[1], ARGV[2], ARGV[3], ARGV[4])
if a == nil then return false end
local id = redis.call('XADD', KEYS[1], '*', 'body', body, 'a', ARGV[4])
redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
redis.call('XDEL', KEYS[1], ARGV[2])
return id
"""

_HEARTBEAT = _LUA_LIB + """
if owned(KEYS[1], ARGV[1], ARGV[2], ARGV[3], ARGV[4]) == nil then return 0 end
return #redis.call('XCLAIM', KEYS[1], ARGV[1], ARGV[3], 0, ARGV[2], 'JUSTID')
"""

# KEYS: 스트림, dead. ARGV: 그룹, id, 사유, 남길 개수(0=제한 없음), 남길 ms(0=제한 없음), 본문 최대 길이
_DEAD = _LUA_LIB + """
local a, body = entry(KEYS[1], ARGV[2])
local keep, ms = tonumber(ARGV[4]), tonumber(ARGV[5])
if keep > 0 then
  redis.call('XADD', KEYS[2], 'MAXLEN', keep, '*', 'id', ARGV[2], 'reason', ARGV[3],
             'body', string.sub(body or '', 1, tonumber(ARGV[6])), 'a', tostring(a or 0))
else
  redis.call('XADD', KEYS[2], '*', 'id', ARGV[2], 'reason', ARGV[3],
             'body', string.sub(body or '', 1, tonumber(ARGV[6])), 'a', tostring(a or 0))
end
if ms > 0 then
  local t = redis.call('TIME')
  local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
  redis.call('XTRIM', KEYS[2], 'MINID', tostring(now - ms))
end
redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
redis.call('XDEL', KEYS[1], ARGV[2])
return 1
"""

# KEYS: 스트림, dead. ARGV: 그룹, 유휴 ms → {전체, 쥔 것, 되찾을 차례, dead}
_STATS = """
local total = redis.call('XLEN', KEYS[1])
local pend = redis.call('XPENDING', KEYS[1], ARGV[1])[1]
local due = 0
if pend > 0 then due = #redis.call('XPENDING', KEYS[1], ARGV[1], 'IDLE', ARGV[2], '-', '+', pend) end
return {total, pend, due, redis.call('XLEN', KEYS[2])}
"""

# KEYS[1] 스트림. ARGV: 그룹, id, 유휴 ms → 순번(1부터) 또는 -1. 되찾을 차례인 작업(PEL, id 순)이 새 작업보다 앞
_POSITION = """
local k, g, id, idle = KEYS[1], ARGV[1], ARGV[2], ARGV[3]
if #redis.call('XRANGE', k, id, id) == 0 then return -1 end
local pend = redis.call('XPENDING', k, g)[1]
local due = {}
if pend > 0 then due = redis.call('XPENDING', k, g, 'IDLE', idle, '-', '+', pend) end
if #redis.call('XPENDING', k, g, id, id, 1) > 0 then
  for i, d in ipairs(due) do
    if d[1] == id then return i end
  end
  return -1
end
local upto = #redis.call('XRANGE', k, '-', id)
local taken = 0
if pend > 0 then taken = #redis.call('XPENDING', k, g, '-', id, pend) end
return #due + upto - taken
"""


class RedisQueue:
    def __init__(self, client: redis.Redis, cfg: QueueConfig, prefix: str = "docflow:q", poll: float = 1.0):
        """client는 decode_responses=True"""
        if not PREFIX_RX.fullmatch(prefix or ""):  # 해시 태그 {prefix}를 깨는 중괄호·빈 값은 받지 않는다
            raise ValueError("QUEUE_PREFIX는 영문·숫자·:·_·- 1~64자")
        self.r, self.cfg, self.prefix, self.poll = client, cfg, prefix, poll
        self.on_dead = None  # app.jobqueue 약속의 on_dead
        self._ready: set[str] = set()
        reg = client.register_script
        self._put, self._reclaim_s, self._ack, self._release = reg(_PUT), reg(_RECLAIM), reg(_ACK), reg(_RELEASE)
        self._fail = reg(_FAIL)
        self._heartbeat, self._dead, self._stats, self._position = reg(_HEARTBEAT), reg(_DEAD), reg(_STATS), reg(_POSITION)

    @classmethod
    def connect(cls, env, cfg: QueueConfig) -> RedisQueue:
        """REDIS_URL은 호스트만 (redis:// 또는 rediss://). 비밀번호는 REDIS_PASSWORD_FILE, CA는 REDIS_CA_FILE.
        URL·비밀번호는 예외 문구에 싣지 않는다"""
        url = env.get("REDIS_URL") or ""
        try:
            parts = urlsplit(url)  # scheme은 소문자로 바뀐다 (REDISS:// 도 rediss)
            has_password = bool(parts.password)
            query_keys = {k.lower() for k, _ in parse_qsl(parts.query, keep_blank_values=True)}
        except ValueError:
            raise ValueError("REDIS_URL을 읽지 못함") from None
        if parts.scheme not in ("redis", "rediss") or not parts.hostname:
            raise ValueError("REDIS_URL은 redis:// 또는 rediss://<호스트>")
        if has_password or "password" in query_keys:  # redis-py는 ?password=도 비밀번호로 쓴다
            raise ValueError("REDIS_URL에 비밀번호를 넣지 않는다 — REDIS_PASSWORD_FILE(Secret 마운트)")
        kw: dict = {"decode_responses": True, "socket_connect_timeout": 5,
                    "socket_timeout": 10,  # XREADGROUP BLOCK(poll, 1초)보다 길게
                    "health_check_interval": 30}
        if env.get("REDIS_USERNAME"):
            kw["username"] = env["REDIS_USERNAME"]
        if env.get("REDIS_PASSWORD_FILE"):
            kw["password"] = Path(env["REDIS_PASSWORD_FILE"]).read_text(encoding="utf-8").strip()
        if env.get("REDIS_CA_FILE"):
            if parts.scheme != "rediss":
                raise ValueError("REDIS_CA_FILE은 rediss://일 때만")
            kw["ssl_ca_certs"] = env["REDIS_CA_FILE"]
        if parts.scheme == "rediss":
            kw["ssl_cert_reqs"] = "required"
            kw["ssl_check_hostname"] = True  # 인증서 이름(SAN)이 REDIS_URL 호스트와 같아야 한다
        # urlsplit이 소문자로 바꾼 scheme으로 다시 짠다 — redis-py는 'REDISS://'를 거절하고, check_transport는 대소문자를 무시한다
        return cls(redis.Redis.from_url(urlunsplit(parts), **kw), cfg, prefix=env.get("QUEUE_PREFIX", "docflow:q"))

    def key(self, kind: str) -> str:
        self.cfg.spec(kind)
        return f"{{{self.prefix}}}:{kind}"  # 해시 태그 — 모든 대기열 키가 한 클러스터 슬롯 (머리말)

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

    def _grouped(self, kinds: Sequence[str], fn: Callable[[], T]) -> T:
        """그룹을 보장하고 fn. 그 사이 스트림이 사라졌으면(NOGROUP) 캐시를 버리고 다시 만든 뒤 한 번 더"""
        for kind in kinds:
            self._ensure(kind)
        try:
            return fn()
        except redis.ResponseError as e:
            if "NOGROUP" not in str(e):
                raise
            for kind in kinds:
                self._ensure(kind, force=True)
            return fn()

    def put(self, jobs: Sequence[NewJob]) -> list[str]:
        if not jobs:
            return []
        groups = group_by_kind(jobs, self.cfg)
        kinds = sorted(groups)
        keys = [self.key(kind) for kind in kinds]
        args: list = [len(kinds)]
        for kind in kinds:
            args += [self.cfg.spec(kind).limit, len(groups[kind])]
        for j in jobs:
            args += [kinds.index(j.kind) + 1, j.body()]
        res = self._grouped(kinds, lambda: self._put(keys=keys, args=args))
        if res and res[0] == "full":
            kind = kinds[int(res[1]) - 1]
            raise QueueFull(kind, int(res[2]), len(groups[kind]), self.cfg.spec(kind).limit, self.cfg.retry_after)
        return [str(x) for x in res]

    def _to_dead(self, kind: str, msg_id: str, reason: str, body=None) -> None:
        k = self.key(kind)
        self._dead(keys=[k, f"{k}:dead"],
                   args=[GROUP, msg_id, reason, self.cfg.dead_keep, int(self.cfg.dead_days * 86_400_000), BODY_MAX])
        notify_dead(self.on_dead, kind, body if reason == "attempts" else None, reason)

    def _job(self, kind: str, consumer: str, msg_id: str, a, body, delivered: int) -> Job | None:
        try:
            attempts = int(a or 0) + delivered
        except (TypeError, ValueError):
            self._to_dead(kind, msg_id, "invalid")
            return None
        if attempts > self.cfg.max_attempts:  # 실패·워커 죽음으로 max_attempts번 다 쓴 작업
            self._to_dead(kind, msg_id, "attempts", body)
            return None
        try:
            return parse_job(msg_id, kind, consumer, attempts, body)
        except ValueError:  # 약속에 맞지 않는 본문 — 워커에 넘기지 않고 빼서 뒤 작업을 막지 않는다
            self._to_dead(kind, msg_id, "invalid")
            return None

    def _take_once(self, kind: str, consumer: str, block_ms: int | None) -> Job | None:
        k = self.key(kind)
        idle_ms = int(self.cfg.spec(kind).idle * 1000)
        while True:
            got = self._reclaim_s(keys=[k], args=[GROUP, consumer, idle_ms])
            if not got:
                break
            job = self._job(kind, consumer, got[0], got[1], got[2], int(got[3]))
            if job:
                return job
        while True:
            got = self.r.xreadgroup(GROUP, consumer, {k: ">"}, count=1, block=block_ms)
            if not got or not got[0][1]:
                return None
            msg_id, fields = got[0][1][0]
            fields = fields or {}
            job = self._job(kind, consumer, msg_id, fields.get("a"), fields.get("body"), 1)  # 새 항목: 이번이 첫 전달
            if job:
                return job
            block_ms = None  # 죽은 작업으로 뺐으면 기다리지 않고 다음 것을 본다

    def take(self, kind: str, consumer: str, wait: float = 0.0) -> Job | None:
        end = time.monotonic() + wait
        while True:
            left = end - time.monotonic()
            block = int(min(self.poll, left) * 1000) if left > 0 else 0
            job = self._grouped([kind], lambda b=block: self._take_once(kind, consumer, b or None))
            if job or end - time.monotonic() <= 0:
                return job

    def _fenced(self, script, job: Job):
        k = self.key(job.kind)
        return self._grouped([job.kind], lambda: script(keys=[k], args=[GROUP, job.id, job.consumer, job.lease]))

    def ack(self, job: Job) -> bool:
        return int(self._fenced(self._ack, job) or 0) == 1

    def release(self, job: Job) -> str | None:
        new_id = self._fenced(self._release, job)
        return str(new_id) if new_id else None

    def fail(self, job: Job) -> str | None:
        new_id = self._fenced(self._fail, job)
        return str(new_id) if new_id else None

    def heartbeat(self, job: Job) -> bool:
        return int(self._fenced(self._heartbeat, job) or 0) == 1

    def stats(self, kind: str) -> QueueStats:
        k = self.key(kind)
        idle_ms = int(self.cfg.spec(kind).idle * 1000)
        total, pend, due, dead = (int(x) for x in self._grouped([kind], lambda: self._stats(keys=[k, f"{k}:dead"],
                                                                                              args=[GROUP, idle_ms])))
        return QueueStats(waiting=total - pend + due, running=pend - due, dead=dead)

    def count_for(self, vol: str) -> int:
        """그 철의 작업 수 — 스트림을 훑는다 (종류마다 상한 안이라 작다, 감시가 몇 분마다만 부른다)"""
        n = 0
        for kind in self.cfg.kinds:
            k = self.key(kind)
            last = "-"
            while True:
                rows = self.r.xrange(k, min=last, max="+", count=500)
                for _, fields in rows:
                    try:
                        if json.loads((fields or {}).get("body") or "{}").get("vol") == vol:
                            n += 1
                    except (ValueError, AttributeError):
                        pass
                if len(rows) < 500:
                    break
                last = "(" + rows[-1][0]
        return n

    def position(self, kind: str, job_id: str) -> int | None:
        k = self.key(kind)
        idle_ms = int(self.cfg.spec(kind).idle * 1000)
        try:
            n = int(self._grouped([kind], lambda: self._position(keys=[k], args=[GROUP, job_id, idle_ms])))
        except redis.ResponseError as e:
            if "Invalid stream ID" in str(e):
                return None
            raise
        return n if n > 0 else None
