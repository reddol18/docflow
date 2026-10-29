"""저장된 결과로 '검토 표시' 규칙을 바꿨을 때를 미리 계산한다 (재실행·LLM 없음).

추가 규칙 후보: 불일치 = GPT 텍스트 추출 값(B)과 손글씨 판독 값이 둘 다 있는데 서로 다름.
실행: .venv/Scripts/python.exe -I -X utf8 doc_platform/eval/flag_whatif.py work/platform/*.json
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from rescore import gt_serial, load_gt, match, pdate  # noqa: E402

docs = {}
for p in sys.argv[1:]:
    for d in json.loads(Path(p).read_text(encoding="utf-8")):
        docs[d["doc_id"]] = d
_, idx, _, _ = load_gt()
idx = {p: r for p, r in idx.items() if p in docs}

for fld, bkey in [("serial", "b_serial"), ("date", "b_date")]:
    ps = [p for p in idx if fld == "date" or gt_serial(idx[p]["docno"]) is not None]
    gt = {p: gt_serial(idx[p]["docno"]) if fld == "serial" else idx[p]["date"] for p in ps}
    conv = (lambda x: x) if fld == "serial" else pdate
    wrong = [p for p in ps if not match(conv(docs[p]["fields"][fld]), gt[p])]
    for name, rule in [
        ("지금 규칙", lambda d: bool(d["flags"].get(fld))),
        ("+ 불일치", lambda d: bool(d["flags"].get(fld)) or bool(d["info"][bkey]) and d["info"][bkey] != d["fields"][fld]),
    ]:
        fl = [p for p in ps if rule(docs[p])]
        caught = [p for p in wrong if rule(docs[p])]
        print(f"{fld:6s} {name:8s} 표시 {len(fl):3d}/{len(ps)} ({len(fl) / len(ps):.0%}), "
              f"틀린 것 중 표시 {len(caught)}/{len(wrong)} ({len(caught) / max(len(wrong), 1):.0%})")
