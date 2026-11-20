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


def _fake_ocr(calls=None):
    def ocr(path):
        if calls is not None:
            calls.append(path.stem)
        return {"lines": ["가상"], "items": [{"text": "가상", "score": 1.0, "box": [0, 0, 100, 20]}]}
    return ocr


def test_rejects_non_synthetic(tw, tmp_path):
    _bundle(tmp_path, "가상-0001", 1, {1}, synthetic=False)
    with pytest.raises(SystemExit):
        tw.bundles(tmp_path)


def test_rejects_name_without_synth_prefix(tw, tmp_path):
    _bundle(tmp_path, "가상-0001", 1, {1})  # synthetic=true여도 이름이 SYNTH…가 아니면 거부
    with pytest.raises(SystemExit):
        tw.bundles(tmp_path)


def test_refuses_real_work(tw, tmp_path, monkeypatch):
    """PoC 확정 정답이 든 work(또는 목록 없이 정답이 있는 폴더, 목록 밖 철이 있는 폴더)에는 쓰지 않는다"""
    src = tmp_path / "src"
    _bundle(src, "SYNTH0001-1995-0001", 1, {1})
    poc = tmp_path / "poc_work"
    (poc / "gt").mkdir(parents=True)
    (poc / "gt" / "review.csv").write_text("page\n가상-0001-0001\n", encoding="utf-8-sig")
    before = (poc / "gt" / "review.csv").read_bytes()
    with pytest.raises(SystemExit):  # 목록 없이 정답이 있음
        tw.run(src, poc, ocr_page=_fake_ocr())
    monkeypatch.setattr(tw, "POC_WORK", poc)
    with pytest.raises(SystemExit):  # PoC work 자체
        tw.run(src, poc, ocr_page=_fake_ocr())
    assert (poc / "gt" / "review.csv").read_bytes() == before and not (poc / "synth_manifest.json").exists()
    other = tmp_path / "other"
    (other / "platform").mkdir(parents=True)
    (other / "platform" / "가상-0001.json").write_text("[]", encoding="utf-8")
    with pytest.raises(SystemExit):  # 목록 밖 철
        tw.run(src, other, ocr_page=_fake_ocr())


def test_reocr_when_image_changes(tw, tmp_path, monkeypatch):
    """같은 철 이름으로 다시 만든 철(다른 level 등) — 이미지 해시가 다르면 다시 OCR, 없어진 쪽의 옛 OCR은 지운다"""
    monkeypatch.setattr(tw.layout, "is_start", lambda d: (True, []))
    src, work = tmp_path / "src", tmp_path / "work"
    vol = "SYNTH0001-1995-0001"
    _bundle(src, vol, 2, {1, 2})
    calls = []
    tw.run(src, work, ocr_page=_fake_ocr(calls))
    assert len(calls) == 2
    tw.run(src, work, ocr_page=_fake_ocr(calls))
    assert len(calls) == 2  # 같은 이미지는 다시 안 읽음
    (src / vol / f"{vol}-0002.jpg").write_bytes(b"heavy")  # 다시 만든 쪽
    (src / vol / f"{vol}-0001.jpg").unlink()
    (src / vol / "gt.csv").write_text(
        "page,확인,is_start,docno,date,recipient,title,check,note,saved\n"
        f"{vol}-0002,경계,Y,,,,,,합성,\n", encoding="utf-8-sig")
    tw.run(src, work, ocr_page=_fake_ocr(calls))
    assert calls[2:] == [f"{vol}-0002"]
    o = work / "ocr_platform" / vol
    assert sorted(p.stem for p in o.glob("*.json")) == [f"{vol}-0002"]
    assert json.loads((o / f"{vol}-0002.json").read_text(encoding="utf-8"))["image_sha256"] == tw.sha256(src / vol / f"{vol}-0002.jpg")


