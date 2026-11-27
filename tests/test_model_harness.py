"""모델 비교 하네스 (https://github.com/reddol18/docflow/issues/22) — LLM·실제 문서 없이 판정·집계·응답 해석만.
실제 행렬 실행(OpenAI·Ollama·저장 결과 되돌리기)은 eval/promptfoo/run.ps1로 하고 PR에 결과를 붙인다."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

from app import fields

ROOT = Path(__file__).parent.parent
PF = ROOT / "eval" / "promptfoo"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"harness_{name}", PF / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gate():
    before = list(sys.path)
    g = _load("gate")
    yield g
    sys.path[:] = before


GIMHAE = fields.load(fields.DIR / "gimhae.yaml")


def test_parse_extract_shapes():
    ok = fields.parse_extract(GIMHAE, json.dumps({"docno": {"value": "가상 12345-1", "confidence": 0.9},
                                                  "date": {"value": "2001-02-03", "confidence": "0.8"},
                                                  "recipient": "모양 틀림", "title": {"confidence": "높음"}}))
    assert ok["docno"] == {"value": "가상 12345-1", "confidence": 0.9}
    assert ok["date"]["confidence"] == 0.8
    assert ok["recipient"] == {"value": "", "confidence": 0.0}  # dict가 아니면 빈 값
    assert ok["title"] == {"value": "", "confidence": 0.0}  # 확신도가 숫자가 아니면 0
    assert set(ok) == {f["name"] for f in fields.prompt_fields(GIMHAE)}
    assert fields.parse_extract(GIMHAE, "[1, 2]")["docno"]["value"] == ""
    with pytest.raises(ValueError):
        fields.parse_extract(GIMHAE, "JSON 아님")


def _row(prov, prompt, doc, out='{"docno": {"value": "x"}}', prompt_tok=1000, comp_tok=100, ms=1500, cached=False,
         pid=None):
    return {"provider": {"id": pid or f"openai:chat:{prov}", "label": prov}, "prompt": {"label": prompt},
            "vars": {"vol": "가상-0001", "doc_id": doc}, "latencyMs": ms,
            "response": {"output": out, "cached": cached, "tokenUsage": {"prompt": prompt_tok, "completion": comp_tok}}}


def test_group_cost_latency(gate):
    rows = [_row("gpt-4.1-mini", "current", "d1", ms=1000), _row("gpt-4.1-mini", "current", "d2", ms=3000),
            _row("gpt-4.1-mini", "current", "d3", ms=9, cached=True),
            _row("gpt-4o-mini", "current", "d1"), _row("local-ollama", "current", "d1", pid="openai:chat:llama3.1:8b")]
    g = gate.group(rows)
    assert set(g) == {("gpt-4.1-mini", "current"), ("gpt-4o-mini", "current"), ("local-ollama", "current")}
    c = gate.cost_latency(g[("gpt-4.1-mini", "current")])
    # 단가는 app/llm.py PRICE (USD / 1M 토큰): 1000×0.40 + 100×1.60 = 560e-6 달러 / 건
    assert c["usd_per_doc"] == pytest.approx(560e-6)
    assert c["n"] == 3 and c["n_timed"] == 2  # 캐시 응답은 지연에서 뺀다
    assert c["ms_mean"] == 2000 and c["ms_p95"] == 3000
    assert gate.cost_latency(g[("local-ollama", "current")])["usd_per_doc"] == 0  # 단가 없는 로컬 모델 = 0


def _cand(silent, fields_, errors=0):
    return {"s": {"silent": silent, "fields": dict(zip(["docno", "date", "recipient", "title"], fields_))},
            "errors": errors}


def test_judge_thresholds(gate):
    crit = yaml.safe_load((PF / "criteria.yaml").read_text(encoding="utf-8"))
    assert crit["baseline"] == {"provider": "gpt-4.1-mini", "prompt": "current"}
    base = _cand(2.0, [64.0, 79.0, 46.0, 88.0])
    assert gate.judge(_cand(3.0, [61.0, 79.0, 46.0, 88.0]), base, crit) == []  # 딱 기준값까지는 통과
    why = gate.judge(_cand(3.1, [60.9, 79.0, 46.0, 88.0], errors=1), base, crit)
    assert len(why) == 3
    assert "표시 없이 틀린" in why[0] and "docno" in why[1] and "읽기 실패" in why[2]
    off = {"extract": {"silent_wrong_pp_max": None, "field_drop_pp_max": None, "parse_error_max": None}}
    assert gate.judge(_cand(50.0, [0, 0, 0, 0], errors=9), base, off) == []  # null = 보지 않음


def test_main_exit_codes(gate, tmp_path, monkeypatch):
    """기준을 어긴 후보가 있으면 1, 기준 후보가 없으면 2, 모두 통과면 0"""
    res = tmp_path / "results.json"
    res.write_text(json.dumps({"results": {"results": [_row("gpt-4.1-mini", "current", "d1"),
                                                       _row("gpt-4o-mini", "current", "d1")]}}), encoding="utf-8")
    good = {"silent": 1.0, "fields": {f: 80.0 for f in gate.FIELDS}, "b_fields": {f: 50.0 for f in gate.FIELDS},
            "flag_recall": {}, "all_ok": 0}
    bad = dict(good, silent=5.0)
    monkeypatch.setattr(gate.common, "load_vol", lambda vol: ([], {}))
    monkeypatch.setattr(gate, "rebuild", lambda res_, data: ({}, {}, 0))
    monkeypatch.setattr(gate, "metrics", lambda docs: {"n_gt": 1, "n_docs": 1})
    which = iter([good, bad])
    monkeypatch.setattr(gate, "summary", lambda m: next(which))
    monkeypatch.setattr(gate, "table", lambda rows: "")
    argv = ["gate.py", str(res), "--out", str(tmp_path)]
    monkeypatch.setattr(sys, "argv", argv)
    assert gate.main() == 1  # gpt-4o-mini: 표시 없이 틀린 비율 +4%p

    which = iter([good, good])
    assert gate.main() == 0
    assert json.loads((tmp_path / "gate.json").read_text(encoding="utf-8"))[1]["verdict"] == "통과"

    res.write_text(json.dumps({"results": {"results": [_row("gpt-4o-mini", "current", "d1")]}}), encoding="utf-8")
    which = iter([good])
    assert gate.main() == 2

    # 단가 모르는 원격 모델이 섞이면 0원으로 치지 않고 2 (채점 전에 멈춘다)
    res.write_text(json.dumps({"results": {"results": [_row("gpt-4.1-mini", "current", "d1"),
                                                       _row("가상-모델", "current", "d1")]}}), encoding="utf-8")
    which = iter([good, good])
    assert gate.main() == 2


JSON_FMT = {"type": "json_object"}
OLD_BASE_CFG = {"temperature": 0, "response_format": JSON_FMT}  # 지난 기준 파일처럼 max_tokens 없음 (promptfoo 기본)
NOW_CFG = [  # 실제 results.json처럼 config.providers에는 거른 제공자도 모두 들어 있다
    {"id": "openai:chat:gpt-4.1-mini", "label": "gpt-4.1-mini", "config": dict(OLD_BASE_CFG, max_tokens=512)},
    {"id": "openai:chat:가상:7b", "label": "local-ollama",
     "config": dict(OLD_BASE_CFG, max_tokens=512, apiBaseUrl="http://127.0.0.1:11434/v1")}]


def _vrow(prov, doc, vol, raw=None, **kw):
    r = _row(prov, "current", doc, **kw)
    r["vars"]["vol"] = vol
    r["prompt"]["raw"] = raw if raw is not None else f"지시문+가상 글자 {doc}"
    return r


def _old_results(tmp_path, base_cfg=OLD_BASE_CFG, base_pid=None):
    old = tmp_path / "old.json"
    pid = base_pid or "openai:chat:gpt-4.1-mini"
    old.write_text(json.dumps({"config": {"providers": [{"id": pid, "label": "gpt-4.1-mini", "config": base_cfg}]},
                               "results": {"results": [_vrow("gpt-4.1-mini", "d1", "가상-0001", pid=pid),
                                                       _vrow("gpt-4.1-mini", "d2", "가상-0001", pid=pid),
                                                       _vrow("gpt-4.1-mini", "d9", "가상-0002", pid=pid),
                                                       _vrow("gpt-4o-mini", "d1", "가상-0001")]}}), encoding="utf-8")
    return old


def _now(rows):
    return {"config": {"providers": NOW_CFG}, "results": {"results": rows}}


def _local(docs=("d1", "d2"), vol="가상-0001", raw=None):
    return [_vrow("local-ollama", d, vol, raw=raw, pid="openai:chat:가상:7b") for d in docs]


def test_baseline_from_stored_results(gate, tmp_path):
    """--baseline-results: 기준 줄만·이번 결과의 철만 가져오고, 설정 차이 max_tokens는 경고"""
    old = _old_results(tmp_path)
    want = ("gpt-4.1-mini", "current")
    res = tmp_path / "now.json"
    res.write_text(json.dumps(_now(_local())), encoding="utf-8")
    rows, cfg, ids = gate.load_results(res)
    assert ids == {"gpt-4.1-mini": "openai:chat:gpt-4.1-mini", "local-ollama": "openai:chat:가상:7b"}
    g, warns, err = gate.add_baseline(gate.group(rows), cfg, ids, old, want)
    assert err is None
    assert set(g) == {want, ("local-ollama", "current")}  # 기준만 가져오고 다른 후보(gpt-4o-mini)는 뺀다
    assert set(g[want]) == {"d1", "d2"}  # 이번 결과에 없는 철(가상-0002)은 뺀다
    assert len(warns) == 1 and "max_tokens" in warns[0] and "promptfoo 기본" in warns[0]
    assert gate.unpriced([r for x in g.values() for r in x.values()], cfg) == []  # 기준 단가는 이번 설정으로


@pytest.mark.parametrize("case, want_err", [
    ("already", "이미"), ("no_vol", "없다"), ("docs", "문서 1건"), ("prompt_raw", "지시문"),
    ("model", "기준 모델"), ("temperature", "temperature"), ("response_format", "response_format")])
def test_baseline_rejects(gate, tmp_path, case, want_err):
    """기준과 같은 조건이 아니면 비교하지 않는다 (main은 종료 코드 2)"""
    want = ("gpt-4.1-mini", "current")
    base_cfg, base_pid, rows = OLD_BASE_CFG, None, _local()
    if case == "already":
        rows = rows + [_vrow("gpt-4.1-mini", "d1", "가상-0001")]
    elif case == "no_vol":
        rows = _local(vol="가상-0003")
    elif case == "docs":
        rows = _local(docs=("d1",))
    elif case == "prompt_raw":  # 지시문·문서 글자가 바뀐 뒤의 후보
        rows = _local(raw="바뀐 지시문")
    elif case == "model":
        base_pid = "openai:chat:가상-다른-모델"
    elif case == "temperature":
        base_cfg = dict(OLD_BASE_CFG, temperature=0.7)
    else:
        base_cfg = {"temperature": 0}
    old = _old_results(tmp_path, base_cfg, base_pid)
    res = tmp_path / "now.json"
    res.write_text(json.dumps(_now(rows)), encoding="utf-8")
    rows_, cfg, ids = gate.load_results(res)
    _, _, err = gate.add_baseline(gate.group(rows_), cfg, ids, old, want)
    assert err and want_err in err


def test_baseline_main(gate, tmp_path, monkeypatch):
    """main: 기준 없이 돌린 결과 + --baseline-results → 판정, gate.md에 B 표·가져온 파일 이름·경고. 어긋나면 2"""
    old = _old_results(tmp_path)
    res = tmp_path / "results.json"
    res.write_text(json.dumps(_now(_local())), encoding="utf-8")
    s = {"silent": 1.0, "fields": {f: 80.0 for f in gate.FIELDS}, "b_fields": {f: 50.0 for f in gate.FIELDS},
         "flag_recall": {"serial": None, "date": None}, "all_ok": 0}
    monkeypatch.setattr(gate.common, "load_vol", lambda vol: ([], {}))
    monkeypatch.setattr(gate, "rebuild", lambda res_, data: ({}, {}, 0))
    monkeypatch.setattr(gate, "metrics", lambda docs: {"n_gt": 1, "n_docs": 2})
    monkeypatch.setattr(gate, "summary", lambda m: s)
    monkeypatch.setattr(sys, "argv", ["gate.py", str(res), "--out", str(tmp_path), "--baseline-results", str(old)])
    assert gate.main() == 0
    out = json.loads((tmp_path / "gate.json").read_text(encoding="utf-8"))
    assert [(r["provider"], r["verdict"]) for r in out] == [("gpt-4.1-mini", "기준"), ("local-ollama", "통과")]
    md = (tmp_path / "gate.md").read_text(encoding="utf-8")
    assert "텍스트 추출(B)만" in md and "| local-ollama | current | 50.0% |" in md and "old.json" in md
    assert "경고: 기준 설정 max_tokens" in md

    res.write_text(json.dumps(_now(_local(raw="바뀐 지시문"))), encoding="utf-8")
    assert gate.main() == 2


@pytest.fixture(scope="module")
def plan():
    return _load("plan")


def _plan(plan, providers="", prompts="", model="가상:7b"):
    read = lambda n: yaml.safe_load((PF / n).read_text(encoding="utf-8"))  # noqa: E731
    return plan.plan(read("promptfooconfig.yaml"), read("criteria.yaml"), providers, prompts, model)


def test_plan_filters_like_promptfoo(plan):
    """run.ps1이 promptfoo 부르기 전에: 걸러질 후보(id·label, 대소문자 구분), 키 필요 여부, 기준과 겹침"""
    p = _plan(plan, "^local-ollama$", "^current$")
    assert p == {"providers": ["local-ollama"], "prompts": ["current"], "need_key": False, "external": False,
                 "baseline_overlap": False}
    p = _plan(plan, "^openai:chat:가상:7b$")  # id로도 걸린다 (로컬 모델 이름은 채워서 본다)
    assert p["providers"] == ["local-ollama"] and not p["need_key"]
    assert _plan(plan, "GPT-4o")["providers"] == []  # 대소문자 구분 (promptfoo RegExp와 같게)
    p = _plan(plan, "gpt-4o-mini|local")
    assert p["providers"] == ["gpt-4o-mini", "local-ollama"] and p["need_key"] and not p["baseline_overlap"]
    p = _plan(plan, "mini", "^current$")  # 기준(gpt-4.1-mini·current)이 걸림 → run.ps1 -BaselineFrom이면 부르기 전에 멈춘다
    assert p["need_key"] and p["baseline_overlap"]
    assert not _plan(plan, "mini", "^before_ocr_fix$")["baseline_overlap"]  # 판이 다르면 겹치지 않는다
    assert _plan(plan, "", "")["baseline_overlap"]  # 빈 정규식 = 모두
    assert _plan(plan, "openai:chat:gpt-4\\.1")["providers"] == ["gpt-4.1-mini"]


def test_plan_remote_is_external(plan):
    """원격 OpenAI 호환 후보(#80): 키(OpenAI) 불필요, 외부라 합성 철만 — run.ps1이 common.py --require-synthetic으로 막는다"""
    read = lambda n: yaml.safe_load((PF / n).read_text(encoding="utf-8"))  # noqa: E731
    p = plan.plan(read("promptfooconfig.yaml"), read("criteria.yaml"), "^remote$", "^current$", "", "가상:27b")
    assert p["providers"] == ["remote"] and not p["need_key"] and p["external"] and not p["baseline_overlap"]
    p = plan.plan(read("promptfooconfig.yaml"), read("criteria.yaml"), "^openai:chat:가상:27b$", "", "", "가상:27b")
    assert p["providers"] == ["remote"]  # 원격 모델 이름도 채워서 id로 거른다
    assert _plan(plan, "^gpt-4\\.1-mini$")["external"]  # OpenAI도 외부
    assert not _plan(plan, "^(local-ollama|replay)$")["external"]
    # Ollama 클라우드 모델은 local-ollama label이어도 글자가 ollama.com으로 간다 → 외부
    assert _plan(plan, "^local-ollama$", model="gpt-oss:120b-cloud")["external"]
    assert _plan(plan, "^local-ollama$", model="가상:cloud")["external"]
    assert not _plan(plan, "^local-ollama$", model="cloudy:7b")["external"]


def test_price_of(gate):
    """모델 = 'openai:chat:' 뒤 전부, 로컬·replay = 0, 모르는 원격 모델 = None"""
    assert gate.price_of(_row("gpt-4.1-mini", "current", "d1")) == (0.40, 1.60)
    # ':'가 든 원격 모델 이름은 끝 조각(예전 split(':')[-1])으로 단가를 찾지 않는다
    assert gate.price_of(_row("x", "current", "d1", pid="openai:chat:ft:gpt-4.1-mini:가상조직::abc")) is None
    assert gate.price_of(_row("x", "current", "d1", pid="openai:chat:gpt-4.1-mini:가상")) is None
    local = _row("내-로컬", "current", "d1", pid="openai:chat:llama3.1:8b")
    assert gate.price_of(local) is None  # label도 주소도 로컬이라는 표시가 없으면 모름
    assert gate.price_of(local, {"내-로컬": {"apiBaseUrl": "http://127.0.0.1:11434/v1"}}) == (0, 0)
    assert gate.price_of(local, {"내-로컬": {"apiBaseUrl": "http://localhost:8080/v1"}}) == (0, 0)
    assert gate.price_of(local, {"내-로컬": {"apiBaseUrl": "https://가상.example/v1"}}) is None
    assert gate.price_of(_row("local-ollama", "current", "d1", pid="openai:chat:qwen2.5:7b")) == (0, 0)
    assert gate.price_of(_row("replay", "current", "d1", pid="file://replay.py")) == (0, 0)
    # 원격 GPU(label remote, #80)는 런파드 프록시·SSH 터널(127.0.0.1)일 때만 토큰 단가 0 — 비용은 GPU 시간으로 따로
    rem = _row("remote", "current", "d1", pid="openai:chat:gemma3:27b")
    assert gate.price_of(rem, {"remote": {"apiBaseUrl": "https://abc123-11434.proxy.runpod.net/v1"}}) == (0, 0)
    assert gate.price_of(rem, {"remote": {"apiBaseUrl": "http://127.0.0.1:18434/v1"}}) == (0, 0)
    # 그 밖의 https 주소(유료 OpenAI 호환 API일 수 있음)·꼬리만 비슷한 주소는 단가 모름 → gate 종료 코드 2
    assert gate.price_of(rem, {"remote": {"apiBaseUrl": "https://api.가상.example/v1"}}) is None
    assert gate.price_of(rem, {"remote": {"apiBaseUrl": "https://proxy.runpod.net.가상.example/v1"}}) is None
    assert gate.unpriced([rem], {"remote": {"apiBaseUrl": "https://api.가상.example/v1"}}) == ["openai:chat:gemma3:27b"]
    # 옛 결과처럼 provider가 문자열이어도 id를 읽는다 (gate.md 모델 id 줄)
    assert gate.provider_id({"provider": "openai:chat:gpt-4.1-mini"}) == "openai:chat:gpt-4.1-mini"
    assert gate.provider_id({"provider": {"id": "openai:chat:가상"}}) == "openai:chat:가상"
    assert gate.provider_id({}) == "?"
    assert gate.price_of(_row("x", "current", "d1", pid="anthropic:messages:가상")) is None
    assert gate.unpriced([_row("gpt-4o-mini", "current", "d1"), local]) == ["openai:chat:llama3.1:8b"]
    with pytest.raises(ValueError):
        gate.cost_latency({"d1": local})


def test_response_kept_when_assertion_fails(gate, monkeypatch):
    """promptfoo는 단언(check.py) 실패·예외에도 error를 채운다 → 응답이 있으면 읽고, 제공자 오류·응답 없음만 읽기 실패"""
    good = json.dumps({"docno": {"value": "가상 12345-7", "confidence": 0.9}})
    rows = {
        "d1": dict(_row("m", "current", "d1", out=good), error="Python 단언 예외 (가상)", failureReason=1),
        "d2": dict(_row("m", "current", "d2", out=None), error="API 오류 (가상)", failureReason=2),
        "d3": dict(_row("m", "current", "d3"), response={"error": "가상 시간 초과", "output": "{}"}, failureReason=2),
        "d4": dict(_row("m", "current", "d4", out='{"docno": {"value": "끊긴'), error="JSON 아님", failureReason=1),
    }
    assert gate.response_of(rows["d1"]) == good
    assert gate.response_of(rows["d2"]) is None and gate.response_of(rows["d3"]) is None
    assert gate.assert_failed(rows) == 2  # d1(응답 읽힘)·d4(응답은 있으나 JSON 아님)

    def fake_assemble(data, b_of):
        docs = {d: b_of({"doc_id": d}) for d in ["d1", "d2", "d3", "d4", "d5"]}  # d5 = 결과 줄 없음
        return docs, {}

    monkeypatch.setattr(gate, "assemble", fake_assemble)
    docs, _, errors = gate.rebuild(rows, {})
    assert docs["d1"]["docno"]["value"] == "가상 12345-7"  # 단언이 실패해도 좋은 응답은 그대로 채점
    assert errors == 4  # d2·d3 제공자 오류, d4 JSON 아님, d5 응답 없음
    assert docs["d2"] == gate.EMPTY


def test_config_matrix():
    cfg = yaml.safe_load((PF / "promptfooconfig.yaml").read_text(encoding="utf-8"))
    labels = [p["label"] for p in cfg["providers"]]
    assert labels == ["gpt-4.1-mini", "gpt-4o-mini", "local-ollama", "remote", "replay"]
    assert [p["label"] for p in cfg["prompts"]] == ["current", "before_ocr_fix"]
    for p in cfg["providers"][:4]:
        assert p["config"]["temperature"] == 0 and p["config"]["response_format"] == {"type": "json_object"}
        assert p["config"]["max_tokens"] == 512  # 건당 출력 상한 (정상 응답 최대 310토큰)
    assert cfg["providers"][2]["config"]["apiBaseUrl"].startswith("http://127.0.0.1:")  # 로컬 모델은 이 PC에서만
    remote = cfg["providers"][3]["config"]
    # 원격 키는 환경변수 이름으로만 (값이 설정·결과 파일에 남지 않게), apiKey를 두지 않는다
    assert remote["apiKeyEnvar"] == "DOCFLOW_REMOTE_API_KEY" and "apiKey" not in remote
    assert remote["apiBaseUrl"] == "{{ env.DOCFLOW_REMOTE_BASE }}"


def _common(monkeypatch, work):
    monkeypatch.setenv("DOCFLOW_EVAL_WORK", str(work))
    monkeypatch.delenv("DOCFLOW_EVAL_VOLS", raising=False)
    before = list(sys.path)
    c = _load("common")
    sys.path[:] = before
    return c


def test_common_gt_and_synthetic_guard(monkeypatch, tmp_path):
    """정답은 work 폴더의 gt/review.csv, 외부 후보는 synth_manifest.json에 있는 철에만 (#80)"""
    (tmp_path / "platform").mkdir()
    for v in ("SYNTH0001-1995-0001", "가상-0001"):
        (tmp_path / "platform" / f"{v}.json").write_text("[]", encoding="utf-8")
    (tmp_path / "gt").mkdir()
    (tmp_path / "gt" / "review.csv").write_text(
        "page,확인,is_start,docno,date,recipient,title,check,note,saved\n"
        "SYNTH0001-1995-0001-0001,경계,Y,가상 12345-1,1995-01-02,가상수신,가상제목,,합성,\n"
        "SYNTH0001-1995-0001-0002,경계,N,,,,,,합성,\n", encoding="utf-8-sig")
    c = _common(monkeypatch, tmp_path)
    rows, idx, bnd, src = c.load_gt()
    assert len(rows) == 2 and list(idx) == ["SYNTH0001-1995-0001-0001"] and idx["SYNTH0001-1995-0001-0001"]["date"] == "1995-01-02"
    assert bnd == {"SYNTH0001-1995-0001-0001": True, "SYNTH0001-1995-0001-0002": False} and set(src.values()) == {"human"}
    assert c.non_synthetic() == ["SYNTH0001-1995-0001", "가상-0001"]  # 목록이 없으면 모두 합성 아님
    (tmp_path / "synth_manifest.json").write_text(json.dumps({"vols": [{"vol": "SYNTH0001-1995-0001"}]}), encoding="utf-8")
    assert c.non_synthetic() == ["가상-0001"]
    assert c.non_synthetic(["SYNTH0001-1995-0001"]) == []
    # 목록에 있어도 이름이 SYNTH…가 아니면 합성으로 보지 않는다 (손으로 쓴 목록 막기)
    (tmp_path / "synth_manifest.json").write_text(json.dumps({"vols": [{"vol": "SYNTH0001-1995-0001"}, {"vol": "가상-0001"}]}),
                                                  encoding="utf-8")
    assert c.non_synthetic() == ["가상-0001"]


def test_schema_per_vol_form(monkeypatch, tmp_path, gate):
    """철마다 그 양식의 칸 정의·지시문으로 추출·판정·조립 (https://github.com/reddol18/docflow/issues/139).
    manifest에 form이 없거나 실제 철이면 김해 — 김해 철의 current 지시문은 예전과 같은 글자(prompt_versions.NEW)"""
    (tmp_path / "platform").mkdir()
    vols = {"SYNTH0001-1995-0001": None, "SYNTH0002A-1995-0001": "agency1990", "SYNTH0003D-2004-0001": "draft2000"}
    (tmp_path / "synth_manifest.json").write_text(json.dumps(
        {"vols": [{"vol": v, **({"form": f} if f else {})} for v, f in vols.items()]}), encoding="utf-8")
    c = _common(monkeypatch, tmp_path)
    assert [c.form_of(v) for v in vols] == ["gimhae", "agency1990", "draft2000"] and c.form_of("가상-0001") == "gimhae"
    assert c.schema_file_of("SYNTH0003D-2004-0001").name == "synth_draft2000.yaml"
    assert c.schema_of("SYNTH0001-1995-0001") == GIMHAE
    draft = c.schema_of("SYNTH0003D-2004-0001")
    assert not any(f.get("rule") for f in draft["fields"])  # 김해 규칙 칸이 섞이지 않는다
    # 지시문: gate가 불러 둔 prompts 모듈(같은 common)로 — WORK만 이 시험 폴더로
    prompts = sys.modules["prompts"]
    monkeypatch.setattr(prompts.common, "WORK", tmp_path)
    assert prompts.system_of("current", "SYNTH0001-1995-0001") == prompts.NEW
    assert prompts.system_of("current", "SYNTH0003D-2004-0001") == fields.extract_prompt(draft) != prompts.NEW
    assert prompts.system_of("current", "SYNTH0002A-1995-0001") != prompts.NEW
    assert prompts.system_of("before_ocr_fix", "SYNTH0003D-2004-0001") == prompts.OLD
    # 조립: 새 양식 철에서만 app/graph 칸 정의를 잠시 바꾸고 되돌린다
    monkeypatch.setattr(gate.common, "WORK", tmp_path)
    before = gate.graph.SCHEMA
    with gate.graph_schema("SYNTH0003D-2004-0001"):
        assert gate.graph.SCHEMA == draft
    with gate.graph_schema("SYNTH0001-1995-0001"):
        assert gate.graph.SCHEMA is before
    assert gate.graph.SCHEMA is before
    assert gate.empty_of(draft) == {f["name"]: {"value": "", "confidence": 0.0} for f in fields.prompt_fields(draft)}


def test_load_gt_no_silent_poc_fallback(monkeypatch, tmp_path):
    """기본(PoC) work가 아닌데 정답 파일이 없으면 PoC 정답으로 넘어가지 않고 멈춘다"""
    c = _common(monkeypatch, tmp_path)
    with pytest.raises(SystemExit):
        c.load_gt()


def _cli(tmp_path, *args, vols=""):
    import os
    import subprocess
    env = {**os.environ, "DOCFLOW_EVAL_WORK": str(tmp_path), "DOCFLOW_EVAL_VOLS": vols, "PYTHONUTF8": "1"}
    return subprocess.run([sys.executable, "-I", str(PF / "common.py"), *args], env=env, capture_output=True,  # noqa: S603
                          check=False).returncode


def test_require_synthetic_cli(tmp_path):
    """run.ps1이 부르는 --require-synthetic: 합성만이면 0, 아니면 2, 모르는 인자·인자 없음·없는 철도 0이 아님 (닫힌 쪽)"""
    (tmp_path / "platform").mkdir()
    (tmp_path / "platform" / "SYNTH0001-1995-0001.json").write_text("[]", encoding="utf-8")
    (tmp_path / "synth_manifest.json").write_text(json.dumps({"vols": [{"vol": "SYNTH0001-1995-0001"}]}), encoding="utf-8")
    assert _cli(tmp_path, "--require-synthetic") == 0
    assert _cli(tmp_path, "--require-synthetic", "--더") != 0
    assert _cli(tmp_path) != 0
    assert _cli(tmp_path, "--require-synthetic", vols="SYNTH9999-1990-0001") == 2  # 없는 철
    (tmp_path / "platform" / "가상-0001.json").write_text("[]", encoding="utf-8")
    assert _cli(tmp_path, "--require-synthetic") == 2
    assert _cli(tmp_path, "--require-synthetic", vols="SYNTH0001-1995-0001") == 0
    empty = tmp_path / "empty"
    (empty / "platform").mkdir(parents=True)
    assert _cli(empty, "--require-synthetic") == 2  # 고른 철 0개도 통과시키지 않는다


def test_scoring_uses_work_gt(gate, monkeypatch):
    """check.py·gate.py는 정답을 common.load_gt(work 폴더 기준)에서 읽는다 — rescore.load_gt(PoC 고정)가 아니라"""
    import ast
    import types
    tree = ast.parse((PF / "check.py").read_text(encoding="utf-8"))
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert "load_gt" not in imported
    calls = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert "common.load_gt" in calls
    seen = {}
    fake = types.ModuleType("score")
    fake.metrics = lambda docs, gt=None, form_of=None: seen.setdefault("gt", gt)
    monkeypatch.setitem(sys.modules, "score", fake)
    monkeypatch.setattr(gate.common, "load_gt", lambda: "work-gt")
    gate.metrics({})
    assert seen["gt"] == "work-gt"


def test_prompt_versions_match_app():
    """current 판 = 운영 app/llm.extract가 보내는 글자"""
    sys.path.insert(0, str(ROOT / "eval"))
    try:
        import prompt_versions
    finally:
        sys.path.remove(str(ROOT / "eval"))
    assert prompt_versions.NEW == fields.extract_prompt(GIMHAE)
    assert "OCR 텍스트에는" not in prompt_versions.OLD and prompt_versions.OLD != prompt_versions.NEW
