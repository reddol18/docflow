"""#8 평가 도구 (eval/forms_compare.py·eval/forms_vision.py) — 합성만 받는지, 단가·판정. 값은 모두 지어낸 것."""
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))
import forms_vision as fv  # noqa: E402


def test_price_same_as_app():
    pytest.importorskip("openai")
    sys.path.insert(0, str(ROOT))
    from app import llm
    assert fv.PRICE["gpt-4.1-mini"] == llm.PRICE["gpt-4.1-mini"]
    assert fv.IMAGE_TOKENS == llm.IMAGE_TOKENS["gpt-4.1-mini"]


def _vol(root, name, synthetic=True):
    d = root / name
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"synthetic": synthetic, "vol": name}), encoding="utf-8")
    (d / f"{name}-0001.jpg").write_bytes(b"x")
    return d


def test_only_synthetic_dirs(tmp_path):
    _vol(tmp_path / "a", "SYNTH0001A-1995-0001")
    dirs = fv.synthetic_dirs([tmp_path / "a"])
    assert fv.image_of("SYNTH0001A-1995-0001-0001", dirs).name == "SYNTH0001A-1995-0001-0001.jpg"
    with pytest.raises(SystemExit):
        fv.image_of("가상-1995-0001-0001", dirs)
    _vol(tmp_path / "b", "가상-1995-0001", synthetic=False)
    with pytest.raises(SystemExit, match="합성 철이 아니다"):
        fv.synthetic_dirs([tmp_path / "b"])


def test_estimate_and_cap(tmp_path):
    assert fv.estimate(100, "gpt-4.1-mini") == pytest.approx(100 * (2800 * 0.40 + 200 * 1.60) / 1e6)
    _vol(tmp_path / "a", "SYNTH0001A-1995-0001")
    gt = tmp_path / "gt.csv"
    gt.write_text("page,확인,is_start,docno,date,recipient,title,check,note,saved\n"
                  "SYNTH0001A-1995-0001-0001,경계,Y,가상 1-1,1995-01-02,가상,가상 알림,,합성,\n", encoding="utf-8-sig")
    rep = tmp_path / "forms.json"
    rep.write_text(json.dumps({"forms": {"agency1990": {"boundary_wrong": [], "field_wrong": [
        {"page": "SYNTH0001A-1995-0001-0001", "fields": ["title"], "found": True}]}}}), encoding="utf-8")
    args = ["--forms", str(rep), "--bundles", str(tmp_path / "a"), "--gt", str(gt), "--out", str(tmp_path / "o")]
    assert fv.main([*args, "--max-usd", "0.0001"]) == 2  # 추정이 상한을 넘으면 부르지 않는다
    assert fv.main([*args, "--dry-run"]) == 0


def test_judge_and_summary():
    idx = {"P-0001": {"docno": "가상 1-1", "date": "1995-01-02", "recipient": "가상", "title": "가상 알림"}}
    t = {"kind": "fields", "form": "draft2000", "page": "P-0001", "fields": ["title", "date"]}
    j = fv.judge(t, {"docno": "가상 1-1", "date": "1995-01-02", "recipient": "가상", "title": "가상 통보"}, idx, {})
    assert j["fixed"] == {"title": False, "date": True} and j["all4"] is False
    b = fv.judge({"kind": "boundary", "page": "P-0002"}, {"first_page": False}, idx, {"P-0002": False})
    assert b["ok"] is True
    assert fv.judge(t, None, idx, {}) == {"read_fail": True}
    s = fv.summarize([{**t, "usd": 0.001, "judge": j}, {"kind": "boundary", "form": "draft2000", "usd": 0.001, "judge": b}])
    k = s["draft2000"]
    assert k["boundary"] == [1, 1] and k["fields_cells"] == [1, 2] and k["fields_pages"] == [0, 1]


def test_forms_compare_refuses_repo_paths(tmp_path):
    sys.path.insert(0, str(ROOT / "eval"))
    import forms_compare
    with pytest.raises(SystemExit, match="저장소 밖"):
        forms_compare.main(["--work", str(ROOT), "--results", "x.json", "--out", str(tmp_path)])


