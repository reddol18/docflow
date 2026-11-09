"""검수 오류목록 내보내기 — 지어낸 철로 열 값이 수정 이력·단계 기록과 맞는지 (https://github.com/reddol18/docflow/issues/88).
실제 Postgres(PGlite)에 돌린다. TEST_PG_URL이 없으면 건너뛴다. 표를 지우므로 운영 DB에 대고 돌리지 말 것.
문서·값·사유·사람은 모두 지어낸 것."""
import datetime as dt
import io
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app import auth, store, workflow

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_PG_URL"), reason="TEST_PG_URL 없음")
USERS = {"s": (1, "admin"), "w": (2, "reviewer"), "q": (3, "inspector"), "v": (8, "viewer"), "i": (9, "inspector")}
TABLES = ("inspection", "inspection_plan", "review_submitters", "audit", "events", "edits", "sessions", "review_time",
          "doc_stage", "stage_log", "stage_gen", "exports", "users")


def h(name):
    return {"authorization": f"Bearer {name}", "x-docflow-csrf": "1"}


def doc(did, flagged=False):
    return {"doc_id": did, "vol": "v1", "pages": [did], "flags": {"title": ["확신도"]} if flagged else {},
            "fields": {"docno": "가상-1", "serial": "1", "date": "1999-01-01", "recipient": "가상시청", "title": "가상 제목"},
            "hw": {}, "reviewed": True}


@pytest.fixture
def main(pg_store, monkeypatch, tmp_path):
    from app import main
    with pg_store.pool.connection() as c:
        for t in TABLES:
            c.execute(f"DELETE FROM {t}")  # noqa: S608 — 고정된 표 이름
        for name, (uid, role) in USERS.items():
            c.execute("INSERT INTO users (id, username, name, role) VALUES (%s, %s, %s, %s)",
                      (uid, name, f"가상 사용자 {name}", role))
    monkeypatch.setattr(auth, "MODES", frozenset({"local"}))
    monkeypatch.setattr(auth, "session_user",
                        lambda t: auth.User(USERS[t][0], t, t, USERS[t][1]) if t in USERS else None)
    monkeypatch.setattr(store, "DATA", tmp_path)
    state = {"docs": [doc("d01", True), doc("d02"), doc("d03")], "indexed": 3}
    snap = SimpleNamespace(next=(), interrupts=[], values=state)

    def update_state(cfg, v, as_node=None):
        new = {d["doc_id"]: d for d in v["docs"]}
        state["docs"] = [new.get(d["doc_id"], d) for d in state["docs"]]

    monkeypatch.setattr(main, "G", SimpleNamespace(get_state=lambda cfg: snap, update_state=update_state,
                                                    invoke=lambda arg, cfg: None))
    monkeypatch.setattr(main.llm, "embed", lambda vol, texts, **k: [[0.0]] * len(texts))
    monkeypatch.setattr(main.store, "upsert_vectors", lambda docs, vecs: None)
    monkeypatch.setattr(main.flow, "doc_text", lambda vol, d: "x")
    wf = workflow.load(workflow.DIR / "three_stage.yaml")
    monkeypatch.setattr(main, "WORKFLOW", wf)
    monkeypatch.setattr(main, "ENTRY", workflow.entry(wf))
    monkeypatch.setattr(main, "SAMPLE", workflow.sample_stage(wf))
    return main


def approve(c, who, did, stage, **fields):
    r = c.post(f"/workflow/v1/{did}/approve", headers=h(who), json={"stage": stage, "fields": fields})
    assert r.status_code == 200, r.text


def sheet(r):
    ws = load_workbook(io.BytesIO(r.content)).worksheets[0]
    rows = [list(x) for x in ws.iter_rows(values_only=True)]
    return [dict(zip(rows[0], x)) for x in rows[1:]]


def kst_date(ts):
    return ts.astimezone(store.KST).date().isoformat()


