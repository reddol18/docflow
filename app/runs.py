"""철 실행 상태와 대기열 넣기 (https://github.com/reddol18/docflow/issues/5 2단계). API(app/main.py)와 워커(app/worker.py)가 같이 쓴다.

예전에는 API 프로세스 안 스레드 하나(runner)가 철을 통째로 돌리고 프로세스 안 dict(running)가 '실행 중'이었다.
지금은 철 실행 = 대기열 작업들 + vol_run 행(app/store.py SCHEMA):
  /run      쪽 OCR이 안 된 쪽마다 ocr_page 작업(ref = 실행 번호) — 다 읽은 철이면 바로 graph 작업 하나
  쪽 작업   워커가 쪽 하나 OCR → 남은 쪽 수(pages_left, 같은 쪽은 run_page로 한 번만 셈)가 0이 되면 graph 작업을 한 번 넣는다
  graph     워커가 LangGraph 그래프를 돌린다 (체크포인트는 예전처럼 Postgres). 검토 대기·끝·한도 멈춤·오류로 행을 닫는다
  /resume·검토 제출   graph 작업 하나 (arg = 이어 가기 / 검토 값)
실행 번호(run)는 vol_run_seq에서 받는다 (철 사이에서도 단조 증가). 순서 (API, main.start_run):
  1. prepare  행을 'preparing'으로 확정 — 끝난 행(ended)이거나 행이 없을 때만 (조건부, 아니면 409). 이때부터 '실행 중'
  2. 넣기     대기열에 작업 — 가득 차면(QueueFull 429·QueueBusy 503) rollback으로 앞선 행을 되돌린다 (아무것도 안 바뀜)
  3. before   리셋·검토 기록 — 실패하면 rollback (넣은 작업은 실행 번호가 행과 달라 워커가 버린다)
  4. ready    'queued' — 워커는 'preparing' 행의 작업을 시도로 세지 않고 되돌린다(release, 잠깐 기다림)
작업의 실행 번호가 행과 다르면(옛 실행·되돌린 실행) 버린다(ack). 행이 끝났어도 버린다.
상태: preparing → queued(넣음) → running(워커가 시작) → ended(outcome: ok·review·budget·error).
활성(preparing·queued·running)인데 작업이 없고 오래 그대로인 행은 워커의 감시(sweep)가 오류로 닫는다 — 작업이 유실돼도
(버림·본문 깨짐·Redis 초기화) 철이 영원히 409로 남지 않게.
"""

from __future__ import annotations

import json
import os
import threading

from app import jobqueue, store
from app.jobqueue import GRAPH, OCR_PAGE, NewJob

ACTIVE = ("preparing", "queued", "running")
# 활성 행이 작업 없이 이만큼(초) 그대로면 감시가 닫는다. 쪽·그래프 작업은 처리 중 행을 늘 새로 쓴다(쪽마다·살아 있음 표시마다)
STALE = float(os.environ.get("RUN_STALE_SECONDS", "600"))
PREPARING_STALE = 120.0  # preparing은 API가 몇 초 안에 끝낸다 — 이보다 오래면 API가 도중에 죽은 것
_queue = None
_qlock = threading.Lock()


class Busy(Exception):
    """이미 실행 중 (활성 행) — API는 409"""


def queue():
    """대기열 하나 (프로세스마다). QUEUE_BACKEND로 고른다 (app/jobqueue.py open_queue)"""
    global _queue
    with _qlock:
        if _queue is None:
            q = jobqueue.open_queue()
            q.on_dead = on_dead
            _queue = q
        return _queue


def set_queue(q):
    """시험용: 대기열을 바꿔 끼운다 (None이면 다음 queue()가 다시 연다)"""
    global _queue
    with _qlock:
        if q is not None:
            q.on_dead = on_dead
        _queue = q


def next_run() -> int:
    with store.pool.connection() as c:
        return c.execute("SELECT nextval('vol_run_seq') AS n").fetchone()["n"]


