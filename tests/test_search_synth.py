"""합성 철 검색 채점 eval/search_synth.py (https://github.com/reddol18/docflow/issues/61).
모델을 부르지 않는다 — 합성 아닌 입력 막기, 지표 셈, 임베딩 캐시(캐시면 부르지 않음)만 본다. 값은 지어낸 것."""
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parent.parent
EVAL = ROOT / "eval"


@pytest.fixture(scope="module")
def ssy():
    before = list(sys.path)
    spec = importlib.util.spec_from_file_location("search_synth", EVAL / "search_synth.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod
    sys.path[:] = before


def _docs(ec):
    vals = [("건축 56120-928", "1994-01-02", "주식회사 가온건설 대표 김가상", "건축허가 신청에 대한 처리 결과"),
            ("도시 56120-9280", "1998-03-04", "내부결재", "도로점용허가 신청에 대한 회신")]
    out = []
    for i, (dn, dt, rc, ti) in enumerate(vals):
        v = {"docno": dn, "date": dt, "recipient": rc, "title": ti}
        out.append({"key": ("SYNTH0001-1994-0001", f"SYNTH0001-1994-0001-000{i + 1}"), "values": v, "text": f"글{i}",
                    "search_text": ec.fields.search_text(ec.SCHEMA, v), "year": ec.fields.year_of(ec.SCHEMA, v)})
    return out


# ---------- 합성 아닌 입력 ----------

def test_rejects_real_query_set(ssy, tmp_path):
    docs = _docs(ssy.ec)
    real = ROOT / "eval" / "data" / "search_queries.jsonl"  # 실제 자료 질의 세트 (relevant·must_cite 칸)
    with pytest.raises(SystemExit):
        ssy.load_set(real, docs)
    bad = tmp_path / "q.jsonl"
    bad.write_text(json.dumps({"id": "q01", "q": "가상"}, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SystemExit):
        ssy.load_set(bad, docs)
    # 답변 질의 꼴인데 근거가 합성 철이 아님
    bad.write_text(json.dumps({"id": "a01", "type": "수신", "question": "가상", "answer": "x",
                               "source_docs": ["5350109-1997-0002/p1"], "answerable": True}, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SystemExit):
        ssy.load_set(bad, docs)


def test_main_rejects_non_synthetic_work_before_client(ssy, tmp_path, monkeypatch):
    w = tmp_path / "work"
    (w / "platform").mkdir(parents=True)
    (w / "platform" / "5350109-1997-0002.json").write_text("[]", encoding="utf-8")  # 실제 자료 꼴 철, 목록 없음
    monkeypatch.setattr(ssy.cc, "make_client", lambda *a, **k: pytest.fail("부르면 안 된다"))
    with pytest.raises(ssy.cc.NotSynthetic):
        ssy.main(["--work", str(w), "--queries", str(EVAL / "data" / "synth_search_queries.jsonl")])


def test_load_sets(ssy, tmp_path):
    docs = _docs(ssy.ec)
    a = tmp_path / "a.jsonl"
    a.write_text(json.dumps({"id": "a01", "type": "수신", "question": "문서번호 건축 56120-928 문서의 수신자는?",
                             "answer": "x", "source_docs": ["SYNTH0001-1994-0001/SYNTH0001-1994-0001-0001"],
                             "answerable": True}, ensure_ascii=False), encoding="utf-8")
    qs = ssy.load_set(a, docs)
    assert qs == [{"id": "a01", "type": "수신", "q": "문서번호 건축 56120-928 문서의 수신자는?", "year": None,
                   "gold": ["SYNTH0001-1994-0001/SYNTH0001-1994-0001-0001"], "answerable": True}]
    s = tmp_path / "s.jsonl"
    s.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in [
        {"id": "s01", "type": "주제", "q": "도로점용", "year": 1998, "match": {"title": "도로점용", "year": 1998}},
        {"id": "s02", "type": "답 없음", "q": "인사 발령", "match": {}}]), encoding="utf-8")
    qs = ssy.load_set(s, docs)
    assert qs[0]["gold"] == ["SYNTH0001-1994-0001/SYNTH0001-1994-0001-0002"] and qs[0]["year"] == 1998
    assert qs[1]["gold"] == [] and qs[1]["answerable"] is False


# ---------- 지표 ----------

def test_query_metrics(ssy):
    m = ssy.query_metrics(["x", "a", "y", "b"], ["a", "b"])
    assert m == {"r1": 0.0, "r5": 1.0, "r10": 1.0, "mrr": 0.5, "all5": True}
    m = ssy.query_metrics(["a"] + [f"x{i}" for i in range(9)] + ["b"], ["a", "b"])
    assert m["r1"] == 1.0 and m["r5"] == 0.5 and m["r10"] == 0.5 and m["all5"] is False
    assert ssy.query_metrics([], ["a"]) == {"r1": 0.0, "r5": 0.0, "r10": 0.0, "mrr": 0.0, "all5": False}
    assert ssy.query_metrics(["a"], []) is None


def _row(qid, typ, gold, ranking, top_sim=0.5, docnos=(), pinned=()):
    from app import retrieval
    r = retrieval.Ranking([], [], {}, [("V", "x")], list(pinned), list(docnos))
    return {"id": qid, "type": typ, "gold": gold, "answerable": bool(gold), "top_sim": top_sim,
            "pinned": ["/".join(k) for k in pinned], "docno_miss": r.docno_miss,
            "metrics": {m: ssy_metrics(ranking, gold) for m in ("vector", "keyword", "rrf", "app")} if gold else None,
            "_ranking": r}


def ssy_metrics(ranking, gold):
    g = set(gold)
    return {"r1": float(ranking[0] in g), "r5": len(set(ranking[:5]) & g) / min(5, len(g)), "r10": 1.0,
            "mrr": 1.0 if ranking[0] in g else 0.5, "all5": g <= set(ranking[:5])}


def test_summarize_and_sweep(ssy):
    rows = [_row("a1", "수신", ["d1"], ["d1", "d2"], 0.6),
            _row("a2", "수신", ["d3"], ["d2", "d3"], 0.35),
            _row("a3", "여러 문서", ["d1", "d9"], ["d1", "d2"], 0.5),
            _row("n1", "답 없음", [], [], 0.30),
            _row("n2", "답 없음(없는 문서번호)", [], [], 0.55, docnos=[("도시", "56120-1")])]
    s = ssy.summarize(rows)
    assert s[ssy.ALL]["app"]["n"] == 3 and s[ssy.ALL]["app"]["all5"] == 2
    assert s["수신"]["app"] == {"n": 2, "r1": 0.5, "r5": 1.0, "r10": 1.0, "mrr": 0.75, "all5": 2}
    assert s["여러 문서"]["app"]["r5"] == 0.5 and s["여러 문서"]["app"]["all5"] == 0
    sw = ssy.sweep(rows, [None, 0.32, 0.4, 0.55])
    assert sw[0] == {"t": None, "none_n": 1, "tp": 1, "precision": 1.0, "recall": 0.5, "false_none": 0,
                     "false_none_ids": []}  # 문서번호 없음만
    assert sw[1]["t"] == 0.32 and sw[1]["tp"] == 2 and sw[1]["false_none"] == 0 and sw[1]["recall"] == 1.0
    assert sw[2]["t"] == 0.4 and sw[2]["false_none_ids"] == ["a2"] and sw[2]["precision"] == round(2 / 3, 3)
    assert sw[3]["t"] == 0.55 and sw[3]["false_none_ids"] == ["a2", "a3"]
    # 결과가 앞 임계값과 같으면 줄을 뺀다
    assert [x["t"] for x in ssy.sweep(rows, [None, 0.1, 0.2, 0.32])] == [None, 0.32]


def test_threshold_pick_loo_and_saved_rows(ssy):
    """보수적 임계값 = 가장 작은 답 있는 질의 유사도 (잘못 '없음' 0), 하나 빼고 고르기, 저장된 줄(_ranking 없음)도 같은 판정"""
    rows = [_row("a1", "수신", ["d1"], ["d1"], 0.6), _row("a2", "수신", ["d3"], ["d3"], 0.35),
            _row("a3", "수신", ["d1"], ["d1"], 0.2, docnos=[("도시", "1-1")], pinned=[("V", "d1")]),  # 문서번호가 맞음: 빼고 고른다
            _row("n1", "답 없음", [], [], 0.30), _row("n2", "답 없음(없는 회사)", [], [], 0.5)]
    assert ssy.scored_sims(rows) == [0.35, 0.6]
    assert ssy.pick_threshold(rows) == 0.35 and ssy.pick_threshold(rows, max_false=1) == 0.6
    lo = ssy.loo(rows)
    # a2를 빼면 임계값 0.6 → a2를 '없음'으로 (잘못), n1은 어느 쪽이든 '없음', n2(0.5)는 a2가 있을 때 0.35라 아님
    assert lo == {"tp": 1, "n_neg": 2, "false_none": 1, "false_none_ids": ["a2"], "none_ids": ["n1"]}
    rep = ssy.none_report(rows, 0.35)
    assert rep["all"] == "1/2" and rep["core"] == "1/1" and rep["excluded"] == "0/1" and rep["false_none"] == []
    saved = [{k: v for k, v in r.items() if k != "_ranking"} | {"docno_miss": False, "pinned": r["_ranking"].pinned}
             for r in rows]
    assert [ssy.predict_none(r, 0.35) for r in saved] == [ssy.predict_none(r, 0.35) for r in rows]


# ---------- 임베딩 캐시 ----------

class FakeClient:
    def __init__(self):
        self.calls = []
        self.embeddings = SimpleNamespace(create=self.create)

    def create(self, model, input):  # noqa: A002 — SDK 인자 이름
        self.calls.append(list(input))
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10 * len(input)),
                               data=[SimpleNamespace(index=i, embedding=[float(len(t)), 1.0]) for i, t in enumerate(input)])


