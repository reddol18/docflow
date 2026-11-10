"""docflow API.

  GET   /healthz                       살아 있는지 (로그인 없음)
  GET   /volumes                       철 목록과 철마다 상태 요약
  POST  /volumes/{vol}/pages           쪽 이미지 올리기 (multipart, 여러 장, JPEG만)
  POST  /volumes/{vol}/run             흐름 시작 (?review=human|auto)
  POST  /volumes/{vol}/resume          멈춘 곳부터 다시 (Pod 재시작 뒤 등). 사람 검토 대기는 /review로 이어 간다
  GET   /volumes/{vol}                 상태: 다음 노드, 검토 대기 여부, 문서 수, 비용
  GET   /volumes/{vol}/events          노드 이벤트 (진행 단계 화면)
  GET   /volumes/{vol}/documents       문서별 결과 (B 값·손글씨 판독·표시 포함)
  POST  /volumes/{vol}/timings         검토 화면이 잰 문서별 작업 시간
  PATCH /documents/{vol}/{doc_id}      완료된 문서의 색인 칸 고치기 (재색인)
  GET   /documents/{vol}/{doc_id}/edits  수정 이력 (칸·이전 값·새 값·사용자·시각·단계 입력/검사)
  POST  /volumes/{vol}/inspection      검사 목록 만들기 (표시 문서 전부 + 표본 + 100% 검수 칸) — 검사자
  GET   /volumes/{vol}/inspection      검사 목록과 진행 (문서마다 이유·확인할 칸·검사 가능 여부·막힌 까닭 block)
  POST  /inspection/{vol}/{doc_id}     검사 결과 제출 (고친 칸만, 없으면 통과). 자기가 입력한 문서는 403
  GET   /workflow                      검토 흐름 설정 (단계·맡는 역할·전수/표본·반려 규칙 — app/workflows/*.yaml)
  GET   /volumes/{vol}/workflow        문서마다 지금 단계·차례(재검수)·반려 사유·내가 승인/반려할 수 있나
  POST  /workflow/{vol}/{doc_id}/approve  단계 승인 (고친 칸 함께). 마지막 단계면 확정
  POST  /workflow/{vol}/{doc_id}/reject   단계 반려 (사유 필수) → 정해진 앞 단계로, 다시 올라오면 재검수
  GET   /documents/{vol}/{doc_id}/stages  단계 승인·반려 기록 (단계·차례·사람·사유·고친 칸)
  GET   /crops/{vol}/{name}            손글씨 칸·수신·제목 줄을 잘라 둔 이미지
  GET   /head/{vol}/{page}             쪽 위 45% 줄인 이미지 (검토용)
  GET   /review/{vol}                  사람 검토 화면 → 아래 POST로 고친 값을 넣으면 흐름이 이어진다
  POST  /review/{vol}                  검토 화면 폼 제출
  POST  /review/{vol}/json             검토 값을 JSON으로 제출 (스크립트·Next 검토 화면)
  GET   /search                        ?q=&year_from=&year_to=&k= 벡터(Qdrant) + 낱말(Postgres) 결과를 RRF로 합침 (사용자별 호출 제한)
  GET   /ask                           ?q= 검색 상위 문서로 GPT가 답하고 근거 doc_id를 단다 (사용자별 호출 제한)
  GET   /graph                         흐름 그림 (mermaid)
  GET   /schema                        색인 칸 스키마 (보이는 칸·rule·손글씨 칸, 검색 열) — 화면이 칸 목록을 여기서 받는다
  GET   /report                        공정·품질 리포트 (?day_from=&day_to=, 기본 최근 14일)
  GET   /report.csv                    같은 리포트 CSV
  GET   /export/check                  출력 전 검증 (?vol=…, 없으면 끝난 철 전부): 필수 빈칸·형식 위반·검토 안 끝난 건·칸 규칙 위반
  GET   /export.xlsx                   납품 양식 엑셀 (철 목록·건 색인 시트, 쪽 순서 유지) — 관리자
  GET   /export/errors.xlsx            검수 오류목록 엑셀 (?vol=…, 문서·칸·오류 내용·수정 전후 값·담당자·조치일·재검수 판정) — 관리자
  DELETE /volumes/{vol}                납품 뒤 사업 데이터 일괄 삭제 (?confirm=철 이름, 내보낸 철만) — 관리자
  /auth/*                              로그인·로그아웃·OIDC (app/auth.py)
머리말 목록은 시험(`tests/test_input.py`)이 실제 라우트와 비교한다 (https://github.com/reddol18/docflow/issues/49).

인증: /healthz와 로그인 경로를 빼고 모두 로그인 필요 (앱 전체 의존성 auth.guard, 기본 거부).
역할은 경로마다 더 좁힌다 — 보기 viewer, 검토·수정(입력) reviewer, 검사 inspector, 업로드·실행 admin.
검토 단계(입력 뒤 검수 단계들)와 단계마다 맡는 역할은 흐름 설정 WORKFLOW가 정한다 (app/workflow.py,
https://github.com/reddol18/docflow/issues/86). 설정이 없으면 위의 입력 → 표본 검사 2단계 그대로.
"""
import datetime as dt
import filecmp
import html
import os
import re
import secrets
import sys
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from typing import Annotated, Literal

import cv2
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import auth, error_list, errors, export, field_rules, flags, inspection, layout, llm, ocr, report, store, workflow
from app import fields as schema_fields  # 'fields'는 경로 함수 인자 이름(고친 칸 dict)으로 쓰인다
from app import graph as flow
from app import upload as uploads  # 경로 함수 이름 upload와 겹치지 않게
from app.auth import Admin, Reviewer, Viewer, audit_as
from app.review_input import EDITABLE, changes, clean_fixes

# 점만으로 된 이름(., .., ...)은 경로 이탈이라 거부. fullmatch + \Z — '$'는 끝 줄바꿈 앞에서도 맞아 'v1\n'이 지나간다
VOL_RX = re.compile(r"(?!\.+\Z)[\w.-]{1,80}")
Q_MAX = 200  # 검색어 길이 상한: 질의는 문서 제목(색인 칸 500자 안)보다 짧고, 임베딩·답변 입력 토큰을 묶는다
CELL_MAX = 500  # 색인 칸 하나의 길이 상한 (제목도 이 안에 든다) — JSON·폼 검토와 검토 화면 입력칸이 같이 쓴다
CellValue = Annotated[str, Field(max_length=CELL_MAX)]
runner = ThreadPoolExecutor(max_workers=1)  # OCR이 CPU를 다 쓰므로 철은 한 번에 하나씩
running: dict[str, str] = {}  # vol -> 상태 문자열
G = None


@asynccontextmanager
async def lifespan(_):
    global G
    store.init()
    saver = PostgresSaver(store.pool)
    saver.setup()
    G = flow.build(saver)
    startup_migrate()
    yield
    auth.audit_flush()  # 미뤄 둔 로그인·로그아웃 감사 행을 쓰고 닫는다
    store.lock_pool.close()
    store.pool.close()


def vol_guard(request: Request):
    """경로의 철 이름을 본문 검증보다 먼저 본다 — 업로드처럼 본문이 필요한 경로도 잘못된 철 이름은 400 (422가 아니라).
    앱 의존성 순서상 로그인 확인(auth.guard) 다음에 돈다 → 로그인 안 한 요청은 그대로 401."""
    vol = request.path_params.get("vol")
    if vol is not None:
        check(vol)


# /docs·/openapi.json은 guard 밖이라 기본으로 끈다 (API_DOCS=1이면 개발용으로 켬)
_docs = os.environ.get("API_DOCS") == "1"
app = FastAPI(title="docflow", lifespan=lifespan, dependencies=[Depends(auth.guard), Depends(vol_guard)],
              docs_url="/docs" if _docs else None, redoc_url=None, openapi_url="/openapi.json" if _docs else None)
app.include_router(auth.router)
# 업로드 본문 상한 — 받은 바이트를 세며 본문 상한·/tmp 예산에서 끊는다 (app/upload.py). 본문은 처리 함수가 로그인 확인 뒤에 읽는다
UPLOAD_PATH_RX = re.compile(r"/volumes/[^/]+/pages")
app.add_middleware(uploads.BodyCap, match=lambda method, path: method == "POST" and UPLOAD_PATH_RX.fullmatch(path))

# 응답 보안 헤더 (https://github.com/reddol18/docflow/issues/47, ASVS V14.4.3·V14.4.4·V14.4.7·V8.2.1)
# API는 무엇도 그리지 않으므로 'none'. 서버가 그리는 HTML(/review)만 그 화면에 필요한 만큼 연다 (인라인 스크립트는 없다)
API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
HTML_CSP = ("default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'none'")
SECURITY_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY"}


@app.exception_handler(store.PathEscape)
async def path_escape(request: Request, exc: store.PathEscape):
    return PlainTextResponse("bad path", status_code=400)


@app.exception_handler(llm.LLMOff)
async def llm_off(request: Request, exc: llm.LLMOff):
    """LLM을 쓰지 않는 설치(LLM_PROVIDER=off)에서 LLM이 필요한 요청 — 화면엔 errors 공개 문구만, 설정 이름은 서버 로그에"""
    print(f"[llm_off] {request.method} {request.url.path}: {exc}", file=sys.stderr, flush=True)
    return JSONResponse(errors.public(exc), status_code=503)


@app.exception_handler(llm.BudgetExceeded)
async def budget_exceeded(request: Request, exc: llm.BudgetExceeded):
    """요청 안에서 부른 LLM이 그날 예산에 걸림 (검색·질문은 query 예산, 문서 수정 임베딩은 index 예산) — 오류가 아니라
    한도라 503 + Retry-After(다음 날 0시 KST까지). 화면엔 errors 공개 문구만"""
    print(f"[budget] {request.method} {request.url.path}: {exc}", file=sys.stderr, flush=True)
    return JSONResponse(errors.public(exc), status_code=503, headers={"Retry-After": str(_until_tomorrow())})


def _until_tomorrow():
    _, start = store.kst_day()
    return max(1, int((start + dt.timedelta(days=1) - dt.datetime.now(store.KST)).total_seconds()))


@app.exception_handler(store.LockBusy)
async def lock_busy(request: Request, exc: store.LockBusy):
    """문서·철 잠금을 store.LOCK_WAIT 안에 얻지 못함 (https://github.com/reddol18/docflow/issues/86)"""
    return JSONResponse({"detail": "다른 사람이 처리 중 — 잠시 뒤 다시"}, status_code=409)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    try:
        res = await call_next(request)
    except Exception:  # 처리 안 된 예외의 500은 이 미들웨어 바깥(ServerErrorMiddleware)에서 만들어져 헤더가 빠진다 → 여기서 만든다
        errors.log_exception(f"{request.method} {request.url.path}")
        res = PlainTextResponse("Internal Server Error", status_code=500)
    for k, v in SECURITY_HEADERS.items():
        res.headers[k] = v  # 민감 응답(색인·쪽 이미지·리포트)이 브라우저·중간 캐시에 남지 않게 모두 no-store
    if _docs and request.url.path.startswith("/docs"):
        return res  # 개발용 Swagger UI(API_DOCS=1)는 CDN 스크립트·인라인 스크립트가 필요하다 — CSP를 걸지 않는다
    html_page = res.headers.get("content-type", "").startswith("text/html")
    res.headers["Content-Security-Policy"] = HTML_CSP if html_page else API_CSP
    return res


def cfg(vol):
    return {"configurable": {"thread_id": vol}, "max_concurrency": 4}


def check(vol):
    if not VOL_RX.fullmatch(vol):
        raise HTTPException(400, "bad volume id")


@contextmanager
def audited(user, action, vol=None, doc_id=None, **info):
    """감사 로그 한 줄 (https://github.com/reddol18/docflow/issues/28): 성공 ok, 요청 거절 rejected_<상태>, 예외 error(분류 코드).
    처리 중에 yield된 info에 개수·칸 이름 같은 요약을 더할 수 있다 — 값·원문은 넣지 않는다.
    성공 결과를 ok 말고 다른 이름으로 남기려면 info["result"] (예: 바뀐 것 없는 업로드 unchanged)"""
    try:
        yield info
    except HTTPException as e:
        info.pop("result", None)
        audit_safe(user, action, vol, doc_id, f"rejected_{e.status_code}", **info)
        e.audited = True  # 예외 처리기(rejected_http)가 같은 거절을 다시 남기지 않게
        raise
    except store.LockBusy:  # 다른 요청이 같은 문서·철을 처리 중 → 409 (lock_busy 처리기)
        info.pop("result", None)
        audit_safe(user, action, vol, doc_id, "rejected_409", **info)
        raise
    except Exception as e:
        info.pop("result", None)
        audit_safe(user, action, vol, doc_id, "error", code=errors.code_of(e), **info)
        raise
    audit_safe(user, action, vol, doc_id, info.pop("result", "ok"), **info)


