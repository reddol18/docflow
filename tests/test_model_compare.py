"""비전·임베딩 모델 비교 (https://github.com/reddol18/docflow/issues/80 조건 4·5).
eval/hw_compare.py·eval/embed_compare.py·eval/compare_common.py.
모델을 부르지 않는다 — 가짜 클라이언트로 판정·예산·합성 철 막기·순위 계산만 본다."""
import importlib.util
import json
import random
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parent.parent
EVAL = ROOT / "eval"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mods():
    before = list(sys.path)
    m = SimpleNamespace(cc=_load("compare_common", EVAL / "compare_common.py"),
                        hw=_load("hw_compare", EVAL / "hw_compare.py"),
                        em=_load("embed_compare", EVAL / "embed_compare.py"),
                        render=_load("synth_render", EVAL / "synth" / "render.py"),
                        bundle=None)
    yield m
    sys.path[:] = before


def _work(tmp_path, vols, manifest=None):
    w = tmp_path / "work"
    (w / "platform").mkdir(parents=True)
    for v in vols:
        (w / "platform" / f"{v}.json").write_text("[]", encoding="utf-8")
    man = vols if manifest is None else manifest
    (w / "synth_manifest.json").write_text(json.dumps({"vols": [{"vol": v} for v in man]}), encoding="utf-8")
    return w


# ---------- 합성 철만 · 주소 ----------

def test_synthetic_only(mods, tmp_path):
    cc = mods.cc
    w = _work(tmp_path, ["SYNTH0001-1995-0001"])
    assert cc.synthetic_vols(w) == ["SYNTH0001-1995-0001"]
    w2 = _work(tmp_path / "b", ["SYNTH0001-1995-0001", "5350109-1997-0002"], manifest=["SYNTH0001-1995-0001"])
    with pytest.raises(cc.NotSynthetic):
        cc.synthetic_vols(w2)  # 목록에 없는(실제 자료) 철이 섞임
    with pytest.raises(cc.NotSynthetic):
        cc.synthetic_vols(_work(tmp_path / "c", ["5350109-1997-0002"]))  # 목록에 있어도 SYNTH 아님
    assert cc.synthetic_vols(w2, ["SYNTH0001-1995-0001"]) == ["SYNTH0001-1995-0001"]
    with pytest.raises(cc.NotSynthetic):  # 합성 철 폴더 meta.json이 없거나 synthetic이 아님
        cc.synthetic_vols(w, bundles=tmp_path / "nobundle")
    bd = tmp_path / "bundles" / "SYNTH0001-1995-0001"
    bd.mkdir(parents=True)
    (bd / "meta.json").write_text(json.dumps({"vol": "SYNTH0001-1995-0001", "synthetic": True}), encoding="utf-8")
    assert cc.synthetic_vols(w, bundles=tmp_path / "bundles") == ["SYNTH0001-1995-0001"]


def test_poc_work_refused(mods, monkeypatch, tmp_path):
    monkeypatch.setattr(mods.cc, "POC_WORK", tmp_path)
    with pytest.raises(mods.cc.NotSynthetic):
        mods.cc.synthetic_vols(tmp_path)


@pytest.mark.parametrize("url,ok", [
    ("https://abc-11434.proxy.runpod.net/v1", True), ("http://127.0.0.1:18434/v1", True), ("http://localhost:11434/v1/", True),
    ("http://10.0.0.5:11434/v1", False), ("http://example.com/v1", False), ("https://u:p@example.com/v1", False),
    ("ftp://127.0.0.1/v1", False)])
def test_base_url(mods, url, ok):
    if ok:
        assert mods.cc.check_base_url(url) == url.rstrip("/")
    else:
        with pytest.raises(SystemExit):
            mods.cc.check_base_url(url)


def test_out_dir_outside_repo(mods, tmp_path):
    with pytest.raises(SystemExit):
        mods.cc.out_dir(ROOT, "hw_test_should_not_exist")
    assert mods.cc.out_dir(tmp_path, "hw").is_dir()


def test_budget(mods):
    b = mods.cc.Budget(0.01)
    assert b.reserve(0.006)
    assert not b.reserve(0.006)  # 잡아 둔 몫까지 세면 넘는다
    b.settle(0.006, 0.001)
    assert b.reserve(0.006) and abs(b.spent - 0.001) < 1e-12


# ---------- 손글씨 칸 정답 (생성기) ----------

needs_font = pytest.mark.skipif(not Path("C:/Windows/Fonts/batang.ttc").is_file(), reason="바탕 글꼴(Windows)이 없음")