_COLS = ("run, kind, arg, state, outcome, user_id, username, graph_put, graph_job, graph_lease, graph_beat, started, "
         "pages_left, updated_at")


def row(vol):
    with store.pool.connection() as c:
        return c.execute(f"SELECT {_COLS} FROM vol_run WHERE vol = %s", (vol,)).fetchone()  # noqa: S608 — 고정 열 목록


def active(vol) -> str | None:
    """실행 중이면 상태('preparing'·'queued'·'running'), 아니면 None"""
    r = row(vol)
    return r["state"] if r and r["state"] in ACTIVE else None


def active_all() -> dict[str, str]:
    """실행 중인 철 전부 {철: 상태} — 철 목록이 철마다 다시 읽지 않게 한 번에"""
    with store.pool.connection() as c:
        return {r["vol"]: r["state"] for r in c.execute(
            "SELECT vol, state FROM vol_run WHERE state = ANY(%s)", (list(ACTIVE),)).fetchall()}


def prepare(vol, run, kind, arg, user=None, pages_left=0, graph_put=False):
    """1단계: 행 확정 (preparing). 끝난 행·행 없음일 때만 — 아니면 Busy. 앞선 행(되돌리기용)을 돌려준다"""
    prev = row(vol)
    with store.pool.connection() as c:
        won = c.execute("""INSERT INTO vol_run (vol, run, kind, arg, state, user_id, username, graph_put, pages_left)
                           VALUES (%s, %s, %s, %s::jsonb, 'preparing', %s, %s, %s, %s)
                           ON CONFLICT (vol) DO UPDATE SET run = EXCLUDED.run, kind = EXCLUDED.kind, arg = EXCLUDED.arg,
                             state = 'preparing', outcome = NULL, user_id = EXCLUDED.user_id, username = EXCLUDED.username,
                             graph_put = EXCLUDED.graph_put, graph_job = NULL, graph_lease = NULL, graph_beat = NULL,
                             started = false, pages_left = EXCLUDED.pages_left, updated_at = clock_timestamp()
                           WHERE vol_run.state = 'ended'
                           RETURNING run""",
                        (vol, run, kind, json.dumps(arg, ensure_ascii=False), getattr(user, "id", None),
                         getattr(user, "username", None), graph_put, pages_left)).fetchone()
        if not won:
            raise Busy(vol)
        c.execute("DELETE FROM run_page WHERE vol = %s AND run <> %s", (vol, run))
    return prev if prev and prev["run"] != run else None


def rollback(vol, run, prev):
    """넣기·before가 실패했을 때: 이 실행의 행을 앞선 행으로 되돌린다 (없었으면 지운다)"""
    with store.pool.connection() as c:
        if prev is None:
            c.execute("DELETE FROM vol_run WHERE vol = %s AND run = %s", (vol, run))
        else:
            c.execute("""UPDATE vol_run SET run = %s, kind = %s, arg = %s::jsonb, state = %s, outcome = %s, user_id = %s,
                           username = %s, graph_put = %s, graph_job = %s, graph_lease = %s, graph_beat = %s, started = %s,
                           pages_left = %s, updated_at = %s
                         WHERE vol = %s AND run = %s""",
                      (prev["run"], prev["kind"], json.dumps(prev["arg"], ensure_ascii=False), prev["state"],
                       prev["outcome"], prev["user_id"], prev["username"], prev["graph_put"], prev["graph_job"],
                       prev["graph_lease"], prev["graph_beat"], prev["started"], prev["pages_left"], prev["updated_at"],
                       vol, run))
        c.execute("DELETE FROM run_page WHERE vol = %s AND run = %s", (vol, run))


def ready(vol, run):
    with store.pool.connection() as c:
        c.execute("UPDATE vol_run SET state = 'queued', updated_at = clock_timestamp() "
                  "WHERE vol = %s AND run = %s AND state = 'preparing'", (vol, run))


