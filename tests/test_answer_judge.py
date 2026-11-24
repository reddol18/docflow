"""답변 심판·사람 채점표·일치도의 순수 로직 (https://github.com/reddol18/docflow/issues/21). Ragas·LLM 호출 없음."""
import asyncio
import csv
import importlib.util
import json
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parent.parent
ANS = ROOT / "eval" / "answer"


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def jd():
    return _load("answer_judge", "eval/answer/judge.py")


@pytest.fixture(scope="module")
def hs():
    return _load("human_sheet", "eval/answer/human_sheet.py")


@pytest.fixture(scope="module")
def ag():
    before = list(sys.path)
    mod = _load("answer_agreement", "eval/answer/agreement.py")
    yield mod
    sys.path[:] = before


TH = {"faithfulness": 0.8, "answer_relevancy": 0.7, "context_precision": 0.5, "context_recall": 0.8, "citation_accuracy": 1.0}


def answers(n_ans=2):
    rows = []
    for i in range(1, n_ans + 1):
        rows.append({"id": f"a{i:02d}", "type": "수신", "question": f"질문 {i}", "answerable": True, "expected_answer": "참고",
                     "source_docs": ["SYNTH1-1991-0001/SYNTH1-1991-0001-0001"], "answer": "답 [SYNTH1-1991-0001/SYNTH1-1991-0001-0001]",
                     "cited": ["SYNTH1-1991-0001/SYNTH1-1991-0001-0001"],
                     "retrieved": [{"key": "SYNTH1-1991-0001/SYNTH1-1991-0001-0001", "rank": 1, "context": "[k] 글" * 300}]})
    rows.append({"id": "a99", "type": "답 없음(없는 회사)", "question": "q", "answerable": False, "expected_answer": "없음",
                 "source_docs": [], "answer": "목록에 없습니다.", "cited": [],
                 "retrieved": [{"key": "SYNTH1-1991-0001/SYNTH1-1991-0001-0002", "rank": 1, "context": "글"}]})
    return {"label": "t", "rows": rows}


# ---------- judge ----------

def test_prompt_file_complete_and_korean(jd):
    p, h = jd.load_prompts()
    assert len(h) == 64 and p["version"]
    assert set(p["thresholds"]) == set(jd.METRICS) - {"refusal"}
    for text in [*p["ragas"].values(), *p["custom"].values()]:
        assert sum("가" <= ch <= "힣" for ch in text) > 20  # 한국어 지시문