def audit_safe(user, action, vol=None, doc_id=None, result="ok", **info):
    """감사 기록이 실패해도 이미 끝난 처리를 500으로 바꾸거나 원래 거절(409 등)을 가리지 않는다 — 실패는 서버 로그로.
    쓰기는 auth.audit_event 한 곳 (처리 함수 안의 행은 바로 쓴다 — 교차검사가 audit의 review 행을 읽는다)"""
    auth.audit_event(user, action, result, vol, doc_id, **info)


# ---------- 처리 함수 앞에서 거절된 요청 (https://github.com/reddol18/docflow/issues/63) ----------
# 로그인(401)·역할·CSRF(403)·본문 검증(422)은 의존성·FastAPI가 처리 함수 앞에서 막아 audited()까지 오지 않는다.
# 예외 처리기에서 경로의 감사 행위 이름(auth.audit_as)으로 rejected_<상태> 한 행을 남긴다:
#   대상  GET이 아닌 경로 전부 + audit_as가 붙은 GET 경로(엑셀 반출·OIDC 콜백)
#   남김  행위·결과·메서드·사유 분류(no_session/bad_session/csrf/role)·422 오류 개수, 규칙에 맞는 철·문서 id
#   안 남김  헤더·쿠키·토큰·본문·검증 오류 내용(입력 값이 들어 있다)·규칙에 안 맞는 경로 값
#   중복  audited() 안에서 난 거절은 이미 남겼으므로(e.audited) 건너뛴다
#   상한  사용자를 모르는 행(401 등)과 CSRF 거절(공격 사이트가 일으킨다)은 auth.audit_event의 상한 창(AUDIT_ANON_MAX/60초)
#   실패  감사 쓰기·분류가 실패해도 원래 401·403·422 응답은 그대로 (서버 로그로)
REJECT_AUDIT = {401, 403, 422}
ID_RX = re.compile(r"[\w.-]{1,120}")


def _session_owner(request):
    """본문을 못 읽은 422(깨진 JSON 등)는 로그인 확인(guard)보다 먼저 난다 → 세션 토큰으로 사용자만 가볍게 찾는다.
    guard가 같은 요청에 하는 조회와 같다. CSRF 확인은 하지 않는다 (누구 요청인지만 — 권한을 주지 않는다)"""
    if not auth.MODES:
        return auth.DEV_USER
    token, _ = auth._token(request)
    return auth.session_user(token) if token else None


def _wants_audit(request, status, exc):
    if status not in REJECT_AUDIT or getattr(exc, "audited", False):
        return False
    route = request.scope.get("route")
    # 감사 대상이 아닌 읽기 경로는 건너뛴다
    return request.method not in auth.SAFE_METHODS or bool(getattr(getattr(route, "endpoint", None), "audit_action", None))


def _audit_rejected(request, status, exc):
    try:
        action = getattr(getattr(request.scope.get("route"), "endpoint", None), "audit_action", None)
        user = getattr(exc, "user", None) or getattr(request.state, "user", None)
        info = {"method": request.method}
        if status == 422:
            info["errors"] = len(exc.errors()) if hasattr(exc, "errors") else 0
            if user is None:
                user = _session_owner(request)
        reason = getattr(exc, "reason", None)
        if reason:
            info["reason"] = reason
        pp = request.path_params
        vol = pp.get("vol") if VOL_RX.fullmatch(pp.get("vol") or "") else None
        doc_id = pp.get("doc_id") if ID_RX.fullmatch(pp.get("doc_id") or "") else None
        auth.audit_event(user, action or "unaudited_route", f"rejected_{status}", vol, doc_id,
                         limited=user is None or reason == "csrf", **info)
    except Exception:
        errors.log_exception(f"audit rejected {status}")


@app.exception_handler(StarletteHTTPException)
async def rejected_http(request: Request, exc: StarletteHTTPException):
    if _wants_audit(request, exc.status_code, exc):
        await run_in_threadpool(_audit_rejected, request, exc.status_code, exc)
    return await http_exception_handler(request, exc)


@app.exception_handler(RequestValidationError)
async def rejected_validation(request: Request, exc: RequestValidationError):
    if _wants_audit(request, 422, exc):
        await run_in_threadpool(_audit_rejected, request, 422, exc)
    return await request_validation_exception_handler(request, exc)


def api_routes(routes, prefix=""):
    """(경로, 라우트) 전부 — include_router로 넣은 라우터 안까지 (FastAPI 0.16x는 넣은 라우터를 묶음 하나로 둔다)"""
    for r in routes:
        if hasattr(r, "original_router"):
            ctx = getattr(r, "include_context", None)
            yield from api_routes(r.original_router.routes, prefix + (getattr(ctx, "prefix", "") or ""))
        elif getattr(r, "methods", None):  # APIRoute, 그리고 /docs 같은 Starlette Route
            yield prefix + r.path, r


def unaudited_routes(application):
    """GET이 아닌데 감사 행위 이름(auth.audit_as)이 없는 경로 [(메서드, 경로)] — 시험이 비어 있음을 확인한다"""
    return [(",".join(sorted(r.methods)), path) for path, r in api_routes(application.routes)
            if set(r.methods) - set(auth.SAFE_METHODS) and not getattr(r.endpoint, "audit_action", None)]


def submit(vol, arg, user=None, reserved=False):
    """철 흐름을 뒤에서 돌린다. 끝난 결과도 감사 로그에 남긴다(run_done: ok / review_waiting / error+분류 코드).
    reserved: 부른 쪽이 이미 running[vol]을 세웠다 (/run이 리셋 전에)"""
    if not reserved and reserve(vol, "queued") is None:
        raise HTTPException(409, f"{vol} 이미 실행 중")

    def job():
        running[vol] = Busy("running")
        store.event(vol, "run", "start", None, resume=arg is None or isinstance(arg, Command))
        try:
            store.set_run_stop(vol, None)  # 다시 돌리면 앞선 멈춤 이유는 없어진다
            G.invoke(arg, cfg(vol))
            snap = G.get_state(cfg(vol))
            print(f"run {vol} stopped/finished: next={snap.next}", flush=True)
            if snap.interrupts:
                store.event(vol, "review", "waiting", None, flagged=len(snap.interrupts[0].value["flagged"]))
                audit_safe(user, "run_done", vol, None, "review_waiting")
            else:
                store.event(vol, "run", "end", None)
                audit_safe(user, "run_done", vol, None, "ok")
        except llm.BudgetExceeded as e:
            # 오류가 아니라 한도 멈춤 (https://github.com/reddol18/docflow/issues/7): 체크포인트까지 한 일은 남고, 멈춤 이유를
            # run_stop 표에 남겨 철 단계가 'budget'으로 보인다 (_stage). 한도가 풀리면(다음 날·한도 올림) /resume으로 이어 간다
            print(f"run {vol} budget stop: {e}", file=sys.stderr, flush=True)
            store.set_run_stop(vol, "budget")  # 철 단계의 근거 (run_stop 표)
            store.event(vol, "run", "budget", None, **errors.public(e))
            audit_safe(user, "run_done", vol, None, "budget_stop")
        except Exception as e:
            errors.log_exception(f"run {vol}")  # 상세는 서버 로그에만 (키 꼴 문자열은 가림)
            store.event(vol, "run", "error", None, **errors.public(e))  # 화면엔 분류 코드만
            audit_safe(user, "run_done", vol, None, "error", code=errors.code_of(e))
        finally:
            running.pop(vol, None)

    runner.submit(job)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/volumes/{vol}/pages")
@audit_as("upload")
async def upload(request: Request, vol: str, user: Admin, replace: bool = False):
    """쪽 이미지 올리기 (https://github.com/reddol18/docflow/issues/26, 검사·상한은 app/upload.py 머리말).
    본문은 여기서 읽는다 — 파일을 경로 인자(File)로 받으면 FastAPI가 로그인·역할·CSRF 확인보다 먼저 본문 전체를 /tmp에
    흘려 쓰므로. 하나라도 틀리면 아무것도 저장하지 않는다. 같은 이름이 이미 있으면: 내용이 같으면 그대로 두고(unchanged —
    끊긴 업로드를 다시 보내도 된다, 감사 결과도 unchanged라 출력 뒤 '바뀐 철'로 세지 않는다), 다르면 409.
    replace=true일 때만 바꾸고 그 쪽의 OCR 결과·잘라낸 칸을 지워 다음 /run에서 다시 OCR한다.
    쓰는 업로드(새 쪽·바꿀 쪽)는 철이 처음(new)이거나 끝난(done) 때만 — 실행 중·검토 대기·중간에 멈춘 철은 409 (이어 가면
    옛 쪽 목록·OCR로 만든 문서가 그대로 색인되므로). 끝난 철은 /run으로 다시 돌려야 반영된다(응답 rerun=true)"""
    with audited(user, "upload", vol, replace=replace) as info:
        form = await _upload_form(request)
        try:
            files = form.getlist("files")
            if (not files or len(files) != len(form.multi_items())
                    or not all(isinstance(f, StarletteUploadFile) for f in files)):
                raise HTTPException(400, "files 칸에 파일만 (multipart/form-data)")
            info["files"] = len(files)
            out = await run_in_threadpool(_upload, vol, files, replace)  # 디코드 검사가 이벤트 루프를 막지 않게
        finally:
            await form.close()
        info.update(saved=out["saved"], replaced=out["replaced"], unchanged=out["unchanged"])
        if not out["saved"]:
            info["result"] = "unchanged"  # store.last_export는 result='ok'인 upload만 '바뀜'으로 센다
    return out


async def _upload_form(request):
    """로그인·역할·CSRF 확인 뒤에 본문을 읽는다. 파일 수는 Starlette가 파싱하면서 센다(넘는 파일은 /tmp에 쓰기 전에 끊음).
    파일 아닌 칸은 4개·64KB까지. 본문 바이트 상한·/tmp 예산은 BodyCap(app/upload.py)이 읽는 동안 끊는다(413·429)"""
    try:
        return await request.form(max_files=uploads.MAX_FILES, max_fields=4, max_part_size=64 * 1024)
    except StarletteHTTPException as e:
        if e.status_code != 400:
            raise  # BodyCap의 413·429
        if "Too many files" in str(e.detail):
            raise HTTPException(400, f"한 번에 {uploads.MAX_FILES}장까지") from None
        raise HTTPException(400, "multipart 본문을 읽을 수 없음") from None


class Busy(str):
    """running[vol] 값. str이라 화면·상태 응답엔 글자로 나가고, 객체마다 달라 '내가 세운 표시인가'를 is로 가린다"""


def reserve(vol, what):
    """running[vol]을 원자적으로 세운다 (dict.setdefault는 GIL 아래 한 번에 — 확인과 세우기 사이에 끼어들 수 없다).
    세웠으면 그 표시, 이미 다른 일이 세워 두었으면 None"""
    tok = Busy(what)
    return tok if running.setdefault(vol, tok) is tok else None


def release(vol, tok):
    """내가 세운 표시일 때만 지운다 — 다른 요청이 세운 '실행 중'을 지워 실행 중인 철을 '실행 아님'으로 만들지 않게"""
    if running.get(vol) is tok:
        running.pop(vol, None)


