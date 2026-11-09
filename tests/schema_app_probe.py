"""스키마 하나로 앱을 띄워 철 하나를 끝까지 돌리고 화면·API 결과를 JSON으로 남긴다 (https://github.com/reddol18/docflow/issues/72).
tests/test_schema_customers.py가 FIELD_SCHEMA·EXPORT_TEMPLATE을 준 새 프로세스로 부른다 — 앱은 불러올 때 스키마를 읽고
(칸 목록·규칙·양식 확인) 시작하므로, 다른 스키마는 새 프로세스에서만 '앱이 시작한다'를 그대로 잴 수 있다.

  python -I tests/schema_app_probe.py <작업 폴더> <결과 JSON> [수정 단계 JSON]

LLM·OCR·DB는 부르지 않는다 (가짜로 바꿔 끼움). 값은 모두 지어낸 것.
흐름: 철 실행(human) → 검토 대기 → GET /schema · /review/{vol} → POST /review/{vol}/json(고친 칸 하나) → 색인 끝
      → GET /volumes/{vol}/documents · /search · /export/check · /export.xlsx
"""
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from openpyxl import load_workbook  # noqa: E402

from app import auth, fields, graph, layout, llm, main, ocr, store  # noqa: E402 — 여기서 앱이 스키마·양식을 읽고 시작한다

VOL = "1234567-1999-0001"  # 기본 양식(aihub88)의 철 이름 규칙에 맞춘 지어낸 이름
PAGES = ["p-0001", "p-0002", "p-0003", "p-0004"]
STARTS = {"p-0001", "p-0003", "p-0004"}
FIX = "사람이 고친 가상 값"


def fake_value(f, i):
    """prompt 칸의 지어낸 LLM 값 — rule·방식·ISAD(G) 칸으로 고른다 (칸 이름은 스키마마다 다르다)"""
    r = f.get("rule")
    if r == "docno":
        return f"가상 12345-{100 + i}"
    if r == "recipient":
        return "가상시장"
    if f["method"] == "vocab":
        return f["vocab"][0]
    if r == "date" or f.get("isad", f["name"]) == "dates":
        return f"1999-03-{10 + i:02d}"
    return f"가상 {f['label']} {i}"


def setup(tmp):
    store.DATA = tmp
    (tmp / "pages" / VOL).mkdir(parents=True)
    for p in PAGES:
        (tmp / "pages" / VOL / f"{p}.jpg").write_bytes(b"x")
    ocr.ocr_page = lambda path: {"lines": [path.stem], "items": []}
    ocr.read_image = lambda path: np.zeros((100, 100, 3), np.uint8)
    layout.is_start = lambda d: (d["page"] in STARTS, {})
    layout.value_box = lambda items, k, ratio: ((0, 0, 10, 10), None)
    layout.fix_recipient = lambda d, v: (v, "")
    # LLM 확신도 0.5 → LLM 칸마다 검토 표시가 붙어 사람 검토에서 멈춘다
    llm.extract = lambda vol, lines, s=None: {
        f["name"]: {"value": fake_value(f, PAGES.index(lines[0])), "confidence": 0.5}
        for f in fields.prompt_fields(s or fields.SCHEMA)}
    llm.read_cell = lambda vol, k, png: ("가상 12345-100" if k == "docno" else "99. 3. 10.", 0.99)
    llm.embed = lambda vol, texts: [[0.0]] * len(texts)
    for name in ("event", "save_documents", "upsert_vectors", "delete_doc_vectors", "add_review_submitter", "add_edits",
                 "audit", "add_export"):
        setattr(store, name, lambda *a, **k: None)
    # 검토 단계(https://github.com/reddol18/docflow/issues/86): 단계 행·검사 목록 없음 → 출력 전 검증이 미확정을 센다
    store.stage_rows = lambda vol: {}
    store.inspection_plan = lambda vol: None
    store.inspection_rows = lambda vol: []
    store.spent_today = lambda: 0.0
    store.usage_summary = lambda vol=None: []
    auth.MODES = frozenset({"local"})
    auth.session_user = lambda t: auth.User(1, "probe", "가상 관리자", "admin")


