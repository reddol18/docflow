"""문서 처리 흐름 (LangGraph). 철(스캔 묶음) 하나가 그래프 실행 한 번(thread_id = 철 id)이다.

철 그래프
  ocr → split ─(문서마다 Send)→ doc(하위 그래프) → validate ─(검토 필요 있음 + human)→ review(interrupt) → index
                                                              └(없음 또는 auto)──────────────────────→ index
문서 하위 그래프
  extract(GPT 텍스트) → locate(손글씨 칸 찾기) ─(못 찾음)→ widen(머리부 범위 넓힘) → locate …
                                              └(찾음 또는 더 못 넓힘)→ read(GPT 비전) → merge

PoC에서 스크립트를 차례로 돌리던 단계(run_ocr → gt_draft → 추출 → recipient_fix → crop_fields → hw_read → hw_flags)를
노드로 옮겼다. OCR 결과는 쪽마다 파일로 남겨, 중간에 Pod가 죽어도 다시 돌리면 이미 읽은 쪽은 건너뛴다.

LLM_PROVIDER=off (app/llm.py)면 추출·판독·임베딩을 부르지 않고 건너뛴다: 추출 칸은 빈 값·확신도 0, 손글씨 칸은 못 읽음,
벡터는 넣지 않고 그 문서의 예전 벡터는 지운다. 건너뛴 단계는 문서 info.llm_skipped와
events(extract·read의 skipped, index의 embedded)로 드러난다. 건너뛴 문서는 스키마와 상관없이(app/flags.py 규칙과 별도로)
LLM이 채울 칸에 'LLM 꺼짐' 검토 표시가 붙고, review_mode가 auto여도 사람 검토(review)에서 멈춘다.
"""
import json
import operator
import os
import re
import time
from typing import Annotated, TypedDict

import cv2
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from app import errors, fields, flags, layout, llm, ocr, store

# 검토 표시 규칙은 app/flags.py. 환경변수 FLAG_CONFIG(JSON)로 기준값·추가 규칙을 덮어쓸 수 있다.
# 예: FLAG_CONFIG='{"conf_date": 0.9, "rules": ["doc_head", "rec_part"]}'
# 예전 환경변수 CONF_SERIAL·CONF_DATE도 계속 받는다 (FLAG_CONFIG가 우선)
FLAG_CONFIG = {
    **flags.DEFAULT,
    **{k: float(os.environ[e]) for k, e in [("conf_serial", "CONF_SERIAL"), ("conf_date", "CONF_DATE")] if os.environ.get(e)},
    **json.loads(os.environ.get("FLAG_CONFIG") or "{}"),
}
HEAD_RATIOS = [0.40, 0.48, 0.56]  # 머리부가 쪽 위 몇 % 안에 있다고 볼지. PoC는 0.40 고정이라 창원 양식(41~43%)에서 놓쳤다

# 색인 칸은 스키마(app/fields.py, FIELD_SCHEMA)에서. 스키마의 rule 칸은 아래 규칙이 값을 만든다 (merge).
# docno·serial·date는 김해 양식의 손글씨 칸 판독 + LLM 값, recipient는 LLM 값을 배치로 고친 값, pages는 쪽수.
# 칸 이름이 아니라 rule 이름으로 판단한다 — 기관 매핑이 칸 이름을 바꿔도 손글씨 칸·검토 표시가 그대로 돈다
# (https://github.com/reddol18/docflow/issues/72). 검토 표시 규칙(app/flags.py)도 rule 이름으로 칸을 찾는다.
SCHEMA = fields.SCHEMA
RULES = ("docno", "serial", "date", "recipient", "pages")
# 손글씨 칸을 찾아 읽어야 하는 규칙 → 그 규칙이 쓰는 잘라낸 칸 (layout.value_box 종류 = 잘라낸 파일 {doc_id}_{칸}.png
# = 문서 hw 키). 일련번호는 문서번호 칸에서 나온다
HW_CELLS = {"docno": "docno", "serial": "docno", "date": "date"}
HW_RULES = set(HW_CELLS)
CELLS = ("docno", "date")  # 잘라낸 칸 전부 (찾는 순서)
# 사람이 원천 칸을 고치면 다시 계산하는 규칙 (스키마 derive_from)
DERIVE = {"serial": layout.serial_of}  # 원천 칸(derive_from) 값 → 이 칸 값
fields.check_rules(SCHEMA, RULES)


def check_derive(schema):
    bad = sorted({f["rule"] for f in schema["fields"] if "derive_from" in f} - set(DERIVE))
    if bad:
        raise fields.SchemaError(f"다시 계산할 수 없는 rule {bad} (가능: {sorted(DERIVE)})")


check_derive(SCHEMA)


def rederive(values, changed, schema=None):
    """사람이 고친 칸(changed)에서 나오는 칸을 다시 계산한다 (김해: 문서번호 → 일련번호). values를 바꿔서 돌려준다"""
    for f in (schema or SCHEMA)["fields"]:
        if f.get("derive_from") in changed:
            values[f["name"]] = fields.normalize(f, DERIVE[f["rule"]](values.get(f["derive_from"]) or ""))
    return values