def test_rerun_after_crash(tw, tmp_path, monkeypatch):
    """OCR 중에 죽어 manifest 없이 platform 철 파일(·정답)이 남아도 같은 명령을 다시 돌리면 끝까지 간다"""
    monkeypatch.setattr(tw.layout, "is_start", lambda d: (True, []))
    src, work = tmp_path / "src", tmp_path / "work"
    _bundle(src, "SYNTH0001-1995-0001", 1, {1})
    _bundle(src, "SYNTH0002-1996-0001", 1, {1})

    def dies_on_second(path):
        if path.stem.startswith("SYNTH0002"):
            raise RuntimeError("가상 OCR 실패")
        return _fake_ocr()(path)

    with pytest.raises(RuntimeError):
        tw.run(src, work, ocr_page=dies_on_second)
    assert (work / "platform" / "SYNTH0001-1995-0001.json").exists() and not (work / "synth_manifest.json").exists()
    (work / "gt" / "review.csv").write_text(  # gt까지 쓰고 manifest 전에 죽은 경우도 같게
        "page,확인,is_start\nSYNTH0001-1995-0001-0001,경계,Y\n", encoding="utf-8-sig")
    m = tw.run(src, work, ocr_page=_fake_ocr())
    assert [v["vol"] for v in m["vols"]] == ["SYNTH0001-1995-0001", "SYNTH0002-1996-0001"]
    # 이번 src에 없는 철의 남은 파일은 여전히 막는다
    other = tmp_path / "other"
    _bundle(other, "SYNTH0003-1997-0001", 1, {1})
    (work / "platform" / "SYNTH0009-1990-0001.json").write_text("[]", encoding="utf-8")
    with pytest.raises(SystemExit):
        tw.run(other, work, ocr_page=_fake_ocr())


def test_drops_gt_rows_outside_manifest(tw, tmp_path, monkeypatch):
    """정답을 다시 쓸 때 manifest에 없는 철의 줄(죽은 실행이 남긴 것)은 버린다"""
    monkeypatch.setattr(tw.layout, "is_start", lambda d: (True, []))
    src, work = tmp_path / "src", tmp_path / "work"
    _bundle(src, "SYNTH0001-1995-0001", 2, {1, 2})
    tw.run(src, work, ocr_page=_fake_ocr())
    with (work / "gt" / "review.csv").open("a", encoding="utf-8", newline="") as f:
        f.write("SYNTH0002-1996-0001-0001,경계,Y,,,,,,합성,\n")
    tw.run(src, work, ocr_page=_fake_ocr())
    with (work / "gt" / "review.csv").open(encoding="utf-8-sig") as f:
        assert sorted(r["page"] for r in csv.DictReader(f)) == ["SYNTH0001-1995-0001-0001", "SYNTH0001-1995-0001-0002"]


def test_second_batch_merges(tw, tmp_path, monkeypatch):
    """같은 work에 다른 묶음을 더 넣으면 manifest·정답을 합친다 (앞 묶음이 사라지지 않음)"""
    monkeypatch.setattr(tw.layout, "is_start", lambda d: (True, []))
    work = tmp_path / "work"
    _bundle(tmp_path / "a", "SYNTH0001-1995-0001", 2, {1, 2})
    _bundle(tmp_path / "b", "SYNTH0002-1996-0001", 3, {1})
    tw.run(tmp_path / "a", work, ocr_page=_fake_ocr())
    m = tw.run(tmp_path / "b", work, ocr_page=_fake_ocr())
    assert [v["vol"] for v in m["vols"]] == ["SYNTH0001-1995-0001", "SYNTH0002-1996-0001"]
    with (work / "gt" / "review.csv").open(encoding="utf-8-sig") as f:
        assert len(list(csv.DictReader(f))) == 5
    m = tw.run(tmp_path / "a", work, ocr_page=_fake_ocr())  # 같은 철을 다시 넣으면 바꾼다 (겹치지 않음)
    assert len(m["vols"]) == 2
    with (work / "gt" / "review.csv").open(encoding="utf-8-sig") as f:
        assert len(list(csv.DictReader(f))) == 5


def test_work_outside_repo(tw):
    assert tw.main([str(ROOT), str(ROOT / "eval" / "가상work")]) == 2
