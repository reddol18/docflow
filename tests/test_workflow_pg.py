"""검토 단계 설정 — N단계 승인·반려·재검수, 작업자 ≠ 검수자, 미확정 세기, 감사 로그 (https://github.com/reddol18/docflow/issues/86).
실제 Postgres(PGlite)에 돌린다. TEST_PG_URL이 없으면 건너뛴다. 표를 지우므로 운영 DB에 대고 돌리지 말 것.
문서·값·사유는 모두 지어낸 것."""
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import auth, store, workflow

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_PG_URL"), reason="TEST_PG_URL 없음")
# w·w2 작업자(reviewer), q·q2 품질관리자(inspector), s·s2 감독관(admin), v 보기
USERS = {"s": (1, "admin"), "w": (2, "reviewer"), "q": (3, "inspector"), "q2": (4, "inspector"), "w2": (6, "reviewer"),
         "s2": (7, "admin"), "v": (8, "viewer")}
TABLES = ("inspection", "inspection_plan", "review_submitters", "audit", "events", "edits", "sessions", "review_time",
          "doc_stage", "stage_log", "exports", "users")


def h(name):
    return {"authorization": f"Bearer {name}", "x-docflow-csrf": "1"}


def doc(did, flagged=False):
    return {"doc_id": did, "vol": "v1", "pages": [did], "flags": {"title": ["확신도"]} if flagged else {},
            "fields": {"docno": "가상-1", "serial": "1", "date": "1999-01-01", "recipient": "가상시청", "title": "가상 제목"},
            "hw": {}, "reviewed": True}


def use_flow(main, monkeypatch, wf):
    monkeypatch.setattr(main, "WORKFLOW", wf)
    monkeypatch.setattr(main, "ENTRY", workflow.entry(wf))
    monkeypatch.setattr(main, "SAMPLE", workflow.sample_stage(wf))


@pytest.fixture
def main(pg_store, monkeypatch, tmp_path):
    from app import main
    with pg_store.pool.connection() as c:
        for t in TABLES:
            c.execute(f"DELETE FROM {t}")  # noqa: S608 — 고정된 표 이름
        for name, (uid, role) in USERS.items():
            c.execute("INSERT INTO users (id, username, role) VALUES (%s, %s, %s)", (uid, name, role))
    monkeypatch.setattr(auth, "MODES", frozenset({"local"}))
    monkeypatch.setattr(auth, "session_user",
                        lambda t: auth.User(USERS[t][0], t, t, USERS[t][1]) if t in USERS else None)
    monkeypatch.setattr(store, "DATA", tmp_path)
    docs = [doc("d01", True), doc("d02"), doc("d03")]
    state = {"docs": docs, "indexed": len(docs)}
    snap = SimpleNamespace(next=(), interrupts=[], values=state)

    def update_state(cfg, v, as_node=None):
        new = {d["doc_id"]: d for d in v["docs"]}
        state["docs"] = [new.get(d["doc_id"], d) for d in state["docs"]]

    monkeypatch.setattr(main, "G", SimpleNamespace(get_state=lambda cfg: snap, update_state=update_state,
                                                    invoke=lambda arg, cfg: None))
    monkeypatch.setattr(main.llm, "embed", lambda vol, texts: [[0.0]] * len(texts))
    monkeypatch.setattr(main.store, "upsert_vectors", lambda docs, vecs: None)
    monkeypatch.setattr(main.flow, "doc_text", lambda vol, d: "x")
    return main


def approve(c, who, did, stage, **fields):
    return c.post(f"/workflow/v1/{did}/approve", headers=h(who), json={"stage": stage, "fields": fields})


def reject(c, who, did, stage, reason):
    return c.post(f"/workflow/v1/{did}/reject", headers=h(who), json={"stage": stage, "reason": reason})


def where(c, who="v"):
    return {d["doc_id"]: d for d in c.get("/volumes/v1/workflow", headers=h(who)).json()["docs"]}


def unconfirmed(c):
    return c.get("/export/check", params={"vol": "v1"}, headers=h("v")).json()["unconfirmed"]