def test_cache_miss_then_hit_no_api_call(ssy, tmp_path):
    fake = FakeClient()
    made = []

    def client_fn():
        made.append(1)
        return fake, True

    budget = ssy.cc.Budget(0.2)
    vecs, tok, usd, n = ssy.embed_cached(["가상 글", "둘", "가상 글"], "text-embedding-3-small", tmp_path / "c", client_fn, budget)
    assert vecs == [[4.0, 1.0], [1.0, 1.0], [4.0, 1.0]] and n == 2 and fake.calls == [["가상 글", "둘"]]
    assert tok == 20 and usd > 0
    assert sorted(p.name for p in (tmp_path / "c").iterdir()) == sorted({ssy.text_key("가상 글"), ssy.text_key("둘"),
                                                                         ssy.DIM_FILE})
    assert (tmp_path / "c" / ssy.DIM_FILE).read_text(encoding="utf-8") == "2"

    def no_client():
        pytest.fail("캐시에 있으면 클라이언트도 만들지 않는다")

    vecs2, tok2, usd2, n2 = ssy.embed_cached(["둘", "가상 글"], "text-embedding-3-small", tmp_path / "c", no_client, budget)
    assert vecs2 == [[1.0, 1.0], [4.0, 1.0]] and (tok2, usd2, n2) == (0, 0.0, 0) and len(fake.calls) == 1