def mark_running(vol, run):
    with store.pool.connection() as c:
        c.execute("UPDATE vol_run SET state = 'running', updated_at = clock_timestamp() "
                  "WHERE vol = %s AND run = %s AND state IN ('queued', 'running')", (vol, run))


def end(vol, run, outcome, token=None) -> bool:
    """실행을 닫는다 (검토 값 arg도 지운다). token=(작업 id, 임대)이면 그 임대가 쥔 실행일 때만. 닫았으면 True"""
    sql = ("UPDATE vol_run SET state = 'ended', outcome = %s, arg = NULL, graph_job = NULL, graph_lease = NULL, "
           "updated_at = clock_timestamp() WHERE vol = %s AND run = %s AND state <> 'ended'")
    args = [outcome, vol, run]
    if token is not None:
        sql += " AND graph_job = %s AND graph_lease = %s"
        args += list(token)
    with store.pool.connection() as c:
        return c.execute(sql, args).rowcount == 1  # noqa: S608 — 고정 조각만 붙인다


# ---------- 그래프 임대 (한 실행의 그래프는 한 워커만) ----------

def claim_graph(vol, run, job_id, lease, stale):
    """그래프를 쥔다. ('ok', 행) / ('wait', None) 같은 작업을 앞선 임대가 아직 쥠(살아 있음 표시가 stale초 안) —
    되돌려 나중에 / ('dup', None) 다른 작업이 쥐고 있음 — 이 작업은 버린다"""
    with store.pool.connection() as c:
        got = c.execute("""UPDATE vol_run SET state = 'running', graph_job = %(j)s, graph_lease = %(l)s,
                                 graph_beat = clock_timestamp(), updated_at = clock_timestamp()
                           WHERE vol = %(v)s AND run = %(r)s AND state IN ('queued', 'running')
                             AND (graph_job IS NULL OR (graph_job = %(j)s AND graph_lease = %(l)s)
                                  OR graph_beat < clock_timestamp() - make_interval(secs => %(s)s))
                           RETURNING kind, arg, started, user_id, username""",
                        {"j": job_id, "l": lease, "v": vol, "r": run, "s": stale}).fetchone()
        if got:
            return "ok", got
        r = c.execute("SELECT graph_job FROM vol_run WHERE vol = %s AND run = %s", (vol, run)).fetchone()
    return ("wait" if r and r["graph_job"] == job_id else "dup"), None


def beat_graph(vol, run, token) -> bool:
    with store.pool.connection() as c:
        return c.execute("UPDATE vol_run SET graph_beat = clock_timestamp(), updated_at = clock_timestamp() "
                         "WHERE vol = %s AND run = %s AND state <> 'ended' AND graph_job = %s AND graph_lease = %s",
                         (vol, run, *token)).rowcount == 1


def release_graph(vol, run, token):
    """실패한 시도가 그래프를 내려놓는다 — 다음 시도(새 항목 id일 수 있음)가 쥘 수 있게"""
    with store.pool.connection() as c:
        c.execute("UPDATE vol_run SET graph_job = NULL, graph_lease = NULL, graph_beat = NULL "
                  "WHERE vol = %s AND run = %s AND graph_job = %s AND graph_lease = %s", (vol, run, *token))


def mark_started(vol, run, token) -> bool:
    with store.pool.connection() as c:
        return c.execute("UPDATE vol_run SET started = true WHERE vol = %s AND run = %s AND graph_job = %s AND graph_lease = %s",
                         (vol, run, *token)).rowcount == 1


def put(jobs):
    return queue().put(jobs)


def ocr_todo(vol, pages):
    return [p for p in pages if not store.ocr_path(vol, p).exists()]


