"""docflow API.

  POST /volumes/{vol}/pages      쪽 이미지 올리기 (multipart, 여러 장)
  POST /volumes/{vol}/run        흐름 시작 (?review=human|auto)
  POST /volumes/{vol}/resume     멈춘 곳부터 다시 (Pod 재시작 뒤 등). 사람 검토 대기는 /review로 이어 간다
  GET  /volumes/{vol}            상태: 다음 노드, 검토 대기 여부, 문서 수, 비용
  GET  /volumes/{vol}/documents  문서별 결과 (B 값·손글씨 판독·표시 포함)
  GET  /review/{vol}             사람 검토 화면 → POST로 고친 값을 넣으면 흐름이 이어진다
  GET  /search?q=&year_from=&year_to=   벡터(Qdrant) + 낱말(Postgres) 결과를 RRF로 합침
  GET  /ask?q=                   검색 상위 문서로 GPT가 답하고 근거 doc_id를 단다
  GET  /graph                    흐름 그림 (mermaid)
"""
import html
import re
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import cv2
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command

from app import graph as flow
from app import llm, ocr, store

VOL_RX = re.compile(r"^[\w.-]{1,80}$")
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


app = FastAPI(title="docflow", lifespan=lifespan)


def cfg(vol):
    return {"configurable": {"thread_id": vol}, "max_concurrency": 4}


def check(vol):
    if not VOL_RX.match(vol):
        raise HTTPException(400, "bad volume id")


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
            traceback.print_exc()
            store.event(vol, "run", "error", None, error=f"{type(e).__name__}: {e}"[:300])
        finally:
            running.pop(vol, None)

    runner.submit(job)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/volumes/{vol}/pages")
async def upload(vol: str, files: list[UploadFile] = File(...)):
    check(vol)
    d = store.pages_dir(vol)
    d.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in files:
        name = f.filename.rsplit("/", 1)[-1]
        if not re.fullmatch(r"[\w.-]+\.jpg", name):
            raise HTTPException(400, f"bad file name {name}")
        (d / name).write_bytes(await f.read())
        n += 1
    return {"vol": vol, "saved": n, "total": len(list(d.glob("*.jpg")))}


@app.post("/volumes/{vol}/run")
def run(vol: str, review: str = "human"):
    check(vol)
    if review not in ("human", "auto"):
        raise HTTPException(400, "review=human|auto")
    if not any(store.pages_dir(vol).glob("*.jpg")):
        raise HTTPException(404, "올린 쪽이 없음")
    submit(vol, {"vol": vol, "review_mode": review})
    return {"vol": vol, "started": True}


@app.post("/volumes/{vol}/resume")
def resume(vol: str):
    check(vol)
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
        "ocr_done": len(list((store.DATA / "ocr" / vol).glob("*.json"))),
        "uploaded": len(list(store.pages_dir(vol).glob("*.jpg"))),
        "documents": len(docs),
        "orphans": v.get("orphans", []),
        "flagged_documents": sum(bool(d.get("flags")) for d in docs),
        "indexed": v.get("indexed"),
        "usage": store.usage_summary(vol),
        "spent_today_usd": round(store.spent_today(), 6),
    }


@app.get("/volumes")
def volumes():
    root = store.DATA / "pages"
    vols = sorted(p.name for p in root.iterdir() if p.is_dir()) if root.exists() else []
    return [summary(v) for v in vols]


@app.get("/volumes/{vol}")
def status(vol: str):
    check(vol)
    return summary(vol)


@app.get("/volumes/{vol}/events")
def volume_events(vol: str, after: int = 0):
    check(vol)
    return store.events(vol, after)