def edit_steps(c, steps):
    """색인이 끝난 철에 문서 수정(PATCH)을 차례로 하고, 매번 문서별 표시·출력 전 검증을 남긴다 (칸 검증 규칙 시험,
    https://github.com/reddol18/docflow/issues/87). steps = [{"doc": 문서 순번, "fields": {칸: 값}}]"""
    import contextlib
    store.doc_lock = store.vol_lock = lambda *a: contextlib.nullcontext()
    store.inspection_row = lambda vol, doc_id: None
    store.entered = lambda vol, uid: (False, set())
    store.stage_actors = lambda vol, uid: {}
    store.inspected_docs_by = lambda vol, uid: set()
    saved = []
    store.save_documents = lambda docs: saved.append(sorted(d["doc_id"] for d in docs))
    ids = [d["doc_id"] for d in c.get(f"/volumes/{VOL}/documents").json()]
    out = []
    for s in steps:
        saved.clear()
        res = c.patch(f"/documents/{VOL}/{ids[s['doc']]}", json=s["fields"])
        docs = c.get(f"/volumes/{VOL}/documents").json()
        out.append({"status": res.status_code, "saved": saved[0] if saved else [],
                    "flags": [d["flags"] for d in docs], "fields": [d["fields"] for d in docs],
                    "export_check": c.get("/export/check", params={"vol": VOL}).json()})
    return out


def run(tmp, out, steps=None):
    setup(tmp)
    main.G = graph.build(MemorySaver())
    main.G.invoke({"vol": VOL, "review_mode": "human"}, main.cfg(VOL))
    c = TestClient(main.app)  # with 없이 → lifespan(DB)을 돌리지 않는다
    c.headers.update({"authorization": "Bearer probe", "x-docflow-csrf": "1"})
    r = {"schema": c.get("/schema").json()}
    snap = main.G.get_state(main.cfg(VOL))
    r["waiting"] = bool(snap.interrupts)
    r["flagged"] = snap.interrupts[0].value["flagged"] if snap.interrupts else []
    r["review_html"] = c.get(f"/review/{VOL}").text
    r["crops"] = sorted(p.name for p in (tmp / "crops" / VOL).glob("*.png")) if (tmp / "crops" / VOL).exists() else []
    if r["crops"]:
        r["crop_status"] = c.get(f"/crops/{VOL}/{r['crops'][0]}").status_code
    editable = [f["name"] for f in r["schema"]["fields"] if f["editable"]]
    r["fixed"] = {"doc_id": r["flagged"][0]["doc_id"], "field": editable[0]} if r["flagged"] else None
    if r["fixed"]:
        res = c.post(f"/review/{VOL}/json", json={r["fixed"]["doc_id"]: {r["fixed"]["field"]: FIX}})
        r["review_submit"] = res.status_code
        main.runner.submit(lambda: None).result()  # 일꾼이 하나라 앞선 이어 가기(색인)가 끝날 때까지 기다린다
    r["status"] = c.get(f"/volumes/{VOL}").json()
    r["docs"] = c.get(f"/volumes/{VOL}/documents").json()
    saved = {d["doc_id"]: {"vol": VOL, "fields": d["fields"]} for d in r["docs"]}
    store.vector_search = lambda vec, yf=None, yt=None, limit=20: [{"doc_id": k} for k in saved]
    store.keyword_search = lambda words, yf=None, yt=None, limit=20: [{"doc_id": k} for k in saved]
    store.get_documents = lambda ids: {k: saved[k] for k in ids if k in saved}
    r["hits"] = c.get("/search", params={"q": "가상"}).json()
    r["export_check"] = c.get("/export/check", params={"vol": VOL}).json()
    x = c.get("/export.xlsx", params={"vol": VOL})
    r["export_status"] = x.status_code
    if x.status_code == 200:
        wb = load_workbook(io.BytesIO(x.content))
        r["xlsx"] = {ws.title: [list(row) for row in ws.iter_rows(values_only=True)] for ws in wb.worksheets}
    if steps:
        r["steps"] = edit_steps(c, steps)
    out.write_text(json.dumps(r, ensure_ascii=False, default=str), encoding="utf-8")


if __name__ == "__main__":
    run(Path(sys.argv[1]), Path(sys.argv[2]),
        json.loads(Path(sys.argv[3]).read_text(encoding="utf-8")) if len(sys.argv) > 3 else None)