def test_three_stage_flow_end_to_end(main, monkeypatch):
    """작업자 → 품질관리자 → 감독관: 승인·반려(사유 필수)·재검수·확정, 건너뛰기·역할·작업자≠검수자 막기, 미확정 세기, 감사 로그"""
    use_flow(main, monkeypatch, workflow.load(workflow.DIR / "three_stage.yaml"))
    c = TestClient(main.app)
    assert c.get("/workflow", headers=h("v")).json()["stages"][1] == {
        "name": "qc", "label": "품질관리자", "role": "inspector", "kind": "review", "select": "all", "rate": None,
        "reject": True, "reject_to": "entry", "submit": None, "actions": True}
    w = where(c)
    assert {d: (x["stage"], x["round"], x["confirmed"]) for d, x in w.items()} == {
        "d01": ("entry", 1, False), "d02": ("entry", 1, False), "d03": ("entry", 1, False)}
    assert w["d01"]["can_approve"] is False  # 보기 역할
    assert where(c, "w")["d01"]["can_approve"] is True and where(c, "w")["d01"]["can_reject"] is False
    assert unconfirmed(c) == 3  # 마지막 단계 승인 전 = 미확정

    # 단계 건너뛰기·역할: 입력 단계에 있는 문서를 품질관리·감독관 단계로 승인할 수 없다
    assert approve(c, "q", "d01", "qc").status_code == 409
    assert approve(c, "s", "d01", "supervisor").status_code == 409
    assert approve(c, "w", "d01", "qc").status_code == 403  # 작업자 역할은 품질관리 단계를 못 맡는다
    assert approve(c, "v", "d01", "entry").status_code == 403  # 보기 역할
    assert approve(c, "w", "d01", "nope").status_code == 404
    assert approve(c, "w", "d09", "entry").status_code == 404
    assert reject(c, "w", "d01", "entry", "사유").status_code == 409  # 입력 단계는 반려하는 단계가 아님

    # 1차: 작업자 w가 고쳐서 넘김 → 품질관리자 q가 사유를 적어 반려 → 그 문서만 입력으로 (차례 2)
    r = approve(c, "w", "d01", "entry", title="가상 작업자 제목")
    assert r.status_code == 200 and r.json() == {"doc_id": "d01", "stage": "entry", "to": "qc", "round": 1,
                                                 "fixed_fields": ["title"]}
    assert approve(c, "w", "d01", "entry").status_code == 409  # 이미 넘긴 단계를 다시 승인할 수 없다
    assert c.patch("/documents/v1/d01", headers=h("w"), json={"title": "몰래"}).status_code == 409  # 검수로 넘어간 문서
    assert reject(c, "q", "d01", "qc", "").status_code == 422  # 사유 필수
    assert reject(c, "q", "d01", "qc", "   \x07 ").status_code == 422  # 빈칸·제어 문자만도 안 됨
    assert reject(c, "q", "d01", "qc", "가" * 501).status_code == 422
    r = reject(c, "q", "d01", "qc", "가상 사유: 제목 오기")
    assert r.status_code == 200 and r.json() == {"doc_id": "d01", "stage": "qc", "to": "entry", "round": 2}
    x = where(c)["d01"]
    assert (x["stage"], x["round"], x["recheck"], x["last_reason"], x["rejected_from"]) == (
        "entry", 2, True, "가상 사유: 제목 오기", "qc")
    assert where(c)["d02"]["stage"] == "entry"  # 다른 문서는 그대로

    # 작업자 ≠ 검수자: 품질관리에서 반려한 q는 같은 문서의 입력을 맡을 수 없다 (역할은 넓어도)
    assert approve(c, "q", "d01", "entry").status_code == 403
    assert c.patch("/documents/v1/d01", headers=h("q"), json={"title": "x"}).status_code == 403
    assert where(c, "q")["d01"]["can_approve"] is False
    # 돌아온 문서는 입력 경로로 고칠 수 있고, 다시 넘기면 재검수
    assert c.patch("/documents/v1/d01", headers=h("w"), json={"title": "가상 고친 제목"}).status_code == 200
    assert approve(c, "w", "d01", "entry").json()["round"] == 2
    assert approve(c, "w2", "d01", "qc").status_code == 403  # 작업자 역할
    r = approve(c, "q", "d01", "qc", recipient="가상구청")
    assert r.status_code == 200 and r.json()["to"] == "supervisor" and r.json()["fixed_fields"] == ["recipient"]
    assert approve(c, "q", "d01", "supervisor").status_code == 403  # 감독관은 admin
    assert approve(c, "s", "d01", "supervisor").json()["to"] == workflow.CONFIRMED
    x = where(c)["d01"]
    assert x["confirmed"] and x["label"] == "확정"

    # 확정된 문서는 어느 경로로도 못 바꾼다
    assert approve(c, "s", "d01", "supervisor").status_code == 409
    assert reject(c, "s", "d01", "supervisor", "가상 사유").status_code == 409
    assert c.patch("/documents/v1/d01", headers=h("s"), json={"title": "x"}).status_code == 409
    assert unconfirmed(c) == 2

    # d02: 감독관 s2가 입력을 넘겼으면 같은 문서의 감독관 단계를 맡을 수 없다 → 다른 감독관 s가 확정
    assert approve(c, "s2", "d02", "entry").status_code == 200
    assert approve(c, "q2", "d02", "qc").status_code == 200
    assert approve(c, "s2", "d02", "supervisor").status_code == 403
    assert reject(c, "s", "d02", "supervisor", "가상 사유: 쪽 순서").json()["to"] == "entry"  # 감독관 반려도 입력으로
    assert where(c)["d02"]["round"] == 2
    assert approve(c, "s2", "d02", "entry").status_code == 200
    assert approve(c, "q2", "d02", "qc").status_code == 200
    assert approve(c, "s", "d02", "supervisor").status_code == 200
    assert unconfirmed(c) == 1  # d03만

    # 단계 기록 (오류목록 내보내기의 원천): 누가·어느 단계·차례·어디로·사유·고친 칸
    hist = c.get("/documents/v1/d01/stages", headers=h("v")).json()
    assert [(e["stage"], e["round"], e["action"], e["to_stage"], e["username"], e["reason"], e["fixed_fields"])
            for e in hist] == [
        ("entry", 1, "approve", "qc", "w", None, ["title"]),
        ("qc", 1, "reject", "entry", "q", "가상 사유: 제목 오기", None),
        ("entry", 2, "approve", "qc", "w", None, []),
        ("qc", 2, "approve", "supervisor", "q", None, ["recipient"]),
        ("supervisor", 2, "approve", workflow.CONFIRMED, "s", None, [])]
    edits = c.get("/documents/v1/d01/edits", headers=h("v")).json()
    assert [(e["stage"], e["username"], e["field"]) for e in edits] == [
        ("entry", "w", "title"), ("entry", "w", "title"), ("qc", "q", "recipient")]

    # 감사 로그: 단계 이동·반려·재검수 (사유 원문은 넣지 않고 길이만), 막힌 시도도 남는다
    log = [r for r in reversed(store.audit_log()) if r["action"] in ("stage_approve", "stage_reject")]
    ok = [(r["action"], r["doc_id"], r["info"]["stage"], r["info"]["to"], r["info"]["round"]) for r in log
          if r["result"] == "ok" and r["doc_id"] == "d01"]
    assert ok == [("stage_approve", "d01", "entry", "qc", 1), ("stage_reject", "d01", "qc", "entry", 2),
                  ("stage_approve", "d01", "entry", "qc", 2), ("stage_approve", "d01", "qc", "supervisor", 2),
                  ("stage_approve", "d01", "supervisor", workflow.CONFIRMED, 2)]
    assert next(r for r in log if r["action"] == "stage_approve" and r["info"].get("round") == 2
                and r["info"]["stage"] == "qc")["info"]["recheck"] is True
    rej = next(r for r in log if r["action"] == "stage_reject" and r["result"] == "ok")
    assert rej["info"]["reason_chars"] == len("가상 사유: 제목 오기") and "제목 오기" not in str(rej["info"])
    assert {r["result"] for r in log} >= {"ok", "rejected_403", "rejected_409", "rejected_404"}