def page_done(vol, run, page) -> int | None:
    """쪽 하나를 끝냈다고 센다 (같은 쪽은 한 번만). 남은 쪽 수, 이 실행이 아니면 None"""
    with store.pool.connection() as c:
        r = c.execute("""WITH ins AS (INSERT INTO run_page (vol, run, page) VALUES (%(v)s, %(r)s, %(p)s)
                                      ON CONFLICT DO NOTHING RETURNING 1)
                         UPDATE vol_run SET pages_left = greatest(pages_left - (SELECT count(*) FROM ins), 0),
                                            updated_at = clock_timestamp()
                         WHERE vol = %(v)s AND run = %(r)s AND state <> 'ended' RETURNING pages_left""",
                      {"v": vol, "r": run, "p": page}).fetchone()
    return r["pages_left"] if r else None


def maybe_put_graph(vol, run, force=False) -> bool:
    """남은 쪽이 0이면 graph 작업을 넣는다 — 한 번만 (graph_put 조건부 갱신: 동시에 끝난 두 쪽 작업 중 하나만 이긴다).
    force: 이 쪽 작업이 재시도라 앞선 시도가 표시만 하고 넣기 전에 죽었을 수 있을 때 — 다시 넣는다
    (graph 작업이 둘이 되어도 워커가 graph 임대로 하나만 돌린다). 넣지 못하면(대기열 가득) 표시를 되돌리고 예외 → 쪽 작업 재시도"""
    with store.pool.connection() as c:
        won = c.execute("UPDATE vol_run SET graph_put = true, updated_at = clock_timestamp() "
                        "WHERE vol = %s AND run = %s AND state IN ('queued', 'running') AND pages_left = 0 "
                        "AND (NOT graph_put OR %s) RETURNING run", (vol, run, force)).fetchone()
    if not won:
        return False
    try:
        put([NewJob(GRAPH, vol=vol, ref=str(run))])
    except BaseException:
        with store.pool.connection() as c:
            c.execute("UPDATE vol_run SET graph_put = false WHERE vol = %s AND run = %s", (vol, run))
        raise
    return True


def start_jobs(vol, run, pages):
    """/run이 넣을 작업: 안 읽은 쪽마다 ocr_page, 다 읽었으면 graph 하나. (작업들, 남은 쪽 수)"""
    todo = ocr_todo(vol, pages)
    if todo:
        return [NewJob(OCR_PAGE, vol=vol, page=p, ref=str(run)) for p in todo], len(todo)
    return [NewJob(GRAPH, vol=vol, ref=str(run))], 0


def close_error(vol, run, code, **info) -> bool:
    if end(vol, run, "error"):
        store.event(vol, "run", "error", None, code=code, **info)
        return True
    return False


def on_dead(kind, body, reason):
    """대기열이 작업을 죽은 작업으로 뺐을 때 (실패·워커 죽음 max_attempts번, 또는 본문이 약속과 다름).
    철 작업이면 그 실행을 오류로 닫고 진행 화면에 남긴다. /ask면 요청을 오류로. 철을 모르면(본문 깨짐) 감시(sweep)가 닫는다"""
    body = body or {}
    ref = body.get("ref")
    if kind == jobqueue.ASK:
        if ref:
            ask_done(ref, None, "internal")
        return
    vol = body.get("vol")
    try:
        run = int(ref)
    except (TypeError, ValueError):
        return
    if vol and close_error(vol, run, "queue_dead", job=kind, page=body.get("page")):
        print(f"[queue] {vol} 실행 {run}: {kind} 작업이 죽은 작업이 됨 ({reason})", flush=True)


