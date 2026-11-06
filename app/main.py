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
  GET   /documents/{vol}/{doc_id}/edits  수정 이력 (칸·이전 값·새 값·사용자·시각)
  GET   /crops/{vol}/{name}            손글씨 칸을 잘라 둔 이미지
  GET   /head/{vol}/{page}             쪽 위 45% 줄인 이미지 (검토용)
  GET   /review/{vol}                  사람 검토 화면 → 아래 POST로 고친 값을 넣으면 흐름이 이어진다
  POST  /review/{vol}                  검토 화면 폼 제출
  POST  /review/{vol}/json             검토 값을 JSON으로 제출 (스크립트·Next 검토 화면)
  GET   /search                        ?q=&year_from=&year_to=&k= 벡터(Qdrant) + 낱말(Postgres) 결과를 RRF로 합침
  GET   /ask                           ?q= 검색 상위 문서로 GPT가 답하고 근거 doc_id를 단다
  GET   /graph                         흐름 그림 (mermaid)
  GET   /report                        공정·품질 리포트 (?day_from=&day_to=, 기본 최근 14일)
  GET   /report.csv                    같은 리포트 CSV
  /auth/*                              로그인·로그아웃·OIDC (app/auth.py)
머리말 목록은 시험(`tests/test_input.py`)이 실제 라우트와 비교한다 (https://github.com/reddol18/docflow/issues/49).

인증: /healthz와 로그인 경로를 빼고 모두 로그인 필요 (앱 전체 의존성 auth.guard, 기본 거부).
역할은 경로마다 더 좁힌다 — 보기 viewer, 검토·수정 reviewer, 업로드·실행 admin.
"""
import datetime as dt
import html
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from typing import Annotated, Literal

import cv2
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command
from pydantic import BaseModel, Field

from app import auth, errors, llm, ocr, report, store
from app import graph as flow
from app.auth import Admin, Reviewer, Viewer
from app.review_input import EDITABLE, changes, clean_fixes

# 점만으로 된 이름(., .., ...)은 경로 이탈이라 거부. fullmatch + \Z — '$'는 끝 줄바꿈 앞에서도 맞아 'v1\n'이 지나간다
VOL_RX = re.compile(r"(?!\.+\Z)[\w.-]{1,80}")
Q_MAX = 200  # 검색어 길이 상한: 질의는 문서 제목(색인 칸 500자 안)보다 짧고, 임베딩·답변 입력 토큰을 묶는다
CELL_MAX = 500  # 색인 칸 하나의 길이 상한 (제목도 이 안에 든다) — JSON·폼 검토와 검토 화면 입력칸이 같이 쓴다
CellValue = Annotated[str, Field(max_length=CELL_MAX)]
JPEG_MAGIC = b"\xff\xd8\xff"
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
    yield
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

# 응답 보안 헤더 (https://github.com/reddol18/docflow/issues/47, ASVS V14.4.3·V14.4.4·V14.4.7·V8.2.1)
# API는 무엇도 그리지 않으므로 'none'. 서버가 그리는 HTML(/review)만 그 화면에 필요한 만큼 연다 (인라인 스크립트는 없다)
API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
HTML_CSP = ("default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'none'")
SECURITY_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY"}


@app.exception_handler(store.PathEscape)
async def path_escape(request: Request, exc: store.PathEscape):
    return PlainTextResponse("bad path", status_code=400)


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
    처리 중에 yield된 info에 개수·칸 이름 같은 요약을 더할 수 있다 — 값·원문은 넣지 않는다."""
    try:
        yield info
    except HTTPException as e:
        store.audit(user, action, vol, doc_id, f"rejected_{e.status_code}", **info)
        raise
    except Exception as e:
        store.audit(user, action, vol, doc_id, "error", code=errors.code_of(e), **info)
        raise
    store.audit(user, action, vol, doc_id, "ok", **info)


def submit(vol, arg):
    if vol in running:
        raise HTTPException(409, f"{vol} 이미 실행 중")
    running[vol] = "queued"

    def job():
        running[vol] = "running"
        store.event(vol, "run", "start", None, resume=arg is None or isinstance(arg, Command))
        try:
            G.invoke(arg, cfg(vol))
            snap = G.get_state(cfg(vol))
            print(f"run {vol} stopped/finished: next={snap.next}", flush=True)
            if snap.interrupts:
                store.event(vol, "review", "waiting", None, flagged=len(snap.interrupts[0].value["flagged"]))
            else:
                store.event(vol, "run", "end", None)
        except Exception as e:
            errors.log_exception(f"run {vol}")  # 상세는 서버 로그에만 (키 꼴 문자열은 가림)
            store.event(vol, "run", "error", None, **errors.public(e))  # 화면엔 분류 코드만
        finally:
            running.pop(vol, None)

    runner.submit(job)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/volumes/{vol}/pages")
async def upload(vol: str, user: Admin, files: list[UploadFile] = File(...)):
    with audited(user, "upload", vol, files=len(files)) as info:
        out = await _upload(vol, files)
        info["saved"] = out["saved"]
    return out


async def _upload(vol, files):
    d = store.pages_dir(vol)
    names = []
    for f in files:  # 1) 이름과 앞 3바이트만 모두 확인한다 (파일 전체를 메모리에 올리지 않는다)
        name = (f.filename or "").rsplit("/", 1)[-1]
        if not re.fullmatch(r"[\w.-]+\.jpg", name):
            raise HTTPException(400, f"bad file name {name}")
        head = await f.read(len(JPEG_MAGIC))
        await f.seek(0)
        if head != JPEG_MAGIC:
            raise HTTPException(400, f"not a JPEG: {name}")
        names.append(name)
    # 2) 임시 이름(.*.part — *.jpg 목록에 안 잡힘)으로 흘려 쓰고, 다 쓴 뒤에 한꺼번에 바꾼다
    #    → 하나라도 틀리거나 쓰다 실패하면 저장되는 것이 없다 (같은 이름의 기존 쪽도 그대로)
    d.mkdir(parents=True, exist_ok=True)
    parts = []
    try:
        for f, name in zip(files, names):
            tmp = d / f".{name}.part"
            parts.append(tmp)
            with tmp.open("wb") as out:
                shutil.copyfileobj(f.file, out)
        for tmp, name in zip(parts, names):
            os.replace(tmp, d / name)
    finally:
        for tmp in parts:
            tmp.unlink(missing_ok=True)
    return {"vol": vol, "saved": len(names), "total": len(list(d.glob("*.jpg")))}


@app.post("/volumes/{vol}/run")
def run(vol: str, user: Admin, review: str = "human"):
    with audited(user, "run", vol, review=review[:10]):
        if review not in ("human", "auto"):
            raise HTTPException(400, "review=human|auto")
        if not any(store.pages_dir(vol).glob("*.jpg")):
            raise HTTPException(404, "올린 쪽이 없음")
        submit(vol, {"vol": vol, "review_mode": review})
    return {"vol": vol, "started": True}


@app.post("/volumes/{vol}/resume")
def resume(vol: str, user: Admin):
    with audited(user, "resume", vol):
        snap = G.get_state(cfg(vol))
        if not snap.next:
            raise HTTPException(409, "이어 갈 단계가 없음 (끝났거나 시작 안 함)")
        if snap.interrupts:
            raise HTTPException(409, "사람 검토 대기 중 → /review 에서 이어 감")
        submit(vol, None)
    return {"vol": vol, "resumed_at": list(snap.next)}


def summary(vol):
    snap = G.get_state(cfg(vol))
    v = snap.values or {}
    docs = v.get("docs", [])
    if running.get(vol) == "queued":
        stage = "queued"
    elif snap.interrupts:
        stage = "review"
    elif vol in running:
        stage = "running"
    elif v.get("indexed") is not None and not snap.next:
        stage = "done"
    elif snap.next:
        stage = "stopped"  # Pod 재시작 등으로 끊김 → /resume
    else:
        stage = "new"
    return {
        "vol": vol,
        "stage": stage,
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
        "spent_today_usd": round(store.spent_today(), 6),
    }


def volume_names():
    """쪽 폴더 목록 중 철 이름 규칙에 맞는 것만 (규칙에 안 맞는 폴더 하나가 목록·리포트 전체를 400으로 만들지 않게)"""
    root = store.DATA / "pages"
    return sorted(p.name for p in root.iterdir() if p.is_dir() and VOL_RX.fullmatch(p.name)) if root.exists() else []


@app.get("/volumes")
def volumes(_: Viewer):
    vols = volume_names()
    return [summary(v) for v in vols]


@app.get("/volumes/{vol}")
def status(vol: str, _: Viewer):
    return summary(vol)


@app.get("/volumes/{vol}/events")
def volume_events(vol: str, _: Viewer, after: int = 0):
    return store.events(vol, after)


@app.patch("/documents/{vol}/{doc_id}")
def edit_document(vol: str, doc_id: str, fields: dict[str, CellValue], user: Reviewer):
    """색인이 끝난 문서를 사람이 고친다: 그래프 상태·Postgres·Qdrant를 함께 바꾼다.
    검토 대기 중인 철은 /review/{vol}/json 으로 흐름을 이어 가야 하므로 여기서는 받지 않는다."""
    with audited(user, "edit", vol, doc_id) as info:
        d, changed = _edit_document(vol, doc_id, fields, user)
        info["fields"] = changed  # 칸 이름만 (값은 edits 표)
    return d


def _edit_document(vol, doc_id, fields, user):
    snap = G.get_state(cfg(vol))
    if snap.interrupts or vol in running:
        raise HTTPException(409, "실행 중이거나 검토 대기 중")
    docs = {d["doc_id"]: d for d in (snap.values or {}).get("docs", [])}
    if doc_id not in docs:
        raise HTTPException(404)
    old = docs[doc_id]["fields"]
    fix = {k: str(v).strip() for k, v in fields.items() if k in EDITABLE}
    fix = {k: v for k, v in fix.items() if v != (old.get(k) or "")}
    if not fix:
        return docs[doc_id], []
    d = dict(docs[doc_id], fields={**old, **fix}, edited=True)
    if "docno" in fix:
        d["fields"]["serial"] = flow.layout.serial_of(fix["docno"])
    G.update_state(cfg(vol), {"docs": [d]}, as_node="index")
    store.save_documents([d])
    # 이력은 값이 저장된 바로 뒤, 외부 호출(임베딩·Qdrant) 전에 — 그쪽이 실패해도 누가 무엇을 바꿨는지는 남는다
    store.add_edits(vol, [(doc_id, k, old.get(k), v) for k, v in fix.items()], user.id, "edit")
    store.upsert_vectors([d], llm.embed(vol, [flow.doc_text(vol, d)]))
    store.event(vol, "edit", "end", doc_id, fields=list(fix), user=user.username)
    return d, list(fix)


@app.get("/documents/{vol}/{doc_id}/edits")
def document_edits(vol: str, doc_id: str, _: Viewer):
    return store.edit_history(vol, doc_id)


@app.get("/volumes/{vol}/documents")
def documents(vol: str, _: Viewer):
    return (G.get_state(cfg(vol)).values or {}).get("docs", [])


@app.get("/crops/{vol}/{name}")
def crop(vol: str, name: str, _: Viewer):
    if not re.fullmatch(r"[\w.-]+_(docno|date)\.png", name):
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


LABEL = {"docno": "문서번호", "date": "시행일자", "recipient": "수신", "title": "제목", "serial": "일련번호"}


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
        for k in ["docno", "date", "recipient", "title"]:
            fk = "serial" if k == "docno" else k
            fl = " ".join(d["flags"].get(fk, []))
            val = html.escape(d["fields"][k] or "")
            out.append(f"<div class=f><label>{LABEL[k]}</label><input name='{did}__{k}' value=\"{val}\" maxlength={CELL_MAX}{ro}>"
                       f" <span class=flag>{html.escape(fl)}</span></div>")
            if k in ("docno", "date") and d["hw"].get(k, {}).get("text") is not None:
                # 인라인 onerror 대신 파일이 있을 때만 그린다 (CSP가 인라인 스크립트를 막는다)
                if store.crop_path(vol, did, k).exists():
                    out.append(f"<img class=crop src='/crops/{html.escape(vol)}/{html.escape(did)}_{k}.png'>")
                out.append(f"<span class=flag>손글씨 판독: {html.escape(d['hw'][k]['text'])} (확신도 {d['hw'][k]['conf']})</span>")
        out.append("</div></div>")
    out.append("<button type=submit>검토 끝 — 이어서 색인</button></form>" if editor else "</form>")
    return "".join(out)


def record_review(vol, flagged, fixes, user):
    """공정 리포트용: 표시된 문서·칸 수와 사람이 고친 칸 수 (https://github.com/reddol18/docflow/issues/11).
    수정 이력: 고친 칸마다 이전 값·새 값·사용자 (https://github.com/reddol18/docflow/issues/6)."""
    store.add_edits(vol, changes(flagged, fixes), user.id, "review")
    store.event(vol, "review", "submit", None, docs=len(flagged),
                cells=sum(len(d["flags"]) for d in flagged),
                fixed_docs=len(fixes), fixed_cells=sum(len(v) for v in fixes.values()), user=user.username)


def resume_review(vol, flagged, fixes, user):
    """이력을 먼저 남기고 흐름을 잇는다 — 이력 저장이 실패하면 고친 값도 반영되지 않는다 (누가 바꿨는지 잃지 않게)."""
    if vol in running:
        raise HTTPException(409, f"{vol} 이미 실행 중")
    record_review(vol, flagged, fixes, user)
    submit(vol, Command(resume=fixes))


@app.post("/review/{vol}")
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
        resume_review(vol, flagged, fixes, user)
        info.update(docs=len(flagged), fixed_cells=sum(len(v) for v in fixes.values()))
    return RedirectResponse(f"/volumes/{vol}", status_code=303)


@app.post("/review/{vol}/json")
def review_json(vol: str, body: Annotated[dict[str, dict[str, CellValue]], Field(max_length=1000)], user: Reviewer):
    """스크립트·검토 화면용: {doc_id: {칸: 값}} 으로 이어 가기. 검토 대기 문서의 바뀐 칸만 반영한다."""
    with audited(user, "review", vol, via="json") as info:
        snap = G.get_state(cfg(vol))
        if not snap.interrupts:
            raise HTTPException(409, "검토 대기 없음")
        flagged = snap.interrupts[0].value["flagged"]
        fixes = clean_fixes(flagged, [(did, k, v) for did, f in body.items() for k, v in f.items()])
        resume_review(vol, flagged, fixes, user)
        info.update(docs=len(flagged), fixed_cells=sum(len(v) for v in fixes.values()))
    return {"vol": vol, "fixed": {k: list(v) for k, v in fixes.items()}}


class Timing(BaseModel):
    doc_id: str = Field(pattern=r"^[\w.-]{1,120}$")
    mode: Literal["review", "edit"] = "review"
    active_ms: int = Field(ge=0, le=4 * 3600 * 1000)
    opened_at: dt.datetime | None = None
    fields_changed: int = Field(0, ge=0, le=10)


@app.post("/volumes/{vol}/timings")
def timings(vol: str, rows: Annotated[list[Timing], Field(max_length=1000)], user: Reviewer):
    """검토 화면이 확정(검토 제출·문서 저장) 때 보내는 문서별 작업 시간 (자리 비움 제외).
    이 철에 없는 문서와 1초 미만(넘겨 보기만 한 것)은 버린다."""
    known = {d["doc_id"] for d in (G.get_state(cfg(vol)).values or {}).get("docs", [])}
    keep = [r.model_dump() for r in rows if r.active_ms >= 1000 and r.doc_id in known]
    store.add_review_times(vol, keep, user.id)
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
    return {"day_from": lo, "day_to": hi, "volume": report.volume_counts([summary(v) for v in vols]),
            "daily": report.daily(lo, hi), "review_time": report.review_time_summary(lo, hi),
            "columns": dict(report.COLUMNS)}


@app.get("/report.csv")
def report_csv(_: Viewer, day_from: dt.date | None = None, day_to: dt.date | None = None):
    lo, hi = _range(day_from, day_to)
    return Response(report.to_csv(report.daily(lo, hi)), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="docflow_report_{lo}_{hi}.csv"'})


def query_words(q):
    """낱말 검색에 넣을 낱말: 두 글자 이상, 연도(1997·1997년)는 뺀다"""
    return [w for w in re.split(r"\s+", q) if len(w) >= 2 and not re.fullmatch(r"\d{4}년?", w)]


def rrf(*lists):
    """Reciprocal Rank Fusion 점수 {doc_id: 점수}. 검색 평가(`eval/search_score.py`)도 이 함수를 쓴다"""
    score = {}
    for lst in lists:
        for rank, r in enumerate(lst):
            score[r["doc_id"]] = score.get(r["doc_id"], 0) + 1 / (60 + rank + 1)
    return score


CANDIDATES = 30  # 방식마다 가져오는 후보 수


def ranked(q, vec, year_from=None, year_to=None):
    """/search의 순위 계산(질의 임베딩 뒤) → (벡터 결과, 낱말 결과, RRF 점수, RRF 순 doc_id).
    검색 평가(`eval/search_score.py`)도 이 함수를 그대로 부른다"""
    v = store.vector_search(vec, year_from, year_to, limit=CANDIDATES)
    kw = store.keyword_search(query_words(q), year_from, year_to, limit=CANDIDATES)
    score = rrf(v, kw)
    return v, kw, score, sorted(score, key=score.get, reverse=True)


def hybrid(q, year_from=None, year_to=None, k=10):
    vec = llm.embed("-", [q])[0]
    v, kw, score, order = ranked(q, vec, year_from, year_to)
    top = order[:k]
    rows = store.get_documents(top)
    vr = {r["doc_id"]: i + 1 for i, r in enumerate(v)}
    kr = {r["doc_id"]: i + 1 for i, r in enumerate(kw)}
    return [{"doc_id": d, "rrf": round(score[d], 5), "vector_rank": vr.get(d), "keyword_rank": kr.get(d),
             **{c: rows[d][c] for c in ["vol", "docno", "date", "recipient", "title"]}} for d in top if d in rows]


@app.get("/search")
def search(q: Annotated[str, Query(min_length=1, max_length=Q_MAX)], _: Viewer, year_from: int | None = None,
           year_to: int | None = None, k: Annotated[int, Query(ge=1, le=50)] = 10):
    return hybrid(q, year_from, year_to, k)


@app.get("/ask")
def ask(q: Annotated[str, Query(min_length=1, max_length=Q_MAX)], _: Viewer, year_from: int | None = None,
        year_to: int | None = None):
    hits = hybrid(q, year_from, year_to, 5)
    for h in hits:
        h["body"] = " ".join(store.load_ocr(h["vol"], h["doc_id"])["lines"][:40])
    return {"answer": llm.answer(q, hits), "sources": [{k: h[k] for k in h if k != "body"} for h in hits]}


@app.get("/graph", response_class=PlainTextResponse)
def graph_view(_: Viewer):
    return G.get_graph(xray=True).draw_mermaid()