@needs_font
@pytest.mark.parametrize("seed", range(6))
def test_generator_hand_cells(mods, seed):
    """손글씨 단어마다 칸 이름(cell)이 붙고, 같은 칸 단어를 이으면 그 칸에 쓴 글이다"""
    render = mods.render
    nof = render.Fonts(hand=[])
    r = random.Random(seed)
    c = render.new_case(r)
    p = render.first_page(c, nof, random.Random(f"{seed}-p"))
    hand = [a for a in p.ann if a["ttype"] == "handwritten"]
    assert hand and all(a.get("cell") in render.HAND_CELLS for a in hand)
    cells = {}
    for a in hand:
        cells.setdefault(a["cell"], []).append(a["text"])
    assert "".join(cells["serial"]) == c.serial
    assert " ".join(cells["date"]) == f"{c.year % 100}. {c.month}. {c.day}."
    assert all(k in cells for k in render.SIGN_CELLS)
    assert all(a.get("cell") is None for a in p.ann if a["ttype"] != "handwritten")
    a2 = render.attach_page(c, nof, random.Random(seed))
    assert [a["cell"] for a in a2.ann if a["ttype"] == "handwritten"] == ["sign_attach"]


@needs_font
def test_label_cells_roundtrip(mods):
    """bundle.label의 annotation.cell → hw_compare.label_cells: 칸 글자는 이어지고 상자는 합쳐진다"""
    sys.path.insert(0, str(EVAL / "synth"))
    bundle = _load("synth_bundle", EVAL / "synth" / "bundle.py")
    render = mods.render
    r = random.Random(5)
    c = render.new_case(r)
    p = render.first_page(c, render.Fonts(hand=[]), random.Random("5-p"))
    boxes = [a["box"] for a in p.ann]
    lab = bundle.label("SYNTH0005-1995-0001-0001", 10, 10, c.year, p.ann, boxes)
    got = mods.hw.label_cells(lab)
    assert got["serial"][0] == c.serial
    assert got["date"][0] == f"{c.year % 100}. {c.month}. {c.day}."
    words = [a["box"] for a in p.ann if a.get("cell") == "date"]
    assert got["date"][1] == [min(b[0] for b in words), min(b[1] for b in words), max(b[2] for b in words), max(b[3] for b in words)]


# ---------- 판정 ----------

@pytest.mark.parametrize("cell,truth,text,ok", [
    ("serial", "1558", "지경 55142-1558", True),
    ("serial", "1558", "1558", False),  # '-'가 없으면 운영(merge)은 칸 판독 일련번호를 쓰지 않는다
    ("serial", "1558", "지경 55142-1588", False),
    ("date", "1997-05-15", "97. 5. 15.", True),
    ("date", "1997-05-15", "97. 5. 16.", False),
    ("recv_date", "3. 15.", "3.15", True),
    ("sign_drafter", "김민서", "김 민 서", True),
    ("sign_drafter", "김민서", "", False),
])
def test_judge(mods, cell, truth, text, ok):
    assert mods.hw.judge(cell, truth, text) is ok


class FakeChat:
    def __init__(self, answers, logprobs_ok=True):
        self.answers, self.logprobs_ok, self.calls = list(answers), logprobs_ok, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.calls.append(kw)
        if "logprobs" in kw and not self.logprobs_ok:
            raise ValueError("logprobs not supported")
        text = self.answers.pop(0)
        if isinstance(text, Exception):
            raise text
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=300, completion_tokens=5),
                               choices=[SimpleNamespace(message=SimpleNamespace(content=text), logprobs=None)])


def _cell(cell="serial", truth="1558", png=b"x"):
    return {"vol": "SYNTH0001-1995-0001", "doc_id": "d", "page": "p", "cell": cell, "kind": "prod", "truth": truth,
            "hard": False, "box": [0, 0, 1, 1], "box_fail": "", "png": png}


def test_reader_prices_and_judges(mods):
    fake = FakeChat(["지경 55142-1558"])
    rd = mods.hw.Reader(fake, "gpt-4.1-mini", True, mods.cc.Budget(1.0))
    r = rd.read(_cell())
    assert r["ok"] and r["tin"] == 300 and abs(r["usd"] - (300 * 0.40 + 5 * 1.60) / 1e6) < 1e-12
    kw = fake.calls[0]
    assert kw["temperature"] == 0 and kw["max_tokens"] == 40 and kw["logprobs"] is True
    from app import llm
    assert kw["messages"][0]["content"][0]["text"] == llm.CELL_PROMPT["docno"]  # 운영 지시문 그대로
    assert kw["messages"][0]["content"][1]["image_url"]["detail"] == "high"
    assert "png" not in r


