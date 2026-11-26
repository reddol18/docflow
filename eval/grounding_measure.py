"""추출 근거 확인(app/grounding.py)이 진짜 문서에 표시를 얼마나 더 붙이나 — 저장된 추출 값으로만 (LLM 호출 없음, 이 PC 안에서).

  python -I -X utf8 eval/grounding_measure.py [--work <work 폴더>]
work/platform/<철>.json의 b(LLM 추출 값)와 work/ocr_platform의 첫 쪽 OCR 앞 80줄(운영 추출 입력과 같음)로 칸마다 근거를 보고,
정답 시작 쪽(gt/review.csv)이면 그 값이 맞았는지(PoC rescore.ok_field)와 묶어 센다. 출력은 개수만 (문서 글자 없음).
https://github.com/reddol18/docflow/issues/27
"""
import argparse
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "promptfoo"))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default="")
    a = ap.parse_args(argv)
    if a.work:
        os.environ["DOCFLOW_EVAL_WORK"] = str(Path(a.work).resolve())
    import common  # noqa: PLC0415 — DOCFLOW_EVAL_WORK를 넣은 뒤
    from rescore import ok_field  # noqa: PLC0415

    from app import fields, grounding  # noqa: PLC0415
    schema = fields.load(common.SCHEMA_FILE)
    pf = fields.prompt_fields(schema)
    gt = common.load_gt()[1]
    c = Counter()
    for vol in common.vols():
        for d in common.stored_docs(vol):
            lines = common.ocr(vol, d["doc_id"])["lines"][:80]
            b = d.get("b") or {}
            vals = {f["name"]: (b.get(f["name"]) or {}).get("value") or "" for f in pf}
            final = d.get("fields") or {}
            # 운영 merge와 같게: LLM 값이 최종 칸 값에 들어간 칸만 표시 대상 (손글씨 판독·배치 규칙 값이면 빼고)
            bad = {n for n in grounding.ungrounded(pf, vals, lines) if grounding.reached(vals[n], final.get(n))}
            for f in pf:
                c[(f["name"], "reached")] += grounding.reached(vals[f["name"]], final.get(f["name"]))
            g = gt.get(d["doc_id"])
            c["docs"] += 1
            c["lines"] += len(lines)
            c["instr_lines"] += sum(grounding.instruction_line(x) for x in lines)  # 근거에서 빠지는 진짜 줄
            c["docs_flagged"] += bool(bad)
            c["docs_new_review"] += bool(bad) and not d.get("flags")  # 지금 표시가 하나도 없던 문서 = 새로 사람 검토로
            for f in pf:
                n = f["name"]
                if not vals[n]:
                    continue
                c[(n, "values")] += 1
                c[(n, "flagged")] += n in bad
                if g is not None and n in g:
                    ok = ok_field(n, vals[n], g[n])
                    c[(n, "gt_ok" if ok else "gt_bad")] += 1
                    c[(n, "gt_ok_flagged" if ok else "gt_bad_flagged")] += n in bad
    print(f"문서 {c['docs']}건 중 'OCR 근거 없음' 표시가 붙는 문서 {c['docs_flagged']}건 "
          f"(그중 지금 다른 표시가 없던 문서 {c['docs_new_review']}건). 지시 꼴로 근거에서 빠진 줄 {c['instr_lines']}/{c['lines']}")
    print("| 칸 | 값 있음 | LLM 값이 최종 값에 들어감 | 근거 없음 표시 | 정답 맞은 값 | 그중 표시(오탐) | 정답 틀린 값 | 그중 표시 |")
    print("|---|---|---|---|---|---|---|---|")
    for f in pf:
        n = f["name"]
        print(f"| {n} | {c[(n, 'values')]} | {c[(n, 'reached')]} | {c[(n, 'flagged')]} | {c[(n, 'gt_ok')]} | {c[(n, 'gt_ok_flagged')]} | "
              f"{c[(n, 'gt_bad')]} | {c[(n, 'gt_bad_flagged')]} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