def _upload(vol, files, replace):
    names, pages = [], []  # 올린 이름(오류 문구용 — 규칙을 통과한 것만), 쪽 이름(저장은 <쪽>.jpg)
    for i, f in enumerate(files, 1):  # 1) 이름부터 모두 본다 (규칙에 안 맞는 이름은 응답에 되돌려 주지 않는다)
        name = (f.filename or "").rsplit("/", 1)[-1]
        page = uploads.page_of(name)
        if page is None:
            raise HTTPException(400, f"{i}번째 파일 이름이 규칙({uploads.PAGE_RULE})에 맞지 않음")
        if page in pages:
            raise HTTPException(400, f"같은 쪽 이름이 두 번: {name}")
        names.append(name)
        pages.append(page)
    d = store.pages_dir(vol)
    # 2) 쪽 폴더 안 임시 파일에 조각 단위로 쓰며 검사 — 같은 파일 시스템이라 옮기기는 원자적이다.
    #    /tmp(Starlette가 흘려 쓴 곳)에서 바로 옮기면 파일 시스템이 달라 복사가 되고 반쯤 쓴 *.jpg가 보일 수 있다.
    #    실패해도 쪽 폴더는 지우지 않는다 (빈 폴더는 해롭지 않고, 같은 새 철에 동시에 올리는 요청과 다투지 않게)
    tmps = []
    try:
        for f, name, page in zip(files, names, pages):
            tmps.append(uploads.spool(f.file, d, f"{page}.jpg", name))
        new, same, changed = [], [], []
        for tmp, page in zip(tmps, pages):
            dest = store.page_path(vol, page)
            if not dest.exists():
                new.append((tmp, dest))
            elif filecmp.cmp(tmp, dest, shallow=False):
                same.append(page)
            else:
                changed.append((tmp, dest))
        if changed and not replace:
            shown = ", ".join(p.name for _, p in changed[:10]) + (" …" if len(changed) > 10 else "")
            raise HTTPException(409, f"같은 이름의 쪽이 이미 있음 (바꾸려면 ?replace=true): {shown}")
        stage = None
        if new or changed:  # 쓰는 동안 같은 철 실행·이어 가기·일괄 삭제는 409 (running 표시)
            tok = reserve(vol, "uploading")
            if tok is None:
                raise HTTPException(409, f"{vol} 실행 중 — 끝난 뒤 올린다")
            try:
                stage = _flow_stage(G.get_state(cfg(vol)))  # 표시를 세운 뒤에 읽는다 — 그사이 /run이 시작할 수 없다
                if stage not in ("new", "done"):
                    raise HTTPException(409, "검토 대기·중간에 멈춘 철 — 끝까지 마치거나(/review·/resume) "
                                             "처음부터 다시 돌린(/run) 뒤 올린다")
                _place(vol, new, changed)
            finally:
                release(vol, tok)
    finally:
        for tmp in tmps:
            if os.path.exists(tmp):
                os.unlink(tmp)
    return {"vol": vol, "saved": len(new) + len(changed), "replaced": len(changed), "unchanged": len(same),
            "rerun": stage == "done", "total": len(list(d.glob("*.jpg")))}


def _place(vol, new, changed):
    """3) 새 쪽은 같은 이름이 없을 때만 놓는다 — 그사이 다른 요청이 같은 이름을 만들었으면 409, 그 밖의 실패(디스크 등)도
    이번에 놓은 새 쪽을 모두 되돌린다. 바꿀 쪽은 OCR 결과·잘라낸 칸을 먼저 지운 뒤 바꾼다 — 사이에 멈춰도
    '새 이미지 + 옛 OCR'은 남지 않는다(옛 이미지 + OCR 없음 → 다음 실행이 옛 이미지를 다시 읽을 뿐). 바꾸기가 중간에
    실패하면 새 쪽은 되돌리고, 이미 바꾼 쪽은 새 이미지 + OCR 없음으로 남는다 (어긋남 없음)"""
    placed = []
    try:
        for tmp, dest in new:
            uploads.place_new(tmp, dest)
            placed.append(dest)
        for tmp, dest in changed:
            page = dest.stem
            store.ocr_path(vol, page).unlink(missing_ok=True)
            for cell in CROP_KINDS:
                store.crop_path(vol, page, cell).unlink(missing_ok=True)
            os.replace(tmp, dest)
    except BaseException as e:
        for p in placed:
            p.unlink(missing_ok=True)
        if isinstance(e, FileExistsError):
            raise HTTPException(409, "같은 이름의 쪽을 다른 요청이 먼저 올림 — 다시 시도") from None
        raise


@app.post("/volumes/{vol}/run")
@audit_as("run")
def run(vol: str, user: Admin, review: str = "human"):
    ok_mode = review in ("human", "auto")
    # 사용자가 넣은 임의 문자열은 남기지 않는다
    with audited(user, "run", vol, review=review if ok_mode else "invalid") as info:
        if not ok_mode:
            raise HTTPException(400, "review=human|auto")
        if not any(store.pages_dir(vol).glob("*.jpg")):
            raise HTTPException(404, "올린 쪽이 없음")
        # 리셋 전에 실행 중 표시를 먼저 세운다 → 리셋하는 동안 수정·검사·승인·반려는 409 (색인이 끝난 철만 받으므로)
        tok = reserve(vol, "queued")  # 확인과 세우기를 한 번에 (업로드·일괄 삭제와 다투지 않게)
        if tok is None:
            raise HTTPException(409, f"{vol} 이미 실행 중")
        try:
            if store.reset_inspection(vol):  # 처음부터 다시 돌리면 문서가 바뀐다 → 예전 검사 목록은 버린다 (검사 이력은 edits에 남음)
                store.event(vol, "inspection", "reset", None, user=user.username)
            # 검토 단계 위치도 처음으로, 실행 세대를 올린다 (승인·반려 기록 stage_log는 세대와 함께 남음)
            info["stage_reset"] = moved = store.reset_stages(vol)
            if moved:
                store.event(vol, "workflow", "reset", None, user=user.username, docs=moved)
            submit(vol, {"vol": vol, "review_mode": review}, user, reserved=True)
        except BaseException:
            release(vol, tok)
            raise
    return {"vol": vol, "started": True}


@app.post("/volumes/{vol}/resume")
@audit_as("resume")
def resume(vol: str, user: Admin):
    with audited(user, "resume", vol):
        snap = G.get_state(cfg(vol))
        if not snap.next:
            raise HTTPException(409, "이어 갈 단계가 없음 (끝났거나 시작 안 함)")
        if snap.interrupts:
            raise HTTPException(409, "사람 검토 대기 중 → /review 에서 이어 감")
        submit(vol, None, user)
    return {"vol": vol, "resumed_at": list(snap.next)}


def _stage(vol, snap):
    if running.get(vol) == "queued":
        return "queued"
    if snap.interrupts:
        return "review"
    if vol in running:
        return "running"
    stage = _flow_stage(snap)
    if stage == "stopped" and store.run_stop(vol) == "budget":
        return "budget"  # 비용 한도로 멈춤 — 끊긴 철(stopped)과 같이 /resume으로 이어 간다
    return stage


def _flow_stage(snap):
    """그래프 상태만으로 본 단계 (running 표시는 보지 않는다): review / done / stopped / new"""
    v = snap.values or {}
    if snap.interrupts:
        return "review"
    if v.get("indexed") is not None and not snap.next:
        return "done"
    if snap.next:
        return "stopped"  # Pod 재시작 등으로 끊김 → /resume
    return "new"


def day_totals():
    """철과 상관없는 오늘 비용 값 — 응답마다 한 번만 읽는다 (철 목록이 철마다 다시 읽지 않게)"""
    return {"spent_today_usd": round(store.spent_today(), 6),
            "budget": store.budget_today(llm.LIMITS)}  # 오늘 예산 {index·query: {used, held, limit}}


def summary(vol, totals=None):
    snap = G.get_state(cfg(vol))
    v = snap.values or {}
    docs = v.get("docs", [])
    return {
        "vol": vol,
        "stage": _stage(vol, snap),
        "running": running.get(vol),
        "next": list(snap.next),
        "waiting_review": bool(snap.interrupts),
        "pages": len(v.get("pages", [])),
        "ocr_done": len(list(store.ocr_dir(vol).glob("*.json"))),
        "uploaded": len(list(store.pages_dir(vol).glob("*.jpg"))),
        "documents": len(docs),
        "orphans": v.get("orphans", []),
        "flagged_documents": sum(bool(d.get("flags")) for d in docs),
        "indexed": v.get("indexed"),
        "usage": store.usage_summary(vol),
        **(totals or day_totals()),
    }


def volume_names():
    """쪽 폴더 목록 중 철 이름 규칙에 맞는 것만 (규칙에 안 맞는 폴더 하나가 목록·리포트 전체를 400으로 만들지 않게)"""
    root = store.DATA / "pages"
    return sorted(p.name for p in root.iterdir() if p.is_dir() and VOL_RX.fullmatch(p.name)) if root.exists() else []


@app.get("/volumes")
def volumes(_: Viewer):
    vols = volume_names()
    totals = day_totals() if vols else None
    return [summary(v, totals) for v in vols]


@app.get("/volumes/{vol}")
def status(vol: str, _: Viewer):
    return summary(vol)


@app.get("/volumes/{vol}/events")
def volume_events(vol: str, _: Viewer, after: int = 0):
    return store.events(vol, after)


@app.patch("/documents/{vol}/{doc_id}")
@audit_as("edit")
def edit_document(vol: str, doc_id: str, fields: dict[str, CellValue], user: Reviewer):
    """색인이 끝난 문서를 사람이 고친다: 그래프 상태·Postgres·Qdrant를 함께 바꾼다.
    검토 대기 중인 철은 /review/{vol}/json 으로 흐름을 이어 가야 하므로 여기서는 받지 않는다."""
    with audited(user, "edit", vol, doc_id) as info, store.doc_lock(vol, doc_id):  # 확인과 쓰기 사이에 단계가 못 바뀌게
        row = store.inspection_row(vol, doc_id)
        if row and row["inspected_at"] is not None:  # 검사가 끝난 문서를 입력 경로로 바꾸면 검사 결과가 무의미해진다
            raise HTTPException(409, "검사가 끝난 문서는 고칠 수 없음")
        _entry_edit_allowed(vol, doc_id, user)
        d, changed, reflagged = _edit_document(vol, doc_id, fields, user)
        info["fields"] = changed  # 칸 이름만 (값은 edits 표)
    # reflagged: 기계 검수(철 안 중복)로 표시가 바뀐 다른 문서 — 화면은 이것이 있을 때만 목록을 다시 읽는다
    return {**d, "reflagged": reflagged}


def _edit_document(vol, doc_id, fields, user, source="edit"):
    # 그래프 상태 읽기 → 쓰기는 철 잠금 안에서: 같은 철 다른 문서의 동시 수정이 서로 덮어쓰지 않게.
    # 잠금 순서는 문서(부른 쪽) → 철로 고정 (https://github.com/reddol18/docflow/issues/86)
    with store.vol_lock(vol):
        snap = G.get_state(cfg(vol))
        if snap.interrupts or vol in running:
            raise HTTPException(409, "실행 중이거나 검토 대기 중")
        docs = {d["doc_id"]: d for d in (snap.values or {}).get("docs", [])}
        if doc_id not in docs:
            raise HTTPException(404)
        old = docs[doc_id]["fields"]
        fix = schema_fields.normalize_input(schema_fields.SCHEMA,  # LLM 값과 같은 표기 규칙 (미상·연대·위치 등)
                                            {k: str(v).strip() for k, v in fields.items() if k in EDITABLE})
        fix = {k: v for k, v in fix.items() if v != (old.get(k) or "")}
        if not fix:
            return docs[doc_id], [], []
        docs[doc_id] = dict(docs[doc_id], fields=flow.rederive({**old, **fix}, fix), edited=True)
        # 기계 검수(칸 검증 규칙)를 고친 값으로 다시 본다 — 철 안 중복은 다른 문서 표시도 바꾼다 (flags.refresh는 바뀐
        # 문서만 새 객체). 규칙 없는 스키마는 이 문서 하나만 저장 — 지금과 같다
        new = {x["doc_id"]: x for x in flags.refresh(list(docs.values()), schema_fields.SCHEMA)}
        d = new[doc_id]
        others = [x for k, x in new.items() if k != doc_id and x is not docs[k]]
        # 임베딩은 저장 전에 (https://github.com/reddol18/docflow/issues/7): 예산 한도(BudgetExceeded → 503)·LLM 오류면
        # 아무것도 저장하지 않는다 — 값만 바뀌고 벡터가 낡은 채 남거나, 같은 값으로 다시 내도 차이가 없어 임베딩되지 않는 일이
        # 없게. 철 잠금을 쥔 채 부른다 (같은 철 다른 문서 수정은 그동안 기다린다, store.LOCK_WAIT)
        vec = llm.embed(vol, [flow.doc_text(vol, d)]) if llm.enabled() else None
        G.update_state(cfg(vol), {"docs": [d, *others]}, as_node="index")
        # Postgres 쓰기도 철 잠금 안에서 — 잠금 밖에서 쓰면 그 사이 다른 문서를 고친 요청의 값·표시를 낡은 것으로 덮는다.
        # 다른 문서는 표시만 쓴다 (칸 값·검토 여부는 그 문서를 고치는 요청 몫). 둘은 한 트랜잭션(store.save_edit).
        # 다른 문서 표시가 그대로면(규칙 없는 스키마는 늘) 지금처럼 이 문서만 저장
        if others:
            store.save_edit(vol, d, [(x["doc_id"], x["flags"]) for x in others])
        else:
            store.save_documents([d])
    # 이력은 값이 저장된 바로 뒤, 외부 쓰기(Qdrant) 전에 — 그쪽이 실패해도 누가 무엇을 바꿨는지는 남는다
    store.add_edits(vol, [(doc_id, k, old.get(k), v) for k, v in fix.items()], user.id, source)
    if vec is not None:
        store.upsert_vectors([d], vec)
    else:  # LLM_PROVIDER=off: 예전 값으로 만든 벡터를 남기지 않는다
        store.delete_doc_vectors(vol, [doc_id])
    store.event(vol, source, "end", doc_id, fields=list(fix), user=user.username)
    return d, list(fix), [x["doc_id"] for x in others]