def test_prompt_file_missing_key(jd, tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("version: x\nthresholds: {}\nragas: {}\ncustom: {}\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        jd.load_prompts(f)


def _fake_ragas_metrics():
    """Ragas 0.4.3 지시문 객체 흉내 — 입력·출력 모델 필드는 ragas.metrics.collections의 것과 같게"""
    from pydantic import BaseModel

    class SGIn(BaseModel):
        question: str
        answer: str

    class SGOut(BaseModel):
        statements: list[str]

    class NLIOne(BaseModel):
        statement: str
        reason: str
        verdict: int

    class NLIIn(BaseModel):
        context: str
        statements: list[str]

    class NLIOut(BaseModel):
        statements: list[NLIOne]

    class ARIn(BaseModel):
        response: str

    class AROut(BaseModel):
        question: str
        noncommittal: int

    ns = lambda i=None, o=None: SimpleNamespace(instruction="english", examples=["en"], input_model=i, output_model=o)
    return {"faithfulness": SimpleNamespace(statement_generator_prompt=ns(SGIn, SGOut), nli_statement_prompt=ns(NLIIn, NLIOut)),
            "answer_relevancy": SimpleNamespace(prompt=ns(ARIn, AROut)), "context_precision": SimpleNamespace(prompt=ns()),
            "context_recall": SimpleNamespace(prompt=ns())}


def test_apply_prompts(jd):
    p, _ = jd.load_prompts()
    m = _fake_ragas_metrics()
    jd.apply_prompts(m, p)
    assert m["context_recall"].prompt.instruction == p["ragas"]["context_recall.prompt"]
    assert m["context_recall"].prompt.examples == ["en"]  # 예시를 안 준 지표는 Ragas 예시 그대로
    ar = m["answer_relevancy"].prompt.examples
    assert len(ar) == len(p["ragas_examples"]["answer_relevancy.prompt"])
    assert any(o.noncommittal == 0 and "보완 요구" in i.response for i, o in ar)  # 부정적인 처리결과도 회피가 아님
    del m["answer_relevancy"].prompt
    with pytest.raises(SystemExit):
        jd.apply_prompts(m, p)


def test_apply_prompts_bad_example(jd):
    p, _ = jd.load_prompts()
    p = {**p, "ragas_examples": {"answer_relevancy.prompt": [{"input": {"response": "x"}, "output": {"question": "q"}}]}}
    with pytest.raises(SystemExit):  # noncommittal 없음
        jd.apply_prompts(_fake_ragas_metrics(), p)


@pytest.mark.parametrize("ex", ["{nope.prompt: [{input: {}, output: {}}]}", "{answer_relevancy.prompt: []}",
                                "{answer_relevancy.prompt: [{input: {}}]}"])
def test_prompt_file_bad_examples(jd, tmp_path, ex):
    import yaml
    p = yaml.safe_load((ANS / "judge_prompts_ko.yaml").read_text(encoding="utf-8"))
    p["ragas_examples"] = yaml.safe_load(ex)
    f = tmp_path / "p.yaml"
    f.write_text(yaml.safe_dump(p, allow_unicode=True), encoding="utf-8")
    with pytest.raises(SystemExit):
        jd.load_prompts(f)


def test_examples_fit_real_ragas(jd):
    """심판 환경(ragas 설치)에서만: 예시가 실제 Ragas 모델에 맞는지"""
    pytest.importorskip("ragas")
    from ragas.metrics.collections.answer_relevancy.util import AnswerRelevancePrompt
    from ragas.metrics.collections.faithfulness.util import NLIStatementPrompt, StatementGeneratorPrompt
    p, _ = jd.load_prompts()
    m = {"faithfulness": SimpleNamespace(statement_generator_prompt=StatementGeneratorPrompt(),
                                         nli_statement_prompt=NLIStatementPrompt()),
         "answer_relevancy": SimpleNamespace(prompt=AnswerRelevancePrompt()),
         "context_precision": SimpleNamespace(prompt=SimpleNamespace(instruction="")),
         "context_recall": SimpleNamespace(prompt=SimpleNamespace(instruction=""))}
    jd.apply_prompts(m, p)
    s = m["faithfulness"].nli_statement_prompt.to_string(m["faithfulness"].nli_statement_prompt.input_model(
        context="c", statements=["s"]))
    assert "접수·처리과는 서식 칸 이름" in s and "Einstein" not in s


def test_parse_metrics(jd):
    assert jd.parse_metrics("") == set(jd.METRICS)
    assert jd.parse_metrics("faithfulness, answer_relevancy") == {"faithfulness", "answer_relevancy", "refusal"}
    with pytest.raises(SystemExit):
        jd.parse_metrics("faithfulnes")


@pytest.mark.parametrize("url,ok", [("http://127.0.0.1:18434/v1", True), ("https://x.proxy.runpod.net/v1", True),
                                    ("http://10.0.0.5:11434/v1", False), ("https://u:p@h/v1", False), ("ftp://h", False)])
def test_check_base_url(jd, url, ok):
    if ok:
        assert jd.check_base_url(url + "/") == url
    else:
        with pytest.raises(SystemExit):
            jd.check_base_url(url)


def test_binarize(jd):
    b = jd.binarize
    assert b("faithfulness", 0.8, TH, True, False) == "P"
    assert b("faithfulness", 0.79, TH, True, False) == "F"
    assert b("faithfulness", 1.0, TH, True, True) == "N"  # 답을 피한 답
    assert b("citation_accuracy", None, TH, True, True) == "N"
    assert b("answer_relevancy", 0.0, TH, True, True) == "F"  # 답 있는 질의를 피하면 적합도 실패
    assert b("context_precision", math.nan, TH, True, False) == "N"
    assert b("context_recall", 0.9, TH, False, True) == "N"  # 답 없는 질의
    assert b("refusal", None, TH, False, True) == "P"
    assert b("refusal", None, TH, False, False) == "F"
    assert b("refusal", None, TH, True, True) == "N"
    assert b("refusal", None, TH, False, None) == "N"  # 판정 실패


def test_citation_value(jd):
    keys = ["V/a", "V/b"]
    assert jd.citation_value(["V/a"], keys, {"V/a": True}, False) == 1.0
    assert jd.citation_value(["V/a", "V/x"], keys, {"V/a": True}, False) == 0.5  # 가져오지 않은 문서
    assert jd.citation_value(["V/a", "V/b"], keys, {"V/a": True, "V/b": False}, False) == 0.5
    assert jd.citation_value([], keys, {}, False) == 0.0  # 근거 표시 없이 답함
    assert jd.citation_value([], keys, {}, True) is None


def test_summarize(jd):
    rows = [{"answerable": True, "scores": {"faithfulness": 1.0, "refusal": 0}, "binary": {**{m: "N" for m in jd.METRICS},
                                                                                         "faithfulness": "P"}},
            {"answerable": True, "scores": {"faithfulness": 0.5, "refusal": 1}, "binary": {**{m: "N" for m in jd.METRICS},
                                                                                         "faithfulness": "F"},
             "errors": {"context_recall": "x"}},
            {"answerable": False, "scores": {"refusal": 1}, "binary": {**{m: "N" for m in jd.METRICS}, "refusal": "P"}},
            {"answerable": False, "scores": {"refusal": 0}, "binary": {**{m: "N" for m in jd.METRICS}, "refusal": "F"}}]
    s = jd.summarize(rows, TH)
    assert (s["faithfulness"]["n"], s["faithfulness"]["pass_rate"], s["faithfulness"]["mean"]) == (2, 0.5, 0.75)
    assert s["refusal"]["dont_know_rate_unanswerable"] == 0.5 and s["refusal"]["refusal_rate_answerable"] == 0.5
    assert s["context_recall"]["errors"] == 1


def test_meter_blocks_other_hosts(jd):
    m = jd.Meter("http://127.0.0.1:18434/v1", ("/chat/completions",))
    req = lambda s, h, port, path: SimpleNamespace(url=SimpleNamespace(scheme=s, host=h, port=port, path=path))
    asyncio.run(m.on_request(req("http", "127.0.0.1", 18434, "/v1/chat/completions")))
    for bad in (("https", "api.openai.com", None, "/v1/chat/completions"), ("http", "127.0.0.1", 18434, "/v1/embeddings"),
                ("http", "127.0.0.1", 11434, "/v1/chat/completions")):  # 같은 호스트 다른 포트(이 PC Ollama)도 막음
        with pytest.raises(jd.JudgeLeak):
            asyncio.run(m.on_request(req(*bad)))
    assert len(m.leaks) == 3
    o = jd.Meter("https://api.openai.com/v1", ("/embeddings",))  # 기본 포트 443 = 포트 표기 없음
    asyncio.run(o.on_request(req("https", "api.openai.com", None, "/v1/embeddings")))
    asyncio.run(o.on_request(req("https", "api.openai.com", 443, "/v1/embeddings")))


def test_leak_in_wrapped_exception(jd):
    """SDK가 훅 예외를 다른 예외로 싸도 JudgeLeak을 찾는다"""
    leak = jd.JudgeLeak("x")
    try:
        try:
            raise leak
        except jd.JudgeLeak as e:
            raise ConnectionError("wrapped") from e
    except ConnectionError as w:
        assert jd.leak_in(w) is leak
    assert jd.leak_in(ValueError("y")) is None


def test_parse_deadline(jd):
    assert jd.parse_deadline("2026-11-22T00:40:00Z") == jd.parse_deadline("2026-11-22T00:40:00")  # 시간대 없음 = UTC
    assert jd.parse_deadline("2026-11-22T09:40:00+09:00") == jd.parse_deadline("2026-11-22T00:40:00Z")


def test_meter_counts(jd):
    m = jd.Meter("http://h/v1", ("/embeddings",))

    class R:
        async def aread(self):
            return b""

        def json(self):
            return {"model": "text-embedding-3-small", "usage": {"prompt_tokens": 7}}
    asyncio.run(m.on_response(R()))
    assert m.dump() == {"calls": 1, "prompt_tokens": 7, "completion_tokens": 0, "models": ["text-embedding-3-small"]}


def test_take_env_removes(jd, monkeypatch):
    monkeypatch.setenv("DOCFLOW_TEST_SECRET_X", "v")
    assert jd.take_env("DOCFLOW_TEST_SECRET_X") == "v"
    import os
    assert "DOCFLOW_TEST_SECRET_X" not in os.environ


def test_pick_and_out_path(jd, tmp_path):
    rows = answers()["rows"]
    assert [r["id"] for r in jd.pick(rows, "a99, a01")] == ["a99", "a01"]
    with pytest.raises(SystemExit):
        jd.pick(rows, "zz")
    with pytest.raises(SystemExit):
        jd.out_path(ROOT / "x.json", "l")  # 저장소 안
    assert jd.out_path(tmp_path / "a.json", "l") == tmp_path / "judge" / "l.json"


def test_judge_row_with_fake_llm(jd):
    """가짜 심판으로 질의 하나 — 답을 피한 답이면 충실도·인용 N, 인용은 가져온 문서만 판정"""
    p, _ = jd.load_prompts()
    from pydantic import BaseModel

    class Refusal(BaseModel):
        reason: str
        refused: int

    class Support(BaseModel):
        reason: str
        supported: int

    class LLM:
        def __init__(self, refused):
            self.refused, self.prompts = refused, []

        async def agenerate(self, prompt, model):
            self.prompts.append(prompt)
            return model(reason="r", refused=self.refused) if model is Refusal else model(reason="r", supported=1)

    class Metric:
        def __init__(self, v):
            self.v = v

        async def ascore(self, **kw):
            return SimpleNamespace(value=self.v)
    metrics = {m: Metric(1.0) for m in jd.RAGAS_METRICS}
    row = answers()["rows"][0]
    res = asyncio.run(jd.judge_row(row, metrics, LLM(0), p, {"Refusal": Refusal, "Support": Support}))
    assert res["binary"] == {"faithfulness": "P", "answer_relevancy": "P", "context_precision": "P", "context_recall": "P",
                             "citation_accuracy": "P", "refusal": "N"}
    llm = LLM(1)
    res = asyncio.run(jd.judge_row(row, metrics, llm, p, {"Refusal": Refusal, "Support": Support}))
    assert res["binary"]["faithfulness"] == "N" and res["binary"]["citation_accuracy"] == "N"
    assert all("가" <= ch <= "힣" for ch in llm.prompts[0][:2])  # 한국어 지시문으로 시작
    un = answers()["rows"][-1]
    res = asyncio.run(jd.judge_row(un, metrics, LLM(1), p, {"Refusal": Refusal, "Support": Support}))
    assert res["binary"]["refusal"] == "P" and set(res["scores"]) == {"refusal"}
    # --metrics: 고른 지표만 (refusal은 늘). 나머지는 점수 없이 N, 인용 판정 호출도 없음
    llm = LLM(0)
    res = asyncio.run(jd.judge_row(row, metrics, llm, p, {"Refusal": Refusal, "Support": Support},
                                   jd.parse_metrics("faithfulness,answer_relevancy")))
    assert set(res["scores"]) == {"refusal", "faithfulness", "answer_relevancy"}
    assert res["binary"] == {"faithfulness": "P", "answer_relevancy": "P", "context_precision": "N", "context_recall": "N",
                             "citation_accuracy": "N", "refusal": "N"}
    assert len(llm.prompts) == 1  # refusal만 (Ragas는 가짜 지표)

    class BadSupport(LLM):  # 인용 판정 응답이 형식 오류 → 실패(F)가 아니라 해당 없음(N)
        async def agenerate(self, prompt, model):
            if model is Support:
                raise ValueError("형식 오류")
            return await super().agenerate(prompt, model)
    res = asyncio.run(jd.judge_row(row, metrics, BadSupport(0), p, {"Refusal": Refusal, "Support": Support}))
    assert res["binary"]["citation_accuracy"] == "N" and "citation_accuracy" in res["errors"]

    class Leaky(LLM):  # 허용 밖 요청이 SDK 예외로 싸여 와도 멈춘다
        async def agenerate(self, prompt, model):
            try:
                raise jd.JudgeLeak("밖")
            except jd.JudgeLeak as e:
                raise ConnectionError("APIConnectionError") from e
    with pytest.raises(jd.JudgeLeak):
        asyncio.run(jd.judge_row(row, metrics, Leaky(0), p, {"Refusal": Refusal, "Support": Support}))


def test_run_all_saves_each_row_and_stops_at_deadline(jd, monkeypatch):
    async def fake_row(row, *a):
        return {"scores": {}, "binary": {}, "reasons": {}, "errors": {}, "seconds": 0}
    monkeypatch.setattr(jd, "judge_row", fake_row)
    rows = answers()["rows"]
    saved = []
    out = asyncio.run(jd.run_all(rows, {}, None, {}, {}, None, lambda o, done: saved.append((len(o), done))))
    assert len(out) == 3 and saved == [(1, False), (2, False), (3, True)]
    out = asyncio.run(jd.run_all(rows, {}, None, {}, {}, 1.0, lambda o, done: None))  # 마감이 이미 지남
    assert out == []


# ---------- 사람 채점표 ----------

def test_sheet_has_no_judge_scores_and_na_cells(hs, tmp_path):
    rows = hs.sheet_rows(answers())
    cols = hs.columns()
    assert not any(c in cols for c in ("scores", "binary", "심판"))
    a, u = rows[0], rows[-1]
    heads = {k: h for k, h, _ in hs.RUBRIC}
    assert a[heads["refusal"]] == "-" and a[heads["faithfulness"]] == ""
    assert u[heads["refusal"]] == "" and u[heads["context_recall"]] == "-"
    p = tmp_path / "h.xlsx"
    hs.write_xlsx(p, rows)
    from openpyxl import load_workbook
    wb = load_workbook(p)
    assert wb.sheetnames == ["채점", "채점 기준"]
    assert [c.value for c in wb["채점"][1]] == cols


def test_sheet_refuses_overwrite(hs, tmp_path):
    a = tmp_path / "answers.json"
    a.write_text(json.dumps(answers(), ensure_ascii=False), encoding="utf-8")
    assert hs.main(["--answers", str(a), "--csv"]) == 0
    with pytest.raises(SystemExit):
        hs.main(["--answers", str(a), "--csv"])


# ---------- 일치도 ----------

def test_kappa_known_values(ag):
    # 사람 P P P F F, 심판 P P F F F: po=0.8, pe=0.6*0.4+0.4*0.6=0.48 → (0.8-0.48)/0.52
    pairs = list(zip("PPPFF", "PPFFF"))
    assert ag.kappa(pairs) == pytest.approx(0.32 / 0.52)
    assert ag.kappa(list(zip("PFPF", "PFPF"))) == pytest.approx(1.0)
    assert ag.kappa(list(zip("PFPF", "FPFP"))) == pytest.approx(-1.0)
    assert math.isnan(ag.kappa(list(zip("PPP", "PPP"))))  # 한 값만 — 셀 수 없음
    assert math.isnan(ag.kappa([]))


def test_norm(ag):
    assert [ag.norm(v) for v in ("p", " 통과", "F", "x", "N", "-", None, 1, "0.0")] == ["P", "P", "F", "F", "N", "N", "", "P", "F"]
    with pytest.raises(ValueError):
        ag.norm("아마")


def judge_rows(binaries):
    return [{"id": i, "binary": b} for i, b in binaries.items()]


def test_join_and_gate(ag):
    human = {"a01": {"faithfulness": "P"}, "a02": {"faithfulness": "F"}, "a03": {"faithfulness": ""},
             "a04": {"faithfulness": "N"}, "a05": {"faithfulness": "P"}}
    jr = judge_rows({"a01": {"faithfulness": "P"}, "a02": {"faithfulness": "F"}, "a03": {"faithfulness": "P"},
                     "a04": {"faithfulness": "P"}, "a05": {"faithfulness": "F"}, "a06": {"faithfulness": "P"}})
    j = ag.join(human, jr, ["faithfulness"])["faithfulness"]
    assert j["pairs"] == [("P", "P"), ("F", "F"), ("P", "F")]
    assert j["missing_human"] == ["a03"] and j["only_one"] == ["a04"]
    r = ag.evaluate(human, jr, 0, ["faithfulness"])["faithfulness"]
    assert (r["n"], r["agreement"], r["verdict"]) == (3, 0.667, "지시문 고쳐 재채점")
    assert ag.evaluate(human, jr, 1, ["faithfulness"])["faithfulness"]["verdict"] == "제외"
    with pytest.raises(ValueError):
        ag.join({"zz": {}}, jr, ["faithfulness"])


def test_main_only_judged_metrics(ag, hs, tmp_path, capsys):
    """judge.py --metrics 결과(metrics_judged)면 그 지표만 일치도를 낸다"""
    rows = hs.sheet_rows(answers())
    heads = {k: h for k, h, _ in hs.RUBRIC}
    rows[0][heads["faithfulness"]] = "P"
    c = tmp_path / "h.csv"
    hs.write_csv(c, rows)
    jr = [{"id": r["id"], "binary": {"faithfulness": "P", "answer_relevancy": "P", "refusal": "N"}} for r in answers()["rows"]]
    j = tmp_path / "j.json"
    j.write_text(json.dumps({"label": "x", "judge": {"model": "m"}, "prompts": {"version": "ko-2", "sha256": "0" * 64},
                             "metrics_judged": ["faithfulness", "answer_relevancy", "refusal"], "rows": jr}), encoding="utf-8")
    o = tmp_path / "o.json"
    assert ag.main(["--human", str(c), "--judge", str(j), "--revision", "1", "--out", str(o)]) == 0
    assert list(json.loads(o.read_text(encoding="utf-8"))["metrics"]) == ["faithfulness", "answer_relevancy", "refusal"]


def test_gate_pass_and_nan_kappa(ag):
    assert ag.verdict(0.8, 0.6, 0) == "사용"
    assert ag.verdict(0.9, 0.59, 0) == "지시문 고쳐 재채점"
    assert ag.verdict(1.0, math.nan, 1) == "제외"
    assert ag.verdict(None, math.nan, 0) == "지시문 고쳐 재채점"


def test_skew_rule(ag):
    """일치율 ≥ 80%이고 한쪽 값이 90% 넘게 몰리면 kappa 대신 일치율만으로 사용 (11-23 사용자 결정)"""
    assert ag.skew([]) is None
    assert ag.skew(list(zip("PPPPPPPPPP", "PPPPPPPPPP"))) == 1.0
    # 사람 P 10/11(90.9%) 심판 P 10/11, 어긋난 2건 → 일치율 9/11, kappa < 0.6, 쏠림 0.909
    pairs = list(zip("PPPPPPPPPPF", "PPPPPPPPPFP"))
    assert ag.skew(pairs) == pytest.approx(10 / 11)
    assert ag.kappa(pairs) < 0.6
    assert ag.verdict(9 / 11, ag.kappa(pairs), 1, ag.skew(pairs)) == "사용(쏠림 규칙)"
    assert ag.verdict(1.0, math.nan, 1, 1.0) == "사용(쏠림 규칙)"  # kappa 셀 수 없음 — 모두 P
    assert ag.verdict(0.89, 0.44, 0, 0.889) == "지시문 고쳐 재채점"  # 90% 이하는 규칙 밖 (경계: 초과만)
    assert ag.verdict(0.89, 0.44, 0, 0.9) == "지시문 고쳐 재채점"
    assert ag.verdict(0.79, 0.0, 1, 1.0) == "제외"  # 일치율 미달이면 쏠림이어도 제외
    assert ag.verdict(0.9, 0.7, 0, 0.95) == "사용"  # 기본 기준을 넘으면 그냥 사용
    r = ag.evaluate({"a01": {"refusal": "P"}, "a02": {"refusal": "P"}},
                    judge_rows({"a01": {"refusal": "P"}, "a02": {"refusal": "P"}}), 1, ["refusal"])["refusal"]
    assert (r["kappa"], r["skew"], r["verdict"]) == (None, 1.0, "사용(쏠림 규칙)")


def test_gt_context(ag):
    k = [f"S/d{i}" for i in range(4)]
    rows = [{"id": "a01", "answerable": True, "source_docs": [k[0]], "retrieved": [{"key": k[0], "rank": 2}]},
            {"id": "a02", "answerable": True, "source_docs": [k[0]], "retrieved": [{"key": k[0], "rank": 3}]},
            {"id": "a03", "answerable": True, "source_docs": k[:3],
             "retrieved": [{"key": k[0], "rank": 1}, {"key": k[3], "rank": 2}, {"key": k[1], "rank": 3}]},
            {"id": "a04", "answerable": True, "source_docs": k[:2],
             "retrieved": [{"key": k[1], "rank": 1}, {"key": k[0], "rank": 2}]},
            {"id": "a99", "answerable": False, "source_docs": [], "retrieved": [{"key": k[0], "rank": 1}]}]
    g = ag.gt_context(rows)
    assert g["a01"] == {"context_precision": "P", "context_recall": "P"}
    assert g["a02"] == {"context_precision": "F", "context_recall": "P"}
    assert g["a03"] == {"context_precision": "F", "context_recall": "F"}  # 1~2위에 근거 1건뿐, 3건 중 2건만
    assert g["a04"] == {"context_precision": "P", "context_recall": "P"}
    assert g["a99"] == {"context_precision": "N", "context_recall": "N"}
    human = {"a01": {"faithfulness": "P", "context_precision": ""}}
    assert ag.apply_gt(human, g)["a01"] == {"faithfulness": "P", "context_precision": "P", "context_recall": "P"}
    with pytest.raises(ValueError):
        ag.apply_gt({"zz": {}}, g)


def test_read_human_csv_and_xlsx(ag, hs, tmp_path):
    rows = hs.sheet_rows(answers())
    heads = {k: h for k, h, _ in hs.RUBRIC}
    rows[0][heads["faithfulness"]] = "통과"
    rows[1][heads["faithfulness"]] = "f"
    rows[-1][heads["refusal"]] = "P"
    c = tmp_path / "h.csv"
    hs.write_csv(c, rows)
    x = tmp_path / "h.xlsx"
    hs.write_xlsx(x, rows)
    for p in (c, x):
        h = ag.read_human(p)
        assert h["a01"]["faithfulness"] == "P" and h["a02"]["faithfulness"] == "F"
        assert h["a99"]["refusal"] == "P" and h["a99"]["faithfulness"] == "N" and h["a01"]["context_recall"] == ""
    with c.open(encoding="utf-8-sig", newline="") as f:
        data = list(csv.DictReader(f))
    data[1]["id"] = "a01"
    with c.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(data[0]))
        w.writeheader()
        w.writerows(data)
    with pytest.raises(ValueError):
        ag.read_human(c)
