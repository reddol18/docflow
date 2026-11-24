"""검색 답변 행렬 — 두 번째 후보 답변(같은 검색 재사용·지연)·쌍별 승률 심판·답변 통과 기준
(https://github.com/reddol18/docflow/issues/22 조건 3). LLM·임베딩 호출 없음, 지어낸 값만 쓴다."""
import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).parent.parent
K1, K2, K3 = ("SYNTH1-1991-0001/SYNTH1-1991-0001-0001", "SYNTH1-1991-0001/SYNTH1-1991-0001-0003",
              "SYNTH1-1991-0001/SYNTH1-1991-0001-0005")


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pw():
    before = list(sys.path)
    mod = _load("answer_pairwise", "eval/answer/pairwise.py")
    yield mod
    sys.path[:] = before


@pytest.fixture(scope="module")
def ga():
    before = list(sys.path)
    mod = _load("answer_gen_answers_m", "eval/answer/gen_answers.py")
    yield mod
    sys.path[:] = before


@pytest.fixture(scope="module")
def agate():
    before = list(sys.path)
    mod = _load("promptfoo_answer_gate", "eval/promptfoo/answer_gate.py")
    yield mod
    sys.path[:] = before


def row(i, answer, keys=(K1, K2), answerable=True):
    return {"id": i, "type": "수신", "question": f"질문 {i}", "answerable": answerable, "expected_answer": "참고 답",
            "answer": answer, "retrieved": [{"key": k, "context": f"[{k}] 글 {k[-4:]}"} for k in keys]}


# ---------- pairwise ----------

def test_pairwise_prompt_file(pw, tmp_path):
    p, sha = pw.load_prompts()
    assert p["version"] and len(sha) == 64
    assert "자리" not in p["template"] and "A인지 B인지" in p["instruction"]  # 자리 쏠림을 지시문에서도 막는다
    bad = tmp_path / "p.yaml"
    bad.write_text(yaml.safe_dump({"version": "x", "instruction": "i", "template": "{question} {answer_a}"}), encoding="utf-8")
    with pytest.raises(SystemExit, match="answer_b"):
        pw.load_prompts(bad)


@pytest.mark.parametrize("text,want", [
    ('{"reason": "r", "winner": "A"}', "A"), ('{"winner": "b", "reason": "r"}', "B"), ('{"winner": "tie"}', "tie"),
    ('```json\n{"reason": "x", "winner": "무승부"}\n```', "tie"), ('앞말 {"winner": "B"} 뒷말', "B")])
def test_parse_verdict(pw, text, want):
    assert pw.parse_verdict(text)[0] == want


@pytest.mark.parametrize("text", ["", "그냥 A", '{"winner": "C"}', "[1, 2]"])
def test_parse_verdict_bad(pw, text):
    with pytest.raises(ValueError):
        pw.parse_verdict(text)


@pytest.mark.parametrize("ab,ba,want", [
    ("A", "B", ("a", True)),     # 두 순서 모두 a
    ("B", "A", ("b", True)),     # 두 순서 모두 b
    ("A", "A", ("tie", False)),  # 늘 첫 자리 — 자리 쏠림이면 무승부
    ("B", "B", ("tie", False)),
    ("tie", "tie", ("tie", True)),
    ("A", "tie", ("tie", False)), ("tie", "A", ("tie", False))])
def test_combine_both_orders(pw, ab, ba, want):
    assert pw.combine(ab, ba) == want


def test_documents_union_and_messages(pw):
    ra, rb = row("a1", "x", (K1, K2)), row("a1", "y", (K2, K3))
    docs, same = pw.documents_of(ra, rb)
    assert not same and docs.count(K2) == 1 and docs.index(K1) < docs.index(K2) < docs.index(K3)
    assert pw.documents_of(ra, row("a1", "z", (K1, K2)))[1]
    p, _ = pw.load_prompts()
    m = pw.messages(p, "질문", "참고", docs, "첫 답", "둘째 답")
    tag = m[0]["content"].split("<data-")[1].split(">")[0]
    user = m[1]["content"]
    assert user.index("첫 답") < user.index("둘째 답")
    assert f"<data-{tag}>\n첫 답\n</data-{tag}>" in user and user.count(f"<data-{tag}>") == 3