def test_error_list_matches_history(main, monkeypatch):
    c = TestClient(main.app)
    # 예전 세대: 검수 단계 수정과 반려 기록이 있던 d03 — 철을 다시 돌린 뒤에는 오류목록에 나오면 안 된다
    store.add_edits("v1", [("d03", "docno", "가상-0", "가상-1")], 3, "stage:qc")
    store.add_stage_log("v1", "d03", "qc", 1, "reject", "entry", 3, reason="가상 예전 사유")
    assert store.reset_stages("v1") == 0

    approve(c, "w", "d01", "entry", title="가상 고친 제목")  # 확신도 표시 칸을 고쳐 넘김
    approve(c, "w", "d02", "entry")  # 오류 없음
    approve(c, "w", "d03", "entry")
    r = c.post("/workflow/v1/d01/reject", headers=h("q"), json={"stage": "qc", "reason": "=가상 사유: 수신 오기"})
    assert r.status_code == 200
    assert c.patch("/documents/v1/d01", headers=h("w"), json={"recipient": "가상구청"}).status_code == 200
    approve(c, "w", "d01", "entry")
    approve(c, "q", "d01", "qc", date="1999-01-02")  # 품질관리자가 일자를 고치며 승인 (2차)
    approve(c, "s", "d01", "supervisor")
    r = c.post("/workflow/v1/d03/reject", headers=h("i"), json={"stage": "qc", "reason": "가상 사유: 쪽 누락"})
    assert r.status_code == 200

    assert c.get("/export/errors.xlsx?vol=v1", headers=h("q")).status_code == 403  # 관리자만
    assert c.get("/export/errors.xlsx?vol=../x", headers=h("s")).status_code == 400
    r = c.get("/export/errors.xlsx?vol=v1", headers=h("s"))
    assert r.status_code == 200 and r.headers["content-disposition"].startswith("attachment")
    rows = sheet(r)
    assert [(x["문서"], x["칸"]) for x in rows] == [("d01", "시행일자"), ("d01", "수신"), ("d01", "제목"), ("d03", "(문서)")]

    # 열 값 = 수정 이력(edits)·단계 기록(stage_log, 지금 세대)
    hist = store.edit_history("v1", "d01")
    stages = store.stage_history("v1", "d01")
    by = {x["칸"]: x for x in rows}
    for label, field in (("시행일자", "date"), ("수신", "recipient"), ("제목", "title")):
        es = [e for e in hist if e["field"] == field]
        assert (by[label]["수정 전 값"], by[label]["수정 후 값"]) == (es[0]["old"], es[-1]["new"])
        assert by[label]["조치일"] == kst_date(es[-1]["ts"]) and by[label]["조치"] == "수정"
    reject = next(s for s in stages if s["action"] == "reject")
    assert by["수신"]["오류 내용"] == f"반려 · 품질관리자: {reject['reason']}" and by["수신"]["오류 구분"] == "반려"
    assert by["제목"]["오류 내용"] == "확신도" and by["제목"]["오류 구분"] == "표시"
    assert by["시행일자"]["오류 내용"] == "검수 수정 · 품질관리자"
    final = [s for s in stages if s["stage"] in ("qc", "supervisor")][-2:]
    assert [(s["stage"], s["action"], s["round"]) for s in final] == [("qc", "approve", 2), ("supervisor", "approve", 2)]
    assert by["시행일자"]["재검수 판정"] == "품질관리자: 합격 (검수 수정) (2차) · 감독관: 합격"
    assert by["수신"]["재검수 판정"] == "품질관리자: 합격 (2차) · 감독관: 합격"
    # 제목은 1차에 고쳤고 그 뒤 반려(1차)·합격(2차) — 마지막 판정만
    assert by["제목"]["재검수 판정"] == "품질관리자: 합격 (2차) · 감독관: 합격"
    d3 = rows[-1]
    assert (d3["오류 내용"], d3["조치"], d3["담당자"] or "", d3["재검수 판정"]) == (
        "반려 · 품질관리자: 가상 사유: 쪽 누락", "미조치", "", "품질관리자: 불합격(반려)")

    # 담당자: 기본은 가린 ID — 사용자 id·로그인 이름·표시 이름이 파일에 없다. 같은 사람은 같은 값
    actors = {x["칸"]: x["담당자"] for x in rows[:3]}
    assert actors["수신"] == actors["제목"] != actors["시행일자"]
    assert all(a.startswith("담당자-") for a in actors.values())
    flat = " ".join(str(v) for x in rows for v in x.values())
    assert "가상 사용자" not in flat
    again = {x["칸"]: x["담당자"] for x in sheet(c.get("/export/errors.xlsx?vol=v1", headers=h("s")))[:3]}
    assert again["수신"] != actors["수신"]  # 내보낼 때마다 키가 바뀐다 — 파일끼리 잇지 못한다

    # 설정으로 표시 이름
    monkeypatch.setattr(main, "ERROR_TEMPLATE", {**main.ERROR_TEMPLATE, "actor": "name"})
    named = {x["칸"]: x["담당자"] for x in sheet(c.get("/export/errors.xlsx?vol=v1", headers=h("s")))}
    assert (named["수신"], named["시행일자"]) == ("가상 사용자 w", "가상 사용자 q")

    # 행 상한
    monkeypatch.setattr(main, "ERROR_ROWS_MAX", 2)
    assert c.get("/export/errors.xlsx?vol=v1", headers=h("s")).status_code == 400

    # 감사 로그: 요청(결과·철 수·행 수·담당자 방식) + 철마다 (값·사유는 없음)
    with store.pool.connection() as cn:
        logs = cn.execute("SELECT action, vol, result, info, username FROM audit WHERE action LIKE 'error_list%%' "
                          "ORDER BY id").fetchall()
    assert [(x["action"], x["result"]) for x in logs] == [
        ("error_list_request", "rejected_403"),  # 관리자 아닌 사람의 반출 시도 (https://github.com/reddol18/docflow/issues/63)
        ("error_list_request", "rejected_400"), ("error_list_request", "ok"), ("error_list", "ok"),
        ("error_list_request", "ok"), ("error_list", "ok"), ("error_list_request", "ok"), ("error_list", "ok"),
        ("error_list_request", "rejected_400")]
    assert logs[0]["username"] == "q" and logs[0]["vol"] is None and logs[0]["info"]["reason"] == "role"
    assert logs[2]["info"] == {"vols": 1, "actor": "masked", "volumes": 1, "rows": 4} and logs[2]["username"] == "s"
    assert logs[3]["vol"] == "v1" and logs[3]["info"] == {"rows": 4}
    assert logs[6]["info"]["actor"] == "name"
    assert "가상" not in str([x["info"] for x in logs])
    assert not store.last_export("v1")  # 납품 출력 기록(일괄 삭제 근거)은 남기지 않는다


