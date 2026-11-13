"""철 실행 상태와 대기열 넣기 (https://github.com/reddol18/docflow/issues/5 2단계). API(app/main.py)와 워커(app/worker.py)가 같이 쓴다.

예전에는 API 프로세스 안 스레드 하나(runner)가 철을 통째로 돌리고 프로세스 안 dict(running)가 '실행 중'이었다.
지금은 철 실행 = 대기열 작업들 + vol_run 행(app/store.py SCHEMA):
  /run      쪽 OCR이 안 된 쪽마다 ocr_page 작업(ref = 실행 번호) — 다 읽은 철이면 바로 graph 작업 하나
  쪽 작업   워커가 쪽 하나 OCR → 철의 쪽이 모두 읽혔으면 graph 작업을 한 번만 넣는다 (vol_run.graph_put 조건부 갱신)
  graph     워커가 LangGraph 그래프를 돌린다 (체크포인트는 예전처럼 Postgres). 검토 대기·끝·한도 멈춤·오류로 행을 닫는다
  /resume·검토 제출   graph 작업 하나 (arg = 이어 가기 / 검토 값)
실행 번호(run)는 vol_run_seq에서 받는다 (철 사이에서도 단조 증가). 넣는 순서는 대기열 먼저 → 리셋·검토 기록 → 행 확정(commit):
  대기열이 가득 차면(QueueFull 429·QueueBusy 503) 아무것도 바뀌지 않는다 (running 표시·검사 목록·단계 그대로).
  워커가 행보다 먼저 작업을 꺼내면(행 run < 작업 run) 잠깐 뒤 실패(fail)로 되돌려 다시 본다 — 행이 끝내 안 생기면(API가 죽음)
  max_attempts 뒤 죽은 작업. 행 run > 작업 run이면 옛 실행의 작업이라 버린다 (ack).
상태: queued(넣음) → running(워커가 시작) → ended(outcome: ok·review·budget·error). queued·running이면 '실행 중' (busy).
"""

from __future__ import annotations

import json
import threading

from app import jobqueue, store
from app.jobqueue import GRAPH, OCR_PAGE, NewJob

ACTIVE = ("queued", "running")
_queue = None
_qlock = threading.Lock()


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


def row(vol):
    with store.pool.connection() as c:
        return c.execute("SELECT run, kind, arg, state, outcome, user_id, username, graph_put, graph_job, started "
                         "FROM vol_run WHERE vol = %s", (vol,)).fetchone()


def active(vol) -> str | None:
    """실행 중이면 'queued' 또는 'running', 아니면 None"""
    r = row(vol)
    return r["state"] if r and r["state"] in ACTIVE else None


def last_outcome(vol) -> str | None:
    r = row(vol)
    return r["outcome"] if r and r["state"] == "ended" else None


def commit(vol, run, kind, arg, user=None, graph_put=False):
    """행 확정 — 이 뒤로 워커가 이 실행의 작업을 처리한다. 앞선 행(끝난 실행)은 덮어쓴다"""
    with store.pool.connection() as c:
        c.execute("""INSERT INTO vol_run (vol, run, kind, arg, state, user_id, username, graph_put)
                     VALUES (%s, %s, %s, %s::jsonb, 'queued', %s, %s, %s)
                     ON CONFLICT (vol) DO UPDATE SET run = EXCLUDED.run, kind = EXCLUDED.kind, arg = EXCLUDED.arg,
                       state = 'queued', outcome = NULL, user_id = EXCLUDED.user_id, username = EXCLUDED.username,
                       graph_put = EXCLUDED.graph_put, graph_job = NULL, started = false, updated_at = clock_timestamp()""",
                  (vol, run, kind, json.dumps(arg, ensure_ascii=False), getattr(user, "id", None),
                   getattr(user, "username", None), graph_put))


def mark_running(vol, run):
    with store.pool.connection() as c:
        c.execute("UPDATE vol_run SET state = 'running', updated_at = clock_timestamp() "
                  "WHERE vol = %s AND run = %s AND state = 'queued'", (vol, run))