def test_summarize(pw):
    rows = [{"result": "a", "identical": False, "consistent": True, "same_retrieval": True,
             "orders": {"ab": {"position": "A"}, "ba": {"position": "B"}}},
            {"result": "tie", "identical": False, "consistent": False, "same_retrieval": True,
             "orders": {"ab": {"position": "A"}, "ba": {"position": "A"}}},
            {"result": "tie", "identical": True, "consistent": True, "same_retrieval": False, "orders": {}},
            {"result": "b", "identical": False, "consistent": True, "same_retrieval": True,
             "orders": {"ab": {"position": "B"}, "ba": {"position": "A"}}},
            {"result": None, "identical": False, "consistent": None, "same_retrieval": True, "orders": {"ab": None}}]
    s = pw.summarize(rows)
    assert (s["n"], s["a_wins"], s["b_wins"], s["ties"], s["identical"], s["inconsistent"], s["errors"]) == (4, 1, 1, 2, 1, 1, 1)
    assert s["win_rate_a"] == s["win_rate_b"] == 0.5
    assert s["first_position_rate"] == round(4 / 6, 3) and s["different_retrieval"] == 1
    assert pw.summarize([])["win_rate_a"] is None


class FakeClient:
    """심판 대신: 자리 A에 놓인 답 글로 정해 둔 판정을 돌려준다"""

    def __init__(self, pick):
        self.pick, self.calls = pick, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, model, messages, **kw):
        self.calls.append(kw)
        user = messages[1]["content"]
        first = user.split("[답 A]")[1].split("[답 B]")[0]
        out = self.pick(first)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=out))])


def test_judge_row_both_orders(pw):
    p, _ = pw.load_prompts()
    ra, rb = row("a1", "좋은 답"), row("a1", "나쁜 답")
    good = FakeClient(lambda first: json.dumps({"winner": "A" if "좋은" in first else "B", "reason": "r"}))
    r = asyncio.run(pw.judge_row(good, "m", p, ra, rb, 100))
    assert (r["result"], r["consistent"], len(good.calls)) == ("a", True, 2)
    assert good.calls[0]["temperature"] == 0 and good.calls[0]["response_format"] == {"type": "json_object"}
    biased = FakeClient(lambda first: '{"winner": "A", "reason": "첫 자리"}')
    r = asyncio.run(pw.judge_row(biased, "m", p, ra, rb, 100))
    assert (r["result"], r["consistent"]) == ("tie", False)
    same = FakeClient(lambda first: '{"winner": "A"}')
    r = asyncio.run(pw.judge_row(same, "m", p, ra, row("a1", " 좋은   답 "), 100))
    assert (r["result"], r["identical"], len(same.calls)) == ("tie", True, 0)  # 같은 답이면 부르지 않는다
    broken = FakeClient(lambda first: "형식 없음")
    r = asyncio.run(pw.judge_row(broken, "m", p, ra, rb, 100))
    assert r["result"] is None and set(r["errors"]) == {"ab", "ba"}


def test_judge_row_leak_stops(pw):
    p, _ = pw.load_prompts()

    class Leaky(FakeClient):
        async def create(self, model, messages, **kw):
            raise RuntimeError("싸인 오류") from pw.jd.JudgeLeak("허용 밖")

    with pytest.raises(pw.jd.JudgeLeak):
        asyncio.run(pw.judge_row(Leaky(None), "m", p, row("a1", "x"), row("a1", "y"), 100))


def test_run_all_deadline(pw):
    p, _ = pw.load_prompts()
    good = FakeClient(lambda first: '{"winner": "A"}')
    rows = [(row("a1", "x"), row("a1", "y")), (row("a2", "x"), row("a2", "y"))]
    saved = []
    out = asyncio.run(pw.run_all(good, "m", p, rows, 100, lambda o, done: saved.append(done), deadline=1.0))
    assert out == [] and saved == [] and good.calls == []  # 지난 마감 — 새 질의를 시작하지 않는다
    out = asyncio.run(pw.run_all(good, "m", p, rows, 100, lambda o, done: saved.append(done)))
    assert len(out) == 2 and saved == [False, True]


