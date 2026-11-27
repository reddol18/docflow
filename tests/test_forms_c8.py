"""#8 평가 도구 (eval/forms_compare.py·eval/forms_vision.py) — 합성만 받는지, 단가·판정. 값은 모두 지어낸 것."""
import json
import sys
from pathlib import Path

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
