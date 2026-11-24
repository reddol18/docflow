"""검색 순위 (https://github.com/reddol18/docflow/issues/61): app/retrieval.py 순수 함수와 /search·/ask의 문서번호 고정.
색인·LLM 없이 — 저장소 함수는 가짜로 바꾼다. 값은 지어낸 것."""
from app import retrieval as rt


def test_query_words():
    assert rt.query_words("1997년 도로 점용 허가 가") == ["도로", "점용", "허가"]
    assert rt.query_words("1997 공장설립") == ["공장설립"]


def test_docnos_in_sentence_and_bare():
    assert rt.docnos_in("허가 55477-258") == [("허가", "55477-258")]
    assert rt.docnos_in("문서번호 지경 56120-275 문서의 수신자는 누구인가?") == [("지경", "56120-275")]
    assert rt.docnos_in("지경56120-275, 도시 13010 - 2704 둘 다") == [("지경", "56120-275"), ("도시", "13010-2704")]
    assert rt.docnos_in("지경 56120-275 지경 56120-275") == [("지경", "56120-275")]
    # 날짜·연도·긴 번호의 일부는 문서번호가 아니다
    assert rt.docnos_in("1995년 12월 24일에 시행") == []
    assert rt.docnos_in("시행 1995-12-24") == []
    assert rt.docnos_in("도로점용허가 신청 회신") == []


def test_docno_number_exact():
    assert rt.docno_number("지경 56120-804") == "56120-804"
    assert rt.docno_number("지경 56120 - 8040") == "56120-8040"
    assert rt.docno_number("") == "" and rt.docno_number("내부결재") == ""
    assert rt.docno_head("지경 56120-804") == "지경" and rt.docno_head("56120-804") == ""
    cands = [(("V", "p1"), "지경 56120-8040"), (("V", "p2"), "지경 56120-804"), (("V", "p3"), "지경 156120-804")]
    assert rt.docno_matches("지경 56120-804", cands) == [("V", "p2")]  # '…-804'는 '…-8040'·'1…'과 다르다


def test_docno_head_preferred_then_number_only():
    cands = [(("V1", "a"), "지경 55142-141"), (("V2", "b"), "환경 55142-141")]
    assert rt.docno_matches("문서번호 환경 55142-141 문서", cands) == [("V2", "b")]
    # 부서 약칭이 같은 문서가 없으면 번호만 같은 문서 모두 (약칭 판독이 틀린 경우)
    assert rt.docno_matches("도시 55142-141", cands) == [("V1", "a"), ("V2", "b")]
    assert rt.docno_matches("도시 55142-142", cands) == []


def test_fuse_pins_docno_ahead_of_rrf():
    v = [("V", "x"), ("V", "y"), ("V", "z")]
    kw = [("V", "z")]
    score, order = rt.fuse(v, kw)
    assert order == [("V", "z"), ("V", "x"), ("V", "y")]  # z는 두 쪽에 다 있다
    score, order = rt.fuse(v, kw, pinned=[("V", "y"), ("V", "new")])
    assert order == [("V", "y"), ("V", "new"), ("V", "z"), ("V", "x")]
    assert score[("V", "new")] == 0.0 and score[("V", "z")] == 1 / 63 + 1 / 61
    # 행(dict) 후보도 받는다 (운영 store 결과)
    assert rt.fuse([{"vol": "V", "doc_id": "x"}], [])[1] == [("V", "x")]


def test_rank_and_no_match():
    v, kw = [("V", "x"), ("V", "y")], [("V", "y")]
    r = rt.rank("문서번호 도시 56120-252 문서의 처리결과", v, kw, [(("V", "w"), "도시 56120-2520")])
    assert r.docnos == [("도시", "56120-252")] and r.pinned == [] and r.docno_miss
    assert rt.no_match(r)
    r = rt.rank("도시 56120-252", v, kw, [(("V", "w"), "도시 56120-252")])
    assert r.order[0] == ("V", "w") and not rt.no_match(r, top_sim=0.1, threshold=0.5)  # 번호가 맞으면 '없음' 아님
    r = rt.rank("도로점용허가 회신", v, kw)
    assert r.order == [("V", "y"), ("V", "x")] and not r.docno_miss
    assert not rt.no_match(r) and not rt.no_match(r, top_sim=0.3)  # 임계값은 아직 없다 (7단계)
    assert rt.no_match(r, top_sim=0.3, threshold=0.4) and not rt.no_match(r, top_sim=0.5, threshold=0.4)