def test_pair_rows_checks(pw):
    a = {"queries_sha256": "h", "rows": [row("a1", "x"), row("a2", "y")]}
    b = {"queries_sha256": "h", "rows": [row("a2", "z"), row("a1", "w")]}
    assert [r[0]["id"] for r in pw.pair_rows(a, b)] == ["a1", "a2"]
    assert [r[1]["answer"] for r in pw.pair_rows(a, b, "a2")] == ["z"]
    with pytest.raises(SystemExit, match="질의 세트"):
        pw.pair_rows(a, {**b, "queries_sha256": "other"})
    with pytest.raises(SystemExit, match="id"):
        pw.pair_rows(a, b, "a9")
    with pytest.raises(SystemExit, match="합성"):
        pw.pair_rows(a, {"queries_sha256": "h", "rows": [row("a1", "x", ("REAL/1",)), row("a2", "y")]})
    with pytest.raises(SystemExit, match="실패"):
        pw.pair_rows(a, {"queries_sha256": "h", "rows": [{**row("a1", "x"), "error": "APIError"}, row("a2", "y")]})


def test_pairwise_out_path_outside_repo(pw, tmp_path):
    with pytest.raises(SystemExit):
        pw.out_path(ROOT / "x.json", "l")
    assert pw.out_path(tmp_path / "ans.json", "l") == tmp_path.resolve() / "pairwise" / "l.json"


# ---------- gen_answers: 같은 검색 재사용·지시문 판·지연 ----------

def _hits(ga):
    from app import llm
    docs = {("SYNTH1-1991-0001", "SYNTH1-1991-0001-0001"): {"values": {"docno": "도시 1-100", "date": "1991-01-02",
                                                                         "recipient": "내부결재", "title": "건축허가"}}}
    key = list(docs)[0]
    hits = ga.ask_hits([key], docs, {key: "본문" * 400}, {key: 0.03}, [key], [key])
    prev = {"id": "a1", "retrieved": [{"key": llm.cite_key(h["vol"], h["doc_id"]), "rrf": h["rrf"], "vector_rank": 1,
                                       "keyword_rank": 1, "fields": h["fields"], "context": ga.context_of(h)} for h in hits]}
    return hits, prev, {key: "본문" * 400}


def test_reuse_hits_same_messages(ga):
    from app import llm
    hits, prev, bodies = _hits(ga)
    again = ga.reuse_hits(prev, bodies)
    assert llm.answer_messages("q", again, ga.ec.SCHEMA)[1:] != [] and \
        [ga.context_of(h) for h in again] == [ga.context_of(h) for h in hits]
    with pytest.raises(SystemExit, match="다르다"):
        ga.reuse_hits(prev, {k: "바뀐 본문" for k in bodies})
    with pytest.raises(SystemExit, match="work에 없다"):
        ga.reuse_hits(prev, {})


def test_load_reuse_checks_queries(ga, tmp_path):
    qs = [{"id": "a1", "question": "q"}]
    p = tmp_path / "prev.json"
    p.write_text(json.dumps({"queries_sha256": ga.ec.digest(qs), "rows": [{"id": "a1"}]}), encoding="utf-8")
    assert set(ga.load_reuse(p, qs)[1]) == {"a1"}
    with pytest.raises(SystemExit, match="질의 세트"):
        ga.load_reuse(p, [{"id": "a1", "question": "다른 q"}])


def test_prompt_name_of(ga):
    from app import llm
    assert ga.answer_prompts()["current"] == llm.ANSWER_PROMPT
    assert ga.prompt_name_of({"answer_prompt": "v2"}) == "v2"
    assert ga.prompt_name_of({"settings": {"answer_prompt_sha256": ga.ec.digest(llm.ANSWER_PROMPT)}}) == "current"
    assert ga.prompt_name_of({"settings": {"answer_prompt_sha256": "abcdef0123"}}) == "sha:abcdef01"


class FakeOpenAI:
    def __init__(self):
        self.sent = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.sent.append(kw)
        return SimpleNamespace(model="m-1", system_fingerprint=None, usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=100),
                               choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="답"))])


def test_answer_one_prompt_price_latency(ga):
    hits, _, _ = _hits(ga)
    q = {"question": "질문"}
    c = FakeOpenAI()
    budget = ga.cc.Budget(1.0)
    r = ga.answer_one(c, "gpt-4.1-mini", q, hits, budget)
    assert r["usd"] == pytest.approx((1000 * 0.40 + 100 * 1.60) / 1e6) and r["latency_ms"] >= 0
    r = ga.answer_one(c, "qwen2.5:7b", q, hits, budget, system_prompt="다른 판", priced=False)  # 단가 없는 로컬 모델
    assert r["usd"] == 0 and c.sent[-1]["messages"][0] == {"role": "system", "content": "다른 판"}
    assert budget.spent == pytest.approx((1000 * 0.40 + 100 * 1.60) / 1e6)


