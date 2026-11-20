"""합성 철 → 하네스 work 폴더 (eval/synth/to_work.py, https://github.com/reddol18/docflow/issues/80).
PaddleOCR 없이 가짜 OCR로 — 문서 나누기·정답 잇기·합성 목록·합성 아닌 철 거부만 본다."""
import csv
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


@pytest.fixture(scope="module")
def tw():
    before = list(sys.path)
    spec = importlib.util.spec_from_file_location("synth_to_work", ROOT / "eval" / "synth" / "to_work.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod
    sys.path[:] = before


def _bundle(src, vol, pages, starts, synthetic=True):
    d = src / vol
    d.mkdir(parents=True)
    rows = []
    for n in range(1, pages + 1):
        pid = f"{vol}-{n:04d}"
        (d / f"{pid}.jpg").write_bytes(b"")  # 가짜 OCR은 이미지를 읽지 않는다
        s = n in starts
        rows.append({"page": pid, "확인": "경계", "is_start": "Y" if s else "N", "docno": "가상 12345-1" if s else "",
                     "date": "1995-01-02" if s else "", "recipient": "", "title": "", "check": "", "note": "합성", "saved": ""})
    with (d / "gt.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    (d / "meta.json").write_text(json.dumps({"vol": vol, "synthetic": synthetic, "seed": 1, "level": "mix",
                                             "generator": "synth-0.1"}), encoding="utf-8")


def test_run_builds_work(tw, tmp_path, monkeypatch):
    src, work = tmp_path / "src", tmp_path / "work"
    _bundle(src, "SYNTH0001-1995-0001", 5, {1, 3})
    _bundle(src, "SYNTH0002-1996-0001", 2, {2})  # 첫 쪽이 시작이 아니면 운영 split처럼 버린다 (orphans)
    starts = {"SYNTH0001-1995-0001-0001", "SYNTH0001-1995-0001-0003", "SYNTH0002-1996-0001-0002"}
    monkeypatch.setattr(tw.layout, "is_start", lambda d: (d["page"] in starts, []))
    calls = []

    def fake_ocr(path):
        calls.append(path.stem)
        return {"page": path.stem, "lines": ["가상"], "items": [{"text": "가상", "score": 1.0, "box": [0, 0, 100, 20]}]}

    m = tw.run(src, work, ocr_page=fake_ocr)
    assert [(v["vol"], v["pages"], v["docs_gt"], v["docs_split"]) for v in m["vols"]] == [
        ("SYNTH0001-1995-0001", 5, 2, 2), ("SYNTH0002-1996-0001", 2, 1, 1)]
    docs = json.loads((work / "platform" / "SYNTH0001-1995-0001.json").read_text(encoding="utf-8"))
    assert [d["pages"] for d in docs] == [["SYNTH0001-1995-0001-0001", "SYNTH0001-1995-0001-0002"],
                                          ["SYNTH0001-1995-0001-0003", "SYNTH0001-1995-0001-0004", "SYNTH0001-1995-0001-0005"]]
    d = docs[0]
    assert all(v == {"value": "", "confidence": 0.0} for v in d["b"].values())  # LLM 값은 비움 — 하네스가 채운다
    assert d["hw"] == {"docno": {"text": "", "conf": 0.0}, "date": {"text": "", "conf": 0.0}}
    assert d["info"]["head_ratio"] == tw.HEAD_RATIOS[-1] and all(d["info"]["box_fail"].values())  # 못 찾으면 끝까지 넓힘
    with (work / "gt" / "review.csv").open(encoding="utf-8-sig") as f:
        assert len(list(csv.DictReader(f))) == 7
    assert json.loads((work / "synth_manifest.json").read_text(encoding="utf-8"))["vols"][0]["seed"] == 1
    assert (work / "ocr_platform" / "SYNTH0001-1995-0001" / "SYNTH0001-1995-0001-0001.json").exists()
    n = len(calls)
    tw.run(src, work, ocr_page=fake_ocr)
    assert len(calls) == n  # 이미 읽은 쪽은 다시 OCR하지 않는다


def test_rejects_non_synthetic(tw, tmp_path):
    _bundle(tmp_path, "가상-0001", 1, {1}, synthetic=False)
    with pytest.raises(SystemExit):
        tw.bundles(tmp_path)


def test_work_outside_repo(tw):
    assert tw.main([str(ROOT), str(ROOT / "eval" / "가상work")]) == 2
