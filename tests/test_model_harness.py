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
    good = {"silent": 1.0, "fields": {f: 80.0 for f in gate.FIELDS}, "b_fields": {}, "flag_recall": {}, "all_ok": 0}
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
    assert labels == ["gpt-4.1-mini", "gpt-4o-mini", "local-ollama", "replay"]
    assert [p["label"] for p in cfg["prompts"]] == ["current", "before_ocr_fix"]
    for p in cfg["providers"][:3]:
        assert p["config"]["temperature"] == 0 and p["config"]["response_format"] == {"type": "json_object"}
        assert p["config"]["max_tokens"] == 512  # 건당 출력 상한 (정상 응답 최대 310토큰)
    assert cfg["providers"][2]["config"]["apiBaseUrl"].startswith("http://127.0.0.1:")  # 로컬 모델은 이 PC에서만


def test_prompt_versions_match_app():
    """current 판 = 운영 app/llm.extract가 보내는 글자"""
    sys.path.insert(0, str(ROOT / "eval"))
    try:
        import prompt_versions
    finally:
        sys.path.remove(str(ROOT / "eval"))
    assert prompt_versions.NEW == fields.extract_prompt(GIMHAE)
    assert "OCR 텍스트에는" not in prompt_versions.OLD and prompt_versions.OLD != prompt_versions.NEW
