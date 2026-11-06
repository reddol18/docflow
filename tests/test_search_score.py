"""검색 평가 채점기와 질의 세트 (https://github.com/reddol18/docflow/issues/20). 색인·LLM 없이."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location("search_score", ROOT / "eval" / "search_score.py")
ss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ss)


def test_metrics():
    ranked = ["a", "x", "b", "y", "c"]
    assert ss.recall_at(ranked, {"a", "b"}, 5) == 1.0
    assert ss.recall_at(ranked, {"a", "z"}, 5) == 0.5
    assert ss.recall_at(ranked, set("abcdefghijkl"), 5) == 3 / 5  # 정답이 k보다 많으면 k로 나눈다
    assert ss.recall_at(ranked, set(), 5) is None
    assert ss.mrr(ranked, {"b"}) == pytest.approx(1 / 3)
    assert ss.mrr(ranked, {"z"}) == 0.0
    assert ss.ndcg_at(["a", "b"], {"a", "b"}) == pytest.approx(1.0)
    assert ss.ndcg_at(["x", "a"], {"a"}) == pytest.approx(1 / 1.5849625, rel=1e-6)


def test_score_groups_and_skips_no_answer():
    queries = [{"id": "q1", "type": "주제", "relevant": ["a"]}, {"id": "q2", "type": "정확 조회", "relevant": ["b"]},
               {"id": "q3", "type": "답 없음", "relevant": []}]
    res = {"q1": {"vector": ["a"], "keyword": ["x"], "rrf": ["a"]},
           "q2": {"vector": ["x", "b"], "keyword": ["b"], "rrf": ["b"]},
           "q3": {"vector": ["a"], "keyword": [], "rrf": ["a"]}}
    s = ss.score(res, queries)
    assert s["rrf"]["전체"]["n"] == 2 and s["rrf"]["전체"]["MRR"] == 1.0
    assert s["vector"]["정확 조회"]["MRR"] == 0.5
    assert "답 없음" not in s["vector"]
    tops = {"q1": {"vector": 0.6, "rrf": 0.03}, "q2": {"vector": 0.5, "rrf": 0.02}, "q3": {"vector": 0.2, "rrf": 0.016}}
    t = ss.top_scores(tops, queries)
    assert t["vector 답 없음"]["max"] == 0.2 and t["vector 답 있음"]["min"] == 0.5


def test_query_set_shape():
    rows = [json.loads(line) for line in (ROOT / "eval" / "data" / "search_queries.jsonl").read_text(encoding="utf-8").splitlines()]
    assert 55 <= len(rows) <= 70
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids))
    allowed = {"id", "type", "q", "year", "relevant", "answer", "must_cite", "rule", "checked"}
    for r in rows:
        assert set(r) <= allowed, r["id"]  # 원문·본문 칸이 없다
        assert {"id", "type", "q", "relevant", "answer", "must_cite", "checked"} <= set(r), r["id"]
        assert set(r["must_cite"]) <= set(r["relevant"]), r["id"]
        assert (r["type"] == "답 없음") == (not r["relevant"]), r["id"]
    types = {r["type"] for r in rows}
    assert {"정확 조회", "주제", "조건", "답 없음"} <= types
    assert 8 <= sum(r["type"] == "답 없음" for r in rows) <= 12
    assert any(r["q"] == "도로 신설 허가" for r in rows)  # 사용자가 보고한 질의


def test_rrf_matches_search(monkeypatch):
    """/search(hybrid)의 순위가 채점기가 부르는 main.ranked()의 순위와 같다 — 후보 수·연도·낱말 나누기까지"""
    from app import main
    assert main.query_words("1997년 도로 점용 허가 가") == ["도로", "점용", "허가"]
    calls = []

    def vector_search(vec, yf, yt, limit):
        calls.append(("v", yf, yt, limit))
        return [{"doc_id": "a", "score": 0.9}, {"doc_id": "b", "score": 0.8}, {"doc_id": "c", "score": 0.7}]

    def keyword_search(words, yf, yt, limit):
        calls.append(("k", tuple(words), yf, yt, limit))
        return [{"doc_id": "c"}, {"doc_id": "b"}]

    monkeypatch.setattr(main.store, "vector_search", vector_search)
    monkeypatch.setattr(main.store, "keyword_search", keyword_search)
    monkeypatch.setattr(main.llm, "embed", lambda vol, texts: [[0.0]] * len(texts))
    row = {"vol": "v", "docno": "", "date": "", "recipient": "", "title": ""}
    monkeypatch.setattr(main.store, "get_documents", lambda ids: {d: row for d in ids})
    got = [h["doc_id"] for h in main.hybrid("1999년 공장 등록", 1999, 1999, k=3)]
    *_, order = main.ranked("1999년 공장 등록", [0.0], 1999, 1999)
    assert got == order[:3] == ["c", "b", "a"]  # 두 쪽에 다 있는 c(1/63+1/61)·b(2/62)가 앞
    assert calls[0] == ("v", 1999, 1999, main.CANDIDATES)
    assert calls[1] == ("k", ("공장", "등록"), 1999, 1999, main.CANDIDATES)
    assert calls[:2] == calls[2:4]  # hybrid와 ranked가 같은 인자로 검색한다