def test_concurrent_claim_and_failed_fix_release(main, monkeypatch):
    """같은 차례를 두 사람이 동시에 넘기지 못하고, 칸 반영이 실패하면 차지를 풀어 다시 할 수 있다"""
    use_flow(main, monkeypatch, workflow.load(workflow.DIR / "three_stage.yaml"))
    c = TestClient(main.app, raise_server_exceptions=False)
    assert approve(c, "w", "d03", "entry").status_code == 200
    with store.pool.connection() as conn:  # 다른 요청이 승인 중 (차지)
        conn.execute("UPDATE doc_stage SET busy_by = 4, busy_at = now() WHERE vol = 'v1' AND doc_id = 'd03'")
    assert approve(c, "q", "d03", "qc").status_code == 409
    assert reject(c, "q", "d03", "qc", "가상 사유").status_code == 409
    store.release_stage("v1", "d03")
    monkeypatch.setattr(main.llm, "embed", lambda vol, texts: (_ for _ in ()).throw(TimeoutError("embed")))
    assert approve(c, "q", "d03", "qc", title="가상").status_code == 500
    assert store.stage_rows("v1")["d03"]["busy_at"] is None and where(c)["d03"]["stage"] == "qc"
    monkeypatch.setattr(main.llm, "embed", lambda vol, texts: [[0.0]] * len(texts))
    assert approve(c, "q", "d03", "qc", title="가상").status_code == 200


