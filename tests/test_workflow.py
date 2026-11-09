"""검토 단계 설정 — 읽기·검사·위치 계산 (https://github.com/reddol18/docflow/issues/86). DB 없이 돈다."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import workflow

ROOT = Path(__file__).resolve().parent.parent


def test_default_is_current_two_stage_flow():
    """설정 없이 띄우면 지금의 2단계: 입력(reviewer, 색인이 끝나면 넘어감) → 표본 검사(inspector, 반려 없음)"""
    wf = workflow.load(workflow.DEFAULT)
    assert workflow.load() == wf  # WORKFLOW 환경변수가 없으면 이 파일
    assert (wf["distinct_actors"], wf["count_unconfirmed"]) == (True, True)  # 미확정 세기: 사용자 결정 2026-11-09
    assert [(s["name"], s["role"], s["kind"], s["select"], s["reject"], s["rate"]) for s in wf["stages"]] == [
        ("entry", "reviewer", "entry", "all", False, None), ("inspect", "inspector", "review", "sample", False, None)]
    assert wf["stages"][0]["submit"] == "on_index"
    assert workflow.initial(wf) == "inspect"  # 색인이 끝난 문서는 곧바로 검사 단계 (입력은 철 검토로 끝남)
    assert workflow.next_stage(wf, "inspect") == workflow.CONFIRMED
    assert workflow.editable_positions(wf) == {"entry", "inspect"}  # 검사 전 문서는 지금처럼 고칠 수 있다
    assert not workflow.actionable(wf, "inspect")  # 표본 검사는 /inspection 경로 그대로
    pub = workflow.public(wf)
    assert [s["actions"] for s in pub["stages"]] == [True, False]


@pytest.mark.parametrize("name", ["three_stage.yaml", "register_approve.yaml"])
def test_example_flows_load(name):
    wf = workflow.load(workflow.DIR / name)
    assert wf["count_unconfirmed"] and wf["distinct_actors"]
    assert workflow.initial(wf) == wf["stages"][0]["name"]  # 작업자가 문서마다 넘긴다


def test_three_stage_positions():
    wf = workflow.load(workflow.DIR / "three_stage.yaml")
    assert [s["name"] for s in wf["stages"]] == ["entry", "qc", "supervisor"]
    assert workflow.next_stage(wf, "entry") == "qc" and workflow.next_stage(wf, "supervisor") == workflow.CONFIRMED
    assert [workflow.stage(wf, n)["reject_to"] for n in ("qc", "supervisor")] == ["entry", "entry"]
    assert workflow.editable_positions(wf) == {"entry"}  # 검수로 넘어간 문서는 반려돼야 입력 경로로 고친다
    reg = workflow.load(workflow.DIR / "register_approve.yaml")
    assert workflow.stage(reg, "register")["reject_to"] == "arrange"


def _flow(*stages, **top):
    return {"stages": [{"name": "entry", "role": "reviewer"}, *stages], **top}


@pytest.mark.parametrize("t, msg", [
    ([], "사전"),
    ({"stages": []}, "stages"),
    ({"stages": [{"name": "entry", "role": "reviewer"}], "extra": 1}, "모르는 키"),
    ({"stages": [{"name": "Entry", "role": "reviewer"}]}, "name"),
    ({"stages": [{"name": "entry", "role": "viewer"}]}, "role"),  # 보기 역할은 단계를 맡을 수 없다
    ({"stages": [{"name": "entry", "role": "reviewer", "submit": "auto"}]}, "submit"),
    ({"stages": [{"name": "entry", "role": "reviewer", "submit": "on_index"}]}, "on_index"),  # 색인만으로 확정 — 거부
    ({"stages": [{"name": "entry", "role": "reviewer", "reject": True}]}, "모르는 키"),  # 입력 단계는 반려 대상이 아니라 시작
    (_flow({"name": "entry", "role": "inspector"}), "중복"),
    (_flow({"name": "qc", "role": "inspector", "select": "half"}), "select"),
    (_flow({"name": "qc", "role": "inspector", "rate": 0.1}), "rate는 sample"),
    (_flow({"name": "qc", "role": "inspector", "select": "sample", "rate": 0}), "0~1"),
    (_flow({"name": "qc", "role": "inspector", "select": "sample", "rate": True}), "0~1"),
    (_flow({"name": "qc", "role": "inspector", "reject": "yes"}), "true/false"),
    (_flow({"name": "qc", "role": "inspector", "reject_to": "entry"}), "reject: true"),
    (_flow({"name": "qc", "role": "inspector", "reject": True, "reject_to": "sv"},
           {"name": "sv", "role": "admin"}), "앞 단계"),  # 뒤 단계로는 반려 못 함
    (_flow({"name": "qc", "role": "inspector", "reject": True, "reject_to": "qc"}), "앞 단계"),  # 자기 자신도 안 됨
    (_flow({"name": "s", "role": "inspector", "select": "sample"}, {"name": "sv", "role": "admin"}), "마지막"),
    (_flow({"name": "s", "role": "inspector", "select": "sample", "reject": True}), "반려"),
    (_flow({"name": "qc", "role": "inspector", "label": ""}), "label"),
    (_flow(distinct_actors="no"), "true/false"),
])
def test_bad_flows_rejected(t, msg):
    with pytest.raises(workflow.WorkflowError, match=msg):
        workflow.parse(t)


def test_on_index_then_full_stage_not_editable_by_entry_path():
    """입력 on_index 다음이 전수 검수 단계면 검수 대기 문서는 입력 경로로 못 고친다 (반려로 입력에 돌아오면 고친다)"""
    wf = workflow.parse({"stages": [{"name": "entry", "role": "reviewer", "submit": "on_index"},
                                    {"name": "qc", "role": "inspector", "reject": True}]})
    assert workflow.initial(wf) == "qc" and workflow.editable_positions(wf) == {"entry"}
    assert workflow.actionable(wf, "qc") and not workflow.actionable(wf, workflow.CONFIRMED)


def test_reject_to_defaults_to_previous_stage():
    wf = workflow.parse(_flow({"name": "qc", "role": "inspector"}, {"name": "sv", "role": "admin", "reject": True}))
    assert workflow.stage(wf, "sv")["reject_to"] == "qc"


def test_missing_file_is_workflow_error(tmp_path):
    with pytest.raises(workflow.WorkflowError):
        workflow.load(tmp_path / "없음.yaml")
    (tmp_path / "bad.yaml").write_text("stages: [", encoding="utf-8")
    with pytest.raises(workflow.WorkflowError):
        workflow.load(tmp_path / "bad.yaml")


def test_clean_reason_drops_invisible_and_control():
    """반려 사유는 한 줄: 줄바꿈·탭 → 빈칸, 분류 Cc·Cf·Zl·Zp(방향 제어·폭 0·소프트 하이픈·줄 구분) 글자는 지운다"""
    from app import main
    assert main.clean_reason("가상\n사유\t둘") == "가상 사유 둘"
    assert main.clean_reason("가‮상​ ⁦사­유﻿ 끝\x07") == "가상 사유 끝"
    assert main.clean_reason("​‪ \x00\n") == ""
    assert main.clean_reason("漢字·한글 1-2 (가상)") == "漢字·한글 1-2 (가상)"  # 보이는 글자는 그대로


class _FakeConn:
    def __init__(self, fail_unlock):
        self.fail_unlock, self.closed, self.sql = fail_unlock, False, []

    def execute(self, sql, args=None):
        self.sql.append(sql.split("(")[0])
        if "unlock" in sql and self.fail_unlock:
            raise OSError("연결 끊김")
        return type("R", (), {"fetchone": lambda _: {"ok": True}})()

    def close(self):
        self.closed = True


class _FakePool:
    def __init__(self, conn):
        self.conn, self.put = conn, 0

    def getconn(self, timeout=None):
        return self.conn

    def putconn(self, c):
        self.put += 1


@pytest.mark.parametrize("fail", [False, True])
def test_lock_unlock_failure_closes_connection(monkeypatch, fail):
    """advisory lock을 풀다 실패하면 연결을 닫는다 (닫힌 연결은 풀이 버리고, 세션이 끝나며 서버가 잠금을 푼다).
    겹친 잠금(문서 → 철)은 같은 연결에 건다"""
    from app import store
    conn = _FakeConn(fail)
    pool = _FakePool(conn)
    monkeypatch.setattr(store, "lock_pool", pool)
    with store.doc_lock("v", "d"), store.vol_lock("v"):
        pass
    assert conn.closed is fail and pool.put == 1
    assert [s for s in conn.sql if "advisory" in s] == ["SELECT pg_try_advisory_lock", "SELECT pg_try_advisory_lock",
                                                        "SELECT pg_advisory_unlock", "SELECT pg_advisory_unlock"]


def test_lock_wait_has_deadline(monkeypatch):
    """같은 키를 다른 스레드가 쥐고 있으면 LOCK_WAIT 뒤 LockBusy (끝없이 기다리지 않는다)"""
    import threading

    from app import store
    monkeypatch.setattr(store, "lock_pool", _FakePool(_FakeConn(False)))
    monkeypatch.setattr(store, "LOCK_WAIT", 0.2)
    held, done = threading.Event(), threading.Event()

    def hold():
        with store.doc_lock("v", "d"):
            held.set()
            done.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    held.wait(5)
    try:
        with pytest.raises(store.LockBusy), store.doc_lock("v", "d"):
            pass
    finally:
        done.set()
        t.join()


def _start_app(env_workflow):
    env = {k: v for k, v in os.environ.items() if k != "WORKFLOW"}
    env.update(WORKFLOW=str(env_workflow), LLM_PROVIDER="openai", AUTH_MODES="local", PYTHONUTF8="1")
    code = f"import sys; sys.path.insert(0, {str(ROOT)!r}); import app.main as m; print(len(m.WORKFLOW['stages']))"
    return subprocess.run([sys.executable, "-I", "-c", code], env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300)


def test_app_stops_on_bad_workflow_and_starts_on_example(tmp_path):
    """앱은 불러올 때 흐름 설정을 읽고 검사한다 — 틀리면 시작하지 않는다"""
    bad = tmp_path / "bad.yaml"
    bad.write_text("stages:\n  - name: entry\n    role: viewer\n", encoding="utf-8")
    p = _start_app(bad)
    assert p.returncode != 0 and "WorkflowError" in p.stderr
    p = _start_app(workflow.DIR / "three_stage.yaml")
    assert p.returncode == 0, p.stderr[-2000:]
    assert p.stdout.strip() == "3"