def test_totals_latency(ga):
    base = {"answerable": False, "source_docs": [], "retrieved": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            "model": "m", "dropped_citations": 0}
    t = ga.totals([{**base, "usd": 0.002, "latency_ms": 1000}, {**base, "usd": 0.004, "latency_ms": 3000}, {**base, "usd": 0.0}])
    assert (t["usd_per_query"], t["latency_ms_mean"], t["latency_ms_p95"]) == (0.002, 2000, 3000)


# ---------- answer_gate ----------

def summary(faith, rel, dk, ref_ans, errors=0, n_f=18, n_r=32):
    def m(p, n, err=0):
        return {"n": n, "pass_rate": p, "mean": p, "errors": err, "threshold": 1.0}
    out = {"faithfulness": m(faith, n_f, errors), "answer_relevancy": m(rel, n_r), "context_precision": m(0.6, 32),
           "context_recall": m(0.5, 32), "citation_accuracy": m(None, 0), "refusal": m(1.0, 10)}
    out["refusal"].update(dont_know_rate_unanswerable=dk, refusal_rate_answerable=ref_ans)
    return out


def write_cand(d, name, model, s, prompt="current", latency=True, judge_model="j", usd=0.001, priced=True):
    ids = ["a1", "a2"]
    ans = {"label": name, "chat_model": model, "answer_prompt": prompt, "queries_sha256": "h", "priced": priced,
           "rows": [{"id": i, "usd": usd, **({"latency_ms": 1000 * (k + 1)} if latency else {})} for k, i in enumerate(ids)]}
    jdg = {"answers_file": f"{name}.json", "complete": True, "judged": 2, "planned": 2,
           "metrics_judged": ["faithfulness", "answer_relevancy", "context_precision", "context_recall", "refusal"],
           "judge": {"model": judge_model, "ollama_digest": "dg"}, "prompts": {"version": "ko-2", "sha256": "s"},
           "summary": s, "rows": [{"id": i} for i in ids]}
    (d / f"{name}.json").write_text(json.dumps(ans, ensure_ascii=False), encoding="utf-8")
    (d / f"{name}_judge.json").write_text(json.dumps(jdg, ensure_ascii=False), encoding="utf-8")
    return [str(d / f"{name}.json"), str(d / f"{name}_judge.json")]


def write_pair(d, a, b, wa, wb, ties):
    n = wa + wb + ties
    pwj = {"a": {"file": f"{a}.json"}, "b": {"file": f"{b}.json"}, "complete": True, "judge": {"model": "j"},
           "prompts": {"version": "pw-1"}, "rows": [{"id": "a1"}, {"id": "a2"}],
           "summary": {"n": n, "a_wins": wa, "b_wins": wb, "ties": ties, "identical": 0, "inconsistent": 0,
                       "win_rate_a": (wa + ties / 2) / n, "win_rate_b": (wb + ties / 2) / n, "first_position_rate": 0.5,
                       "different_retrieval": 0, "errors": 0}}
    p = d / f"{a}_vs_{b}.json"
    p.write_text(json.dumps(pwj), encoding="utf-8")
    return str(p)


CRIT = {"answer": {"baseline": {"model": "gpt-4o-mini", "prompt": "current"}, "faithfulness_pass_drop_pp_max": None,
                   "answer_relevancy_pass_drop_pp_max": None, "dont_know_drop_pp_max": None,
                   "refusal_answerable_rise_pp_max": None, "win_rate_min": None, "judge_error_max": None}}


def crit(**kw):
    return {"answer": {**CRIT["answer"], **kw}}


def test_answer_gate_criteria_file_matches_keys(agate):
    c = yaml.safe_load((ROOT / "eval" / "promptfoo" / "criteria.yaml").read_text(encoding="utf-8"))["answer"]
    assert set(c) == set(CRIT["answer"])  # 판정 함수가 읽는 이름과 설정 파일이 같다
    assert c["baseline"] == {"model": "gpt-4o-mini", "prompt": "current"}