@app.get("/documents/{vol}/{doc_id}/edits")
def document_edits(vol: str, doc_id: str, _: Viewer):
    return store.edit_history(vol, doc_id)


# ---------- 2단계 검증: 검사 (https://github.com/reddol18/docflow/issues/10) ----------
def _inspect_rate():
    raw = os.environ.get("INSPECT_RATE", "0.05")
    try:
        rate = float(raw)
    except ValueError:
        raise RuntimeError(f"INSPECT_RATE는 0~1 사이 소수 (예: 0.05): {raw!r}") from None
    if not 0 < rate <= 1:
        raise RuntimeError(f"INSPECT_RATE는 0~1 사이 소수 (예: 0.05): {raw!r}")
    return rate


# ---------- 검토 흐름 설정 (https://github.com/reddol18/docflow/issues/86) ----------
WORKFLOW = workflow.load()  # 틀리면 시작할 때 멈춘다 (WORKFLOW=경로, 기본 app/workflows/default.yaml — 지금의 2단계)
ENTRY = workflow.entry(WORKFLOW)
SAMPLE = workflow.sample_stage(WORKFLOW)  # 표본 검사 단계 (/inspection 경로). 없는 흐름도 있다
CONFIRMED = workflow.CONFIRMED
REASON_MAX = 500
# 단계 경로의 역할 하한: 흐름에서 가장 좁은 역할. 문서마다 그 단계의 역할은 경로 안에서 다시 본다
StageUser = Annotated[auth.User, auth.require(min((s["role"] for s in WORKFLOW["stages"]), key=auth.ROLES.index))]
# 표본 검사 경로의 역할: 그 단계에 설정한 역할 (기본 inspector — 지금과 같다)
SampleUser = Annotated[auth.User, auth.require(SAMPLE["role"] if SAMPLE else "inspector")]


STATUS_LABEL = {"confirmed": "확정", "excluded": "검사 제외(수정 가능)"}


def startup_migrate():
    """앱 시작 때 (lifespan): 예전 판에서 검사가 끝난 문서(단계 행 없음)를 확정 행으로 — 남은 것만.
    예전 문서 키(doc_id)로 섞인 documents 행을 철의 그래프 상태로 바로잡는다 (https://github.com/reddol18/docflow/issues/50)"""
    if SAMPLE:
        n = store.backfill_inspected(SAMPLE["name"])
        if n:
            print(f"doc_stage: 예전 검사 끝난 문서 {n}건 → 확정", flush=True)
    if G is not None and not store.migration_done(store.DOC_KEY_MIGRATION):
        repair_documents()  # 실패하면 완료 표시를 남기지 않는다 → 다음 시작에 Qdrant 옮기기부터 이어서
        store.mark_migration(store.DOC_KEY_MIGRATION)


def _row_differs(row, d):
    return (row is None or row["fields"] != d["fields"] or (row["flags"] or {}) != (d.get("flags") or {})
            or list(row["pages"]) != list(d["pages"]) or bool(row["reviewed"]) != bool(d.get("reviewed", False))
            or bool(row["edited"]) != bool(d.get("edited")))


def repair_documents():
    """예전 키(doc_id 하나)로 섞인 documents 행 바로잡기 — 문서 키 옮기기에서 한 번 (https://github.com/reddol18/docflow/issues/50).
    예전 키에서는 같은 쪽 이름의 두 철을 처리하면 행이 하나만 남았다: vol은 먼저 처리한 철, 칸 값은 나중 철
    (ON CONFLICT가 vol을 안 바꿈). 그 뒤 한쪽 철을 지웠다면 남은 행에 지운 철 값이 남거나(나중 철을 지움), 살아 있는
    철의 행이 아예 없어졌다(먼저 철을 지움 — 행의 vol이 그 철이라). 그래서 events가 아니라 색인이 끝난 모든 철의 그래프
    상태(철 화면·출력이 읽는 원본, 사람이 고친 값도 여기 있다)와 행을 대조해, 없거나 다른 행을 상태대로 다시 쓴다
    (LLM 호출 없음). 상태에 없는 행은 지우지 않는다. 잃은 벡터는 다시 만들지 않고 개수만 알린다 — 그 문서는 낱말
    검색으로만 찾힌다. 고친 행 수"""
    with store.pool.connection() as c:
        vols = {r["vol"] for r in c.execute("SELECT DISTINCT vol FROM documents").fetchall()}
    vols = sorted(vols | set(volume_names()))
    fixed, keys, skipped = 0, [], []
    for vol in vols:
        if not VOL_RX.fullmatch(vol):
            continue
        snap = G.get_state(cfg(vol))
        v = snap.values or {}
        # 색인이 끝난 철만 (summary의 done과 같은 조건). 색인 전·다시 돌리는 중(검토 대기·멈춤)인 철의 상태는 아직
        # documents에 쓸 값이 아니다
        if v.get("indexed") is None or snap.next or snap.interrupts:
            if v.get("indexed") is not None or snap.next or snap.interrupts:  # 상태가 아예 없는 철은 알릴 것 없음
                skipped.append(f"{vol}({'검토 대기' if snap.interrupts else '멈춤·실행 중' if snap.next else '색인 전'})")
            continue
        docs = v.get("docs", [])
        have = store.get_documents([(vol, d["doc_id"]) for d in docs])
        stale = [d for d in docs if _row_differs(have.get((vol, d["doc_id"])), d)]
        if stale:
            store.restore_documents(vol, stale, store.index_time(vol))
            fixed += len(stale)
        keys += [(vol, d["doc_id"]) for d in docs]
    lost = []
    if keys and llm.enabled():
        try:  # 개수 알림일 뿐 — 세지 못해도 옮기기는 끝낸다
            lost = store.missing_vectors(keys)
        except Exception:
            errors.log_exception("missing_vectors")
    print(f"documents: 문서 키 옮기기 바로잡음 — 철 {len(vols)}개 대조, 행 {fixed}개를 철의 그래프 상태대로 다시 씀",
          flush=True)
    if skipped:
        print(f"documents: 바로잡기에서 건너뛴 철 {len(skipped)}개 {skipped} — 색인이 끝나면 그 철의 행은 다시 쓰인다 "
              "(옮기기 완료 표시는 남긴다)", flush=True)
    if lost:
        print(f"qdrant: 벡터 없는 문서 {len(lost)}개 (철 {sorted({v for v, _ in lost})}) — 낱말 검색으로만 찾힌다 "
              "(예전 키 충돌로 덮어쓴 벡터). 그 문서의 칸을 고치면(PATCH) 벡터를 다시 만든다", flush=True)
    return fixed


def positions(vol, doc_ids, listed=None):
    """문서마다 지금 위치 {doc_id: {stage, round, last_reason, rejected_from, status}}. stage는 저장된 위치 그대로
    (doc_stage 행이 없으면 흐름의 처음 단계 — 예전 철·기본 흐름). status:
      confirmed  마지막 단계까지 승인됨 (표본 검사가 끝난 문서 포함)
      excluded   표본 단계에 와 있는데 검사 목록 밖 — 검사 없이 넘어간 것으로 보아 출력 전 검증에서는 미확정이 아니고,
                 확정과 달리 입력 경로로 고칠 수 있다 (지금 동작)
      pending    그 밖 (그 단계 차례를 기다림)
    listed: 이미 읽은 검사 목록 문서 id 집합 (검사 목록이 있을 때만 — 주면 목록을 다시 읽지 않는다)"""
    rows = store.stage_rows(vol)
    init = workflow.initial(WORKFLOW)
    if listed is None and SAMPLE and store.inspection_plan(vol):
        listed = {r["doc_id"] for r in store.inspection_rows(vol)}
    out = {}
    for d in doc_ids:
        p = dict(rows.get(d) or {"stage": init, "round": 1, "last_reason": None, "rejected_from": None})
        if p["stage"] == CONFIRMED:
            p["status"] = "confirmed"
        elif listed is not None and p["stage"] == SAMPLE["name"] and d not in listed:
            p["status"] = "excluded"
        else:
            p["status"] = "pending"
        out[d] = p
    return out


def acted_stages(vol, user):
    """이 사용자가 맡았던 단계: (철 전체 입력 여부, {doc_id: {단계 이름}}) — 작업자 ≠ 검수자 확인용.
    입력: 철 검토 제출·입력 경로 수정(edits review/edit), 검수: 단계 승인·반려 기록, 표본 검사: 검사 차지·검사 수정"""
    whole, mine = store.entered(vol, user.id)
    acts = store.stage_actors(vol, user.id)
    for d in mine:
        acts.setdefault(d, set()).add(ENTRY["name"])
    if SAMPLE:
        for d in store.inspected_docs_by(vol, user.id):
            acts.setdefault(d, set()).add(SAMPLE["name"])
    return whole, acts


def other_stages(whole, acts, doc_id, stage_name):
    """같은 문서에서 이 단계 말고 맡았던 단계 (비어 있으면 맡아도 된다)"""
    s = set(acts.get(doc_id, ()))
    if whole:
        s.add(ENTRY["name"])
    return s - {stage_name}


def _distinct_check(vol, doc_id, user, stage_name):
    """설정 distinct_actors: 같은 사람이 같은 문서의 두 단계를 맡지 못한다. 사용자를 모르면(인증 끈 개발 모드) 막는다"""
    if not WORKFLOW["distinct_actors"]:
        return
    if user.id is None:
        raise HTTPException(403, "사용자를 알 수 없어 작업자 ≠ 검수자를 확인할 수 없음")
    other = other_stages(*acted_stages(vol, user), doc_id, stage_name)
    if other:
        raise HTTPException(403, f"같은 문서의 다른 단계({', '.join(sorted(other))})를 맡은 사람은 이 단계를 맡을 수 없음")


def _entry_edit_allowed(vol, doc_id, user):
    """입력 경로(PATCH)로 고칠 수 있는 문서인가 (workflow.editable_positions): 입력 단계에 있는 문서만.
    예외는 입력 on_index 바로 다음이 표본 단계인 흐름(기본 흐름) — 검사 전·검사 목록 밖 문서를 지금처럼 고칠 수 있다.
    확정됐거나 검수 단계에 있는 문서는 409 (검수자가 승인하며 고치거나, 반려되어 입력으로 돌아오면 고친다).
    doc_lock 안에서 부른다"""
    stage = positions(vol, [doc_id])[doc_id]["stage"]
    if stage == CONFIRMED:
        raise HTTPException(409, "확정된 문서는 고칠 수 없음")
    if stage not in workflow.editable_positions(WORKFLOW):
        raise HTTPException(409, "검수 단계로 넘어간 문서는 입력 경로로 고칠 수 없음 (반려되면 고친다)")
    if WORKFLOW["distinct_actors"] and user.id is not None:
        other = other_stages(*acted_stages(vol, user), doc_id, ENTRY["name"])
        if other:
            raise HTTPException(403, f"같은 문서의 검수 단계({', '.join(sorted(other))})를 맡은 사람은 입력을 고칠 수 없음")