def test_reader_budget_and_errors(mods):
    rd = mods.hw.Reader(FakeChat([]), "gpt-4.1-mini", True, mods.cc.Budget(0.0))
    assert rd.read(_cell())["error"] == "budget"  # 상한을 넘을 호출은 부르지 않는다
    assert rd.read(_cell(png=None))["error"] == "box_fail"
    rd2 = mods.hw.Reader(FakeChat([RuntimeError("down")]), "qwen2.5vl:7b", False, mods.cc.Budget(0.0))
    r = rd2.read(_cell())
    assert r["error"] == "RuntimeError" and not r["ok"] and r["usd"] == 0.0


def test_reader_logprobs_fallback(mods):
    fake = FakeChat(["97. 5. 15.", "97. 5. 15."], logprobs_ok=False)
    rd = mods.hw.Reader(fake, "qwen2.5vl:7b", False, mods.cc.Budget(0.0))
    assert rd.read(_cell("date", "1997-05-15"))["ok"]
    assert rd.logprobs is False and "logprobs" not in fake.calls[-1]
    assert rd.read(_cell("date", "1997-05-15"))["ok"] and len(fake.calls) == 3


def test_reader_logprobs_fallback_concurrent(mods):
    """logprobs를 받지 않는 서버를 동시에 부름: 첫 호출들이 함께 실패해도(다른 스레드가 플래그를 먼저 바꿈) 칸마다 빼고 다시 부른다"""
    import threading
    n = 4
    gate = threading.Barrier(n)
    lock = threading.Lock()

    class Slow(FakeChat):
        def create(self, **kw):
            if "logprobs" in kw:
                with lock:
                    self.calls.append(kw)
                gate.wait(timeout=5)  # 넷이 모두 logprobs를 넣어 부른 뒤에 함께 실패
                raise ValueError("logprobs not supported")
            with lock:
                self.calls.append(kw)
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
                                   choices=[SimpleNamespace(message=SimpleNamespace(content="97. 5. 15."), logprobs=None)])

    rd = mods.hw.Reader(Slow([]), "qwen2.5vl:7b", False, mods.cc.Budget(0.0))
    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(n) as ex:
        rows = list(ex.map(rd.read, [_cell("date", "1997-05-15")] * n))
    assert all(r["ok"] and not r["error"] for r in rows)
    assert rd.logprobs is False


def test_reader_retry_latency_is_last_call(mods, monkeypatch):
    clock = iter([0.0, 10.0, 10.5, 11.0])  # read 시작 0, 첫 호출 10(실패), 다시 부름 10.5, 끝 11 → 0.5초
    monkeypatch.setattr(mods.hw.time, "perf_counter", lambda: next(clock))
    rd = mods.hw.Reader(FakeChat(["97. 5. 15."], logprobs_ok=False), "qwen2.5vl:7b", False, mods.cc.Budget(0.0))
    assert rd.read(_cell("date", "1997-05-15"))["ms"] == 500


def test_reader_keeps_cost_when_response_bad(mods):
    """응답을 받은(과금된) 뒤 모양이 틀려 실패해도 결과 줄에 토큰·비용이 남는다 (예산 정산과 같은 값)"""
    class Empty(FakeChat):
        def create(self, **kw):
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=300, completion_tokens=5), choices=[])

    b = mods.cc.Budget(1.0)
    r = mods.hw.Reader(Empty([]), "gpt-4.1-mini", True, b).read(_cell())
    assert r["error"] == "IndexError" and r["tin"] == 300 and r["usd"] > 0
    assert abs(b.spent - r["usd"]) < 1e-15 and b.held == pytest.approx(0.0)


def _gt(work):
    (work / "gt").mkdir(parents=True, exist_ok=True)
    (work / "gt" / "review.csv").write_text("page,확인,is_start,docno,date,recipient,title,check,note,saved\n", encoding="utf-8-sig")


@pytest.mark.parametrize("gen,msg", [("synth-0.1", "생성기 판"), ("synth-0.2", r"정답\(annotation\.cell\)이 없는 철")])
def test_collect_refuses_bundles_without_cells(mods, tmp_path, gen, msg):
    """예전 판(칸 이름 없음) 철이 섞이면 기타 칸이 조용히 빠지지 않게 철 단위로 멈춘다"""
    vol = "SYNTH0001-1995-0001"
    w = _work(tmp_path, [vol])
    _gt(w)
    bd = tmp_path / "bundles" / vol
    bd.mkdir(parents=True)
    (bd / "meta.json").write_text(json.dumps({"vol": vol, "synthetic": True, "generator": gen}), encoding="utf-8")
    with pytest.raises(SystemExit, match=msg):
        mods.hw.collect(w, tmp_path / "bundles", [vol])