def end(vol, run, outcome, graph_job=None) -> bool:
    """실행을 닫는다 (검토 값 arg도 지운다). graph_job을 주면 그 작업이 쥔 실행일 때만. 닫았으면 True"""
    sql = ("UPDATE vol_run SET state = 'ended', outcome = %s, arg = NULL, graph_job = NULL, updated_at = clock_timestamp() "
           "WHERE vol = %s AND run = %s AND state <> 'ended'")
    args = [outcome, vol, run]
    if graph_job is not None:
        sql += " AND graph_job = %s"
        args.append(graph_job)
    with store.pool.connection() as c:
        return c.execute(sql, args).rowcount == 1  # noqa: S608 — 고정 조각만 붙인다


def put(jobs):
    return queue().put(jobs)


def ocr_todo(vol, pages):
    return [p for p in pages if not store.ocr_path(vol, p).exists()]


def maybe_put_graph(vol, run, force=False) -> bool:
    """철의 쪽이 모두 읽혔으면 graph 작업을 넣는다 — 한 번만 (graph_put 조건부 갱신: 동시에 끝난 두 쪽 작업 중 하나만 이긴다).
    force: 이 쪽 작업이 되찾긴 재시도라 앞선 시도가 표시만 하고 넣기 전에 죽었을 수 있을 때 — 다시 넣는다
    (graph 작업이 둘이 되어도 워커가 graph_job으로 하나만 돌린다). 넣지 못하면(대기열 가득) 표시를 되돌리고 예외 → 쪽 작업 재시도"""
    from app.graph import vol_pages
    if ocr_todo(vol, vol_pages(vol)):
        return False
    with store.pool.connection() as c:
        won = c.execute("UPDATE vol_run SET graph_put = true, updated_at = clock_timestamp() "
                        "WHERE vol = %s AND run = %s AND state <> 'ended' AND (NOT graph_put OR %s) RETURNING run",
                        (vol, run, force)).fetchone()
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
    """/run이 넣을 작업: 안 읽은 쪽마다 ocr_page, 다 읽었으면 graph 하나. (작업들, graph를 넣었나)"""
    todo = ocr_todo(vol, pages)
    if todo:
        return [NewJob(OCR_PAGE, vol=vol, page=p, ref=str(run)) for p in todo], False
    return [NewJob(GRAPH, vol=vol, ref=str(run))], True


def on_dead(kind, body, reason):
    """대기열이 작업을 죽은 작업으로 뺐을 때 (실패·워커 죽음 max_attempts번, 또는 본문이 약속과 다름).
    철 작업이면 그 실행을 오류로 닫고 진행 화면에 남긴다. /ask면 요청을 오류로"""
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
    if vol and end(vol, run, "error"):
        store.event(vol, "run", "error", None, code="queue_dead", job=kind, page=body.get("page"))
        print(f"[queue] {vol} 실행 {run}: {kind} 작업이 죽은 작업이 됨 ({reason})", flush=True)


# ---------- /ask 요청 (질문 본문은 DB, 대기열에는 id만) ----------

def ask_new(req_id, user, q, year_from, year_to):
    with store.pool.connection() as c:
        c.execute("DELETE FROM ask_request WHERE created_at < clock_timestamp() - interval '1 day'")
        c.execute("INSERT INTO ask_request (id, user_id, username, q, year_from, year_to) VALUES (%s,%s,%s,%s,%s,%s)",
                  (req_id, getattr(user, "id", None), getattr(user, "username", None), q, year_from, year_to))


def ask_get(req_id):
    with store.pool.connection() as c:
        return c.execute("SELECT id, user_id, username, q, year_from, year_to, state, result, code FROM ask_request "
                         "WHERE id = %s", (req_id,)).fetchone()


def ask_done(req_id, result, code=None):
    """결과(또는 오류 코드)를 남기고 질문 본문은 지운다 — 답을 돌려줄 때까지만 둔다"""
    with store.pool.connection() as c:
        c.execute("UPDATE ask_request SET state = %s, result = %s::jsonb, code = %s, q = '', done_at = clock_timestamp() "
                  "WHERE id = %s AND state = 'queued'",
                  ("error" if code else "done", json.dumps(result, ensure_ascii=False) if result is not None else None,
                   code, req_id))


def ask_forget(req_id):
    with store.pool.connection() as c:
        c.execute("DELETE FROM ask_request WHERE id = %s", (req_id,))