INSPECT_RATE = _inspect_rate()  # 표시 없는 문서 표본 비율 — 기본값이자 하한 (관리자만 낮출 수 있다)
if SAMPLE and SAMPLE["rate"] is not None:
    INSPECT_RATE = SAMPLE["rate"]  # 흐름 설정에 적은 비율이 환경변수보다 먼저
FIELD = Literal[EDITABLE]


class InspectionPlanIn(BaseModel):
    rate: float = Field(INSPECT_RATE, ge=0, le=1)
    fields100: list[FIELD] = Field(default_factory=list, max_length=len(EDITABLE))  # 100% 검수 칸
    seed: int | None = Field(None, ge=0, le=2**31 - 1)  # 관리자만: 같은 표본을 다시 만들 때 (시험·재현)


class InspectIn(BaseModel):
    fields: dict[str, CellValue] = Field(default_factory=dict, max_length=10)  # 고친 칸만. 비우면 '이상 없음'


def _done_docs(vol):
    snap = G.get_state(cfg(vol))
    if _stage(vol, snap) != "done":
        raise HTTPException(409, "색인이 끝난 철만 검사한다")
    return (snap.values or {}).get("docs", [])


def _check_fields(doc, reason, fields100):
    """검사자가 볼 칸: 표시 문서는 표시 붙은 칸, 표본은 4칸 전부, 그리고 100% 검수 칸"""
    if reason == "sample":
        return list(EDITABLE)
    flagged = {FLAG_FIELD.get(k, k) for k in (doc.get("flags") or {})} if reason == "flagged" else set()
    return sorted((flagged | set(fields100)) & set(EDITABLE))


def _need_sample():
    if not SAMPLE:
        raise HTTPException(404, "이 검토 흐름에는 표본 검사 단계가 없음")


@app.post("/volumes/{vol}/inspection")
@audit_as("inspection_plan")
def inspection_create(vol: str, body: InspectionPlanIn, user: SampleUser):
    """검사 목록을 만든다. 표본이 치우치지 않게 시드는 서버가 정하고(관리자만 지정), 비율은 INSPECT_RATE 아래로 못 내린다
    (관리자 빼고). 이 철의 검토를 제출한 사람은 만들 수 없다."""
    with audited(user, "inspection_plan", vol, rate=body.rate, fields100=body.fields100) as info:
        _need_sample()
        admin = auth.role_at_least(user.role, "admin")
        if not admin and (body.seed is not None or body.rate < INSPECT_RATE):
            raise HTTPException(403, f"시드 지정과 {INSPECT_RATE} 미만 비율은 관리자만")
        if WORKFLOW["distinct_actors"] and user.id is not None and store.entered(vol, user.id)[0]:
            raise HTTPException(403, "이 철의 검토를 제출한 사람은 검사 목록을 만들 수 없음 (교차검사)")
        docs = _done_docs(vol)
        seed = body.seed if body.seed is not None else secrets.randbelow(2**31)
        rows, summary = inspection.plan(docs, body.rate, body.fields100, seed)
        if not store.create_inspection(vol, summary, rows, user.id):
            raise HTTPException(409, "이미 검사 목록이 있음")
        info.update(docs=len(rows), sample=summary["sample"])
    return {"plan": {k: v for k, v in summary.items() if k != "seed"}, "docs": len(rows)}


@app.get("/volumes/{vol}/inspection")
def inspection_view(vol: str, user: Viewer):
    plan = store.inspection_plan(vol) if SAMPLE else None
    if not plan:
        raise HTTPException(404, "검사 목록 없음")
    docs = {d["doc_id"]: d for d in (G.get_state(cfg(vol)).values or {}).get("docs", [])}
    inspector = bool(SAMPLE) and auth.role_at_least(user.role, SAMPLE["role"])
    whole, acts = acted_stages(vol, user) if inspector and user.id is not None else (False, {})
    rows = store.inspection_rows(vol)
    ids = [r["doc_id"] for r in rows]
    pos = positions(vol, ids, listed=set(ids))  # 목록은 이미 읽었다 — positions가 다시 읽지 않게
    out = []
    for r in rows:
        d = docs.get(r["doc_id"], {})
        mine = WORKFLOW["distinct_actors"] and other_stages(whole, acts, r["doc_id"], SAMPLE["name"])
        block = _inspect_block(r, inspector, user, whole, mine, pos[r["doc_id"]]["stage"])
        out.append({**r, "fields": d.get("fields"), "flags": d.get("flags"),
                    "check_fields": _check_fields(d, r["reason"], plan["fields100"]),
                    "can_inspect": block is None, "block": block})
    return {"plan": plan, "docs": out}


def _inspect_block(row, inspector, user, whole, mine, stage):
    """검사할 수 없는 까닭 (화면이 버튼 대신 보여 준다, https://github.com/reddol18/docflow/issues/66). None이면 검사 가능.
    inspected 이미 검사함 / in_progress 누가 차지해 검사 중(아직 안 끝남) / role 검사 역할 아님 /
    user 사용자를 알 수 없음(인증 끈 개발 모드 — POST가 늘 403) / submitted 이 철의 검토 제출자 /
    entered 이 문서를 입력 단계에서 고침 / other_stage 같은 문서의 다른 검수 단계를 맡음 /
    stage 아직 검사 단계에 오지 않음 (POST /inspection과 같은 규칙)"""
    if row["inspected_at"] is not None:
        return "inspected" if row["fixed_fields"] is not None else "in_progress"
    if not inspector:
        return "role"
    if user.id is None:
        return "user"
    if mine:
        return "submitted" if whole else "entered" if ENTRY["name"] in mine else "other_stage"
    if stage != SAMPLE["name"]:
        return "stage"
    return None


@app.post("/inspection/{vol}/{doc_id}")
@audit_as("inspect")
def inspect_document(vol: str, doc_id: str, body: InspectIn, user: SampleUser):
    """검사 결과. 문서를 먼저 차지하고(동시 제출 막기) 고친 칸은 문서 수정과 같은 길(그래프 상태·Postgres·Qdrant)로
    반영한다. 이력 단계는 inspect. 고치다 실패하면 차지를 풀어 다시 검사할 수 있게 한다.
    교차검사: 이 문서를 입력 단계에서 고쳤거나 이 철의 검토를 제출한 사람은 검사할 수 없다.
    흐름 설정의 표본 단계(기본 흐름은 inspect)에 와 있는 문서만 — 앞 단계를 건너뛰고 검사하지 못한다.
    확인부터 확정까지 문서 잠금(doc_lock) 안에서 — 확인과 쓰기 사이에 다른 단계 처리·입력 수정이 끼어들지 못한다."""
    with audited(user, "inspect", vol, doc_id) as info, store.doc_lock(vol, doc_id):
        _need_sample()
        _done_docs(vol)
        if not store.inspection_row(vol, doc_id):
            raise HTTPException(404, "검사 목록에 없는 문서")
        if user.id is None:
            raise HTTPException(403, "자기가 입력·수정한 문서는 검사할 수 없음 (교차검사)")
        if WORKFLOW["distinct_actors"]:
            other = other_stages(*acted_stages(vol, user), doc_id, SAMPLE["name"])
            if other:
                raise HTTPException(403, "자기가 입력·수정한 문서는 검사할 수 없음 (교차검사)" if ENTRY["name"] in other
                                    else f"같은 문서의 다른 단계({', '.join(sorted(other))})를 맡은 사람은 검사할 수 없음")
        gen = store.current_gen(vol)  # 확인 때 세대 — 그 사이 /run이 다시 돌리면 확정을 쓰지 않는다
        pos = positions(vol, [doc_id])[doc_id]
        if pos["stage"] != SAMPLE["name"]:
            raise HTTPException(409, "아직 검사 단계에 오지 않은 문서")
        if not store.claim_inspection(vol, doc_id, user.id):
            raise HTTPException(409, "이미 검사함")
        # 고치기·끝내기·확정 중 어디서 실패해도(확정의 세대 확인 409 포함) 차지를 풀어 다시 검사할 수 있게 한다.
        # 푸는 것은 이 사용자의 차지일 때만 — 그 사이 목록이 새로 만들어졌으면 남의 행을 건드리지 않는다
        try:
            _, changed, _ = _edit_document(vol, doc_id, body.fields, user, source="inspect")
            # 고친 칸은 이력에서 센다 — 앞선 시도가 저장 뒤(임베딩 등)에서 실패해 이번 제출에 차이가 없어도 빠지지 않게
            fixed = store.inspected_fields(vol, doc_id)
            store.finish_inspection(vol, doc_id, fixed)
            # 표본 단계는 마지막 단계라 검사가 끝나면 위치를 확정으로 옮기고 기록한다 (오류목록 내보내기가 같은 표에서 읽는다)
            if not store.confirm_sample(vol, doc_id, SAMPLE["name"], pos["round"], user.id, fixed, gen):
                raise HTTPException(409, "철을 다시 돌려 검사 목록이 바뀜")
        except BaseException:
            store.release_inspection(vol, doc_id, user.id)
            raise
        info.update(fields=fixed, stage=SAMPLE["name"], to=CONFIRMED)
    return {"doc_id": doc_id, "fixed_fields": fixed}


# ---------- 설정 흐름의 단계 승인·반려 (https://github.com/reddol18/docflow/issues/86) ----------
class StageApproveIn(BaseModel):
    stage: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")  # 승인하려는 단계 — 그 사이 문서가 움직였으면 409
    fields: dict[str, CellValue] = Field(default_factory=dict, max_length=10)  # 승인하며 고친 칸


class StageRejectIn(BaseModel):
    stage: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    reason: str = Field(min_length=1, max_length=REASON_MAX)  # 반려 사유 (필수)


REASON_DROP = {"Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"}  # 제어·서식(방향 제어·폭 0)·줄/문단 구분·대리·사용자 정의·미할당


def clean_reason(s):
    """반려 사유는 한 줄 글: 줄바꿈·탭은 빈칸으로 바꾸고, 보이지 않거나 화면·엑셀에서 다르게 보일 수 있는 글자
    (unicodedata 분류 Cc·Cf·Zl·Zp 등)는 지운다. 빈칸은 하나로 줄인다"""
    s = re.sub(r"[\t\n\r\v\f  \x85]+", " ", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) not in REASON_DROP)
    return re.sub(r" {2,}", " ", s).strip()


def _stage_action(vol, doc_id, name, user):
    """단계 승인·반려 전에 서버가 보는 것: 색인 끝난 철의 문서인가, 설정에 있는 /workflow 단계인가, 그 단계의 역할인가,
    문서가 지금 그 단계에 있나(건너뛰기·확정 문서 막기), 같은 문서의 다른 단계를 맡지 않았나. doc_lock 안에서 부른다"""
    if doc_id not in {d["doc_id"] for d in _done_docs(vol)}:
        raise HTTPException(404, "문서 없음")
    st = workflow.stage(WORKFLOW, name)
    if st is None:
        raise HTTPException(404, "흐름에 없는 단계")
    if not workflow.actionable(WORKFLOW, name):
        raise HTTPException(409, "표본 검사 단계는 /inspection 경로로")
    if not auth.role_at_least(user.role, st["role"]):
        raise HTTPException(403, f"{st['role']} 역할 필요")
    pos = positions(vol, [doc_id])[doc_id]
    if pos["status"] == "confirmed":
        raise HTTPException(409, "이미 확정된 문서")
    if pos["stage"] != name:
        raise HTTPException(409, "문서가 지금 이 단계에 있지 않음")
    _distinct_check(vol, doc_id, user, name)
    return st, pos


@app.get("/workflow")
def workflow_config(_: Viewer):
    return workflow.public(WORKFLOW)