def test_all_volumes_capped_and_one_at_a_time(main, monkeypatch, tmp_path):
    c = TestClient(main.app)
    (tmp_path / "pages" / "v1").mkdir(parents=True)
    monkeypatch.setattr(main, "EXPORT_VOLS_MAX", 0)
    r = c.get("/export/errors.xlsx", headers=h("s"))  # 철을 안 고르면 끝난 철 전부 — 그때도 철 수 상한
    assert r.status_code == 400 and "vol" in r.json()["detail"]
    monkeypatch.setattr(main, "EXPORT_VOLS_MAX", 200)
    assert c.get("/export/errors.xlsx", headers=h("s")).status_code == 200
    assert main._error_list_busy.acquire(blocking=False)
    try:
        assert c.get("/export/errors.xlsx?vol=v1", headers=h("s")).status_code == 409  # 만드는 중이면 바로 409
    finally:
        main._error_list_busy.release()
    with store.pool.connection() as cn:
        got = [x["result"] for x in cn.execute("SELECT result FROM audit WHERE action = 'error_list_request' ORDER BY id")]
    assert got == ["rejected_400", "ok", "rejected_409"]


def test_unfinished_volume_refused(main, monkeypatch):
    monkeypatch.setattr(main, "G", SimpleNamespace(get_state=lambda cfg: SimpleNamespace(
        next=("review",), interrupts=[1], values={"docs": []})))
    assert TestClient(main.app).get("/export/errors.xlsx?vol=v1", headers=h("s")).status_code == 409


def test_since_filter_uses_generation_start(main):
    """세대 시작 시각 뒤 수정만 — 예전 세대 수정은 gen_edits에 없다"""
    store.add_edits("v1", [("d01", "title", "가", "나")], 2, "edit")
    store.reset_stages("v1")
    store.add_edits("v1", [("d01", "title", "나", "다")], 2, "edit")
    got = store.gen_edits("v1")
    assert [(e["old"], e["new"], e["name"]) for e in got] == [("나", "다", "가상 사용자 w")]
    assert isinstance(got[0]["ts"], dt.datetime)