def test_sample_stage_after_full_stage_cannot_be_skipped(main, monkeypatch):
    """설정 흐름의 마지막 표본 단계: 앞 단계를 다 지나야 검사할 수 있고, 앞 단계를 맡은 사람은 검사하지 못한다"""
    use_flow(main, monkeypatch, workflow.parse({"stages": [
        {"name": "entry", "role": "reviewer"},
        {"name": "qc", "role": "inspector", "reject": True},
        {"name": "audit", "role": "inspector", "select": "sample", "rate": 0.5}]}))
    c = TestClient(main.app)
    assert c.post("/volumes/v1/inspection", headers=h("s"), json={"rate": 1, "seed": 1}).status_code == 200
    assert c.post("/inspection/v1/d01", headers=h("q2"), json={}).status_code == 409  # 아직 입력 단계
    assert c.get("/volumes/v1/inspection", headers=h("q2")).json()["docs"][0]["can_inspect"] is False
    assert approve(c, "w", "d01", "entry").status_code == 200
    assert approve(c, "q", "d01", "qc").status_code == 200
    assert approve(c, "q2", "d01", "audit").status_code == 409  # 표본 단계는 /inspection 경로로
    assert c.post("/inspection/v1/d01", headers=h("q"), json={}).status_code == 403  # qc를 맡은 사람
    assert c.post("/inspection/v1/d01", headers=h("q2"), json={}).status_code == 200
    assert where(c)["d01"]["confirmed"] and unconfirmed(c) == 2
    assert c.get("/documents/v1/d01/stages", headers=h("v")).json()[-1]["action"] == "approve"


