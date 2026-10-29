"""문서 처리 흐름 (LangGraph). 철(스캔 묶음) 하나가 그래프 실행 한 번(thread_id = 철 id)이다.

철 그래프
  ocr → split ─(문서마다 Send)→ doc(하위 그래프) → validate ─(검토 필요 있음 + human)→ review(interrupt) → index
                                                              └(없음 또는 auto)──────────────────────→ index
문서 하위 그래프
  extract(GPT 텍스트) → locate(손글씨 칸 찾기) ─(못 찾음)→ widen(머리부 범위 넓힘) → locate …
                                              └(찾음 또는 더 못 넓힘)→ read(GPT 비전) → merge

PoC에서 스크립트를 차례로 돌리던 단계(run_ocr → gt_draft → 추출 → recipient_fix → crop_fields → hw_read → hw_flags)를
노드로 옮겼다. OCR 결과는 쪽마다 파일로 남겨, 중간에 Pod가 죽어도 다시 돌리면 이미 읽은 쪽은 건너뛴다.
"""
import collections
import datetime as dt
import json
import os
import re
import time
from typing import Annotated, TypedDict

import cv2
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from app import layout, llm, ocr, store

CONF_SERIAL = float(os.environ.get("CONF_SERIAL", "0.9"))  # PoC 권장값 (ko-trocr 기준). GPT 확신도에는 다시 맞춰야 함
CONF_DATE = float(os.environ.get("CONF_DATE", "0.8"))
CONF_LLM = 0.8  # 추출 프롬프트가 쓰던 review 기준
HEAD_RATIOS = [0.40, 0.48, 0.56]  # 머리부가 쪽 위 몇 % 안에 있다고 볼지. PoC는 0.40 고정이라 창원 양식(41~43%)에서 놓쳤다
FIELDS = ["docno", "date", "recipient", "title"]


def traced(name, fn):
    """노드 시작·끝·오류를 events 표에 남긴다 (대시보드 진행 화면용)."""
    def run(s):
        vol, doc = s.get("vol"), s.get("doc_id")
        store.event(vol, name, "start", doc)
        t0 = time.time()
        try:
            out = fn(s)
        except Exception as e:
            store.event(vol, name, "error", doc, error=f"{type(e).__name__}: {e}"[:300])
            raise
        store.event(vol, name, "end", doc, seconds=round(time.time() - t0, 2), **_brief(name, out))
        return out
    run.__name__ = name
    return run


def _brief(name, out):
    """이벤트에 붙일 짧은 요약."""
    if not isinstance(out, dict):
        return {}
    if name == "split":
        return {"documents": len(out["segments"]), "orphans": len(out["orphans"])}
    if name == "validate":
        return {"flagged": sum(bool(d["flags"]) for d in out["docs"])}
    if name == "locate":
        return {"missing": [k for k, b in out["boxes"].items() if b is None]}
    if name == "read":
        return {k: v["text"] for k, v in out["hw"].items()}
    if name == "index":
        return {"indexed": out["indexed"]}
    return {}


def merge_docs(a, b):
    """문서 목록 합치기: 같은 doc_id는 새 값으로 바꾸고 쪽 순서로 정렬한다."""
    m = {d["doc_id"]: d for d in (a or [])}
    for d in b or []:
        m[d["doc_id"]] = d
    return [m[k] for k in sorted(m)]


class VolState(TypedDict, total=False):
    vol: str
    review_mode: str  # human = 검토 필요 문서가 있으면 사람 검토에서 멈춤, auto = 멈추지 않음(평가용)
    pages: list[str]
    segments: list[dict]
    orphans: list[str]  # 첫 시작 쪽 앞에 있어 어느 문서에도 못 붙인 쪽
    docs: Annotated[list[dict], merge_docs]
    indexed: int


class DocState(TypedDict, total=False):
    vol: str
    doc_id: str
    pages: list[str]
    b: dict  # GPT 텍스트 추출 (PoC의 B 갈래)
    recipient: str
    recipient_fix: str
    ratio_i: int
    boxes: dict
    box_fail: dict
    hw: dict  # 손글씨 칸 판독 {field: {text, conf}}
    docs: Annotated[list[dict], merge_docs]