@app.get("/volumes/{vol}/workflow")
def workflow_view(vol: str, user: Viewer):
    """철의 문서마다 지금 단계. 색인이 끝나기 전에는 문서 없이 ready=false"""
    snap = G.get_state(cfg(vol))
    ready = _stage(vol, snap) == "done"
    ids = [d["doc_id"] for d in (snap.values or {}).get("docs", [])] if ready else []
    pos = positions(vol, ids)
    whole, acts = acted_stages(vol, user) if ids and user.id is not None else (False, {})
    editable_at = workflow.editable_positions(WORKFLOW)
    out, counts = [], {}
    for d in ids:
        p = pos[d]
        st = workflow.stage(WORKFLOW, p["stage"])
        counts[p["stage"]] = counts.get(p["stage"], 0) + 1
        can = (workflow.actionable(WORKFLOW, p["stage"]) and auth.role_at_least(user.role, st["role"])
               and not (WORKFLOW["distinct_actors"] and (user.id is None or other_stages(whole, acts, d, st["name"]))))
        out.append({"doc_id": d, "stage": p["stage"], "status": p["status"],
                    "label": STATUS_LABEL.get(p["status"]) or (st["label"] if st else None),
                    "round": p["round"], "recheck": p["round"] > 1, "confirmed": p["status"] == "confirmed",
                    "last_reason": p["last_reason"], "rejected_from": p["rejected_from"],
                    # 입력 경로(PATCH)로 고칠 수 있나 — 서버 규칙과 같음 (역할·작업자≠검수자는 PATCH가 다시 본다)
                    "editable": p["stage"] in editable_at,
                    "can_approve": can, "can_reject": can and st["reject"]})
    return {"workflow": workflow.public(WORKFLOW), "ready": ready, "counts": counts, "docs": out}


@app.post("/workflow/{vol}/{doc_id}/approve")
@audit_as("stage_approve")
def stage_approve(vol: str, doc_id: str, body: StageApproveIn, user: StageUser):
    """단계 승인. 고친 칸은 문서 수정과 같은 길로 반영(이력 source: 입력은 edit, 검수는 stage:<단계>)하고 다음 단계로.
    마지막 단계면 확정. 확인·칸 반영·이동을 문서 잠금(doc_lock) 안에서 차례로 한다 — 같은 차례를 두 번 넘기거나,
    확인 뒤 다른 요청이 단계를 바꾼 문서에 칸을 반영하지 않는다. 칸 반영이 실패하면 이동하지 않는다(다시 승인하면 된다)"""
    with audited(user, "stage_approve", vol, doc_id, stage=body.stage) as info, store.doc_lock(vol, doc_id):
        gen = store.current_gen(vol)  # 확인 때 세대 — 그 사이 /run이 다시 돌리면 쓰지 않는다
        st, pos = _stage_action(vol, doc_id, body.stage, user)
        rnd = pos["round"]
        to = workflow.next_stage(WORKFLOW, st["name"])
        source = "edit" if st["kind"] == "entry" else f"stage:{st['name']}"
        if body.fields:
            _edit_document(vol, doc_id, body.fields, user, source=source)
        # 고친 칸은 이력에서 센다 — 앞선 승인이 칸 반영 뒤 실패해 이번에 차이가 없어도 빠지지 않게
        changed = store.stage_fixed_fields(vol, doc_id, st["name"], [source])
        if not store.move_stage(vol, doc_id, st["name"], rnd, "approve", to, rnd, user.id, fixed=changed, gen=gen,
                                init=workflow.initial(WORKFLOW)):
            raise HTTPException(409, "철을 다시 돌렸거나 문서의 단계가 바뀜")
        info.update(to=to, round=rnd, recheck=rnd > 1, fields=changed)
    return {"doc_id": doc_id, "stage": st["name"], "to": to, "round": rnd, "fixed_fields": changed}


@app.post("/workflow/{vol}/{doc_id}/reject")
@audit_as("stage_reject")
def stage_reject(vol: str, doc_id: str, body: StageRejectIn, user: StageUser):
    """단계 반려 (사유 필수): 문서만 정해진 앞 단계(reject_to)로 돌아가고 차례(round)가 하나 는다 → 다시 올라오면 재검수.
    사유는 단계 기록(stage_log)에, 감사 로그에는 단계·차례·사유 길이만"""
    with audited(user, "stage_reject", vol, doc_id, stage=body.stage) as info, store.doc_lock(vol, doc_id):
        reason = clean_reason(body.reason)
        if not reason:
            raise HTTPException(422, "반려 사유를 적어야 함")
        gen = store.current_gen(vol)
        st, pos = _stage_action(vol, doc_id, body.stage, user)
        if not st["reject"]:
            raise HTTPException(409, "이 단계는 반려할 수 없음")
        rnd, to = pos["round"], st["reject_to"]
        if not store.move_stage(vol, doc_id, st["name"], rnd, "reject", to, rnd + 1, user.id, reason=reason, gen=gen,
                                init=workflow.initial(WORKFLOW)):
            raise HTTPException(409, "철을 다시 돌렸거나 문서의 단계가 바뀜")
        info.update(to=to, round=rnd + 1, reason_chars=len(reason))
    return {"doc_id": doc_id, "stage": st["name"], "to": to, "round": rnd + 1}


@app.get("/documents/{vol}/{doc_id}/stages")
def document_stages(vol: str, doc_id: str, _: Viewer):
    return store.stage_history(vol, doc_id)

@app.get("/volumes/{vol}/documents")
def documents(vol: str, _: Viewer):
    return (G.get_state(cfg(vol)).values or {}).get("docs", [])


# 잘라낸 칸 이름 = 손글씨 규칙이 쓰는 칸 (app/graph.py CELLS) + 잘라 둔 인쇄 글자 줄 (app/layout.py LINE_KINDS: 수신·제목,
# https://github.com/reddol18/docflow/issues/35) — 스키마 칸 이름과 상관없다. 다른 이름은 400
CROP_KINDS = (*flow.CELLS, *layout.LINE_KINDS)
CROP_RX = re.compile(r"[\w.-]+_(" + "|".join(map(re.escape, CROP_KINDS)) + r")\.png")


@app.get("/crops/{vol}/{name}")
def crop(vol: str, name: str, _: Viewer):
    if not CROP_RX.fullmatch(name):
        raise HTTPException(400)
    p = store.crop_file(vol, name)
    if not p.exists():
        raise HTTPException(404)
    return Response(p.read_bytes(), media_type="image/png")


@app.get("/head/{vol}/{page}")
def head(vol: str, page: str, _: Viewer):
    """검토용: 쪽 위 45%를 줄여서 보여 준다."""
    if not re.fullmatch(r"[\w.-]+", page):
        raise HTTPException(400)
    img = ocr.read_image(store.page_path(vol, page))
    if img is None:
        raise HTTPException(404)
    h, w = img.shape[:2]
    img = img[: int(h * 0.45)]
    img = cv2.resize(img, (900, int(img.shape[0] * 900 / w)))
    return Response(cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes(), media_type="image/jpeg")


LABEL = schema_fields.labels(schema_fields.SCHEMA)
FLAG_FIELD = schema_fields.flag_keys(schema_fields.SCHEMA)  # 김해: 검토 표시는 일련번호(serial)에 붙지만 사람이 고치는 칸은 문서번호(docno)
FLAG_OF = {v: k for k, v in FLAG_FIELD.items()}
HW_OF = {f["name"]: c for f in schema_fields.SCHEMA["fields"] if (c := flow.hw_cell(f))}  # 칸 → 잘라낸 손글씨 칸 (rule로)
LINE_OF = schema_fields.line_crops(schema_fields.SCHEMA)  # 칸 → 잘라 둔 인쇄 글자 줄 (스키마 crop:)


@app.get("/review/{vol}", response_class=HTMLResponse)
def review_page(vol: str, user: Viewer):
    snap = G.get_state(cfg(vol))
    if not snap.interrupts:
        return f"<meta charset=utf-8><p>{html.escape(vol)}: 검토 대기 없음. <a href='/volumes/{vol}'>상태</a></p>"
    flagged = snap.interrupts[0].value["flagged"]
    editor = auth.role_at_least(user.role, "reviewer")  # 보기 역할은 읽기 전용 (제출은 서버가 403으로도 막음)
    ro = "" if editor else " readonly"
    out = [f"""<!doctype html><meta charset=utf-8><title>검토 {html.escape(vol)}</title>
<style>body{{font:14px sans-serif;margin:16px;max-width:1500px}} .doc{{display:flex;gap:16px;border-top:1px solid #ccc;padding:12px 0}}
.f{{margin:4px 0}} .f label{{display:inline-block;width:70px}} .f input{{width:420px}} .flag{{color:#b8322a;font-size:12px}}
img.crop{{max-height:60px;border:1px solid #999;display:block;margin:2px 0}}</style>
<h1>검토 — {html.escape(vol)} ({len(flagged)}건)</h1>
<p>{"빨간 표시가 붙은 칸만 확인하면 됩니다. 고칠 값만 바꾸고 맨 아래 버튼을 누르면 흐름이 이어져 색인됩니다." if editor
     else "보기 역할이라 읽기만 할 수 있습니다."}</p>
<form method=post>"""]
    for d in flagged:
        did = d["doc_id"]
        out.append(f"<div class=doc><img src='/head/{html.escape(vol)}/{html.escape(did)}' width=600>"
                   f"<div><b>{html.escape(did)}</b> ({len(d['pages'])}쪽)")
        for k in EDITABLE:
            fl = " ".join(d["flags"].get(FLAG_OF.get(k, k), []))
            val = html.escape(d["fields"].get(k) or "")
            out.append(f"<div class=f><label>{html.escape(LABEL[k])}</label><input name='{did}__{k}' value=\"{val}\" "
                       f"maxlength={CELL_MAX}{ro}> <span class=flag>{html.escape(fl)}</span></div>")
            line = LINE_OF.get(k)  # 잘라 둔 원본 줄 (수신·제목) — 있을 때만 (https://github.com/reddol18/docflow/issues/35)
            if line and store.crop_path(vol, did, line).exists():
                out.append(f"<img class=crop src='/crops/{html.escape(vol)}/{html.escape(did)}_{line}.png'>")
            cell = HW_OF.get(k)  # 칸 이름이 아니라 rule이 정한 잘라낸 칸 (https://github.com/reddol18/docflow/issues/72)
            if cell and (d.get("hw") or {}).get(cell, {}).get("text") is not None:
                # 인라인 onerror 대신 파일이 있을 때만 그린다 (CSP가 인라인 스크립트를 막는다)
                if store.crop_path(vol, did, cell).exists():
                    out.append(f"<img class=crop src='/crops/{html.escape(vol)}/{html.escape(did)}_{cell}.png'>")
                hw = d["hw"][cell]
                out.append(f"<span class=flag>손글씨 판독: {html.escape(hw['text'])} (확신도 {hw['conf']})</span>")
        out.append("</div></div>")
    out.append("<button type=submit>검토 끝 — 이어서 색인</button></form>" if editor else "</form>")
    return "".join(out)


def record_review(vol, flagged, fixes, user):
    """공정 리포트용: 표시된 문서·칸 수와 사람이 고친 칸 수 (https://github.com/reddol18/docflow/issues/11).
    수정 이력: 고친 칸마다 이전 값·새 값·사용자 (https://github.com/reddol18/docflow/issues/6)."""
    store.add_review_submitter(vol, user.id)  # 교차검사 근거 — 실패하면 검토 제출도 실패
    store.add_edits(vol, changes(flagged, fixes), user.id, "review")
    store.event(vol, "review", "submit", None, docs=len(flagged),
                cells=sum(len(d["flags"]) for d in flagged),
                fixed_docs=len(fixes), fixed_cells=sum(len(v) for v in fixes.values()), user=user.username)


def resume_review(vol, flagged, fixes, user, interrupt_id):
    """이력을 먼저 남기고 흐름을 잇는다 — 이력 저장이 실패하면 고친 값도 반영되지 않는다 (누가 바꿨는지 잃지 않게).
    resume 값은 {interrupt id: fixes}로 넘긴다 — fixes를 그대로 넣으면 빈 {}를 LangGraph가 'id → 값' 지도로 보고
    버려 review가 다시 멈춘다 (https://github.com/reddol18/docflow/issues/120)."""
    tok = reserve(vol, "queued")  # 이력 쓰기 전에 실행 표시를 세운다 — 쓰는 사이 업로드·/run이 끼지 못하게
    if tok is None:
        raise HTTPException(409, f"{vol} 이미 실행 중")
    try:
        # 화면이 읽은 대기와 지금 대기가 같은지 실행 표시 아래에서 다시 본다 — 그 사이 철이 다시 돌아 대기가 끝났거나
        # 바뀌었으면 이력을 남기지 않고 409 (옛 id로 이어 가면 지도에서 집히지 않아 제출이 조용히 버려진다)
        snap = G.get_state(cfg(vol))
        if not snap.interrupts or snap.interrupts[0].id != interrupt_id:
            raise HTTPException(409, "검토 대기가 바뀜 — 검토 화면을 다시 열어 주세요")
        record_review(vol, flagged, fixes, user)
        submit(vol, Command(resume={interrupt_id: fixes}), user, reserved=True)
    except BaseException:
        release(vol, tok)
        raise