# ---------- 코드 리뷰 1차 (https://github.com/reddol18/docflow/issues/8) ----------

P1 = "SYNTH0001A-1995-0001-0001"
GT_HEAD = "page,확인,is_start,docno,date,recipient,title,check,note,saved\n"


class FakeClient:
    """가짜 OpenAI — 차례대로 응답·예외를 낸다 (실제 호출 없음)"""

    def __init__(self, script):
        self.script, self.n = list(script), 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.n += 1
        x = self.script.pop(0)
        if isinstance(x, BaseException):
            raise x
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=10),
                               choices=[SimpleNamespace(message=SimpleNamespace(content=x))])


def _vision_case(tmp_path, boundary, fields):
    _vol(tmp_path / "a", "SYNTH0001A-1995-0001")
    gt = tmp_path / "gt.csv"
    gt.write_text(GT_HEAD + f"{P1},경계,Y,가상 1-1,1995-01-02,가상,가상 알림,,합성,\n", encoding="utf-8-sig")
    rep = tmp_path / "forms.json"
    rep.write_text(json.dumps({"forms": {"agency1990": {"boundary_wrong": boundary, "field_wrong": fields}}}),
                   encoding="utf-8")
    return ["--forms", str(rep), "--bundles", str(tmp_path / "a"), "--gt", str(gt), "--out", str(tmp_path / "o")]


def test_vision_saves_rows_when_stopped(tmp_path, monkeypatch):
    """중간에 멈춰도(Ctrl+C) 이미 낸 호출 결과·누적 비용이 남고, 호출 실패는 그 쪽만 읽기 실패"""
    args = _vision_case(tmp_path, [{"page": P1, "gt_start": True, "pred_start": False}],
                        [{"page": P1, "fields": ["title"], "found": True}] * 2)
    fake = FakeClient(['{"first_page": true}', RuntimeError("가상 실패"), KeyboardInterrupt()])
    monkeypatch.setattr(fv, "make_client", lambda key, timeout=60: fake)
    monkeypatch.setenv("OPENAI_API_KEY", "가짜")
    with pytest.raises(KeyboardInterrupt):
        fv.main(args)
    assert fake.n == 3
    rep = json.loads((tmp_path / "o" / "vision_gpt-4.1-mini.json").read_text(encoding="utf-8"))
    rows = rep["rows"]
    assert len(rows) == 2 and rows[0]["judge"]["ok"] is True
    assert rows[1]["judge"] == {"read_fail": True} and rows[1]["error"] == "RuntimeError" and rows[1]["usd"] == 0
    assert rep["usd"] == pytest.approx(rows[0]["usd"]) and rep["usd"] > 0
    k = rep["summary"]["agency1990"]
    assert k["read_fail"] == 1 and k["fields_cells"] == [0, 1]
    assert k["cells_by_field"] == {"title": [0, 1]}  # 읽기 실패도 칸별 분모에 든다


def test_vision_checks_gt_before_calls(tmp_path, monkeypatch):
    """정답에 없는 대상 쪽(경계 대상인데 '경계' 확인 쪽 아님, 칸 대상인데 시작 쪽 아님)이면 부르기 전에 멈춘다"""
    p2 = "SYNTH0001A-1995-0001-0002"
    args = _vision_case(tmp_path / "x", [], [{"page": P1, "fields": ["title"], "found": True}])
    monkeypatch.setattr(fv, "make_client", lambda *a, **k: pytest.fail("부르면 안 된다"))
    gt = Path(args[args.index("--gt") + 1])
    gt.write_text(GT_HEAD + f"{P1},,Y,가상 1-1,1995-01-02,가상,가상 알림,,합성,\n", encoding="utf-8-sig")
    assert fv.missing_gt([{"kind": "boundary", "page": P1}], *fv.scoring.load_gt(gt)[1:3]) == [P1]
    assert fv.missing_gt([{"kind": "fields", "page": p2}], *fv.scoring.load_gt(gt)[1:3]) == [p2]
    rep = Path(args[args.index("--forms") + 1])
    rep.write_text(json.dumps({"forms": {"agency1990": {
        "boundary_wrong": [{"page": P1, "gt_start": True, "pred_start": False}], "field_wrong": []}}}), encoding="utf-8")
    with pytest.raises(SystemExit, match="정답에 없는 대상 쪽"):
        fv.main([*args, "--dry-run"])