class DocOut(TypedDict):
    docs: Annotated[list[dict], merge_docs]


# ---------- 철 단위 노드 ----------

def ocr_pages(s: VolState):
    vol = s["vol"]
    pages = sorted(p.stem for p in store.pages_dir(vol).glob("*.jpg"))
    for p in pages:
        out = store.ocr_path(vol, p)
        if out.exists():
            continue
        t0 = time.time()
        d = ocr.ocr_page(store.pages_dir(vol) / f"{p}.jpg")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"page": p, "seconds": round(time.time() - t0, 2), **d}, ensure_ascii=False),
                       encoding="utf-8")
        print(f"ocr {p} {time.time() - t0:.1f}s boxes={len(d['items'])}", flush=True)
        store.event(vol, "ocr_page", "end", None, page=p, seconds=round(time.time() - t0, 2), boxes=len(d["items"]))
    return {"pages": pages}


def split(s: VolState):
    segs, orphans = [], []
    for p in s["pages"]:
        start, sig = layout.is_start(store.load_ocr(s["vol"], p))
        if start:
            segs.append({"doc_id": p, "pages": [p], "signals": sig})
        elif segs:
            segs[-1]["pages"].append(p)
        else:
            orphans.append(p)
    return {"segments": segs, "orphans": orphans}


def fan_out(s: VolState):
    if not s["segments"]:
        return "validate"
    return [Send("doc", {"vol": s["vol"], "doc_id": g["doc_id"], "pages": g["pages"], "ratio_i": 0})
            for g in s["segments"]]


def _valid(d):
    try:
        return dt.date.fromisoformat(d)
    except ValueError:
        return None


def validate(s: VolState):
    """검토 필요 표시 (PoC hw_flags 규칙 + LLM 자체 확신도). 같은 철 앞뒤 3건과 비교하는 '흐름' 규칙이 있어 철 단위로 한다."""
    docs = [dict(d) for d in s.get("docs", [])]
    years = collections.Counter(d["fields"]["date"][:4] for d in docs if d["fields"]["date"])
    ok_years = {y for y, c in years.items() if c >= 3}
    for i, d in enumerate(docs):
        f, info = d["fields"], d["info"]
        nb = docs[max(0, i - 3):i] + docs[i + 1:i + 4]
        fl = {k: [] for k in ["serial", "date", "recipient", "title"]}
        # 날짜
        day = _valid(f["date"]) if f["date"] else None
        if not day:
            fl["date"].append("형식")
        else:
            if f["date"][:4] not in ok_years:
                fl["date"].append("범위")
            nd = [x for x in (_valid(o["fields"]["date"]) for o in nb if o["fields"]["date"]) if x]
            if len(nd) >= 2 and not (min(nd) - dt.timedelta(45) <= day <= max(nd) + dt.timedelta(45)):
                fl["date"].append("흐름")
        if info["conf_date"] and info["conf_date"] < CONF_DATE:
            fl["date"].append("확신도")
        # 일련번호
        if info["blank"]:
            if info["conf_serial"] < CONF_SERIAL:
                fl["serial"].append("빈칸")
        elif not re.fullmatch(r"\d{1,5}", f["serial"] or ""):
            fl["serial"].append("형식")
        else:
            n = int(f["serial"])
            ns = [int(o["fields"]["serial"]) for o in nb if re.fullmatch(r"\d{1,5}", o["fields"]["serial"] or "")
                  and o["info"]["prefix"] == info["prefix"] and o["fields"]["date"][:4] == f["date"][:4]]
            if len(ns) >= 2:
                lo, hi = min(ns), max(ns)
                m = max(50, 0.3 * hi)
                if not (lo - m <= n <= hi + m):
                    fl["serial"].append("흐름")
        if info["conf_serial"] and info["conf_serial"] < CONF_SERIAL:
            fl["serial"].append("확신도")
        # 불일치: GPT 텍스트 추출(B)과 손글씨 칸 판독이 다르면 표시. 비전 판독은 틀려도 확신도가 높아서
        # 확신도만으로는 틀린 일련번호의 56%, 일자의 23%만 잡혔다 (6개 철 129건, eval/flag_whatif.py)
        if info["b_serial"] and info["b_serial"] != f["serial"]:
            fl["serial"].append("불일치")
        if info["b_date"] and info["b_date"] != f["date"]:
            fl["date"].append("불일치")
        # 수신·제목: GPT가 스스로 낮게 본 칸 (PoC에서 'hw + LLM 표시')
        for k in ["recipient", "title"]:
            if d["b"][k]["confidence"] < CONF_LLM:
                fl[k].append("LLM")
        d["flags"] = {k: v for k, v in fl.items() if v}
    return {"docs": docs}