@app.post("/review/{vol}")
@audit_as("review")
async def review_submit(vol: str, request: Request, user: Reviewer):
    with audited(user, "review", vol, via="form") as info:
        snap = G.get_state(cfg(vol))
        if not snap.interrupts:
            raise HTTPException(409, "검토 대기 없음")
        form = await request.form()
        flagged = snap.interrupts[0].value["flagged"]
        fixes = clean_fixes(flagged, [(*key.rpartition("__")[::2], val) for key, val in form.items()])
        # 바뀐 값만 본다 — 고치지 않은 칸은 기존 값(OCR·GPT가 낸 긴 값일 수 있음)이 그대로 다시 오므로 막지 않는다
        if any(len(v) > CELL_MAX for f in fixes.values() for v in f.values()):  # JSON 경로(CellValue)와 같은 상한
            raise HTTPException(422, f"칸 값은 {CELL_MAX}자까지")
        resume_review(vol, flagged, fixes, user, snap.interrupts[0].id)
        info.update(docs=len(flagged), fixed_cells=sum(len(v) for v in fixes.values()))
    return RedirectResponse(f"/volumes/{vol}", status_code=303)


@app.post("/review/{vol}/json")
@audit_as("review")
def review_json(vol: str, body: Annotated[dict[str, dict[str, CellValue]], Field(max_length=1000)], user: Reviewer):
    """스크립트·검토 화면용: {doc_id: {칸: 값}} 으로 이어 가기. 검토 대기 문서의 바뀐 칸만 반영한다."""
    with audited(user, "review", vol, via="json") as info:
        snap = G.get_state(cfg(vol))
        if not snap.interrupts:
            raise HTTPException(409, "검토 대기 없음")
        flagged = snap.interrupts[0].value["flagged"]
        fixes = clean_fixes(flagged, [(did, k, v) for did, f in body.items() for k, v in f.items()])
        resume_review(vol, flagged, fixes, user, snap.interrupts[0].id)
        info.update(docs=len(flagged), fixed_cells=sum(len(v) for v in fixes.values()))
    return {"vol": vol, "fixed": {k: list(v) for k, v in fixes.items()}}


class Timing(BaseModel):
    doc_id: str = Field(pattern=r"^[\w.-]{1,120}$")
    mode: Literal["review", "edit"] = "review"
    active_ms: int = Field(ge=0, le=4 * 3600 * 1000)
    opened_at: dt.datetime | None = None
    fields_changed: int = Field(0, ge=0, le=10)


@app.post("/volumes/{vol}/timings")
@audit_as("timings")
def timings(vol: str, rows: Annotated[list[Timing], Field(max_length=1000)], user: Reviewer):
    """검토 화면이 확정(검토 제출·문서 저장) 때 보내는 문서별 작업 시간 (자리 비움 제외).
    이 철에 없는 문서와 1초 미만(넘겨 보기만 한 것)은 버린다."""
    with audited(user, "timings", vol, rows=len(rows)) as info:
        known = {d["doc_id"] for d in (G.get_state(cfg(vol)).values or {}).get("docs", [])}
        keep = [r.model_dump() for r in rows if r.active_ms >= 1000 and r.doc_id in known]
        store.add_review_times(vol, keep, user.id)
        info["saved"] = len(keep)
    return {"saved": len(keep), "dropped": len(rows) - len(keep)}


def _range(day_from, day_to):
    today = dt.datetime.now(store.KST).date()
    hi = day_to or today
    lo = day_from or hi - dt.timedelta(days=13)
    if lo > hi or (hi - lo).days > 366:
        raise HTTPException(400, "기간은 1일~1년")
    return lo, hi


@app.get("/report")
def report_view(_: Viewer, day_from: dt.date | None = None, day_to: dt.date | None = None):
    """공정·품질 리포트: 물량(철/건/면)·단계별 진척, 날짜별 처리량·검토 표시·수정률·검토 시간·비용."""
    lo, hi = _range(day_from, day_to)
    vols = volume_names()
    totals = day_totals() if vols else None
    return {"day_from": lo, "day_to": hi, "volume": report.volume_counts([summary(v, totals) for v in vols]),
            "daily": report.daily(lo, hi), "review_time": report.review_time_summary(lo, hi),
            "inspection": store.inspection_stats(*report._bounds(lo, hi)),  # 철별 표본 검사·추정 오류율 (https://github.com/reddol18/docflow/issues/10)
            "columns": dict(report.COLUMNS)}