def test_cache_respects_budget(ssy, tmp_path):
    fake = FakeClient()
    with pytest.raises(SystemExit, match="상한"):
        ssy.embed_cached(["가상"], "text-embedding-3-small", tmp_path / "c", lambda: (fake, True), ssy.cc.Budget(0.0))
    assert fake.calls == [] and not (tmp_path / "c").exists()


def test_model_dir(ssy):
    assert ssy.model_dir("bge-m3:latest") == "bge-m3_latest"
    assert ssy.model_dir("text-embedding-3-small") == "text-embedding-3-small"
    for bad in ("../x", "..", ".", "...", ".hidden", "-x", "_x", ":x", "", "a/b", "a\\b", "x" * 81):
        with pytest.raises(SystemExit):
            ssy.model_dir(bad)


def test_cache_dim_mixed_rejected(ssy, tmp_path):
    """같은 모델 캐시에 다른 차원 벡터가 섞이면 멈춘다 — 파일끼리, 기록(_dim)과, 새로 부른 벡터와"""
    c = tmp_path / "c"
    c.mkdir()
    (c / ssy.text_key("가")).write_text(json.dumps([1.0, 0.0]), encoding="utf-8")
    (c / ssy.text_key("나")).write_text(json.dumps([1.0, 0.0, 0.0]), encoding="utf-8")
    no_client = lambda: pytest.fail("부르면 안 된다")  # noqa: E731
    with pytest.raises(SystemExit, match="차원"):
        ssy.embed_cached(["가", "나"], "m", c, no_client, ssy.cc.Budget(0.2))
    # 예전 캐시(기록 없음): 읽으며 차원을 남긴다, 기록과 다르면 멈춘다
    (c / ssy.text_key("나")).unlink()
    assert ssy.embed_cached(["가"], "m", c, no_client, ssy.cc.Budget(0.2))[0] == [[1.0, 0.0]]
    assert (c / ssy.DIM_FILE).read_text(encoding="utf-8") == "2"
    (c / ssy.DIM_FILE).write_text("1024", encoding="utf-8")
    with pytest.raises(SystemExit, match="차원"):
        ssy.embed_cached(["가"], "m", c, no_client, ssy.cc.Budget(0.2))
    # 새로 부른 벡터(FakeClient 2차원)가 기록(3차원)과 다르면 캐시에 쓰지 않는다
    d = tmp_path / "d"
    d.mkdir()
    (d / ssy.DIM_FILE).write_text("3", encoding="utf-8")
    with pytest.raises(SystemExit, match="차원"):
        ssy.embed_cached(["새 글"], "m", d, lambda: (FakeClient(), False), ssy.cc.Budget(0.2))
    assert sorted(p.name for p in d.iterdir()) == [ssy.DIM_FILE]