def test_answer_gate_verdicts(agate, tmp_path):
    base = write_cand(tmp_path, "base", "gpt-4o-mini", summary(0.8, 0.9, 1.0, 0.4))
    cand = write_cand(tmp_path, "cand", "gpt-4.1-mini", summary(0.7, 0.9, 0.9, 0.5), latency=False)
    pair = write_pair(tmp_path, "base", "cand", 3, 1, 2)
    code, md, data = agate.run([base, cand], [pair], CRIT)
    assert code == 0 and "통과" in md
    c = data["candidates"][1]
    assert c["win"] == pytest.approx((1 + 1) / 6) and c["pair"]["wins"] == 1 and c["pair"]["losses"] == 3
    assert c["ms_mean"] is None and data["candidates"][0]["ms_mean"] == 1500  # 지연 없는 옛 파일은 '-'
    code, md, _ = agate.run([base, cand], [pair], crit(faithfulness_pass_drop_pp_max=10.0))
    assert code == 0  # 80% → 70% = 10%p, 넘지 않음
    code, md, _ = agate.run([base, cand], [pair], crit(faithfulness_pass_drop_pp_max=5.0, dont_know_drop_pp_max=0.0,
                                                       refusal_answerable_rise_pp_max=5.0, win_rate_min=0.4))
    assert code == 1
    verdict = [r for r in md.splitlines() if r.startswith("| gpt-4.1-mini")][0]
    for part in ("근거 충실도", "'모른다'", "회피", "승률 0.33"):
        assert part in verdict


def test_answer_gate_judge_errors_and_missing_values(agate, tmp_path):
    base = write_cand(tmp_path, "base", "gpt-4o-mini", summary(0.8, 0.9, 1.0, 0.4, errors=1))
    cand = write_cand(tmp_path, "cand", "local", summary(None, 0.9, 1.0, 0.4, n_f=0), priced=False)
    code, md, _ = agate.run([base, cand], [], crit(judge_error_max=0))
    assert code == 1 and "기준 — 심판 오류 1건" in md and "$0 (GPU" in md
    code, md, _ = agate.run([base, cand], [], crit(faithfulness_pass_drop_pp_max=10.0))
    assert code == 1 and "값 없음" in md  # 모두 피해 충실도 판정이 0건이면 실패


@pytest.mark.parametrize("change,msg", [
    ({"judge_model": "other"}, "심판 모델"),
    ({"model": "gpt-4o-mini"}, "같은 후보"),
    ({"model": "x", "base_model": "y"}, "기준 후보"),
])
def test_answer_gate_input_errors(agate, tmp_path, change, msg):
    base = write_cand(tmp_path, "base", change.get("base_model", "gpt-4o-mini"), summary(0.8, 0.9, 1.0, 0.4))
    cand = write_cand(tmp_path, "cand", change.get("model", "gpt-4.1-mini"), summary(0.8, 0.9, 1.0, 0.4),
                      judge_model=change.get("judge_model", "j"))
    with pytest.raises(agate.InputError, match=msg):
        agate.run([base, cand], [], CRIT)


def test_answer_gate_input_errors_files(agate, tmp_path):
    base = write_cand(tmp_path, "base", "gpt-4o-mini", summary(0.8, 0.9, 1.0, 0.4))
    cand = write_cand(tmp_path, "cand", "gpt-4.1-mini", summary(0.8, 0.9, 1.0, 0.4))
    with pytest.raises(agate.InputError, match="채점한 결과"):
        agate.run([base, [cand[0], base[1]]], [], CRIT)  # 다른 답변의 심판 결과
    with pytest.raises(agate.InputError, match="쌍별 결과가 없는"):
        agate.run([base, cand], [], crit(win_rate_min=0.4))
    j = json.loads(Path(cand[1]).read_text(encoding="utf-8"))
    Path(cand[1]).write_text(json.dumps({**j, "metrics_judged": ["refusal"]}), encoding="utf-8")
    with pytest.raises(agate.InputError, match="채점하지 않은"):
        agate.run([base, cand], [], crit(faithfulness_pass_drop_pp_max=10.0))
    Path(cand[1]).write_text(json.dumps({**j, "complete": False}), encoding="utf-8")
    with pytest.raises(agate.InputError, match="끝까지"):
        agate.run([base, cand], [], CRIT)


