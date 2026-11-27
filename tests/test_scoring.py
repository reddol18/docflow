"""채점 규칙 eval/scoring.py (PoC rescore.py에서 옮김, https://github.com/reddol18/docflow/issues/8 조건 3)와
eval/score.py가 저장소 밖 PoC 폴더(records_poc/scripts) 없이 도는지. 값은 모두 지어낸 것."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))
import scoring  # noqa: E402

GT_HEAD = "page,확인,is_start,docno,date,recipient,title,check,note,saved\n"


def test_norm_and_wildcard():
    assert scoring.norm(" 주식회사 가상 (대표) ") == "주가상대표"
    assert scoring.norm("가상ⓐ 1-2.") == "가상@12"
    assert scoring.match("가상12", "가상1?") and not scoring.match("가상1", "가상1?")
    assert scoring.ok_field("docno", "가상 1234-56", "가상 1234-5?")
    assert scoring.ok_field("title", "가상 '알림'", "가상 알림")


def test_serial_and_date():
    assert scoring.gt_serial("가상 12345-678") == "678"
    assert scoring.gt_serial("가상 12345-") == ""  # 일련번호 빈칸 — 예측도 비어야 맞음
    assert scoring.gt_serial("가상 12345") is None and scoring.gt_serial("") is None
    assert scoring.pdate("1995-03") == "1995-03" and scoring.pdate("1995. 3. 7.") == "1995-03-07"
    assert scoring.pdate("95.3.") == "1995-03"  # 일자 없음 → YYYY-MM
    assert scoring.pdate("2007. 3. 7.") == ""  # 옛 규칙 그대로 1990~2005년만 고쳐 읽는다 (ISO 꼴은 그대로 받음)
    assert scoring.pdate("2007-03-07") == "2007-03-07"
    assert scoring.ok_field("date", "1995.3.7", "1995-03-07") and not scoring.ok_field("date", "", "1995-03-07")


def test_load_gt_many_files(tmp_path):
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    a.write_text(GT_HEAD + "SYNTH0001-1995-0001-0001,경계,Y,가상 1-1,1995-01-02,가상,가상 알림,,합성,\n"
                 "SYNTH0001-1995-0001-0002,경계,,,,,,,Claude 판독,\n", encoding="utf-8-sig")
    b.write_text(GT_HEAD + "SYNTH0002-1995-0001-0001,,Y,가상 1-2,1995-01-03,가상,가상 알림,,합성,\n", encoding="utf-8-sig")
    rows, idx, bnd, src = scoring.load_gt([a, b])
    assert len(rows) == 3 and sorted(idx) == ["SYNTH0001-1995-0001-0001", "SYNTH0002-1995-0001-0001"]
    assert bnd == {"SYNTH0001-1995-0001-0001": True, "SYNTH0001-1995-0001-0002": False}
    assert src["SYNTH0001-1995-0001-0002"] == "claude"
    assert scoring.load_gt(a)[1].keys() == {"SYNTH0001-1995-0001-0001"}


def test_no_poc_imports_in_eval():
    """eval/ 아래 어떤 파일도 PoC 채점 모듈(rescore·gt_draft·score_extract)이나 records_poc/scripts 경로를 쓰지 않는다"""
    bad = []
    for p in (ROOT / "eval").rglob("*.py"):
        if "node_modules" in p.parts:
            continue
        t = p.read_text(encoding="utf-8")
        if any(s in t for s in ("from rescore", "import rescore", "from gt_draft", "from score_extract",
                                'ROOT / "scripts"', 'POC_ROOT / "scripts"')):
            bad.append(p.name)
    assert bad == []


BLOCK = r"""
import importlib.abc, runpy, sys
BAD = {"rescore", "gt_draft", "score_extract", "hw_flags", "hw_score"}
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BAD:
            raise ImportError("PoC 모듈 금지: " + name)
sys.meta_path.insert(0, Block())
poc = sys.argv[1].lower()
sys.path[:] = [p for p in sys.path if not p.lower().startswith(poc)]
sys.argv = sys.argv[2:]
runpy.run_path(sys.argv[0], run_name="__main__")
leak = [p for p in sys.path if p.lower().startswith(poc)]
assert not leak, leak
assert not (BAD & set(sys.modules)), BAD & set(sys.modules)
"""


def test_score_runs_without_poc_folder(tmp_path):
    """측정: records_poc/scripts를 sys.path에서 빼고 PoC 모듈 import를 막아도 eval/score.py가 끝까지 돈다
    (--gt 인자·기본 work 정답 둘 다)"""
    pytest.importorskip("yaml")
    work = tmp_path / "work"
    (work / "platform").mkdir(parents=True)
    (work / "gt").mkdir()
    vol = "SYNTH0001-1995-0001"
    gt = work / "gt" / "review.csv"
    gt.write_text(GT_HEAD + f"{vol}-0001,경계,Y,가상 12345-7,1995-01-02,가상과장,가상 알림,,합성,\n"
                  f"{vol}-0002,경계,,,,,,,합성,\n", encoding="utf-8-sig")
    vals = {"docno": "가상 12345-7", "date": "1995-01-02", "recipient": "가상과장", "title": "가상 알림"}
    doc = {"doc_id": f"{vol}-0001", "vol": vol, "pages": [f"{vol}-0001", f"{vol}-0002"],
           "fields": {**vals, "serial": "7"}, "b": {k: {"value": v} for k, v in vals.items()},
           "info": {"b_serial": "7", "b_date": "1995-01-02"}, "flags": {}}
    res = work / "platform" / f"{vol}.json"
    res.write_text(json.dumps([doc], ensure_ascii=False), encoding="utf-8")
    env = {**os.environ, "PYTHONUTF8": "1"}
    for extra in ([], ["--gt", str(gt)]):
        r = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", BLOCK, str(ROOT.parent / "scripts"),  # noqa: S603
                            str(ROOT / "eval" / "score.py"), str(res), *extra],
                           env=env, capture_output=True, text=True, encoding="utf-8", check=False)
        assert r.returncode == 0, r.stderr[-2000:]
        assert "recall   1/  1 100%" in r.stdout and "4칸 모두 정답   1/  1 100%" in r.stdout