def route_review(s: VolState):
    if s.get("review_mode", "human") == "human" and any(d["flags"] for d in s.get("docs", [])):
        return "review"
    return "index"


def review(s: VolState):
    """사람 검토. interrupt()에서 멈추고 상태가 체크포인트(Postgres)에 남는다.
    검토 화면에서 고친 값이 Command(resume={doc_id: {칸: 값}})로 들어오면 여기서부터 이어 간다.
    (이어 갈 때 이 노드는 처음부터 다시 실행되므로 interrupt 앞에는 부수효과를 두지 않는다.)"""
    flagged = [{"doc_id": d["doc_id"], "pages": d["pages"], "fields": d["fields"], "flags": d["flags"], "hw": d["hw"]}
               for d in s["docs"] if d["flags"]]
    fixes = interrupt({"vol": s["vol"], "flagged": flagged}) or {}
    docs = []
    for d in s["docs"]:
        if d["flags"]:
            d = dict(d, fields={**d["fields"], **fixes.get(d["doc_id"], {})}, reviewed=True)
            if "docno" in fixes.get(d["doc_id"], {}):
                d["fields"]["serial"] = layout.serial_of(d["fields"]["docno"])
        docs.append(d)
    return {"docs": docs}


def doc_text(vol, d):
    f = d["fields"]
    body = " ".join(store.load_ocr(vol, d["pages"][0])["lines"][:60])
    return f"제목: {f['title']}\n수신: {f['recipient']}\n문서번호: {f['docno']}\n시행일자: {f['date']}\n본문: {body}"


def index(s: VolState):
    docs = s.get("docs", [])
    store.save_documents(docs)
    for i in range(0, len(docs), 64):
        chunk = docs[i:i + 64]
        store.upsert_vectors(chunk, llm.embed(s["vol"], [doc_text(s["vol"], d) for d in chunk]))
    return {"indexed": len(docs)}


# ---------- 문서 하위 그래프 ----------

def extract(s: DocState):
    d = store.load_ocr(s["vol"], s["doc_id"])
    b = llm.extract(s["vol"], d["lines"])
    rec, why = layout.fix_recipient(d, b["recipient"]["value"])
    return {"b": b, "recipient": rec, "recipient_fix": why}


def locate(s: DocState):
    items = store.load_ocr(s["vol"], s["doc_id"])["items"]
    ratio = HEAD_RATIOS[s["ratio_i"]]
    boxes, fail = {}, {}
    for k in ["docno", "date"]:
        boxes[k], fail[k] = layout.value_box(items, k, ratio)
    return {"boxes": boxes, "box_fail": fail}


def route_locate(s: DocState):
    missing = [k for k, b in s["boxes"].items() if b is None]
    return "widen" if missing and s["ratio_i"] + 1 < len(HEAD_RATIOS) else "read"


def widen(s: DocState):
    return {"ratio_i": s["ratio_i"] + 1}


def read(s: DocState):
    img = ocr.read_image(store.pages_dir(s["vol"]) / f"{s['doc_id']}.jpg")
    H, W = img.shape[:2]
    hw = {}
    for k, b in s["boxes"].items():
        if not b:
            hw[k] = {"text": "", "conf": 0.0}
            continue
        x0, y0, x1, y1 = max(b[0], 0), max(b[1], 0), min(b[2], W), min(b[3], H)
        ok, buf = cv2.imencode(".png", img[y0:y1, x0:x1])
        p = store.crop_path(s["vol"], s["doc_id"], k)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(buf.tobytes())
        text, conf = llm.read_cell(s["vol"], k, buf.tobytes())
        hw[k] = {"text": text, "conf": conf}
    return {"hw": hw}