def hw_cell(field):
    """칸이 보여 줄 잘라낸 손글씨 칸 (없으면 None) — 화면(/schema hw)·검토 화면·잘라낸 칸 경로가 이것으로 판단"""
    return HW_CELLS.get(field.get("rule"))


def hw_cells(schema):
    """이 스키마로 찾아 읽을 잘라낸 칸 (CELLS 순서)"""
    need = {hw_cell(f) for f in schema["fields"]}
    return [c for c in CELLS if c in need]


def uses_hw(schema):
    return bool(hw_cells(schema))


def llm_kinds(schema):
    """이 스키마로 흐름·검색이 쓰는 LLM 기능 (local 제공자의 모델 이름 확인용)"""
    return ({"embed", "chat"} | ({"extract"} if fields.extract_prompt(schema) else set())
            | ({"cell"} if uses_hw(schema) else set()))


llm.require_models(llm_kinds(SCHEMA))  # local인데 쓰는 기능의 모델 이름이 없으면 시작할 때 멈춘다
OFF_FLAG = "LLM 꺼짐"


def off_flags(d, schema=None):
    """LLM을 건너뛴 문서의 검토 표시 {flag_key: ['LLM 꺼짐']} — 추출 칸(스키마 prompt 칸)·손글씨 규칙 칸(판독)마다"""
    schema = schema or SCHEMA
    skipped = set((d.get("info") or {}).get("llm_skipped") or ())
    names = {f["name"] for f in fields.prompt_fields(schema)} if "extract" in skipped else set()
    if "read" in skipped:
        names |= {f["name"] for f in schema["fields"] if f.get("rule") in HW_RULES}
    keys = {f.get("flag_key", f["name"]) for f in schema["fields"] if f["name"] in names}
    return {k: [OFF_FLAG] for k in sorted(keys)}


def _llm_of(b, rule, schema=None):
    """규칙이 재료로 쓰는 LLM 추출값: 그 rule을 가진 칸의 이름으로 찾는다 (매핑에서 칸 이름을 바꿔도 되게)"""
    name = next((f["name"] for f in (schema or SCHEMA)["fields"] if f.get("rule") == rule), rule)
    return (b.get(name) or {}).get("value") or ""


def traced(name, fn):
    """노드 시작·끝·오류를 events 표에 남긴다 (대시보드 진행 화면용)."""
    def run(s):
        vol, doc = s.get("vol"), s.get("doc_id")
        store.event(vol, name, "start", doc)
        t0 = time.time()
        try:
            out = fn(s)
        except Exception as e:
            store.event(vol, name, "error", doc, **errors.public(e))  # 화면엔 분류 코드만, 상세는 main.submit이 서버 로그로
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
    if name in ("extract", "read") and out.get("skipped"):
        return {"skipped": "llm_off"}
    if name == "read":
        return {k: v["text"] for k, v in out["hw"].items()}
    if name == "index":
        return {"indexed": out["indexed"], "embedded": out.get("embedded", out["indexed"])}
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
    embedded: int  # 벡터를 넣은 문서 수 (LLM_PROVIDER=off면 0)


class DocState(TypedDict, total=False):
    vol: str
    doc_id: str
    pages: list[str]
    skipped: Annotated[list[str], operator.add]  # LLM_PROVIDER=off로 건너뛴 단계 (extract·read)
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


def validate(s: VolState):
    """검토 필요 표시. 앞뒤 문서·같은 분류번호 문서와 비교하는 규칙이 있어 철 단위로 한다 (규칙은 app/flags.py)."""
    docs = [dict(d) for d in s.get("docs", [])]
    # 표시 규칙은 칸을 rule 이름으로 찾는다 — 스키마에 없는 rule의 규칙만 건너뛰고, LLM 추출 칸은 확신도로 본다.
    # 표시 원천을 더할 때(칸 검증 규칙 등)는 여기서 같은 {표시 키: [사유]}로 합친다
    for d, fl in zip(docs, flags.for_schema(docs, SCHEMA, FLAG_CONFIG, HW_RULES)):
        for k, v in off_flags(d).items():  # LLM을 건너뛴 문서는 스키마와 상관없이 표시
            fl[k] = [*fl.get(k, []), *v]
        d["flags"] = fl
    return {"docs": docs}


def skipped_llm(d):
    return bool((d.get("info") or {}).get("llm_skipped"))


def route_review(s: VolState):
    docs = s.get("docs", [])
    if any(skipped_llm(d) for d in docs):  # LLM을 건너뛴 문서는 auto여도 사람이 본다
        return "review"
    if s.get("review_mode", "human") == "human" and any(d["flags"] for d in docs):
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
            fix = fixes.get(d["doc_id"], {})
            d = dict(d, fields=rederive({**d["fields"], **fix}, fix), reviewed=True)
        docs.append(d)
    return {"docs": docs}


def doc_text(vol, d):
    """임베딩할 글: 스키마 search.embed 칸('표시명: 값' 줄) + 첫 쪽 본문"""
    body = " ".join(store.load_ocr(vol, d["pages"][0])["lines"][:60])
    return f"{fields.embed_text(SCHEMA, d['fields'])}\n본문: {body}"