def test_docno_field():
    from app import fields
    assert rt.docno_field(fields.SCHEMA) == "docno"
    assert rt.docno_field({"fields": [{"name": "title"}]}) is None


def _fake_store(monkeypatch, docno_rows):
    from app import main
    calls = []
    monkeypatch.setattr(main.store, "vector_search", lambda vec, yf, yt, limit: [
        {"vol": "V", "doc_id": "a", "score": 0.9}, {"vol": "V", "doc_id": "b", "score": 0.8}])
    monkeypatch.setattr(main.store, "keyword_search", lambda words, yf, yt, limit: [{"vol": "V", "doc_id": "b"}])

    def docno_search(numbers, yf, yt, limit):
        calls.append((tuple(numbers), yf, yt, limit))
        return docno_rows

    monkeypatch.setattr(main.store, "docno_search", docno_search)
    monkeypatch.setattr(main.llm, "embed", lambda vol, texts, **k: [[0.0]] * len(texts))
    monkeypatch.setattr(main.store, "get_documents", lambda keys: {k: {"vol": k[0], "fields": {}} for k in keys})
    return main, calls


def test_hybrid_pins_docno_and_search_empty_on_miss(monkeypatch):
    main, calls = _fake_store(monkeypatch, [{"vol": "V", "doc_id": "c", "docno": "지경 56120-275"},
                                            {"vol": "V", "doc_id": "d", "docno": "지경 56120-2750"}])
    hits = main.hybrid("문서번호 지경 56120-275 문서의 수신자는?", k=3, empty_on_miss=True)
    assert [h["doc_id"] for h in hits] == ["c", "b", "a"]  # 번호 정확 일치 c가 맨 앞, 2750은 아님
    assert hits[0]["vector_rank"] is None and hits[0]["keyword_rank"] is None and hits[0]["rrf"] == 0.0
    assert calls == [(("56120-275",), None, None, main.DOCNO_CANDIDATES)]
    *_, order = main.ranked("문서번호 지경 56120-275 문서의 수신자는?", [0.0])
    assert order[:3] == [("V", "c"), ("V", "b"), ("V", "a")]  # 채점기(search_score)가 부르는 순서도 같다
    # 그 번호의 문서가 없다: /search는 빈 목록, /ask(hybrid 기본)는 RRF 그대로
    main, _ = _fake_store(monkeypatch, [{"vol": "V", "doc_id": "d", "docno": "지경 56120-2750"}])
    assert main.hybrid("지경 56120-275", empty_on_miss=True) == []
    assert [h["doc_id"] for h in main.hybrid("지경 56120-275")] == ["b", "a"]


def test_search_route_uses_empty_on_miss(monkeypatch):
    from app import main
    seen = {}

    def fake_hybrid(q, yf, yt, k, user, empty_on_miss=False):
        seen["empty_on_miss"] = empty_on_miss
        return []

    monkeypatch.setattr(main, "hybrid", fake_hybrid)
    monkeypatch.setattr(main, "rate_limited", lambda route, user: __import__("contextlib").nullcontext())
    assert main.search("지경 56120-275", user=None) == []
    assert seen["empty_on_miss"] is True


def test_clean_words_and_query_year():
    assert rt.clean_words("주식회사 마루상사(대표 윤서우)의 건축허가 신청을 처리한 문서는 언제 시행되었나?") == \
        ["주식회사", "마루상사", "대표", "윤서우", "건축허가", "신청", "처리한"]  # '허가'의 '가'는 조사가 아니다
    assert rt.clean_words("새솔시에서 1995년 12월 24일에 시행한 공장설립 승인") == ["새솔시", "시행한", "공장설립", "승인"]
    assert rt.clean_words("문서번호 지경 56120-275 문서의 수신자는 누구인가?") == ["지경", "56120-275"]
    assert rt.strip_particle("도로") == "도로" and rt.strip_particle("가람군이") == "가람군"  # 떼고 두 글자 이상만
    on = rt.Config(clean=True, year=True)
    assert rt.query_terms("1995년 도로 점용", rt.Config()) == ["도로", "점용"]
    assert rt.query_year("새솔시가 1995년에 시행한", on) == 1995
    assert rt.query_year("1995년 1996년", on) is None and rt.query_year("1995년", rt.Config()) is None
    assert rt.query_year("1995-12-24 시행", on) is None
    assert on.name == "clean_year_kw1" and rt.Config(kw_weight=0.5).name == "kw0.5"


