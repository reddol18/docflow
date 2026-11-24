"""합성 철 답변 평가 — 질의 세트 만들기·/ask 답변 만들기의 순수 로직 (https://github.com/reddol18/docflow/issues/21).
LLM·임베딩 호출 없음. 합성 철은 가짜 라벨(생성기가 그리는 글 꼴)로 만든다."""
import csv
import importlib.util
import json
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mq():
    return _load("answer_make_queries", "eval/answer/make_queries.py")


@pytest.fixture(scope="module")
def ga():
    before = list(sys.path)
    mod = _load("answer_gen_answers", "eval/answer/gen_answers.py")
    yield mod
    sys.path[:] = before


def first_text(city, docno, date, recipient, title, case, law, company, rep, addr, result=None, dept="도시과"):
    y, m, d = date.split("-")
    head = (f"{city} (우) 638-940 {city} 시청로 72 / 전화 (0529) 87-2271 문서번호 {docno.replace('-', '- ')} "
            f"시행일자 {y[2:]}. {int(m)}. {int(d)}. ( 보존 : 3년 ) (경유) 수신 {recipient} 참조 선결 접수 제목 {title} "
            f"1. 귀사에서 {y}. {int(m)}. 1. 우리 시에 제출한 {case} 신청에 대하여 {law} 및 관계 규정에 따라 검토한 결과를 "
            f"다음과 같이 알려 드립니다. 2. 처리 내용 가. 신청인 : 주식회사 {company} (대표 {rep}) 나. 위 치 : {addr}")
    if result is None:  # 본문이 다음 쪽으로 넘어감
        return head + " - 1 -"
    return head + (f" 다. 처리결과 : {result} 3. 위 내용과 관련하여 이의가 있을 때에는 끝. {city}장 기안자 김하태 "
                   f"{dept}-{docno.rsplit('-', 1)[1]} ( {y}. {int(m)}. {int(d)}. ) 접수 건축과-370 {addr}")


def cont_text(result, dept, serial, y):
    return (f"다. 처리결과 : {result} 3. 위 내용과 관련하여 행정심판을 청구할 수 있음을 알려 드립니다. 끝. 새솔시장 "
            f"{dept}-{serial} ( {y}. 1. 1. ) 접수 민원실-1 - 2 -")