def test_default_flow_unchanged(main):
    """설정 없이(기본 흐름): 단계 경로는 아무것도 바꾸지 못하고, 검사·수정·출력 전 검증은 지금과 같다"""
    c = TestClient(main.app)
    assert main.WORKFLOW == workflow.load(workflow.DEFAULT)
    w = where(c, "q")
    assert {x["stage"] for x in w.values()} == {"inspect"} and not any(x["can_approve"] for x in w.values())
    assert approve(c, "w", "d02", "entry").status_code == 409  # 입력은 색인으로 끝났다
    assert approve(c, "q", "d02", "inspect").status_code == 409  # 검사는 /inspection 경로
    chk = c.get("/export/check", params={"vol": "v1"}, headers=h("v")).json()
    assert "unconfirmed" not in chk and set(chk) == {"template", "volumes", "required_blank", "format", "not_reviewed",
                                                     "problems", "problems_total"}
    assert c.patch("/documents/v1/d02", headers=h("w"), json={"title": "가상 수정"}).status_code == 200
    assert c.post("/volumes/v1/inspection", headers=h("s"), json={"rate": 0.5, "seed": 3}).status_code == 200
    assert c.post("/inspection/v1/d01", headers=h("q"), json={}).status_code == 200  # 표시 문서는 늘 검사 목록에
    assert c.patch("/documents/v1/d01", headers=h("w"), json={"title": "x"}).status_code == 409  # 검사 끝난 문서
    listed = {d["doc_id"] for d in c.get("/volumes/v1/inspection", headers=h("q")).json()["docs"]}
    for d in {"d02", "d03"} - listed:  # 표본 밖 문서는 지금처럼 고칠 수 있다
        assert c.patch(f"/documents/v1/{d}", headers=h("w"), json={"recipient": "가상읍"}).status_code == 200
    w = where(c)
    assert w["d01"]["confirmed"] and all(w[d]["confirmed"] for d in {"d02", "d03"} - listed)
    with store.pool.connection() as conn:  # 기본 흐름은 단계 위치 행을 만들지 않는다 (검사 기록만 stage_log에)
        assert conn.execute("SELECT count(*) AS n FROM doc_stage").fetchone()["n"] == 0
    assert [(e["stage"], e["action"]) for e in c.get("/documents/v1/d01/stages", headers=h("v")).json()] == [
        ("inspect", "approve")]


def test_old_database_opens(main, pg_store):
    """단계 표가 없던 예전 DB(검사까지 끝난 철)에 새 판이 시작하면 표만 더하고 옛 철은 그대로 열린다"""
    with pg_store.pool.connection() as conn:
        conn.execute("DROP TABLE doc_stage")
        conn.execute("DROP TABLE stage_log")
        conn.execute("INSERT INTO inspection_plan (vol, rate, fields100, seed, flagged, unflagged, sample) "
                     "VALUES ('v1', 0.5, '{}', 1, 1, 2, 1)")
        conn.execute("INSERT INTO inspection (vol, doc_id, reason, inspected_by, inspected_at, fixed_fields) VALUES "
                     "('v1', 'd01', 'flagged', 3, now(), '{}'), ('v1', 'd02', 'sample', NULL, NULL, NULL)")
    store.init_db()  # 앱 시작과 같은 길
    c = TestClient(main.app)
    w = where(c)
    assert (w["d01"]["confirmed"], w["d02"]["stage"], w["d03"]["confirmed"]) == (True, "inspect", True)
    view = c.get("/volumes/v1/inspection", headers=h("q2")).json()["docs"]
    assert [(d["doc_id"], d["can_inspect"]) for d in view] == [("d01", False), ("d02", True)]
    assert c.patch("/documents/v1/d01", headers=h("w"), json={"title": "x"}).status_code == 409
    assert c.post("/inspection/v1/d02", headers=h("q2"), json={}).status_code == 200
    assert c.get("/documents/v1/d01/stages", headers=h("v")).json() == []


def test_rerun_resets_stage_positions(main, monkeypatch):
    use_flow(main, monkeypatch, workflow.load(workflow.DIR / "three_stage.yaml"))
    c = TestClient(main.app)
    assert approve(c, "w", "d02", "entry").status_code == 200
    store.pages_dir("v1").mkdir(parents=True)
    (store.pages_dir("v1") / "p.jpg").write_bytes(b"\xff\xd8\xff")
    assert c.post("/volumes/v1/run", headers=h("s")).status_code == 200
    assert store.stage_rows("v1") == {}  # 처음부터 다시 돌리면 문서가 바뀐다 → 단계 위치도 처음으로
    assert len(c.get("/documents/v1/d02/stages", headers=h("v")).json()) == 1  # 기록은 남는다