def test_answer_gate_main_exit_codes(agate, tmp_path, capsys):
    base = write_cand(tmp_path, "base", "gpt-4o-mini", summary(0.8, 0.9, 1.0, 0.4))
    cand = write_cand(tmp_path, "cand", "gpt-4.1-mini", summary(0.5, 0.9, 1.0, 0.4))
    cf = tmp_path / "c.yaml"
    cf.write_text(yaml.safe_dump(CRIT), encoding="utf-8")
    out = tmp_path / "out"
    assert agate.main(["--cand", *base, "--cand", *cand, "--criteria", str(cf), "--out", str(out)]) == 0
    assert (out / "answer_gate.md").exists() and json.loads((out / "answer_gate.json").read_text(encoding="utf-8"))["candidates"]
    cf.write_text(yaml.safe_dump(crit(faithfulness_pass_drop_pp_max=10.0)), encoding="utf-8")
    assert agate.main(["--cand", *base, "--cand", *cand, "--criteria", str(cf), "--out", str(out)]) == 1
    assert agate.main(["--cand", *base, "--criteria", str(cf), "--out", str(ROOT / "x")]) == 2  # 저장소 안
    assert agate.main(["--cand", *cand, "--criteria", str(cf), "--out", str(out)]) == 2  # 기준 없음


# ---------- 쌍별 심판 사람 확인표·일치도 (2026-11-24 결정 3) ----------

@pytest.fixture(scope="module")
def psheet():
    before = list(sys.path)
    mod = _load("answer_pairwise_sheet", "eval/answer/pairwise_sheet.py")
    yield mod
    sys.path[:] = before


@pytest.fixture(scope="module")
def pagr():
    before = list(sys.path)
    mod = _load("answer_pairwise_agreement", "eval/answer/pairwise_agreement.py")
    yield mod
    sys.path[:] = before


def pw_fixture(n=30):
    """쌍별 결과·두 답변 파일 (지어낸 값). 결과 a·b·tie를 돌려 가며, 4번째마다 같은 답, 마지막은 오류"""
    res = ["a", "b", "tie"]
    pw_rows, ra, rb = [], [], []
    for k in range(n):
        i = f"q{k:02d}"
        identical = k % 4 == 3
        result = None if k == n - 1 else ("tie" if identical else res[k % 3])
        pw_rows.append({"id": i, "identical": identical, "result": result, "consistent": True})
        ra.append({**row(i, f"모델A 답 {i}"), "expected_answer": f"참고 {i}"})
        rb.append(row(i, f"모델A 답 {i}" if identical else f"모델B 답 {i}", (K2, K3)))
    pw = {"label": "base_vs_cand", "complete": True, "queries_sha256": "h", "a": {"file": "base.json", "model": "gpt-4o-mini"},
          "b": {"file": "cand.json", "model": "gpt-4.1-mini"}, "judge": {"model": "j"},
          "prompts": {"version": "pw-1", "sha256": "s" * 64}, "rows": pw_rows}
    return pw, {"queries_sha256": "h", "rows": ra}, {"queries_sha256": "h", "rows": rb}


def test_pairwise_sheet_pick_deterministic_and_balanced(psheet):
    pw, _, _ = pw_fixture()
    ids = psheet.pick(pw["rows"], 12, 22)
    assert ids == psheet.pick(pw["rows"], 12, 22) and ids != psheet.pick(pw["rows"], 12, 23)
    by = {r["id"]: r for r in pw["rows"]}
    assert len(set(ids)) == 12 and not any(by[i]["identical"] or by[i]["result"] is None for i in ids)
    assert {k: sum(by[i]["result"] == k for i in ids) for k in ("a", "b", "tie")} == {"a": 4, "b": 4, "tie": 4}
    assert len(psheet.pick(pw["rows"], 100, 22)) == len(psheet.candidates(pw["rows"]))  # 모자라면 있는 만큼
    pos = psheet.positions(ids, 22)
    assert sorted(pos.values()).count("a") == 6 and pos == psheet.positions(ids, 22)


def test_pairwise_sheet_blind(psheet):
    pw, a, b = pw_fixture()
    rows, key = psheet.build(pw, a, b, n=10, seed=22)
    assert key["n"] == 10 and set(key["position_a_is"]) == {r["id"] for r in rows}
    for r in rows:
        want_a = "모델A" if key["position_a_is"][r["id"]] == "a" else "모델B"
        assert r["답 A"].startswith(want_a) and r["답 B"] != r["답 A"]
        assert [d["key"] for d in r["docs"]] == [K1, K2, K3]  # 심판과 같은 문서 합집합
    page = psheet.html(rows, pw["label"], "x_pw_human")
    for secret in ("gpt-4o-mini", "gpt-4.1-mini", "position_a_is", '"result"', "base.json", "cand.json"):
        assert secret not in page  # 열쇠·모델 이름·심판 판정이 페이지에 없다
    assert "</script>" not in psheet.html([{**rows[0], "답 A": "</script><b>"}], "l", "s").split("const DATA")[1].split("\n")[0]