def test_vision_client_ignores_proxy_env():
    pytest.importorskip("openai")
    c = fv.make_client("가짜키-시험용")
    assert c._client._trust_env is False  # HTTP(S)_PROXY 환경 변수를 따르지 않는다


def test_reject_real_data_paths(tmp_path, monkeypatch):
    cc = fv.cc
    for bad in (tmp_path / "aihub_extracted" / "88", tmp_path / "records_poc" / "work",
                tmp_path / "records_poc" / "work" / "gt" / "review.csv", cc.POC_WORK, cc.POC_WORK / "platform"):
        with pytest.raises(SystemExit, match="실제 자료"):
            cc.reject_real_data(bad)
    for ok in (tmp_path / "records_poc" / "work_synth" / "w47", tmp_path / "synth_bundles" / "a"):
        assert cc.reject_real_data(ok) == ok.resolve()
    args = _vision_case(tmp_path / "v", [], [])
    real = tmp_path / "aihub_extracted"
    real.mkdir()
    with pytest.raises(SystemExit, match="실제 자료"):
        fv.main([*args, "--bundles", str(real), "--dry-run"])


def test_default_gt_needs_platform_dir(tmp_path):
    import scoring
    (tmp_path / "gt").mkdir()
    (tmp_path / "gt" / "review.csv").write_text(GT_HEAD, encoding="utf-8-sig")
    with pytest.raises(SystemExit, match="--gt"):
        scoring.default_gt([tmp_path / "가상철.json"])  # <work>/가상철.json — platform/ 아래 아님
    (tmp_path / "platform").mkdir()
    assert scoring.default_gt([tmp_path / "platform" / "가상철.json"]) == tmp_path / "gt" / "review.csv"


# ---------- forms_compare.py — 실제 gate 모듈을 가짜 합성 work에 대고 ----------

VA, VD = "SYNTH0001A-1995-0001", "SYNTH0002D-2004-0001"
G = {"docno": "가상 1-1", "date": "1995-01-02", "recipient": "가상", "title": "가상 알림"}


@pytest.fixture()
def fc(tmp_path, monkeypatch):
    """(forms_compare 모듈, gate 모듈, work) — main 안의 `import gate`가 이 gate를 쓴다 (common.WORK = 가짜 work)"""
    import forms_compare
    work = tmp_path / "work"
    (work / "platform").mkdir(parents=True)
    monkeypatch.setenv("DOCFLOW_EVAL_WORK", str(work))  # main이 바꿔도 시험 뒤 되돌린다
    monkeypatch.delenv("DOCFLOW_EVAL_VOLS", raising=False)
    for p in (ROOT / "eval" / "synth", ROOT / "eval", ROOT / "eval" / "promptfoo", ROOT):
        monkeypatch.syspath_prepend(str(p))
    spec = importlib.util.spec_from_file_location("c8_gate", ROOT / "eval" / "promptfoo" / "gate.py")
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    monkeypatch.setattr(gate.common, "WORK", work)
    monkeypatch.setitem(sys.modules, "gate", gate)
    monkeypatch.delitem(sys.modules, "score", raising=False)  # 다른 시험의 가짜 score가 남지 않게
    gate.common.load_gt.cache_clear()
    yield forms_compare, gate, work
    gate.common.load_gt.cache_clear()