def test_bundle_version_marks_cells(mods):
    sys.path.insert(0, str(EVAL / "synth"))
    bundle = _load("synth_bundle_v", EVAL / "synth" / "bundle.py")
    assert bundle.VERSION in mods.hw.CELL_GENERATORS


@pytest.mark.parametrize("n,want", [(1, 1), (20, 19), (30, 29), (49, 47), (100, 95)])
def test_p95_nearest_rank(mods, n, want):
    assert mods.cc.p95(list(range(1, n + 1))) == want


def test_vols_arg_and_disk_check(mods, tmp_path, monkeypatch):
    assert mods.cc.vols_arg(" A, B ,,") == ["A", "B"]
    monkeypatch.setattr(mods.cc.shutil, "disk_usage", lambda p: SimpleNamespace(free=1 * 1024**3))
    with pytest.raises(SystemExit, match="디스크"):
        mods.cc.out_dir(tmp_path, "hw")


def test_summary_and_table(mods):
    rows = [{**_cell(), "text": "", "ok": True, "conf": 0, "tin": 10, "tout": 1, "usd": 0.001, "ms": 100, "error": ""},
            {**_cell(), "hard": True, "text": "", "ok": False, "conf": 0, "tin": 10, "tout": 1, "usd": 0.001, "ms": 300, "error": ""},
            {**_cell("sign_drafter", "김민서"), "text": "", "ok": True, "conf": 0, "tin": 0, "tout": 0, "usd": 0, "ms": 0,
             "error": "box_fail"}]
    s = mods.hw.summarize(rows)
    assert s["serial"] == {"n": 2, "ok": 1, "n_legible": 1, "ok_legible": 1}
    assert s["sign"]["n"] == 1 and s["_"]["box_fail"] == 1 and s["_"]["ms_mean"] == 200
    t = mods.hw.table([{"model": "m", "where": "OpenAI", "summary": s}])
    assert "1/2 (1/1)" in t


# ---------- 임베딩 ----------

def test_query_words_rrf_match_app(mods):
    from app import main, store
    for q in ["도로점용허가 1998년 회신", "a 건축 허가", "1997 공장설립"]:
        assert mods.em.query_words(q) == main.query_words(q)
    v = [{"vol": "S", "doc_id": "a"}, {"vol": "S", "doc_id": "b"}]
    kw = [{"vol": "S", "doc_id": "b"}]
    assert mods.em.rrf([store.doc_key(x) for x in v], [store.doc_key(x) for x in kw]) == main.rrf(v, kw)


def _docs(em):
    vals = [("건축 55142-1", "1995-01-02", "주식회사 가온건설 대표 김민서", "건축허가 신청에 대한 처리 결과"),
            ("도시 13010-2", "1998-03-04", "내부결재", "도로점용허가 신청에 대한 회신"),
            ("환경 45010-3", "1998-05-06", "주식회사 다솜산업 대표 이서준", "도로점용허가 신청에 대한 회신 알림")]
    out = []
    for i, (dn, dt, rc, ti) in enumerate(vals):
        v = {"docno": dn, "date": dt, "recipient": rc, "title": ti}
        out.append({"key": ("SYNTH0001-1995-0001", f"p{i}"), "values": v, "text": "",
                    "search_text": em.fields.search_text(em.SCHEMA, v), "year": em.fields.year_of(em.SCHEMA, v)})
    return out


def test_keyword_vector_relevant(mods):
    em = mods.em
    docs = _docs(em)
    assert em.keyword_rank(["도로점용허가", "알림"], docs) == [docs[2]["key"], docs[1]["key"]]
    assert em.keyword_rank(["도로점용허가"], docs, year=1995) == []
    keys, sims = em.vector_rank([1, 0], docs, [[1, 0], [0, 1], [1, 1]])
    assert keys == [docs[0]["key"], docs[2]["key"], docs[1]["key"]] and sims[0] == pytest.approx(1.0)
    assert em.vector_rank([1, 0], docs, [[1, 0], [0, 1], [1, 1]], year=1998)[0] == [docs[2]["key"], docs[1]["key"]]
    assert em.relevant({"match": {"title": "도로점용", "year": 1998}}, docs) == [docs[1]["key"], docs[2]["key"]]
    assert em.relevant({"match": {}}, docs) == []