@app.patch("/documents/{vol}/{doc_id}")
def edit_document(vol: str, doc_id: str, fields: dict):
    """색인이 끝난 문서를 사람이 고친다: 그래프 상태·Postgres·Qdrant를 함께 바꾼다.
    검토 대기 중인 철은 /review/{vol}/json 으로 흐름을 이어 가야 하므로 여기서는 받지 않는다."""
    check(vol)
    snap = G.get_state(cfg(vol))
    if snap.interrupts or vol in running:
        raise HTTPException(409, "실행 중이거나 검토 대기 중")
    docs = {d["doc_id"]: d for d in (snap.values or {}).get("docs", [])}
    if doc_id not in docs:
        raise HTTPException(404)
    fix = {k: str(v).strip() for k, v in fields.items() if k in ("docno", "date", "recipient", "title")}
    d = dict(docs[doc_id], fields={**docs[doc_id]["fields"], **fix}, edited=True)
    if "docno" in fix:
        d["fields"]["serial"] = flow.layout.serial_of(fix["docno"])
    G.update_state(cfg(vol), {"docs": [d]}, as_node="index")
    store.save_documents([d])
    store.upsert_vectors([d], llm.embed(vol, [flow.doc_text(vol, d)]))
    store.event(vol, "edit", "end", doc_id, fields=list(fix))
    return d


@app.get("/volumes/{vol}/documents")
def documents(vol: str):
    check(vol)
    return (G.get_state(cfg(vol)).values or {}).get("docs", [])


@app.get("/crops/{vol}/{name}")
def crop(vol: str, name: str):
    check(vol)
    if not re.fullmatch(r"[\w.-]+_(docno|date)\.png", name):
        raise HTTPException(400)
    p = store.DATA / "crops" / vol / name
    if not p.exists():
        raise HTTPException(404)
    return Response(p.read_bytes(), media_type="image/png")


@app.get("/head/{vol}/{page}")
def head(vol: str, page: str):
    """검토용: 쪽 위 45%를 줄여서 보여 준다."""
    check(vol)
    if not re.fullmatch(r"[\w.-]+", page):
        raise HTTPException(400)
    img = ocr.read_image(store.pages_dir(vol) / f"{page}.jpg")
    if img is None:
        raise HTTPException(404)
    h, w = img.shape[:2]
    img = img[: int(h * 0.45)]
    img = cv2.resize(img, (900, int(img.shape[0] * 900 / w)))
    return Response(cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes(), media_type="image/jpeg")


LABEL = {"docno": "문서번호", "date": "시행일자", "recipient": "수신", "title": "제목", "serial": "일련번호"}


@app.get("/review/{vol}", response_class=HTMLResponse)
def review_page(vol: str):
    check(vol)
    snap = G.get_state(cfg(vol))
    if not snap.interrupts:
        return f"<meta charset=utf-8><p>{html.escape(vol)}: 검토 대기 없음. <a href='/volumes/{vol}'>상태</a></p>"
    flagged = snap.interrupts[0].value["flagged"]
    out = [f"""<!doctype html><meta charset=utf-8><title>검토 {html.escape(vol)}</title>
<style>body{{font:14px sans-serif;margin:16px;max-width:1500px}} .doc{{display:flex;gap:16px;border-top:1px solid #ccc;padding:12px 0}}
.f{{margin:4px 0}} .f label{{display:inline-block;width:70px}} .f input{{width:420px}} .flag{{color:#b8322a;font-size:12px}}
img.crop{{max-height:60px;border:1px solid #999;display:block;margin:2px 0}}</style>
<h1>검토 — {html.escape(vol)} ({len(flagged)}건)</h1>
<p>빨간 표시가 붙은 칸만 확인하면 됩니다. 고칠 값만 바꾸고 맨 아래 버튼을 누르면 흐름이 이어져 색인됩니다.</p>
<form method=post>"""]
    for d in flagged:
        did = d["doc_id"]
        out.append(f"<div class=doc><img src='/head/{vol}/{did}' width=600><div><b>{html.escape(did)}</b> ({len(d['pages'])}쪽)")
        for k in ["docno", "date", "recipient", "title"]:
            fk = "serial" if k == "docno" else k
            fl = " ".join(d["flags"].get(fk, []))
            val = html.escape(d["fields"][k] or "")
            out.append(f"<div class=f><label>{LABEL[k]}</label><input name='{did}__{k}' value=\"{val}\">"
                       f" <span class=flag>{html.escape(fl)}</span></div>")
            if k in ("docno", "date") and d["hw"].get(k, {}).get("text") is not None:
                out.append(f"<img class=crop src='/crops/{vol}/{did}_{k}.png' onerror=\"this.style.display='none'\">"
                           f"<span class=flag>손글씨 판독: {html.escape(d['hw'][k]['text'])} (확신도 {d['hw'][k]['conf']})</span>")
        out.append("</div></div>")
    out.append("<button type=submit>검토 끝 — 이어서 색인</button></form>")
    return "".join(out)