def test_weighted_fuse_and_vector_only():
    v, kw = [("V", "a"), ("V", "b"), ("V", "c")], [("V", "c"), ("V", "d")]
    assert rt.fuse(v, kw)[1] == [("V", "c"), ("V", "a"), ("V", "b"), ("V", "d")]
    s, order = rt.fuse(v, kw, kw_weight=0.5)
    assert order == [("V", "c"), ("V", "a"), ("V", "b"), ("V", "d")] and s[("V", "d")] == 0.5 / 62
    s, order = rt.fuse(v, kw, kw_weight=0)  # 벡터만 — 낱말에만 있는 d는 빠진다
    assert order == v and ("V", "d") not in s
    assert rt.fuse(v, kw, pinned=[("V", "d")], kw_weight=0)[1][0] == ("V", "d")  # 문서번호 고정은 그대로
    assert rt.no_match(rt.Ranking([], [], {}, []))  # 후보가 하나도 없으면 '없음'


def test_hybrid_query_year_threshold_and_ask_fallback(monkeypatch):
    from app import main
    calls = []

    def vector_search(vec, yf, yt, limit):
        calls.append((yf, yt))
        return [] if yf == 1990 else [{"vol": "V", "doc_id": "a", "score": 0.3}, {"vol": "V", "doc_id": "b", "score": 0.2}]

    monkeypatch.setattr(main.store, "vector_search", vector_search)
    monkeypatch.setattr(main.store, "keyword_search", lambda words, yf, yt, limit: [])
    monkeypatch.setattr(main.llm, "embed", lambda vol, texts, **k: [[0.0]])
    monkeypatch.setattr(main.store, "get_documents", lambda keys: {k: {"vol": k[0], "fields": {}} for k in keys})
    monkeypatch.delenv("SEARCH_NO_MATCH_THRESHOLD", raising=False)
    monkeypatch.delenv("EMBED_MODEL", raising=False)
    monkeypatch.delenv("EMBED_DIM", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    # 질의 연도 → 연도 칸 (화면 연도 칸이 비었을 때만)
    main.hybrid("1995년 도로 점용", k=2)
    main.hybrid("1995년 도로 점용", 1990, 1999, k=2)
    assert calls[-2:] == [(1995, 1995), (1990, 1999)]
    # 질의 연도로 걸러 비면 /search·/ask 둘 다 연도 없이 다시 찾는다 (2026-11-24 결정)
    assert [h["doc_id"] for h in main.hybrid("1990년 영업신고", empty_on_miss=True)] == ["a", "b"]
    assert calls[-2:] == [(1990, 1990), (None, None)]
    assert [h["doc_id"] for h in main.hybrid("1990년 영업신고")] == ["a", "b"] and calls[-1] == (None, None)
    # 화면 연도 칸으로 비면 다시 찾지 않는다: /search '없음'
    assert main.hybrid("영업신고", 1990, 1990, empty_on_miss=True) == [] and calls[-1] == (1990, 1990)
    # 유사도 하한: 기본은 끔
    assert main.no_match_threshold() is None and len(main.hybrid("도로 점용", empty_on_miss=True)) == 2
    # auto = 지금 모델(te3-small 0.34) → 1위 0.3이면 /search '없음', /ask는 그대로
    monkeypatch.setenv("SEARCH_NO_MATCH_THRESHOLD", "auto")
    assert main.no_match_threshold() == rt.NO_MATCH_THRESHOLD["text-embedding-3-small"]
    assert main.hybrid("도로 점용", empty_on_miss=True) == []
    assert len(main.hybrid("도로 점용")) == 2
    monkeypatch.setenv("SEARCH_NO_MATCH_THRESHOLD", "0.25")
    assert len(main.hybrid("도로 점용", empty_on_miss=True)) == 2
    monkeypatch.setenv("SEARCH_NO_MATCH_THRESHOLD", "off")
    assert main.no_match_threshold() is None


def test_no_docno_query_skips_docno_lookup(monkeypatch):
    main, calls = _fake_store(monkeypatch, [])
    main.hybrid("도로점용허가 회신")
    assert calls == []
