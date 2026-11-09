"""보충 스크립트가 저장된 문서 목록(LangGraph 체크포인트)을 읽는지 (https://github.com/reddol18/docflow/issues/35).
실제 Postgres(PGlite)에 돌린다. TEST_PG_URL이 없으면 건너뛴다. 값은 모두 지어낸 것."""
import os
from typing import TypedDict

import pytest

from app import backfill_crops, store

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_PG_URL"), reason="TEST_PG_URL 없음")


class S(TypedDict, total=False):
    docs: list


def test_stored_docs_reads_checkpoint(pg_store):
    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.graph import END, START, StateGraph

    saver = PostgresSaver(store.pool)
    saver.setup()
    docs = [{"doc_id": "가상-0001", "vol": "가상철-1", "info": {"recipient_fix": "목록"}, "fields": {"recipient": "가상과장"}},
            {"doc_id": "가상-0005", "vol": "가상철-1", "info": {"recipient_fix": ""}, "fields": {"recipient": "가상상사"}}]
    g = StateGraph(S)
    g.add_node("n", lambda s: {"docs": docs})
    g.add_edge(START, "n")
    g.add_edge("n", END)
    g.compile(checkpointer=saver).invoke({}, {"configurable": {"thread_id": "가상철-1"}})
    got = backfill_crops.stored_docs("가상철-1")
    assert [d["doc_id"] for d in got] == ["가상-0001", "가상-0005"]
    assert backfill_crops.recipient_of(got[0], backfill_crops.fields.SCHEMA) == ("목록", "가상과장")
    assert backfill_crops.stored_docs("없는철") == []