def test_overlap_and_summary(mods):
    em = mods.em
    assert em.overlap(list("abcde"), list("abxyz")) == 0.4
    assert em.overlap(list("ab"), list("ab")) == 1.0  # 결과가 5개보다 적으면 그 수로 나눈다
    docs = _docs(em)
    qs = [{"id": "q1", "type": "주제", "q": "도로점용허가", "match": {"title": "도로점용"}},
          {"id": "q2", "type": "답 없음", "q": "인사 발령", "match": {}}]
    ranks = em.rank_all(qs, docs, [[1, 0], [0, 1], [0.9, 0.1]], [[0, 1], [1, 0]])
    s, rel = em.summarize(ranks, qs, docs, base=ranks)
    assert s["전체"]["overlap5_rrf"] == 1.0 and s["주제"]["recall5_rrf"] == 1.0 and s["답 없음"]["recall5_rrf"] is None
    assert rel["q2"] == []


@pytest.mark.parametrize("line", [
    # 실제 자료 질의 세트 형식 (eval/data/search_queries.jsonl): 정답 문서 id·참고 답·근거 칸
    '{"id": "q01", "type": "정확 조회", "q": "가상 12345-1", "relevant": ["x"], "must_cite": ["x"], "answer": "a"}',
    '{"id": "s01", "type": "주제", "q": "가", "match": {}, "relevant": ["x"]}',
    '{"id": "q01", "type": "주제", "q": "가", "match": {}}',  # id 형식
    '{"id": "s01", "type": "주제", "q": "가", "match": {"body": "x"}}',  # 모르는 규칙 칸
    'not json',
])
def test_queries_must_be_synthetic_format(mods, tmp_path, line):
    p = tmp_path / "q.jsonl"
    p.write_text(line + "\n", encoding="utf-8")
    with pytest.raises(mods.em.NotSyntheticQueries):
        mods.em.load_queries(p)


def test_real_query_set_refused(mods):
    real = ROOT / "eval" / "data" / "search_queries.jsonl"
    if real.exists():
        with pytest.raises(mods.em.NotSyntheticQueries):
            mods.em.load_queries(real)


def _embed_work(tmp_path):
    vol, page = "SYNTH0001-1995-0001", "SYNTH0001-1995-0001-0001"
    w = _work(tmp_path, [vol])
    (w / "platform" / f"{vol}.json").write_text(json.dumps([{"doc_id": page, "pages": [page]}]), encoding="utf-8")
    (w / "ocr_platform" / vol).mkdir(parents=True)
    (w / "ocr_platform" / vol / f"{page}.json").write_text(json.dumps({"lines": ["합성"]}), encoding="utf-8")
    _gt(w)
    return w


def test_baseline_must_match_inputs(mods, tmp_path, monkeypatch):
    """기준 파일의 검색어·문서 해시가 이번 입력과 다르면 부르기 전에 멈춘다"""
    w = _embed_work(tmp_path)
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"vols": ["SYNTH0001-1995-0001"], "queries_sha256": "x", "docs_sha256": "y", "ranks": {}}),
                    encoding="utf-8")
    monkeypatch.setattr(mods.cc, "make_client", lambda *a, **k: pytest.fail("기준이 다른데 클라이언트를 만듦"))
    with pytest.raises(SystemExit, match="기준 결과"):
        mods.em.main(["--base-url", "http://127.0.0.1:9/v1", "--model", "m", "--label", "t", "--work", str(w),
                      "--baseline", str(base)])


def test_embed_error_hides_message(mods):
    class Boom:
        embeddings = SimpleNamespace(create=lambda **kw: (_ for _ in ()).throw(RuntimeError("Incorrect API key sk-abc...xyz")))

    with pytest.raises(SystemExit) as e:
        mods.em.embed(Boom(), "text-embedding-3-small", ["가"], True, mods.cc.Budget(1.0))
    assert "sk-" not in str(e.value) and "RuntimeError" in str(e.value)


def test_synth_queries_file(mods):
    qs = mods.em.load_queries()
    assert len(qs) >= 20 and len({q["id"] for q in qs}) == len(qs)
    for q in qs:
        assert q["q"].strip() and isinstance(q["match"], dict)
        assert set(q["match"]) <= {"docno", "date", "recipient", "title", "year"}
        assert (q["type"] == "답 없음") == (not q["match"])
        assert q.get("year") == q["match"].get("year")  # 조건 질의: 화면 연도 칸 = 정답 규칙 연도