def test_split_vector_rank(ssy):
    """따로 넣고 합치기: 칸 벡터 1위와 본문 벡터 1위를 RRF로 (둘 다 위인 문서가 앞), sum은 코사인 평균"""
    ec = ssy.ec
    docs = _docs(ec) + [dict(_docs(ec)[0], key=("SYNTH0001-1994-0001", "SYNTH0001-1994-0001-0003"))]
    f = [[1.0, 0.0], [0.6, 0.8], [0.8, 0.6]]  # 질의 [1,0]: 칸은 0001 > 0003 > 0002
    b = [[0.0, 1.0], [0.8, 0.6], [0.9, 0.43589]]  # 본문은 0003 > 0002 > 0001
    keys, sims = ec.vector_rank([1.0, 0.0], docs, ec.SplitVecs(f, b, "rrf"))
    assert [k[1][-1] for k in keys] == ["3", "1", "2"] and sims[0] == pytest.approx(0.9)
    keys, sims = ec.vector_rank([1.0, 0.0], docs, ec.SplitVecs(f, b, "sum"))
    assert [k[1][-1] for k in keys] == ["3", "2", "1"] and sims[0] == pytest.approx(0.85)
    # 연도 칸 거르기는 두 목록 모두에
    keys, _ = ec.vector_rank([1.0, 0.0], docs, ec.SplitVecs(f, b, "rrf"), year=1998)
    assert [k[1][-1] for k in keys] == ["2"]
    with pytest.raises(ValueError):
        ec.SplitVecs(f, b[:2])
    # 메모리 안 /search 순위도 SplitVecs를 받는다
    rows = ssy.rank_queries([{"id": "s01", "type": "주제", "q": "가상", "year": None, "gold": [], "answerable": False}],
                            docs, ec.SplitVecs(f, b, "rrf"), [[1.0, 0.0]])
    assert rows[0]["ranks"]["vector"][0].endswith("0003")


def test_rank_queries_uses_app_ranking(ssy):
    """search_synth의 app 순위 = retrieval.rank (문서번호 고정), 없는 번호면 빈 목록"""
    ec = ssy.ec
    docs = _docs(ec)
    qs = [{"id": "a01", "type": "수신", "q": "문서번호 건축 56120-928 문서의 수신자는?", "year": None,
           "gold": ["SYNTH0001-1994-0001/SYNTH0001-1994-0001-0001"], "answerable": True},
          {"id": "a02", "type": "답 없음", "q": "문서번호 건축 56120-92 문서", "year": None, "gold": [], "answerable": False}]
    rows = ssy.rank_queries(qs, docs, [[0.0, 1.0], [1.0, 0.0]], [[1.0, 0.0], [1.0, 0.0]])
    assert rows[0]["ranks"]["vector"][0].endswith("0002")  # 벡터는 다른 문서가 1위
    assert rows[0]["ranks"]["app"][0].endswith("0001") and rows[0]["pinned"] == [qs[0]["gold"][0]]  # '…-9280'은 고정 안 됨
    assert rows[0]["metrics"]["app"]["r1"] == 1.0
    assert rows[1]["docno_miss"] and rows[1]["ranks"]["app"] == [] and rows[1]["ranks"]["rrf"]
    # 전후 비교의 '후': 지금 규칙(문서번호 고정·'없음')을 벡터만·낱말만에도 건다
    assert rows[0]["ranks"]["vector_app"][0].endswith("0001") and rows[0]["ranks"]["keyword_app"][0].endswith("0001")
    assert rows[1]["ranks"]["vector_app"] == [] == rows[1]["ranks"]["keyword_app"]
    after = ssy.summarize(rows, ssy.AFTER_METHODS)
    assert after[ssy.ALL]["vector_app"]["r1"] == 1.0 and after[ssy.ALL]["vector_app"]["n"] == 1