def sweep(q, stale=None) -> list[str]:
    """감시 (워커가 주기적으로): 활성인데 오래 그대로이고 대기열에 그 철 작업이 없는 실행을 오류로 닫는다.
    preparing은 PREPARING_STALE초면 (API가 넣는 도중 죽음 — 넣은 작업은 실행 번호가 맞아도 상태가 안 바뀌므로 닫는다).
    하루 지난 /ask 요청 행도 지운다. 닫은 철 목록"""
    stale = STALE if stale is None else stale
    with store.pool.connection() as c:
        rows = c.execute("""SELECT vol, run, state FROM vol_run WHERE state = ANY(%s) AND updated_at < clock_timestamp()
                              - make_interval(secs => CASE WHEN state = 'preparing' THEN %s ELSE %s END)""",
                         (list(ACTIVE), min(PREPARING_STALE, stale), stale)).fetchall()
        c.execute("DELETE FROM ask_request WHERE created_at < clock_timestamp() - interval '1 day'")
    out = []
    for r in rows:
        if r["state"] != "preparing" and q.count_for(r["vol"]):
            continue  # 작업이 남아 있다 (되찾기·차례를 기다림)
        if close_error(r["vol"], r["run"], "stale"):
            print(f"[sweep] {r['vol']} 실행 {r['run']}: 작업 없이 {r['state']} — 오류로 닫음", flush=True)
            out.append(r["vol"])
    return out


# ---------- /ask 요청 (질문 본문은 DB, 대기열에는 id만) ----------

def ask_new(req_id, user, q, year_from, year_to, quota_key=None, quota_wins=None):
    with store.pool.connection() as c:
        c.execute("DELETE FROM ask_request WHERE created_at < clock_timestamp() - interval '1 day'")
        c.execute("INSERT INTO ask_request (id, user_id, username, q, year_from, year_to, quota_key, quota_wins) "
                  "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
                  (req_id, getattr(user, "id", None), getattr(user, "username", None), q, year_from, year_to, quota_key,
                   json.dumps([[k, w.isoformat(), cap] for k, w, cap in quota_wins]) if quota_wins is not None else None))


def ask_get(req_id):
    with store.pool.connection() as c:
        return c.execute("SELECT id, user_id, username, q, year_from, year_to, state, result, code, quota_key, quota_wins "
                         "FROM ask_request WHERE id = %s", (req_id,)).fetchone()


def ask_claim(req_id, token, stale):
    """처리를 쥔다 — 한 번만 (queued → running). 앞선 워커의 살아 있음이 stale초보다 오래면 넘겨받는다. 행 또는 None"""
    with store.pool.connection() as c:
        return c.execute("""UPDATE ask_request SET state = 'running', claimed_by = %(t)s, beat = clock_timestamp()
                            WHERE id = %(i)s AND (state = 'queued' OR (state = 'running' AND (claimed_by = %(t)s
                                  OR beat < clock_timestamp() - make_interval(secs => %(s)s))))
                            RETURNING id, user_id, username, q, year_from, year_to""",
                         {"t": token, "i": req_id, "s": stale}).fetchone()


def ask_beat(req_id, token) -> bool:
    with store.pool.connection() as c:
        return c.execute("UPDATE ask_request SET beat = clock_timestamp() WHERE id = %s AND state = 'running' "
                         "AND claimed_by = %s", (req_id, token)).rowcount == 1


def ask_done(req_id, result, code=None, token=None) -> bool:
    """결과(또는 오류 코드)를 남기고 질문 본문은 지운다 — 답을 돌려줄 때까지만 둔다. token이면 그 임대가 쥔 때만"""
    sql = ("UPDATE ask_request SET state = %s, result = %s::jsonb, code = %s, q = '', done_at = clock_timestamp() "
           "WHERE id = %s AND state IN ('queued', 'running')")
    args = ["error" if code else "done", json.dumps(result, ensure_ascii=False) if result is not None else None, code, req_id]
    if token is not None:
        sql += " AND claimed_by = %s"
        args.append(token)
    with store.pool.connection() as c:
        return c.execute(sql, args).rowcount == 1  # noqa: S608 — 고정 조각만 붙인다


def ask_forget(req_id):
    with store.pool.connection() as c:
        c.execute("DELETE FROM ask_request WHERE id = %s", (req_id,))