def write_bundle(root, vol, pages, gt_rows, synthetic=True):
    d = Path(root) / vol
    d.mkdir(parents=True)
    meta = {"vol": vol, "synthetic": synthetic, "pages": [{"page": p, "kind": k} for p, k, _ in pages]}
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    for p, _, text in pages:
        ann = [{"annotation.text": w} for w in text.split(" ") if w]
        (d / f"{p}.json").write_text(json.dumps({"annotations": ann}, ensure_ascii=False), encoding="utf-8")
    with (d / "gt.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["page", "is_start", "docno", "date", "recipient", "title"])
        w.writeheader()
        w.writerows(gt_rows)
    return d


def sample_bundle(tmp_path):
    vol = "SYNTH9001-1998-0001"
    p1, p2, p3, p4 = (f"{vol}-{n:04d}" for n in range(1, 5))
    a = dict(city="새솔시", docno="도시 13010-2918", date="1998-01-14", recipient="송민성 귀하", title="도로점용허가 신청에 대한 회신",
             case="도로점용허가", law="도로법 제40조", company="한결유통", rep="송민성", addr="새솔시 새터길 276")
    b = dict(city="새솔시", docno="건축 56120-624", date="1998-03-05", recipient="내부결재", title="건축허가 신청에 대한 처리 결과",
             case="건축허가", law="건축법 제8조", company="가온건설", rep="송호성", addr="새솔시 중앙로 3")
    pages = [(p1, "first", first_text(**a, result="조건부 허가")),
             (p2, "first", first_text(**b)), (p3, "cont", cont_text("보완 요구", "건축과", "624", "1998")),
             (p4, "attach", "[별지 제8호 서식] 건축허가 검토서")]
    gt = [{"page": p1, "is_start": "Y", **{k: a[k] for k in ("docno", "date", "recipient", "title")}},
          {"page": p2, "is_start": "Y", **{k: b[k] for k in ("docno", "date", "recipient", "title")}},
          {"page": p3, "is_start": "N"}, {"page": p4, "is_start": "N"}]
    return write_bundle(tmp_path / "bundles", vol, pages, gt), vol, (p1, p2)


def test_facts_from_rendered_words(mq, tmp_path):
    d, vol, (p1, p2) = sample_bundle(tmp_path)
    meta, texts, gt = mq.read_bundle(d)
    facts = mq.doc_facts(meta, texts, gt, indexed=[p1])
    assert [f["key"] for f in facts] == [f"{vol}/{p1}", f"{vol}/{p2}"]
    f1, f2 = facts
    assert (f1["city"], f1["company"], f1["rep"], f1["addr"], f1["result"], f1["dept"], f1["law"]) == \
        ("새솔시", "한결유통", "송민성", "새솔시 새터길 276", "조건부 허가", "도시과", "도로법 제40조")
    assert (f1["cont"], f1["attach"], f1["indexed"], f1["date_ko"], f1["year"]) == (False, False, True, "1998년 1월 14일", 1998)
    # 본문이 넘어간 문서: 처리결과·부서는 이어지는 쪽에서
    assert (f2["result"], f2["dept"], f2["cont"], f2["attach"], f2["indexed"]) == ("보완 요구", "건축과", True, True, False)


def test_non_synthetic_bundle_refused(mq, tmp_path):
    d = write_bundle(tmp_path, "SYNTH9002-1995-0001", [("SYNTH9002-1995-0001-0001", "first", "x")], [], synthetic=False)
    with pytest.raises(mq.NotSynthetic):
        mq.read_bundle(d)


def test_missing_fact_is_an_error(mq, tmp_path):
    vol = "SYNTH9003-1995-0001"
    d = write_bundle(tmp_path, vol, [(f"{vol}-0001", "first", "새솔시 글자만")],
                     [{"page": f"{vol}-0001", "is_start": "Y", "docno": "도시 1-1", "date": "1995-01-01", "recipient": "r", "title": "t"}])
    with pytest.raises(ValueError, match="신청인"):
        mq.doc_facts(*mq.read_bundle(d))


def fake_facts(n=120, seed=3):
    """사실표 모양의 가짜 문서 n건 — 생성기 범위(도시 4·회사 6·건 5·연도 1991~1998)와 같은 다양성"""
    r = random.Random(seed)
    cities, companies = ["한빛시", "새솔시", "가람군", "누리시"], ["가온건설", "다솜산업", "마루상사", "바름개발", "새빛전기", "한결유통"]
    cases = ["건축허가", "옥외광고물 표시허가", "도로점용허가", "공장설립 승인", "영업신고 수리"]
    out, seen = [], set()
    for i in range(n):
        vol_n = i // 5
        city, year = cities[vol_n % 4], 1991 + vol_n % 8
        while True:
            docno = f"{r.choice(['건축', '지경', '도시', '환경'])} {r.choice(['55142', '13010'])}-{r.randint(100, 2999)}"
            if docno not in seen:
                seen.add(docno)
                break
        m, d = r.randint(1, 12), r.randint(1, 28)
        vol = f"SYNTH{9100 + vol_n}-{year}-0001"
        doc_id = f"{vol}-{i % 5 + 1:04d}"
        rep = r.choice("김이박") + r.choice("민서준") + r.choice("도현지")
        out.append({"key": f"{vol}/{doc_id}", "vol": vol, "doc_id": doc_id, "city": city, "year": year,
                    "date": f"{year}-{m:02d}-{d:02d}", "date_ko": f"{year}년 {m}월 {d}일", "docno": docno,
                    "recipient": "내부결재", "title": "t", "case": r.choice(cases), "law": "법 제1조",
                    "company": r.choice(companies), "rep": rep, "addr": f"{city} 중앙로 {i}", "result": "허가",
                    "dept": "도시과", "cont": r.random() < 0.3, "attach": False, "indexed": r.random() < 0.92})
    return out


def test_build_counts_and_grounding(mq):
    facts = fake_facts()
    qs = mq.build(facts, seed=21)
    st = mq.stats(qs)
    assert (st["total"], st["answerable"], st["unanswerable"]) == (42, 32, 10)
    assert mq.build(facts, seed=21) == qs  # 같은 시드 같은 세트
    by_key = {f["key"]: f for f in facts}
    used = [s for q in qs for s in q["source_docs"]]
    assert len(used) == len(set(used))  # 한 문서는 한 질의에만
    for q in qs:
        assert mq.QUERY_ID.fullmatch(q["id"]) and set(q) == mq.QUERY_KEYS
        assert q["answerable"] is bool(q["source_docs"])
        for s in q["source_docs"]:
            assert by_key[s]["indexed"]  # 운영에서 따로 나뉜 문서만 근거로
        if q["type"] in ("처리결과", "요약"):  # 처리결과가 첫 쪽(/ask 근거)에 있는 문서만
            assert not by_key[q["source_docs"][0]]["cont"]
        if q["type"] == "처리결과":
            assert q["answer"] == by_key[q["source_docs"][0]]["result"]
        if q["type"] == "여러 문서":
            assert 2 <= len(q["source_docs"]) <= 3
    for q in qs:  # 답 없음: 질문의 문서번호·회사·대표가 사실표에 없다
        if not q["answerable"]:
            assert q["answer"] == mq.NO_ANSWER
            assert not any(f["docno"] in q["question"] for f in facts)


def test_unique_clue(mq):
    facts = fake_facts()
    f = facts[0]
    twin = dict(f, key="SYNTH9999-1991-0001/x", docno="다른 1-1")
    assert mq.unique_by(facts, f, ("docno",))
    assert not mq.unique_by([*facts, twin], f, ("company", "rep", "case"))


def test_josa(mq):
    assert mq.josa("도시과", "이", "가") == "가"
    assert mq.josa("송민성", "이", "가") == "이"
    assert mq.josa("ABC", "이", "가") == "이"


def test_load_queries_only_synthetic(mq, tmp_path):
    ok = {"id": "a01", "type": "수신", "question": "q", "answer": "a", "source_docs": ["SYNTH4711-1998-0001/SYNTH4711-1998-0001-0001"],
          "answerable": True}
    p = tmp_path / "q.jsonl"
    p.write_text(json.dumps(ok, ensure_ascii=False) + "\n", encoding="utf-8")
    assert mq.load_queries(p) == [ok]
    bad = [{"id": "q01", "type": "정확 조회", "q": "x", "relevant": ["C0001_1994_0001-0001"], "must_cite": []},  # 실제 자료 세트 꼴
           {**ok, "source_docs": ["C0012_1994_0001/p-0001"]},  # 합성 아닌 문서
           {**ok, "answerable": False},  # 근거가 있는데 답 없음
           {**ok, "extra": 1}]
    for b in bad:
        p.write_text(json.dumps(b, ensure_ascii=False) + "\n", encoding="utf-8")
        with pytest.raises(mq.NotSynthetic):
            mq.load_queries(p)


def hits_fixture(ga):
    docs = {("SYNTH1-1991-0001", "SYNTH1-1991-0001-0001"): {"values": {"docno": "도시 1-100", "date": "1991-01-02",
                                                                         "recipient": "내부결재", "title": "건축허가"}},
            ("SYNTH1-1991-0001", "SYNTH1-1991-0001-0003"): {"values": {"docno": "도시 1-101", "date": "1991-02-02",
                                                                         "recipient": "r", "title": "t"}}}
    keys = list(docs)
    bodies = {keys[0]: "가" * 700, keys[1]: "본문 둘"}
    scores = {keys[0]: 0.03, keys[1]: 0.02}
    return ga.ask_hits([keys[0], keys[1]], docs, bodies, scores, [keys[1]], [keys[0], keys[1]]), keys


def test_ask_hits_and_context_match_answer_messages(ga):
    from app import llm
    hits, keys = hits_fixture(ga)
    assert [(h["vol"], h["doc_id"], h["vector_rank"], h["keyword_rank"]) for h in hits] == \
        [(*keys[0], None, 1), (*keys[1], 1, 2)]
    assert set(hits[0]["fields"]) == set(llm.fields.shown(ga.ec.SCHEMA))
    msgs = llm.answer_messages("질문", hits, ga.ec.SCHEMA)
    block = msgs[1]["content"]
    ctx = "\n\n".join(ga.context_of(h) for h in hits)
    assert ctx in block  # 넘긴 글 = 결과 파일의 context (본문 600자 자름까지)
    assert "가" * 601 not in ctx


def test_cited_keys_and_totals(ga):
    assert ga.cited_keys("답 [SYNTH1-1991-0001/SYNTH1-1991-0001-0001] 그리고 [SYNTH1-1991-0001/SYNTH1-1991-0001-0001]") == \
        ["SYNTH1-1991-0001/SYNTH1-1991-0001-0001"]
    rows = [{"answerable": True, "source_docs": ["a/b"], "retrieved": [{"key": "a/b"}], "usage": {"prompt_tokens": 100,
             "completion_tokens": 10}, "usd": 0.001, "model": "m-1", "dropped_citations": 1},
            {"answerable": True, "source_docs": ["a/c", "a/d"], "retrieved": [{"key": "a/c"}],
             "usage": {"prompt_tokens": 50, "completion_tokens": 5}, "usd": 0.0005, "model": "m-1", "dropped_citations": 0},
            {"answerable": False, "source_docs": [], "error": "APIError"}]
    t = ga.totals(rows)
    assert (t["n"], t["failed"], t["prompt_tokens"], t["completion_tokens"], t["usd"], t["models"]) == (3, 1, 150, 15, 0.0015, ["m-1"])
    assert t["retrieved_all_sources"] == "1/2" and t["dropped_citations"] == 1


def test_check_sources(ga):
    qs = [{"source_docs": ["V/a"]}, {"source_docs": []}]
    ga.check_sources(qs, [("V", "a")])
    with pytest.raises(SystemExit):
        ga.check_sources(qs, [("V", "b")])
