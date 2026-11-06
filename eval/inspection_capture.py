"""표시 없이 틀린 문서가 검사 표본에 얼마나 잡히나 (https://github.com/reddol18/docflow/issues/10). LLM 없음, 기준 없음(측정만).

플랫폼 결과(/volumes/{vol}/documents JSON)를 PoC 확정 정답(work/gt/review.csv)으로 채점해(eval/score.py와 같은 규칙)
철마다 표시 없는 문서 b건, 그중 4칸 중 하나라도 틀린 문서 w건을 세고, 표본 비율 p마다
  표본 수 n = ceil(b×p) (app/inspection.py sample_size와 같음)
  기대 포착 수 = w×n/b,  1건 이상 잡을 확률 = 1 − C(b−w, n)/C(b, n)  (비복원 무작위 추출, 초기하분포)
를 낸다. 출력은 수치만 — 문서 제목·값은 내지 않는다.

실행 (records_poc 폴더에서): .venv/Scripts/python.exe -I -X utf8 doc_platform/eval/inspection_capture.py work/platform/*.json
"""
import json
import sys
from math import comb
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rescore import load_gt, ok_field  # noqa: E402

from app.inspection import sample_size  # noqa: E402

FIELDS = ["docno", "date", "recipient", "title"]
RATES = [0.03, 0.05, 0.10, 0.20]


def capture(b, w, rate):
    n = sample_size(b, rate)
    if b == 0:
        return n, 0.0, 0.0
    return n, w * n / b, 1 - comb(b - w, n) / comb(b, n)


def main(paths):
    docs = {}
    for path in paths:
        for d in json.loads(Path(path).read_text(encoding="utf-8")):
            docs[d["doc_id"]] = d
    _, idx, _, _ = load_gt()
    vols = sorted({d["vol"] for d in docs.values()})
    per = []
    for v in vols:
        plain = [p for p, d in docs.items() if d["vol"] == v and not d["flags"]]
        wrong = [p for p in plain if p in idx and not all(ok_field(f, docs[p]["fields"][f], idx[p][f]) for f in FIELDS)]
        per.append((v, len(plain), len(wrong)))
    print("| 철 | 표시 없는 문서 b | 그중 틀린 w | " + " | ".join(f"{int(r*100)}%: n / 기대 포착 / P(≥1)" for r in RATES) + " |")
    print("|---|---|---|" + "---|" * len(RATES))
    tot = {r: [0, 0.0] for r in RATES}
    for v, b, w in per:
        cells = []
        for r in RATES:
            n, exp, p1 = capture(b, w, r)
            tot[r][0] += n
            tot[r][1] += exp
            cells.append(f"{n} / {exp:.2f} / {p1:.2f}")
        print(f"| {v} | {b} | {w} | " + " | ".join(cells) + " |")
    B, W = sum(b for _, b, _ in per), sum(w for _, _, w in per)
    print(f"| 합계 | {B} | {W} | " + " | ".join(f"{tot[r][0]} / {tot[r][1]:.2f} / -" for r in RATES) + " |")


if __name__ == "__main__":
    main(sys.argv[1:])