def merge(s: DocState):
    """B 값에 손글씨 칸 판독을 끼워 넣는다 (PoC hw_flags.build와 같은 규칙)."""
    b, hw = s["b"], s["hw"]
    b_doc = b["docno"]["value"]
    b_date = b["date"]["value"] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", b["date"]["value"]) else ""
    doc_text_ = hw["docno"]["text"]
    s_doc = layout.serial_of(doc_text_) if re.search(r"[-~]", doc_text_) else ""
    # 문서번호 칸이 '-'로 끝나면 일련번호 빈칸 문서로 본다 (경찰서 통보 등)
    blank = bool(re.search(r"[-~―—][\s.]*$", doc_text_))
    serial = "" if blank else (s_doc or layout.serial_of(b_doc))
    date = layout.date_of(hw["date"]["text"]) or b_date
    # 문서번호 = 부서 약칭 + 5자리 분류번호 + 손글씨 일련번호.
    # 앞부분은 손글씨 칸 판독(칸만 잘라 읽어 우편번호 등이 안 섞임)을 먼저 쓰고, 없으면 B 값.
    # (처음엔 B 값을 먼저 썼다가 '우621-363'처럼 우편번호를 문서번호로 뽑은 B 값이 그대로 남았다)
    head = ""
    for src in (doc_text_, b_doc):
        m = re.search(r"([가-힣]{1,4})\s*(\d{5})", src or "")
        if m:
            head = f"{m.group(1)} {m.group(2)}"
            break
    docno = f"{head}-{serial}" if (head or serial) else ""
    return {"docs": [{
        "doc_id": s["doc_id"], "vol": s["vol"], "pages": s["pages"],
        "fields": {"docno": docno, "serial": serial, "date": date, "recipient": s["recipient"],
                   "title": b["title"]["value"]},
        "b": b, "hw": hw,
        "info": {"prefix": layout.prefix_of(b_doc) or layout.prefix_of(doc_text_), "blank": blank,
                 "conf_serial": hw["docno"]["conf"], "conf_date": hw["date"]["conf"],
                 "b_serial": layout.serial_of(b_doc), "b_date": b_date,
                 "recipient_fix": s["recipient_fix"], "head_ratio": HEAD_RATIOS[s["ratio_i"]],
                 "box_fail": s["box_fail"]},
        "flags": {},
    }]}


def doc_graph():
    g = StateGraph(DocState, input_schema=DocState, output_schema=DocOut)
    for name, fn in [("extract", extract), ("locate", locate), ("widen", widen), ("read", read), ("merge", merge)]:
        g.add_node(name, traced(name, fn))
    g.add_edge(START, "extract")
    g.add_edge("extract", "locate")
    g.add_conditional_edges("locate", route_locate, ["widen", "read"])
    g.add_edge("widen", "locate")
    g.add_edge("read", "merge")
    g.add_edge("merge", END)
    return g.compile()


def build(checkpointer):
    g = StateGraph(VolState)
    g.add_node("ocr", traced("ocr", ocr_pages))
    g.add_node("split", traced("split", split))
    g.add_node("doc", doc_graph())
    g.add_node("validate", traced("validate", validate))
    g.add_node("review", review)  # interrupt가 예외로 멈추므로 감싸지 않고 main에서 기록한다
    g.add_node("index", traced("index", index))
    g.add_edge(START, "ocr")
    g.add_edge("ocr", "split")
    g.add_conditional_edges("split", fan_out, ["doc", "validate"])
    g.add_edge("doc", "validate")
    g.add_conditional_edges("validate", route_review, ["review", "index"])
    g.add_edge("review", "index")
    g.add_edge("index", END)
    return g.compile(checkpointer=checkpointer)
