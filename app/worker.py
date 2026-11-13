"""대기열 워커 (https://github.com/reddol18/docflow/issues/5 2단계). API와 같은 이미지에서 `python -m app.worker`로 띄운다.

종류마다 스레드 하나(WORKER_KINDS, 기본 ocr_page,graph,ask)가 대기열에서 꺼내 처리한다:
  ocr_page  쪽 하나 OCR(app/graph.py ocr_one) → 남은 쪽이 0이면 graph 작업을 한 번 넣는다 (app/runs.py page_done·maybe_put_graph)
  graph     철 그래프를 돌린다. 끝·검토 대기·한도 멈춤·오류를 예전 runner와 같게 이벤트·감사 로그·run_stop에 남긴다
  ask       /ask 요청 하나 (질문은 DB ask_request, 대기열에는 id만) — 검색 + 답변을 행에 남긴다. API가 행을 읽어 돌려준다
임대 지키기 (Keeper): graph·ask는 살아 있음 표시(QUEUE_HEARTBEAT_GRAPH 30초·QUEUE_HEARTBEAT_ASK 20초)를 대기열과 DB 행에 함께 한다.
  표시가 거절되면(다른 워커가 되찾음) 또는 오류로 (유휴 시간 - 표시 간격)초 넘게 못 하면 임대를 잃은 것으로 보고, 그래프는
  취소 표시(app/cancel.py)로 다음 노드·LLM 예약 직전에 끊는다. 그래프·질문 행은 (작업 id, 임대) 토큰으로 쥐고(vol_run.graph_lease,
  ask_request.claimed_by) 결과도 그 토큰으로만 남긴다 — 되찾은 워커와 옛 워커가 같은 철 그래프를 함께 돌리거나 결과를 덮지 않게.
실패는 fail(시도로 셈, QUEUE_MAX_ATTEMPTS 3번이면 죽은 작업 → app/runs.py on_dead가 실행을 오류로 닫는다). 비용 한도 멈춤은
실패가 아니다(ack, 예전처럼 /resume으로 이어 감). 행이 아직 준비 중(preparing)인 작업은 시도로 세지 않고 되돌린다(release).
작업 본문은 믿지 않는다 — 철·쪽 이름을 경로에 쓰기 전에 API와 같은 규칙(main.VOL_RX, upload.page_of)과 store.under로 다시 본다.
  규칙에 안 맞는 작업은 버리고, 실행 번호를 알면 그 실행을 오류로 닫는다.
감시 (SWEEP_SECONDS마다): 활성인데 작업 없이 오래 그대로인 실행을 오류로 닫는다 (app/runs.py sweep).
기동할 때: 끝나지 않은 철 자동 재개 (resume_unfinished) — 그래프가 중간에 멈춰 있고(next 있음·검토 대기 아님) 실행 중이 아니며
마지막 실행이 오류·한도 멈춤이 아닌 철에 graph 이어 가기 작업을 넣는다 (예전 판에서 Pod 재시작으로 끊긴 철). 실행 중인 철은
대기열에 작업이 남아 있으므로(Postgres 표·Redis AOF) 되찾기가 잇는다.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from types import SimpleNamespace

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command

from app import cancel, errors, jobqueue, llm, runs, store
from app import upload as uploads
from app.jobqueue import ASK, GRAPH, OCR_PAGE, NewJob

KINDS = (OCR_PAGE, GRAPH, ASK)
NOT_YET_WAIT = 1.0  # 행이 준비 중(preparing)인 작업을 되돌리기 전 기다림
HOLD_FACTOR = 2.0  # 작업을 쥔 채 기다리는 상한 = 유휴 시간 × 이것
HOLD_WAIT = 2.0  # 앞선 임대가 살아 있어 작업을 쥔 채 기다릴 때 다시 볼 간격 (살아 있음 표시 간격 20·30초보다 짧게)
SWEEP_SECONDS = float(os.environ.get("SWEEP_SECONDS", "120"))


def first_input(vol, kind, arg):
    """실행 종류 → 그래프 첫 입력: start {vol, review_mode} / review Command(resume=검토 값) / resume None(멈춘 곳부터)"""
    arg = arg or {}
    if kind == "start":
        return {"vol": vol, "review_mode": arg.get("review_mode", "human")}
    if kind == "review":
        return Command(resume=arg.get("resume") or {})
    return None


def unfinished(snap) -> bool:
    """그래프가 중간에 멈췄나 (검토 대기 아님). next만 보면 안 된다: Postgres 체크포인터에서 끊기면(Cancelled — 다음 체크포인트는
    막히고 앞 단계의 대기 쓰기만 남음) next가 비고 tasks에만 남는다 (tests/test_cancel_graph.py). 끝난 그래프는 둘 다 빈다"""
    return bool(snap.next or getattr(snap, "tasks", ())) and not snap.interrupts


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
    outcome = "review" if snap.interrupts else "error" if unfinished(snap) else "ok"  # 끝나지도 멈추지도 않음 → 오류 (/resume)
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


class FencedSaver(cancel.FenceMixin, PostgresSaver):
    """워커의 체크포인터: 임대를 잃은 뒤 진행 중이던 노드의 늦은 체크포인트 쓰기를 막는다 (app/cancel.py FenceMixin).
    langgraph-checkpoint-postgres 3.2.0에서 그래프 실행이 부르는 쓰기는 put(체크포인트 + 채널 블롭 — _dump_blobs·UPSERT 블롭이
    put 안)과 put_writes(대기 쓰기)뿐이고(비동기 aput·aput_writes는 FenceMixin이 같이 덮는다), copy_thread·delete_thread·
    delete_for_runs·prune은 그래프 실행이 부르지 않는 관리용 — 워커는 쓰지 않는다 (철 일괄 삭제의 delete_thread는 API)"""


class BadJob(ValueError):
    """본문의 철·쪽 이름·실행 번호가 규칙에 맞지 않음 — 다시 해도 같으므로 버린다"""


class Keeper:
    """작업 임대 지키기: beat초마다 대기열 heartbeat + 행 표시(row_beat). 거절되면 바로, 오류가 (idle - beat)초 넘게 이어지면
    임대를 잃은 것으로 본다 (그때쯤 다른 워커가 되찾을 수 있다) → lost, on_lost()"""

    def __init__(self, q, job, beat, idle, row_beat=None, on_lost=None):
        self.q, self.job, self.beat, self.idle = q, job, beat, idle
        self.row_beat, self.on_lost = row_beat, on_lost
        self.lost, self._stop = threading.Event(), threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True, name=f"keeper-{job.kind}")

    def __enter__(self):
        if self.beat:
            self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        return False

    def settle(self, fn):
        """결과를 남길 때: 아직 쥐고 있으면 표시를 먼저 멈추고 fn() — 끝난 행에 표시하다 거절돼 '잃음'으로 잘못 보거나
        되찾기를 부르지 않게. 이미 잃었으면 False"""
        if self.lost.is_set():
            return False
        self._stop.set()
        return fn()

    def _lose(self, why):
        if self._stop.is_set():
            return  # 결과를 남기는 중·끝난 뒤의 거절은 잃음이 아니다
        self.lost.set()
        print(f"[worker] {self.job.kind} 작업 임대를 잃음 ({why})", flush=True)
        if self.on_lost:
            self.on_lost()

    def _run(self):
        last_ok = time.monotonic()
        while not self._stop.wait(self.beat):
            try:
                ok = self.q.heartbeat(self.job) and (self.row_beat() if self.row_beat else True)
                if self._stop.is_set():
                    return
            except Exception as e:  # noqa: BLE001 — 대기열·DB가 잠깐 안 될 때: 다시 해 보다가 늦으면 잃은 것으로
                print(f"[worker] 살아 있음 표시 실패: {type(e).__name__}", flush=True)
                if time.monotonic() - last_ok > self.idle - self.beat:
                    return self._lose("표시 실패가 이어짐")
                continue
            if not ok:
                return self._lose("다른 워커가 되찾음")
            last_ok = time.monotonic()


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
    def __init__(self, q=None, name=None, stopping=None):
        self.q = q or runs.queue()
        self.name = name or jobqueue.consumer_name()
        self.stopping = stopping or threading.Event()  # 멈추라는 신호 (SIGTERM) — 기다리던 작업은 되돌리지 않고 놓는다

    # ---------- 공통 ----------

    def _row_for(self, job, run):
        """이 작업의 실행 행. 준비 중이면 'later', 다른 실행(옛 것·되돌린 것)이거나 끝났으면 None"""
        r = runs.row(job.vol)
        if r is None or r["run"] != run or r["state"] == "ended":
            return None
        return "later" if r["state"] == "preparing" else r

    def _later(self, job):
        time.sleep(NOT_YET_WAIT)
        self.q.release(job)  # 시도로 세지 않는다 (행이 준비 중일 때만 — 아직 아무도 그래프를 쥐지 않았다)

    def _hold(self, job, attempt):
        """같은 작업을 쥔 채 기다리며 attempt()를 다시 한다 — 대기열 살아 있음 표시를 계속하면서. 되돌리지(release) 않는다:
        Redis는 되돌리면 새 항목 id가 생겨, 앞선 임대가 쥔 작업 id(graph_job)와 달라져 '다른 작업'으로 버려진다.
        attempt()가 'retry'가 아닌 값을 주면 그것을, 아니면 None — 이 작업의 대기열 임대를 잃었을 때, 멈추라는 신호(stopping)를
        받았을 때, 상한(유휴 시간 2배 — 앞선 임대는 그 안에 표시가 멈추거나 끝나야 한다)을 넘었을 때. None이면 작업을 그대로 두고
        (ack·release 없이) 손을 뗀다 — 대기열의 되찾기가 유휴 시간 뒤 다시 준다"""
        idle = self.q.cfg.spec(job.kind).idle
        deadline = time.monotonic() + HOLD_FACTOR * idle
        while True:
            if self.stopping.wait(min(HOLD_WAIT, idle / 4)):
                print(f"[worker] {job.kind} 기다리다 멈춤 신호 — 작업을 놓는다 (되찾기가 잇는다)", flush=True)
                return None
            if time.monotonic() > deadline:
                print(f"[worker] {job.kind} 앞선 임대를 {HOLD_FACTOR * idle:.0f}초 기다려도 그대로 — 작업을 놓는다 (되찾기가 다시 준다)",
                      flush=True)
                return None
            try:
                if not self.q.heartbeat(job):
                    print(f"[worker] {job.kind} 기다리는 사이 대기열 임대를 잃음", flush=True)
                    return None
            except Exception as e:  # noqa: BLE001 — 잠깐 안 되면 다시 (잃었으면 다음 표시가 거절된다)
                print(f"[worker] 기다리는 중 표시 실패: {type(e).__name__}", flush=True)
                continue
            got = attempt()
            if got != "retry":
                return got

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
            if job.kind != ASK and str(e) != "실행 번호" and job.vol and _main().VOL_RX.fullmatch(job.vol):
                try:  # 철·실행을 알면 그 실행을 닫는다 (영원히 '실행 중'으로 남지 않게) — 쪽 이름만 틀린 경우
                    runs.close_error(job.vol, int(job.ref), "bad_job", job=job.kind)
                except (TypeError, ValueError):
                    pass

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
            if runs.page_done(job.vol, run, job.page) == 0:
                runs.maybe_put_graph(job.vol, run, force=job.attempts > 1)
        except Exception as e:
            errors.log_exception(f"ocr {job.vol}")
            store.event(job.vol, "ocr_page", "error", None, page=job.page, **errors.public(e))
            self.q.fail(job)
            return
        self.q.ack(job)

    # ---------- 그래프 ----------

    def _input(self, job, run, claim, token):
        """이번에 그래프에 넣을 입력. 첫 시도면 실행 종류대로, 앞선 시도가 시작했으면 체크포인트에서 이어 간다.
        ('done', None)이면 그래프가 이미 끝까지 갔다 (앞선 시도가 결과를 남기기 전에 죽음)"""
        main = _main()
        arg = claim["arg"] or {}
        if not claim["started"]:
            runs.mark_started(job.vol, run, token)
            return "go", first_input(job.vol, claim["kind"], arg)
        snap = main.G.get_state(main.cfg(job.vol))
        if snap.interrupts and claim["kind"] == "review":
            if snap.interrupts[0].id in set((arg.get("resume") or {}).keys()):  # 검토 값을 넣기 전에 끊겼다
                return "go", Command(resume=arg.get("resume") or {})
        if unfinished(snap):
            return "go", None
        return "done", None

    def graph(self, job, run):
        main = _main()
        vol = job.vol
        r = self._row_for(job, run)
        if r == "later":
            return self._later(job)
        if r is None:
            return self.q.ack(job)
        spec = self.q.cfg.spec(GRAPH)
        token = (job.id, job.lease)
        status, claim = runs.claim_graph(vol, run, job.id, job.lease, stale=spec.idle)
        if status == "wait":  # 같은 작업의 앞선 임대가 아직 살아 있다 — 쥔 채 기다렸다가 그쪽 표시가 멈추면 넘겨받는다

            def again():
                if self._row_for(job, run) is None:
                    return ("gone", None)
                got = runs.claim_graph(vol, run, job.id, job.lease, stale=spec.idle)
                return "retry" if got[0] == "wait" else got
            got = self._hold(job, again)
            if got is None:
                return  # 기다리는 사이 이 작업의 대기열 임대를 잃었다 — 다른 워커가 잇는다
            status, claim = got
            if status == "gone":
                return self.q.ack(job)
        if status == "dup":  # 같은 실행을 다른 graph 작업이 쥐고 있다 (넣기 재시도로 생긴 겹침) — 이것은 버린다
            return self.q.ack(job)
        user = _user(claim)
        ev = cancel.arm(vol)
        try:
            with Keeper(self.q, job, spec.heartbeat, spec.idle, row_beat=lambda: runs.beat_graph(vol, run, token),
                        on_lost=ev.set) as keep:
                try:
                    what, inp = self._input(job, run, claim, token)
                    execute(main, vol, inp, user, invoke=what == "go",
                            claim=lambda outcome: keep.settle(lambda: runs.end(vol, run, outcome, token)))
                    if not keep.lost.is_set():
                        self.q.ack(job)
                except cancel.Cancelled:
                    print(f"[worker] {vol} 그래프를 끊음 — 임대를 잃음 (되찾은 워커가 잇는다)", flush=True)
                except Exception as e:
                    errors.log_exception(f"run {vol}")  # 상세는 서버 로그에만
                    if keep.lost.is_set():
                        return
                    if job.attempts >= self.q.cfg.max_attempts and runs.end(vol, run, "error", token):
                        store.event(vol, "run", "error", None, **errors.public(e))
                        main.audit_safe(user, "run_done", vol, None, "error", code=errors.code_of(e))
                        self.q.ack(job)
                        return
                    store.event(vol, "run", "error", None, retry=job.attempts, **errors.public(e))  # 다시 해 본다
                    runs.release_graph(vol, run, token)  # 다음 시도(새 항목 id일 수 있음)가 쥘 수 있게
                    self.q.fail(job)
        finally:
            cancel.disarm(vol, ev)

    # ---------- /ask ----------

    def ask(self, job):
        main = _main()
        spec = self.q.cfg.spec(ASK)
        token = f"{job.id}:{job.lease}"
        r = runs.ask_claim(job.ref, token, stale=spec.idle)
        if r is None:  # 끝났거나(버림) 다른 워커가 처리 중 — 그 워커가 죽으면 넘겨받도록 쥔 채 기다린다

            def again():
                row = runs.ask_get(job.ref)
                if row is None or row["state"] in ("done", "error"):
                    return "gone"
                return runs.ask_claim(job.ref, token, stale=spec.idle) or "retry"
            row = runs.ask_get(job.ref)
            r = "gone" if row is None or row["state"] in ("done", "error") else self._hold(job, again)
            if r is None:
                return  # 기다리는 사이 대기열 임대를 잃었다
            if r == "gone":
                return self.q.ack(job)
        user = SimpleNamespace(id=r["user_id"], username=r["username"] or "dev")
        with Keeper(self.q, job, spec.heartbeat, spec.idle, row_beat=lambda: runs.ask_beat(job.ref, token)) as keep:
            try:
                out = main.ask_answer(r["q"], r["year_from"], r["year_to"], user)
            except llm.BudgetExceeded:
                keep.settle(lambda: runs.ask_done(job.ref, None, "budget", token))
            except llm.LLMOff:
                keep.settle(lambda: runs.ask_done(job.ref, None, "llm_off", token))
            except Exception:
                errors.log_exception("ask")
                if job.attempts < self.q.cfg.max_attempts:
                    if keep.settle(lambda: runs.ask_unclaim(job.ref, token)):  # 다음 시도가 다시 쥘 수 있게
                        self.q.fail(job)
                    return
                keep.settle(lambda: runs.ask_done(job.ref, None, "internal", token))
            else:
                keep.settle(lambda: runs.ask_done(job.ref, out, None, token))
        if keep.lost.is_set():  # 임대를 잃어 결과를 남기지 않았다 — ack하면 행이 running으로 남는다. 되찾기에 맡긴다
            print("[worker] ask 임대를 잃어 결과를 남기지 않음 (되찾은 워커가 잇는다)", flush=True)
            return
        self.q.ack(job)

    # ---------- 돌기 ----------

    def run_once(self, kind, wait=0.0) -> bool:
        job = self.q.take(kind, self.name, wait=wait)
        if job is None:
            return False
        self.handle(job)
        return True

    def loop(self, kind, stopping: threading.Event):
        self.stopping = stopping
        while not stopping.is_set():
            try:
                self.run_once(kind, wait=getattr(self.q, "poll", 1.0))
            except Exception:
                errors.log_exception(f"[worker] {kind}")
                stopping.wait(2.0)  # 대기열·DB가 잠깐 안 될 때 빈 돌기를 막는다

    def sweeper(self, stopping: threading.Event):
        while not stopping.wait(SWEEP_SECONDS):
            try:
                runs.sweep(self.q)
            except Exception:
                errors.log_exception("[worker] sweep")


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
    여러 워커가 함께 떠도 행 확정(runs.prepare, 끝난 행일 때만)이 하나만 이긴다. 넣은 철 목록"""
    main = main or _main()
    out = []
    for vol in main.volume_names():
        try:
            snap = main.G.get_state(main.cfg(vol))
            if not unfinished(snap):
                continue
            if store.run_stop(vol) == "budget":
                continue
            run = runs.next_run()
            try:  # 실행 중·마지막 실행 오류·한도 멈춤이면 확정 조건(WHERE)에서 거른다 — 확인과 확정 사이에 끼는 일 없음
                prev = runs.prepare(vol, run, "resume", {}, graph_put=True, skip_outcomes=("error", "budget"))
            except runs.Busy:
                continue
            try:
                runs.put([NewJob(GRAPH, vol=vol, ref=str(run))])
            except (jobqueue.QueueFull, jobqueue.QueueBusy):
                runs.rollback(vol, run, prev)
                continue
            runs.ready(vol, run)
            store.event(vol, "run", "start", None, resume=True, auto=True)
            out.append(vol)
        except Exception:
            errors.log_exception(f"[worker] 자동 재개 {vol}")
    if out:
        print(f"[worker] 끝나지 않은 철 {len(out)}개 이어 감", flush=True)
    return out


def serve():
    """`python -m app.worker` — 종류마다 스레드 + 감시, SIGTERM이면 지금 작업을 끝내고 멈춘다 (못 끝낸 작업은 되찾기가 잇는다)"""
    from app import graph as flow
    main = _main()
    store.init()
    saver = FencedSaver(store.pool)
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
    w = Worker(stopping=stopping)
    print(f"[worker] {w.name} 시작: {', '.join(kinds)}", flush=True)
    threads = [threading.Thread(target=w.loop, args=(k, stopping), name=f"worker-{k}", daemon=True) for k in kinds]
    threads.append(threading.Thread(target=w.sweeper, args=(stopping,), name="worker-sweep", daemon=True))
    for t in threads:
        t.start()
    while not stopping.wait(5):
        pass
    for t in threads:
        t.join(timeout=60)
    store.pool.close()


if __name__ == "__main__":
    serve()