@app.get("/report.csv")
def report_csv(_: Viewer, day_from: dt.date | None = None, day_to: dt.date | None = None):
    lo, hi = _range(day_from, day_to)
    body = report.to_csv(report.daily(lo, hi)) + report.inspection_csv(store.inspection_stats(*report._bounds(lo, hi)))
    return Response(body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="docflow_report_{lo}_{hi}.csv"'})


# ---------- 납품 형식 출력·일괄 삭제 (https://github.com/reddol18/docflow/issues/12) ----------
EXPORT_TEMPLATE = export.load_template()  # 양식 매핑이 틀리면 시작할 때 멈춘다 (EXPORT_TEMPLATE=경로, 기본 AI Hub 88 예시)
RULES = bool(field_rules.ruled(schema_fields.SCHEMA))  # 칸 검증 규칙이 있으면 출력 전 검증에 rule 건수 칸 (https://github.com/reddol18/docflow/issues/87)
EXPORT_VOLS_MAX = 200
VolList = Annotated[list[str], Query(default_factory=list)]


def _export_volumes(vols, need_done):
    """[(vol, 쪽 목록, 문서 목록)]. 철을 안 고르면 끝난 철 전부. need_done이면 안 끝난 철은 409"""
    vols = list(dict.fromkeys(vols))  # 같은 철을 두 번 주면 두 번 나가지 않게 (순서는 지킴)
    if len(vols) > EXPORT_VOLS_MAX:
        raise HTTPException(400, f"철은 {EXPORT_VOLS_MAX}개까지")
    for v in vols:
        check(v)
    out, stages = [], {}
    for v in vols or volume_names():
        snap = G.get_state(cfg(v))
        stage = _stage(v, snap)
        stages[v] = stage
        if stage != "done":
            if need_done and vols:
                raise HTTPException(409, f"색인이 끝나지 않은 철: {v} ({stage})")
            if not vols or stage not in ("review", "stopped", "budget"):
                continue  # 고르지 않았으면 끝난 철만, 골랐으면 문서가 있는 단계만 (검토 대기 건을 세려고)
        vals = snap.values or {}
        out.append((v, vals.get("pages", []), vals.get("docs", [])))
    if not out:
        raise HTTPException(404, "내보낼 철 없음")
    return out, stages


def _unconfirmed(volumes, stages):
    """{vol: 마지막 단계 승인 전 doc_id 집합}. 흐름 설정이 세지 않으면(기본 흐름) None.
    색인이 끝나지 않은 철의 문서는 모두 미확정"""
    if not WORKFLOW["count_unconfirmed"]:
        return None
    out = {}
    for v, _, docs in volumes:
        ids = [d["doc_id"] for d in docs]
        if stages[v] != "done":
            out[v] = set(ids)
        else:
            pos = positions(v, ids)
            # 검사 제외 문서는 그 철의 검사 목록이 모두 끝났을 때만 확정으로 센다 (목록이 없으면 제외 문서도 없다)
            plan_done = bool(SAMPLE) and all(r["fixed_fields"] is not None for r in store.inspection_rows(v))
            out[v] = {d for d in ids if pos[d]["status"] == "pending"
                      or (pos[d]["status"] == "excluded" and not plan_done)}
    return out


@app.get("/export/check")
def export_check(_: Viewer, vol: VolList):
    """출력 전 검증: 필수 열 빈칸·형식 위반·검토 안 끝난 건(·흐름 설정에 따라 미확정 건) 수와 어느 문서·열인지"""
    volumes, stages = _export_volumes(vol, need_done=False)
    _, problems = export.build(EXPORT_TEMPLATE, volumes, _unconfirmed(volumes, stages), schema_fields.SCHEMA)
    return {"template": EXPORT_TEMPLATE["name"], "volumes": [{"vol": v, "stage": stages[v], "docs": len(d)}
                                                             for v, _, d in volumes],
            **export.summary(problems, WORKFLOW["count_unconfirmed"], RULES)}


@app.get("/export.xlsx")
@audit_as("export_request")
def export_xlsx(user: Admin, vol: VolList):
    """납품 양식 엑셀. 검증 문제가 있어도 낸다 (빈칸은 빈칸으로) — 먼저 /export/check로 확인한다"""
    with audited(user, "export_request", None, vols=len(vol)) as info:
        volumes, stages = _export_volumes(vol, need_done=True)
        sheets, problems = export.build(EXPORT_TEMPLATE, volumes, _unconfirmed(volumes, stages), schema_fields.SCHEMA)
        body = export.to_xlsx(sheets)
        info.update(volumes=len(volumes), problems=len(problems))
    for v, _, docs in volumes:  # 철마다 출력 기록 — 일괄 삭제는 지금 내용을 문제 없이 내보낸 철만 받는다
        store.add_export(v, sum(p["vol"] == v for p in problems), user.id)
        audit_safe(user, "export", v, None, "ok", docs=len(docs))
    name = f"docflow_export_{dt.datetime.now(store.KST):%Y%m%d_%H%M}.xlsx"
    return Response(body, media_type=XLSX_TYPE, headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------- 검수 오류목록 (https://github.com/reddol18/docflow/issues/88) ----------
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ERROR_TEMPLATE = error_list.load_template()  # 틀리면 시작할 때 멈춘다 (ERROR_LIST_TEMPLATE=경로, 기본 예시 양식)
ERROR_ROWS_MAX = 50_000  # 한 파일의 오류 행 상한 — 넘으면 철을 나눠 받는다 (메모리·응답 크기, 측정은 README)
_error_list_busy = threading.Lock()  # 오류목록 만들기는 한 번에 하나 (프로세스 안 — 무거운 요청이 겹쳐 메모리를 넘지 않게)


@app.get("/export/errors.xlsx")
@audit_as("error_list_request")
def export_errors(user: Admin, vol: VolList):
    """검수 오류목록 엑셀 — 철마다 오류(문서·칸·오류 내용)와 수정 전후 값·담당자·조치일·재검수 판정. 새로 저장하지 않고
    표시·수정 이력·단계 기록(지금 세대)에서 만든다 (app/error_list.py). 관리자만: 철 전체의 값과 사람(담당자)·반려 사유가
    담긴 반출 파일이라 납품 엑셀(/export.xlsx)과 같은 권한. 철 목록 상한(철을 안 고를 때도)·행 상한·동시 1건(바쁘면 409),
    감사 로그(요청 + 철마다)"""
    with audited(user, "error_list_request", None, vols=len(vol), actor=ERROR_TEMPLATE["actor"]) as info:
        if not _error_list_busy.acquire(blocking=False):
            raise HTTPException(409, "다른 오류목록을 만드는 중 — 잠시 뒤 다시")
        try:
            body, per_vol = _error_list(vol, info)
        finally:
            _error_list_busy.release()
    for v, n in per_vol.items():
        audit_safe(user, "error_list", v, None, "ok", rows=n)
    name = f"docflow_errors_{dt.datetime.now(store.KST):%Y%m%d_%H%M}.xlsx"
    return Response(body, media_type=XLSX_TYPE, headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _error_list(vol, info):
    if not vol and len(volume_names()) > EXPORT_VOLS_MAX:
        raise HTTPException(400, f"철이 {EXPORT_VOLS_MAX}개를 넘음 — vol로 철을 지정한다")
    volumes, _ = _export_volumes(vol, need_done=True)
    rows, per_vol = [], {}
    for v, pages, docs in volumes:
        docs = export.ordered_docs(pages, docs)
        status = {d: p["status"] for d, p in positions(v, [d["doc_id"] for d in docs]).items()}
        found = error_list.collect(v, docs, store.gen_edits(v), store.gen_stage_log(v), schema_fields.SCHEMA,
                                   WORKFLOW, status)
        per_vol[v] = len(found)
        rows += found
        if len(rows) > ERROR_ROWS_MAX:
            raise HTTPException(400, f"오류 행이 {ERROR_ROWS_MAX}개를 넘음 — 철을 나눠 받는다")
    key = secrets.token_bytes(32)  # 가린 ID 키: 이 응답에만 쓰고 버린다 (저장·기록하지 않음)
    body = export.to_xlsx(error_list.build(ERROR_TEMPLATE, rows, key))
    info.update(volumes=len(volumes), rows=len(rows))
    return body, per_vol


@app.delete("/volumes/{vol}")
@audit_as("purge")
def purge(vol: str, user: Admin, confirm: str = "", allow_problems: bool = False):
    """납품 뒤 사업 데이터 일괄 삭제: 쪽 이미지·OCR·잘라낸 칸, 문서·이력·이벤트·검사 행, LangGraph 체크포인트, Qdrant 벡터.
    비용 장부·감사 로그·작업 시간·출력 기록은 남긴다 (값 없음). 실수를 막으려고 confirm에 철 이름을 다시 받는다.
    마지막 출력 뒤에 내용이 바뀌었으면(다시 올림·실행·검토·수정·검사) 409. 그 출력에 검증 문제가 있었으면
    allow_problems=true를 따로 줘야 지운다 (고칠 근거인 쪽 이미지가 사라지므로)"""
    with audited(user, "purge", vol, allow_problems=allow_problems) as info:
        if confirm != vol:
            raise HTTPException(400, "confirm에 철 이름을 그대로 넣어야 함")
        if vol in running:
            raise HTTPException(409, "실행 중인 철")
        last = store.last_export(vol, stage_moves=WORKFLOW["count_unconfirmed"])
        if not last:
            raise HTTPException(409, "납품 형식으로 내보낸 적 없는 철 (/export.xlsx 먼저)")
        if not last["current"]:
            raise HTTPException(409, "내보낸 뒤 내용이 바뀐 철 — 다시 내보낸 뒤 지운다")
        if last["problems"] and not allow_problems:
            raise HTTPException(409, f"마지막 출력에 검증 문제 {last['problems']}건 (지우려면 allow_problems=true)")
        tok = reserve(vol, "purging")  # 지우는 동안 같은 철 실행(/run·/resume)·업로드는 409
        if tok is None:
            raise HTTPException(409, "실행 중인 철")
        try:
            info.update(store.purge_volume(vol, G.checkpointer.delete_thread))
        finally:
            release(vol, tok)
    return {"vol": vol, "deleted": info}


def query_words(q):
    """낱말 검색에 넣을 낱말: 두 글자 이상, 연도(1997·1997년)는 뺀다"""
    return [w for w in re.split(r"\s+", q) if len(w) >= 2 and not re.fullmatch(r"\d{4}년?", w)]


def rrf(*lists):
    """Reciprocal Rank Fusion 점수 {(vol, doc_id): 점수} — 문서 키는 철과 쪽 이름 둘 다
    (https://github.com/reddol18/docflow/issues/50). 검색 평가(`eval/search_score.py`)도 이 함수를 쓴다"""
    score = {}
    for lst in lists:
        for rank, r in enumerate(lst):
            k = store.doc_key(r)
            score[k] = score.get(k, 0) + 1 / (60 + rank + 1)
    return score


CANDIDATES = 30  # 방식마다 가져오는 후보 수


def ranked(q, vec, year_from=None, year_to=None):
    """/search의 순위 계산(질의 임베딩 뒤) → (벡터 결과, 낱말 결과, RRF 점수, RRF 순 (vol, doc_id)).
    검색 평가(`eval/search_score.py`)도 이 함수를 그대로 부른다"""
    v = store.vector_search(vec, year_from, year_to, limit=CANDIDATES)
    kw = store.keyword_search(query_words(q), year_from, year_to, limit=CANDIDATES)
    score = rrf(v, kw)
    return v, kw, score, sorted(score, key=score.get, reverse=True)


def hybrid(q, year_from=None, year_to=None, k=10, user=None):
    # LLM_PROVIDER=off면 질의 임베딩에서 LLMOff → 503 (llm_off 처리기). 낱말 검색만으로 답할지는 정하지 않았다.
    # 질의 임베딩은 검색·질문 예산(query) — 다 쓰면 BudgetExceeded → 503 (budget_exceeded 처리기)
    vec = llm.embed("-", [q], scope="query", user_id=getattr(user, "id", None))[0]
    v, kw, score, order = ranked(q, vec, year_from, year_to)
    top = order[:k]
    rows = store.get_documents(top)
    vr = {store.doc_key(r): i + 1 for i, r in enumerate(v)}
    kr = {store.doc_key(r): i + 1 for i, r in enumerate(kw)}
    shown = schema_fields.shown(schema_fields.SCHEMA)  # 김해: 문서번호·시행일자·수신·제목
    # 칸 값은 fields 아래에 — 스키마 칸 이름이 순위·철 키(doc_id·vol·rrf …)를 덮지 않게 (https://github.com/reddol18/docflow/issues/72)
    return [{"doc_id": k[1], "rrf": round(score[k], 5), "vector_rank": vr.get(k), "keyword_rank": kr.get(k),
             "vol": k[0], "fields": {c: (rows[k]["fields"] or {}).get(c, "") for c in shown}}
            for k in top if k in rows]


# ---------- /search·/ask 사용자별 호출 제한 (https://github.com/reddol18/docflow/issues/7, API6) ----------
# 분당·하루 상한 (통제자 결정 2026-11-10, 환경변수로 바꿈). 넘으면 그 사용자만 429 + Retry-After — 다른 사용자는 그대로.
# 셈은 Postgres(api_quota)에: 하루 쿼터는 재시작·배포 뒤에도 이어져야 하고, 분당 제한도 같은 문장으로 함께 세면
# Pod가 여러 개가 되어도(https://github.com/reddol18/docflow/issues/16) 그대로 맞는다. 요청마다 짧은 문장 2개.
# 사용자 키는 users.id (인증을 끈 개발 모드는 사용자 이름). 상한 0 = 그 창은 막지 않음
RATE = {route: {"min": int(os.environ.get(f"{route.upper()}_PER_MIN", per_min)),
                "day": int(os.environ.get(f"{route.upper()}_PER_DAY", per_day))}
        for route, per_min, per_day in (("ask", 10, 200), ("search", 30, 1000))}


def _now():
    return dt.datetime.now(store.KST)


def _quota_windows(route, now=None):
    now = now or _now()
    _, day = store.kst_day(now)
    lim = RATE[route]
    wins = [("min", now.replace(second=0, microsecond=0), lim["min"]), ("day", day, lim["day"])]
    return [w for w in wins if w[2] > 0]


@contextmanager
def rate_limited(route, user):
    """호출 한 번을 센다 — 경로 함수 안에서 부르므로 질의 검증(422)을 통과한 요청만 센다. 상한이면 429 + Retry-After
    (분당: 지금 분이 끝날 때까지, 하루: 다음 날 0시 KST까지). 안에서 검색·질문 예산이 차거나(BudgetExceeded) LLM이
    꺼져 있으면(LLMOff) — 둘 다 503 — 이 1회를 되돌린다. 사용자 탓이 아닌 거절로 쿼터를 깎지 않게"""
    key = str(user.id) if getattr(user, "id", None) is not None else f"name:{getattr(user, 'username', 'dev')}"
    now = _now()
    wins = _quota_windows(route, now)
    full = store.take_quota(key, route, wins)
    if full:  # 누가 얼마나 막혔나는 서버 로그로 (질의 글자는 남기지 않는다)
        print(f"[rate] {route} {full} 상한 — user {key}", file=sys.stderr, flush=True)
    if full == "min":
        raise HTTPException(429, f"{route} 호출이 많습니다 — 분당 {RATE[route]['min']}회",
                            headers={"Retry-After": str(max(1, 60 - now.second))})
    if full == "day":
        raise HTTPException(429, f"{route} 오늘 호출 한도 {RATE[route]['day']}회를 다 썼습니다",
                            headers={"Retry-After": str(_until_tomorrow())})
    try:
        yield
    except (llm.BudgetExceeded, llm.LLMOff):  # 둘 다 503 — 설치·예산 쪽 거절
        try:
            store.refund_quota(key, route, wins)
        except Exception:
            errors.log_exception(f"[rate] {route} 되돌리기 실패")
        raise


@app.get("/search")
def search(q: Annotated[str, Query(min_length=1, max_length=Q_MAX)], user: Viewer, year_from: int | None = None,
           year_to: int | None = None, k: Annotated[int, Query(ge=1, le=50)] = 10):
    with rate_limited("search", user):
        return hybrid(q, year_from, year_to, k, user)


@app.get("/ask")
def ask(q: Annotated[str, Query(min_length=1, max_length=Q_MAX)], user: Viewer, year_from: int | None = None,
        year_to: int | None = None):
    with rate_limited("ask", user):
        hits = hybrid(q, year_from, year_to, 5, user)
        for h in hits:
            h["body"] = " ".join(store.load_ocr(h["vol"], h["doc_id"])["lines"][:40])
        return {"answer": llm.answer(q, hits, user_id=getattr(user, "id", None)),
                "sources": [{k: h[k] for k in h if k != "body"} for h in hits]}


@app.get("/graph", response_class=PlainTextResponse)
def graph_view(_: Viewer):
    return G.get_graph(xray=True).draw_mermaid()


@app.get("/schema")
def schema_view(_: Viewer):
    """화면용 칸 목록 — 숨김 칸·추출 지시문·고정값은 내보내지 않는다.
    화면은 칸 이름이 아니라 여기 정보로 판단한다 (https://github.com/reddol18/docflow/issues/72):
      rule  값을 만드는 코드 규칙 이름 (없으면 null) — 수신 보정 등 규칙별 안내
      hw    보여 줄 잘라낸 손글씨 칸 (/crops/{vol}/{doc_id}_{hw}.png, 문서 hw[hw]). 없으면 null
      crop  보여 줄 잘라 둔 원본 줄 (/crops/{vol}/{doc_id}_{crop}.png — 수신·제목, 못 찾은 문서는 파일 없음 404). 없으면 null
      search_columns  검색 결과 열 (스키마 search.columns, 기본 보이는 칸 전부)
      validate  칸 검증 규칙 {rules, reasons} (규칙 없는 칸은 키 없음) — reasons는 이 칸의 기계 검수 표시 사유 글자 전부
      machine_flag_prefix  기계 검수 표시 사유의 머리 — 화면은 이것으로 표시 종류를 가린다 (규칙을 바꾼 뒤 남은 사유 포함)"""
    s = schema_fields.SCHEMA
    return {"name": s["name"], "search_columns": list(s["search"]["columns"]),
            "machine_flag_prefix": field_rules.PREFIX,  # 기계 검수 표시 사유의 머리 (서버 상수) — 화면이 표시 종류를 가린다
            "fields": [
        {"name": f["name"], "label": f["label"], "level": f["level"], "method": f["method"],
         "editable": f["name"] in EDITABLE, "flag_key": schema_fields.flag_key(f),
         "isad": f.get("isad") or (f["name"] if "area" in f else None),  # 기본 스키마 칸은 그 자체가 ISAD(G) 칸
         "rule": f.get("rule"), "hw": flow.hw_cell(f), "crop": f.get("crop"),
         # 칸 검증 규칙: 규칙 이름과 이 칸에 붙을 수 있는 기계 검수 표시 사유(정확한 글자 — 화면이 표시 종류를 가린다).
         # 규칙 없는 칸은 키 자체를 싣지 않는다 (규칙 없는 스키마의 응답은 지금과 같은 모양)
         **({"validate": {"rules": f["validate"].names(),
                          "reasons": sorted(field_rules.reason(f, k) for k in field_rules.possible(f))}}
            if "validate" in f else {})}
        for f in s["fields"] if f["name"] in schema_fields.shown(s)]}