def index(s: VolState):
    docs = s.get("docs", [])
    store.save_documents(docs)
    if not llm.enabled():  # LLM_PROVIDER=off: 문서는 저장하고 벡터는 넣지 않는다. 다시 돌린 철의 예전 벡터도 지운다
        store.delete_doc_vectors([d["doc_id"] for d in docs])
        return {"indexed": len(docs), "embedded": 0}
    for i in range(0, len(docs), 64):
        chunk = docs[i:i + 64]
        store.upsert_vectors(chunk, llm.embed(s["vol"], [doc_text(s["vol"], d) for d in chunk]))
    return {"indexed": len(docs), "embedded": len(docs)}


# ---------- 문서 하위 그래프 ----------

def extract(s: DocState):
    d = store.load_ocr(s["vol"], s["doc_id"])
    try:
        b, skipped = llm.extract(s["vol"], d["lines"], SCHEMA), []
    except llm.LLMOff:  # 추출 칸은 빈 값·확신도 0 (가짜 값 없음) → 검토 표시로 사람에게
        b, skipped = fields.parse_extract(SCHEMA, "{}"), ["extract"]  # LLMOff는 prompt 칸이 있을 때만 난다
    rec, why = layout.fix_recipient(d, _llm_of(b, "recipient"))
    return {"b": b, "recipient": rec, "recipient_fix": why, "skipped": skipped}


def locate(s: DocState):
    """손글씨 칸 찾기 — 스키마의 손글씨 규칙 칸(docno·serial·date rule)이 쓰는 칸만. 없으면 찾지 않는다 (비전 호출도 없음)"""
    cells = hw_cells(SCHEMA)
    if not cells:
        return {"boxes": {}, "box_fail": {}}
    items = store.load_ocr(s["vol"], s["doc_id"])["items"]
    ratio = HEAD_RATIOS[s["ratio_i"]]
    boxes, fail = {}, {}
    for k in cells:
        boxes[k], fail[k] = layout.value_box(items, k, ratio)
    return {"boxes": boxes, "box_fail": fail}


def route_locate(s: DocState):
    missing = [k for k, b in s["boxes"].items() if b is None]
    return "widen" if missing and s["ratio_i"] + 1 < len(HEAD_RATIOS) else "read"


def widen(s: DocState):
    return {"ratio_i": s["ratio_i"] + 1}


def read(s: DocState):
    if not s["boxes"]:  # 손글씨 칸을 찾지 않는 스키마 — 쪽 이미지를 읽지 않는다
        return {"hw": {}}
    img = ocr.read_image(store.pages_dir(s["vol"]) / f"{s['doc_id']}.jpg")
    H, W = img.shape[:2]
    hw, skipped = {}, []
    for k, b in s["boxes"].items():
        if not b:
            hw[k] = {"text": "", "conf": 0.0}
            continue
        x0, y0, x1, y1 = max(b[0], 0), max(b[1], 0), min(b[2], W), min(b[3], H)
        ok, buf = cv2.imencode(".png", img[y0:y1, x0:x1])
        p = store.crop_path(s["vol"], s["doc_id"], k)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(buf.tobytes())  # 잘라낸 칸은 남긴다 — 검토 화면에서 사람이 읽는다
        try:
            text, conf = llm.read_cell(s["vol"], k, buf.tobytes())
        except llm.LLMOff:  # 못 읽은 칸과 같게 (빈 글자·확신도 0)
            text, conf, skipped = "", 0.0, ["read"]
        hw[k] = {"text": text, "conf": conf}
    return {"hw": hw, "skipped": skipped}


def merge(s: DocState):
    """B 값에 손글씨 칸 판독을 끼워 넣어 규칙 값을 만들고(PoC hw_flags.build와 같은 규칙), 스키마대로 칸을 조립한다."""
    b = s["b"]
    hw = {k: s["hw"].get(k) or {"text": "", "conf": 0.0} for k in CELLS}
    b_doc = _llm_of(b, "docno")
    b_date = _llm_of(b, "date") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", _llm_of(b, "date")) else ""
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
    rule_values = {"docno": docno, "serial": serial, "date": date, "recipient": s["recipient"],
                   "pages": str(len(s["pages"]))}
    return {"docs": [{
        "doc_id": s["doc_id"], "vol": s["vol"], "pages": s["pages"],
        "fields": fields.assemble(SCHEMA, b, rule_values),
        "b": b, "hw": s["hw"],
        "info": {"prefix": layout.prefix_of(b_doc) or layout.prefix_of(doc_text_), "blank": blank,
                 "conf_serial": hw["docno"]["conf"], "conf_date": hw["date"]["conf"],
                 "b_serial": layout.serial_of(b_doc), "b_date": b_date,
                 "recipient_fix": s["recipient_fix"], "head_ratio": HEAD_RATIOS[s["ratio_i"]],
                 "box_fail": s["box_fail"], "llm_skipped": s.get("skipped") or []},
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
