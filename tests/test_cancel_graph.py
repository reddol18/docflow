"""그래프 취소 표시가 실제 LangGraph에서 어떻게 도는지 (https://github.com/reddol18/docflow/issues/5, app/cancel.py).
lock과 같은 langgraph 판, 메모리 체크포인터. 워커가 임대를 잃으면 노드 안에서 Cancelled(BaseException)가 올라오는데,
  - invoke가 그것을 그대로 올리고(삼키거나 다른 예외로 바꾸지 않음) 체크포인트에 멈춘 곳이 남아 다음 invoke(None)가 끝까지 이어 가는지
  - 체크포인터 펜싱(FenceMixin)이 임대를 잃은 뒤의 늦은 쓰기를 막는지
"""
import operator
from typing import Annotated, TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from app import cancel


class S(TypedDict, total=False):
    log: Annotated[list, operator.add]


class Fenced(cancel.FenceMixin, InMemorySaver):
    pass


def build(saver, nodes):
    g = StateGraph(S)
    names = list(nodes)
    for n in names:
        g.add_node(n, nodes[n])
    g.add_edge(START, names[0])
    for a, b in zip(names, names[1:]):
        g.add_edge(a, b)
    g.add_edge(names[-1], END)
    return g.compile(checkpointer=saver)


@pytest.mark.parametrize("saver", [InMemorySaver, Fenced])
def test_cancelled_node_then_resume_from_checkpoint(saver):
    vol = f"cg-{saver.__name__}"
    ev, calls = cancel.arm(vol), []

    def a(s):
        calls.append("a")
        return {"log": ["a"]}

    def b(s):
        calls.append("b")
        if calls.count("b") == 1:
            ev.set()  # 노드 안에서 임대를 잃음 (Keeper가 표시를 세움)
        cancel.check(vol)
        return {"log": ["b"]}

    def c(s):
        calls.append("c")
        return {"log": ["c"]}
    G = build(saver(), {"a": a, "b": b, "c": c})
    cfg = {"configurable": {"thread_id": vol}}
    with pytest.raises(cancel.Cancelled):
        G.invoke({"log": []}, cfg)
    snap = G.get_state(cfg)
    assert snap.next and "c" not in calls  # 멈춘 곳이 남고, 뒤 노드는 돌지 않았다
    cancel.disarm(vol, ev)  # 되찾은 워커(새 프로세스) — 표시 없음
    G.invoke(None, cfg)
    snap = G.get_state(cfg)
    assert not snap.next and snap.values["log"][-2:] == ["b", "c"] and calls[-1] == "c"


def test_cancelled_inside_parallel_send():
    vol = "cg-send"
    ev = cancel.arm(vol)
    g = StateGraph(S)
    g.add_node("a", lambda s: {"log": ["a"]})

    def b(s):
        ev.set()
        cancel.check(vol)
        return {"log": ["b"]}
    g.add_node("b", b)
    g.add_edge(START, "a")
    g.add_conditional_edges("a", lambda s: [Send("b", {}) for _ in range(3)], ["b"])
    g.add_edge("b", END)
    G = g.compile(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": vol}, "max_concurrency": 4}
    with pytest.raises(cancel.Cancelled):
        G.invoke({"log": []}, cfg)
    assert G.get_state(cfg).next == ("b", "b", "b")
    cancel.disarm(vol, ev)


def test_fence_blocks_late_writes():
    """노드가 일을 마친 뒤(늦게) 임대를 잃었으면 그 노드의 결과는 체크포인트에 남지 않는다 — 되찾은 워커의 상태를 덮지 않게"""
    vol = "cg-late"
    ev = cancel.arm(vol)

    def a(s):
        ev.set()  # 노드 안에서 잃음 — 이 노드는 끝까지 돌지만
        return {"log": ["a-late"]}
    G = build(Fenced(), {"a": a, "b": lambda s: {"log": ["b"]}})
    cfg = {"configurable": {"thread_id": vol}}
    with pytest.raises(cancel.Cancelled):
        G.invoke({"log": []}, cfg)
    snap = G.get_state(cfg)
    assert "a-late" not in (snap.values.get("log") or [])  # 늦은 쓰기는 막혔다
    cancel.disarm(vol, ev)


def test_cancelled_with_postgres_checkpointer(pg_store):
    """워커의 실제 체크포인터(FencedSaver = PostgresSaver + 펜싱, PGlite)에서도: 노드 안 Cancelled → invoke가 올림 →
    멈춘 곳이 남고 → 다음 invoke(None)가 끝까지"""
    from app.worker import FencedSaver
    vol = "cg-pg"
    saver = FencedSaver(pg_store.pool)
    saver.setup()
    saver.delete_thread(vol)
    ev, calls = cancel.arm(vol), []

    def b(s):
        calls.append("b")
        if calls.count("b") == 1:
            ev.set()
        cancel.check(vol)
        return {"log": ["b"]}
    G = build(saver, {"a": lambda s: {"log": ["a"]}, "b": b, "c": lambda s: {"log": ["c"]}})
    cfg = {"configurable": {"thread_id": vol}}
    with pytest.raises(cancel.Cancelled):
        G.invoke({"log": []}, cfg)
    snap = G.get_state(cfg)
    assert not snap.next and snap.tasks  # PG에서는 next가 비고 tasks에만 남는다 — 워커는 둘 다 본다
    from app.worker import unfinished
    assert unfinished(snap)
    cancel.disarm(vol, ev)
    G.invoke(None, cfg)
    snap = G.get_state(cfg)
    assert not snap.next and snap.values["log"][-2:] == ["b", "c"]
    saver.delete_thread(vol)