def _doc(vol, page):
    import score
    return {"vol": vol, "doc_id": page, "b": {f: {"value": G[f]} for f in score.FIELDS},
            "fields": {**G, "serial": "1"}, "info": {"b_serial": "1", "b_date": G["date"]}, "flags": {}}


def _work(work, provider):
    (work / "synth_manifest.json").write_text(json.dumps({"vols": [{"vol": VA, "form": "agency1990"},
                                                                    {"vol": VD, "form": "draft2000"}]}), encoding="utf-8")
    for v in (VA, VD):
        (work / "platform" / f"{v}.json").write_text("[]", encoding="utf-8")
    (work / "gt").mkdir()
    g = ",".join(G[f] for f in ("docno", "date", "recipient", "title"))
    (work / "gt" / "review.csv").write_text(GT_HEAD + f"{VA}-0001,경계,Y,{g},,합성,\n{VA}-0002,경계,N,,,,,,합성,\n"
                                            f"{VD}-0001,경계,Y,{g},,합성,\n", encoding="utf-8-sig")
    label, pid, cfg = provider
    res = work / "results.json"
    res.write_text(json.dumps({"config": {"providers": [{"id": pid, "label": label, "config": cfg}]},
                               "results": {"results": [{"provider": {"id": pid, "label": label}, "prompt": {"label": "current"},
                                                        "vars": {"doc_id": f"{VA}-0001"}, "latencyMs": 100,
                                                        "response": {"output": "{}", "tokenUsage": {"prompt": 10, "completion": 5}}}]}}),
                   encoding="utf-8")
    return res


def test_forms_compare_local_model_and_empty_form(fc, tmp_path, monkeypatch):
    """apiBaseUrl 로컬 후보도 비용(0원)을 센다 (예전: cfg를 버려 ValueError), 문서를 하나도 못 찾은 양식(draft2000)도 0으로 남는다"""
    forms_compare, gate, work = fc
    res = _work(work, ("local", "openai:chat:가상-로컬:8b", {"apiBaseUrl": "http://127.0.0.1:11434/v1"}))
    monkeypatch.setattr(gate.common, "load_vol", lambda v: None)
    monkeypatch.setattr(gate, "rebuild", lambda r, data: ({f"{VA}-0001": _doc(VA, f"{VA}-0001")}, {}, 0))
    out = tmp_path / "out"
    assert forms_compare.main(["--work", str(work), "--results", str(res), "--out", str(out),
                               "--provider", "local", "--name", "t"]) == 0
    rep = json.loads((out / "forms_t.json").read_text(encoding="utf-8"))
    assert rep["usd"] == 0 and rep["n_calls"] == 1
    assert set(rep["forms"]) == {"agency1990", "draft2000"}
    a, d = rep["forms"]["agency1990"], rep["forms"]["draft2000"]
    assert a["all_ok"] == [1, 1] and a["boundary"]["recall"] == [1, 1] and a["docs_split"] == 1
    assert d["docs_split"] == 0 and d["docs_gt"] == 1 and d["all_ok"] == [0, 1] and d["boundary"]["recall"] == [0, 1]
    assert d["field_wrong"] == [{"page": f"{VD}-0001", "fields": list(d["fields_b"]), "found": False}]


def test_forms_compare_unpriced_stops(fc, tmp_path, monkeypatch, capsys):
    forms_compare, gate, work = fc
    res = _work(work, ("remote-x", "openai:chat:가상-유료모델", {}))
    monkeypatch.setattr(gate, "rebuild", lambda *a: pytest.fail("단가 모르면 채점 전에 멈춘다"))
    assert forms_compare.main(["--work", str(work), "--results", str(res), "--out", str(tmp_path / "o"),
                               "--provider", "remote-x"]) == 2
    assert "단가를 모르는 모델" in capsys.readouterr().err


def test_forms_compare_empty_work_message(fc, tmp_path):
    forms_compare, _, work = fc
    with pytest.raises(SystemExit, match="철 결과가 없다"):
        forms_compare.main(["--work", str(work), "--results", str(work / "r.json"), "--out", str(tmp_path / "o")])