def test_pairwise_sheet_main_and_agreement(psheet, pagr, tmp_path):
    pw, a, b = pw_fixture()
    (tmp_path / "pairwise").mkdir()
    pwp = tmp_path / "pairwise" / "base_vs_cand.json"
    pwp.write_text(json.dumps(pw, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "base.json").write_text(json.dumps(a, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "cand.json").write_text(json.dumps(b, ensure_ascii=False), encoding="utf-8")
    assert psheet.main(["--pairwise", str(pwp), "--n", "9"]) == 0
    h = tmp_path / "human"
    csvp, keyp = h / "base_vs_cand_pw_human.csv", h / "_key" / "base_vs_cand_pw_key.json"
    assert csvp.exists() and (h / "base_vs_cand_pw_score.html").exists() and keyp.exists()
    with pytest.raises(SystemExit, match="덮지 않는다"):
        psheet.main(["--pairwise", str(pwp), "--n", "9"])
    key = json.loads(keyp.read_text(encoding="utf-8"))
    judge = {r["id"]: r["result"] for r in pw["rows"]}
    # 사람 = 심판과 같게 채움 (심판 tie인 쌍은 사람 비김) → 일치율 100%, 비김은 빠진다
    import csv as _csv
    with csvp.open(encoding="utf-8-sig", newline="") as f:
        sheet = list(_csv.DictReader(f))
    for r in sheet:
        j, a_is = judge[r["id"]], key["position_a_is"][r["id"]]
        r[psheet.VERDICT_COL] = "비김" if j == "tie" else ("A" if j == a_is else "B")
    filled = tmp_path / "filled.csv"
    with filled.open("w", encoding="utf-8-sig", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=psheet.COLUMNS)
        w.writeheader()
        w.writerows(sheet)
    res = pagr.evaluate(pagr.read_human(filled), key, pw)
    assert res["n"] == 6 and res["agreement"] == 1.0 and res["kappa"] == 1.0 and res["verdict"] == "사용"
    assert res["human_ties"] == {"n": 3, "judge": {"a": 0, "b": 0, "tie": 3}}
    out = tmp_path / "agr.json"
    assert pagr.main(["--human", str(filled), "--key", str(keyp), "--pairwise", str(pwp), "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["agreement"] == 1.0
    with pytest.raises(ValueError, match="열쇠"):
        pagr.evaluate(pagr.read_human(filled), {**key, "pairwise_label": "other"}, pw)


def test_pairwise_agreement_rules(pagr):
    assert pagr.to_side("A", "b") == "b" and pagr.to_side("B", "b") == "a" and pagr.to_side("tie", "a") == "tie"
    assert pagr.norm(" a ") == "A" and pagr.norm("비김") == "tie" and pagr.norm("") == ""
    with pytest.raises(ValueError):
        pagr.norm("C")
    assert pagr.kappa([("a", "a"), ("b", "b")]) == 1.0
    assert pagr.kappa([("a", "a"), ("b", "tie")]) == pytest.approx((0.5 - 0.25) / 0.75)  # 심판 tie는 불일치
    import math as _m
    assert _m.isnan(pagr.kappa([("a", "a"), ("a", "a")]))
    pw = {"label": "l", "queries_sha256": "h", "rows": [{"id": f"q{i}", "result": r} for i, r in
                                                        enumerate(["a"] * 9 + ["b"])]}
    key = {"pairwise_label": "l", "queries_sha256": "h", "position_a_is": {f"q{i}": "a" for i in range(10)}}
    human = {f"q{i}": "A" for i in range(10)}  # 사람 모두 a, 심판 9:1 → 일치 90%, 쏠림 100%
    res = pagr.evaluate(human, key, pw)
    assert res["agreement"] == 0.9 and res["kappa"] == 0.0 and res["verdict"] == "사용(쏠림 규칙)"
    human = {**{f"q{i}": "B" for i in range(5)}, **{f"q{i}": "A" for i in range(5, 10)}}
    res = pagr.evaluate(human, key, pw, revision=1)
    assert res["verdict"] == "제외" and "참고값" in res["win_rate"]
    assert pagr.evaluate({**human, "q0": ""}, key, pw)["blank"] == ["q0"]