@app.post("/review/{vol}")
async def review_submit(vol: str, request: Request):
    check(vol)
    snap = G.get_state(cfg(vol))
    if not snap.interrupts:
        raise HTTPException(409, "검토 대기 없음")
    form = await request.form()
    cur = {d["doc_id"]: d["fields"] for d in snap.interrupts[0].value["flagged"]}
    fixes = {}
    for key, val in form.items():
        did, _, k = key.rpartition("__")
        if did in cur and k in cur[did] and val.strip() != (cur[did][k] or ""):
            fixes.setdefault(did, {})[k] = val.strip()
    submit(vol, Command(resume=fixes))
    return RedirectResponse(f"/volumes/{vol}", status_code=303)


@app.post("/review/{vol}/json")
def review_json(vol: str, fixes: dict):
    """스크립트용: {doc_id: {칸: 값}} 으로 이어 가기."""
    check(vol)
    if not G.get_state(cfg(vol)).interrupts:
        raise HTTPException(409, "검토 대기 없음")
    submit(vol, Command(resume=fixes))
    return {"vol": vol, "fixed": {k: list(v) for k, v in fixes.items()}}


def hybrid(q, year_from=None, year_to=None, k=10):
    vec = llm.embed("-", [q])[0]
    v = store.vector_search(vec, year_from, year_to, limit=30)
    words = [w for w in re.split(r"\s+", q) if len(w) >= 2 and not re.fullmatch(r"\d{4}년?", w)]
    kw = store.keyword_search(words, year_from, year_to, limit=30)
    score = {}
    for lst in (v, kw):  # Reciprocal Rank Fusion
        for rank, r in enumerate(lst):
            score[r["doc_id"]] = score.get(r["doc_id"], 0) + 1 / (60 + rank + 1)
    top = sorted(score, key=score.get, reverse=True)[:k]
    rows = store.get_documents(top)
    vr = {r["doc_id"]: i + 1 for i, r in enumerate(v)}
    kr = {r["doc_id"]: i + 1 for i, r in enumerate(kw)}
    return [{"doc_id": d, "rrf": round(score[d], 5), "vector_rank": vr.get(d), "keyword_rank": kr.get(d),
             **{c: rows[d][c] for c in ["vol", "docno", "date", "recipient", "title"]}} for d in top if d in rows]


@app.get("/search")
def search(q: str, year_from: int | None = None, year_to: int | None = None, k: int = 10):
    return hybrid(q, year_from, year_to, k)


@app.get("/ask")
def ask(q: str, year_from: int | None = None, year_to: int | None = None):
    hits = hybrid(q, year_from, year_to, 5)
    for h in hits:
        h["body"] = " ".join(store.load_ocr(h["vol"], h["doc_id"])["lines"][:40])
    return {"answer": llm.answer(q, hits), "sources": [{k: h[k] for k in h if k != "body"} for h in hits]}


@app.get("/graph", response_class=PlainTextResponse)
def graph_view():
    return G.get_graph(xray=True).draw_mermaid()
