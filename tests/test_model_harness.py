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
    monkeypatch.setattr(gate, "rebuild", lambda res_, vols: ({}, {}, 0))
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


def test_config_matrix():
    cfg = yaml.safe_load((PF / "promptfooconfig.yaml").read_text(encoding="utf-8"))
    labels = [p["label"] for p in cfg["providers"]]
    assert labels == ["gpt-4.1-mini", "gpt-4o-mini", "local-ollama", "replay"]
    assert [p["label"] for p in cfg["prompts"]] == ["current", "before_ocr_fix"]
    for p in cfg["providers"][:3]:
        assert p["config"]["temperature"] == 0 and p["config"]["response_format"] == {"type": "json_object"}
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
