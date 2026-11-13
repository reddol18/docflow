"""대기열 워커 (https://github.com/reddol18/docflow/issues/5 2단계). API와 같은 이미지에서 `python -m app.worker`로 띄운다.

종류마다 스레드 하나(WORKER_KINDS, 기본 ocr_page,graph,ask)가 대기열에서 꺼내 처리한다:
  ocr_page  쪽 하나 OCR(app/graph.py ocr_one) → 철의 쪽이 다 읽혔으면 graph 작업을 한 번 넣는다 (app/runs.py maybe_put_graph)
  graph     철 그래프를 돌린다 (QUEUE_HEARTBEAT_GRAPH마다 살아 있음 표시). 끝·검토 대기·한도 멈춤·오류를 예전 runner와 같게
            이벤트·감사 로그·run_stop에 남긴다
  ask       /ask 요청 하나 (질문은 DB ask_request, 대기열에는 id만) — 검색 + 답변을 행에 남긴다. API가 행을 읽어 돌려준다
실패는 fail(시도로 셈, QUEUE_MAX_ATTEMPTS 3번이면 죽은 작업 → app/runs.py on_dead가 실행을 오류로 닫는다). 비용 한도 멈춤은
실패가 아니다(ack, 예전처럼 /resume으로 이어 감). 작업 본문은 믿지 않는다 — 철·쪽 이름을 경로에 쓰기 전에 API와 같은 규칙
(main.VOL_RX, upload.page_of)과 store.under(저장 폴더 밖 차단)로 다시 본다.
기동할 때: 끝나지 않은 철 자동 재개 (resume_unfinished) — 그래프가 중간에 멈춰 있고(next 있음·검토 대기 아님) 실행 중이 아니며
마지막 실행이 오류·한도 멈춤이 아닌 철에 graph 이어 가기 작업을 넣는다 (예전 판에서 Pod 재시작으로 끊긴 철). 실행 중(queued·
running)인 철은 대기열에 작업이 남아 있으므로(Postgres 표·Redis AOF) 되찾기가 잇는다.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from types import SimpleNamespace

from langgraph.types import Command

from app import errors, jobqueue, llm, runs, store
from app import upload as uploads
from app.jobqueue import ASK, GRAPH, OCR_PAGE, NewJob

KINDS = (OCR_PAGE, GRAPH, ASK)
NOT_YET_WAIT = 1.0  # 실행 행이 아직 확정되지 않은 작업을 되돌리기 전 기다림 (API가 넣은 뒤 행을 쓰는 사이)


def first_input(vol, kind, arg):
    """실행 종류 → 그래프 첫 입력: start {vol, review_mode} / review Command(resume=검토 값) / resume None(멈춘 곳부터)"""
    arg = arg or {}
    if kind == "start":
        return {"vol": vol, "review_mode": arg.get("review_mode", "human")}
    if kind == "review":
        return Command(resume=arg.get("resume") or {})
    return None


def execute(main, vol, inp, user, invoke=True, claim=lambda outcome: True):
    """그래프를 한 번 돌리고 결과를 남긴다 — 예전 main.submit의 실행 몸통 (이벤트·감사 로그·run_stop이 같다).
    claim(outcome)이 True일 때만 남긴다 (워커: 아직 이 실행을 쥐고 있을 때). 결과 ok·review·budget·error를 돌려준다.
    비용 한도는 오류가 아니라 멈춤(budget, https://github.com/reddol18/docflow/issues/7) — /resume으로 이어 간다.
    그 밖의 예외는 그대로 올린다 (워커가 재시도·마지막이면 오류로 닫는다)"""
    try:
        if invoke:
            main.G.invoke(inp, main.cfg(vol))
    except llm.BudgetExceeded as e:
        print(f"run {vol} budget stop: {e}", file=sys.stderr, flush=True)
        if claim("budget"):
            store.set_run_stop(vol, "budget")  # 철 단계의 근거 (run_stop 표)
            store.event(vol, "run", "budget", None, **errors.public(e))
            main.audit_safe(user, "run_done", vol, None, "budget_stop")
        return "budget"
    snap = main.G.get_state(main.cfg(vol))
    print(f"run {vol} stopped/finished: next={snap.next}", flush=True)
    outcome = "review" if snap.interrupts else "error" if snap.next else "ok"  # 끝나지도 멈추지도 않음 → 오류 (/resume)
    if claim(outcome):
        if outcome == "review":
            store.event(vol, "review", "waiting", None, flagged=len(snap.interrupts[0].value["flagged"]))
            main.audit_safe(user, "run_done", vol, None, "review_waiting")
        elif outcome == "ok":
            store.event(vol, "run", "end", None)
            main.audit_safe(user, "run_done", vol, None, "ok")
        else:
            store.event(vol, "run", "error", None, code="internal")
            main.audit_safe(user, "run_done", vol, None, "error", code="internal")
    return outcome


class BadJob(ValueError):
    """본문의 철·쪽 이름이 규칙에 맞지 않음 — 다시 해도 같으므로 버린다"""


def _main():
    from app import main  # FastAPI 앱 모듈 — 그래프·검색·감사 함수를 같이 쓴다 (경로 처리는 하지 않음)
    return main


def check_names(job):
    """경로에 쓰기 전 재검증 (대기열 본문은 믿지 않는다): API와 같은 철 이름 규칙 + 쪽 이름 규칙 + 저장 폴더 안"""
    main = _main()
    if job.vol is None or not main.VOL_RX.fullmatch(job.vol):
        raise BadJob("철 이름 규칙")
    if job.page is not None and uploads.page_of(f"{job.page}.jpg") != job.page:
        raise BadJob("쪽 이름 규칙")
    try:
        store.pages_dir(job.vol)
        if job.page is not None:
            store.page_path(job.vol, job.page)
    except store.PathEscape:
        raise BadJob("저장 폴더 밖") from None


def _run_no(job):
    try:
        return int(job.ref)
    except (TypeError, ValueError):
        raise BadJob("실행 번호") from None


def _user(r):
    return SimpleNamespace(id=r["user_id"], username=r["username"] or "worker") if r and r["user_id"] is not None else None


class Worker:
    def __init__(self, q=None, name=None):
        self.q = q or runs.queue()
        self.name = name or jobqueue.consumer_name()

    # ---------- 공통 ----------

    def _row_for(self, job, run):
        """이 작업의 실행 행. 아직 확정 전이면 'later', 옛 실행이면 None"""
        r = runs.row(job.vol)
        if r is None or r["run"] < run:
            return "later"
        if r["run"] > run or r["state"] == "ended":
            return None
        return r

    def _later(self, job):
        time.sleep(NOT_YET_WAIT)
        self.q.fail(job)

    def handle(self, job):
        try:
            if job.kind == ASK:
                return self.ask(job)
            check_names(job)
            run = _run_no(job)
            if job.kind == OCR_PAGE:
                return self.ocr(job, run)
            return self.graph(job, run)
        except BadJob as e:
            print(f"[worker] {job.kind} 작업 버림: {e}", flush=True)  # 이름 값은 싣지 않는다
            self.q.ack(job)

    # ---------- 쪽 OCR ----------

    def ocr(self, job, run):
        from app import graph as flow
        r = self._row_for(job, run)
        if r == "later":
            return self._later(job)
        if r is None:
            return self.q.ack(job)  # 옛 실행의 쪽 — 결과 파일은 다음 실행이 그대로 쓴다
        runs.mark_running(job.vol, run)
        try:
            if store.page_path(job.vol, job.page).exists():  # 그 사이 지워진 쪽(일괄 삭제)은 읽지 않는다
                flow.ocr_one(job.vol, job.page)
            runs.maybe_put_graph(job.vol, run, force=job.attempts > 1)
        except Exception as e:
            errors.log_exception(f"ocr {job.vol}")
            store.event(job.vol, "ocr_page", "error", None, page=job.page, **errors.public(e))
            self.q.fail(job)
            return
        self.q.ack(job)

    # ---------- 그래프 ----------

    def _claim_graph(self, job, run):
        with store.pool.connection() as c:
            return c.execute("""UPDATE vol_run SET state = 'running', graph_job = %(j)s, updated_at = clock_timestamp()
                                WHERE vol = %(v)s AND run = %(r)s AND state <> 'ended' AND (graph_job IS NULL OR graph_job = %(j)s)
                                RETURNING kind, arg, started, user_id, username""",
                             {"j": job.id, "v": job.vol, "r": run}).fetchone()

    def _input(self, job, run, claim):
        """이번에 그래프에 넣을 입력. 첫 시도면 실행 종류대로, 앞선 시도가 시작했으면 체크포인트에서 이어 간다.
        ('done', None)이면 그래프가 이미 끝까지 갔다 (앞선 시도가 결과를 남기기 전에 죽음)"""
        main = _main()
        arg = claim["arg"] or {}
        if not claim["started"]:
            with store.pool.connection() as c:
                c.execute("UPDATE vol_run SET started = true WHERE vol = %s AND run = %s", (job.vol, run))
            return "go", first_input(job.vol, claim["kind"], arg)
        snap = main.G.get_state(main.cfg(job.vol))
        if snap.interrupts and claim["kind"] == "review":
            ids = set((arg.get("resume") or {}).keys())
            if snap.interrupts[0].id in ids:  # 검토 값을 넣기 전에 끊겼다
                return "go", Command(resume=arg.get("resume") or {})
        if snap.next and not snap.interrupts:
            return "go", None
        return "done", None

    def graph(self, job, run):
        main = _main()
        r = self._row_for(job, run)
        if r == "later":
            return self._later(job)
        if r is None:
            return self.q.ack(job)
        claim = self._claim_graph(job, run)
        if claim is None:  # 같은 실행의 graph 작업을 다른 작업이 돌리는 중 (넣기 재시도로 생긴 겹침) — 이것은 버린다
            return self.q.ack(job)
        vol, user = job.vol, _user(claim)
        lost = threading.Event()
        stop = threading.Event()
        beat = self.q.cfg.spec(GRAPH).heartbeat

        def heartbeat():
            while beat and not stop.wait(beat):
                if not self.q.heartbeat(job):
                    lost.set()  # 다른 워커가 되찾았다 — 끝나도 결과를 남기지 않는다
                    print(f"[worker] {vol} graph 작업을 잃음 (되찾김)", flush=True)
                    return
        hb = threading.Thread(target=heartbeat, daemon=True)
        hb.start()
        try:
            what, inp = self._input(job, run, claim)
            execute(main, vol, inp, user, invoke=what == "go",
                    claim=lambda outcome: not lost.is_set() and runs.end(vol, run, outcome, graph_job=job.id))
            if not lost.is_set():
                self.q.ack(job)
        except Exception as e:
            errors.log_exception(f"run {vol}")  # 상세는 서버 로그에만
            if lost.is_set():
                return
            last = job.attempts >= self.q.cfg.max_attempts
            if last and runs.end(vol, run, "error", graph_job=job.id):
                store.event(vol, "run", "error", None, **errors.public(e))
                main.audit_safe(user, "run_done", vol, None, "error", code=errors.code_of(e))
                self.q.ack(job)
                return
            store.event(vol, "run", "error", None, retry=job.attempts, **errors.public(e))  # 다시 해 본다 (시도 수)
            with store.pool.connection() as c:  # 다음 시도(새 항목 id일 수 있음)가 쥘 수 있게
                c.execute("UPDATE vol_run SET graph_job = NULL WHERE vol = %s AND run = %s AND graph_job = %s",
                          (vol, run, job.id))
            self.q.fail(job)
        finally:
            stop.set()

    # ---------- /ask ----------

    def ask(self, job):
        main = _main()
        r = runs.ask_get(job.ref)
        if r is None or r["state"] != "queued":
            return self.q.ack(job)
        user = SimpleNamespace(id=r["user_id"], username=r["username"] or "dev")
        try:
            out = main.ask_answer(r["q"], r["year_from"], r["year_to"], user)
        except llm.BudgetExceeded:
            runs.ask_done(job.ref, None, "budget")
        except llm.LLMOff:
            runs.ask_done(job.ref, None, "llm_off")
        except Exception:
            errors.log_exception("ask")
            if job.attempts < self.q.cfg.max_attempts:
                self.q.fail(job)
                return
            runs.ask_done(job.ref, None, "internal")
        else:
            runs.ask_done(job.ref, out)
        self.q.ack(job)

    # ---------- 돌기 ----------

    def run_once(self, kind, wait=0.0) -> bool:
        job = self.q.take(kind, self.name, wait=wait)
        if job is None:
            return False
        self.handle(job)
        return True

    def loop(self, kind, stopping: threading.Event):
        while not stopping.is_set():
            try:
                self.run_once(kind, wait=self.q.poll if hasattr(self.q, "poll") else 1.0)
            except Exception:
                errors.log_exception(f"[worker] {kind}")
                stopping.wait(2.0)  # 대기열·DB가 잠깐 안 될 때 빈 돌기를 막는다


def drain(worker=None, kinds=KINDS, limit=10_000):
    """시험·개발용: 꺼낼 작업이 없을 때까지 차례로 처리한다 (한 스레드). 처리한 작업 수"""
    w = worker or Worker()
    n = 0
    while n < limit:
        did = False
        for kind in kinds:
            while n < limit and w.run_once(kind):
                n += 1
                did = True
        if not did:
            return n
    return n


def resume_unfinished(main=None):
    """기동 때: 그래프가 중간에 멈춘 철(검토 대기 아님)을 이어 간다 — 실행 중·한도 멈춤·마지막 실행 오류인 철은 빼고.
    여러 워커가 함께 떠도 실행 번호 확정(commit)을 조건부로 해 하나만 넣는다. 넣은 철 목록"""
    main = main or _main()
    out = []
    for vol in main.volume_names():
        try:
            snap = main.G.get_state(main.cfg(vol))
            if not snap.next or snap.interrupts:
                continue
            r = runs.row(vol)
            if r and (r["state"] in runs.ACTIVE or r["outcome"] in ("error", "budget")):
                continue
            if store.run_stop(vol) == "budget":
                continue
            run = runs.next_run()
            with store.pool.connection() as c:  # 아직 아무도 확정하지 않았을 때만 (다른 워커와 겹치지 않게)
                won = c.execute("""INSERT INTO vol_run (vol, run, kind, arg, state) VALUES (%s, %s, 'resume', '{}'::jsonb, 'queued')
                                   ON CONFLICT (vol) DO UPDATE SET run = EXCLUDED.run, kind = 'resume', arg = '{}'::jsonb,
                                     state = 'queued', outcome = NULL, graph_put = true, graph_job = NULL, started = false,
                                     updated_at = clock_timestamp()
                                   WHERE vol_run.state = 'ended' AND vol_run.outcome IS DISTINCT FROM 'error'
                                     AND vol_run.outcome IS DISTINCT FROM 'budget'
                                   RETURNING run""", (vol, run)).fetchone()
            if not won:
                continue
            try:
                runs.put([NewJob(GRAPH, vol=vol, ref=str(run))])
            except (jobqueue.QueueFull, jobqueue.QueueBusy):
                runs.end(vol, run, "error")
                continue
            store.event(vol, "run", "start", None, resume=True, auto=True)
            out.append(vol)
        except Exception:
            errors.log_exception(f"[worker] 자동 재개 {vol}")
    if out:
        print(f"[worker] 끝나지 않은 철 {len(out)}개 이어 감", flush=True)
    return out


def serve():
    """`python -m app.worker` — 종류마다 스레드, SIGTERM이면 지금 작업을 끝내고 멈춘다 (못 끝낸 작업은 되찾기가 잇는다)"""
    from langgraph.checkpoint.postgres import PostgresSaver

    from app import graph as flow
    main = _main()
    store.init()
    saver = PostgresSaver(store.pool)
    saver.setup()
    main.G = flow.build(saver)
    kinds = [k.strip() for k in os.environ.get("WORKER_KINDS", ",".join(KINDS)).split(",") if k.strip()]
    for k in kinds:
        if k not in KINDS:
            raise SystemExit(f"WORKER_KINDS: 모르는 종류 {k!r}")
    if GRAPH in kinds:
        resume_unfinished(main)
    stopping = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopping.set())
    w = Worker()
    print(f"[worker] {w.name} 시작: {', '.join(kinds)}", flush=True)
    threads = [threading.Thread(target=w.loop, args=(k, stopping), name=f"worker-{k}", daemon=True) for k in kinds]
    for t in threads:
        t.start()
    while not stopping.wait(5):
        pass
    for t in threads:
        t.join(timeout=60)
    store.pool.close()


if __name__ == "__main__":
    serve()
