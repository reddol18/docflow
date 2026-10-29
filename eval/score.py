"""플랫폼 결과(/volumes/{vol}/documents JSON)를 PoC 확정 정답(work/gt/review.csv)으로 채점한다. LLM은 쓰지 않는다.

실행 (records_poc 폴더에서): .venv/Scripts/python.exe -I -X utf8 doc_platform/eval/score.py work/platform/*.json [--detail]
채점 규칙은 PoC scripts/rescore.py를 그대로 쓴다 (정규화, '?' 와일드카드, 일련번호 빈칸, 날짜 YYYY-MM).
결과 JSON에 들어 있는 철만 채점한다.
  경계      확인 열에 '경계'가 있는 쪽: 시작 쪽 판정의 재현율·정밀도
  4칸       정답 시작 쪽마다 B(GPT 텍스트 추출+수신 보정) / 최종(B+손글씨 판독) 정답률. 못 찾은 문서 = 틀림
  일련번호·일자  B → B+hw
  검토 표시  틀린 칸 중 표시된 비율, 전체 중 표시 비율
  문서 단위  4칸 모두 맞은 문서, 표시 없는데 틀린 문서
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from rescore import gt_serial, load_gt, match, ok_field, pct, pdate  # noqa: E402

FIELDS = ["docno", "date", "recipient", "title"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results", nargs="+")
    ap.add_argument("--detail", action="store_true")
    a = ap.parse_args()
    docs = {}
    for path in a.results:
        for d in json.loads(Path(path).read_text(encoding="utf-8")):
            docs[d["doc_id"]] = d
    vols = {d["vol"] for d in docs.values()}
    rows, idx, bnd, _ = load_gt()
    vol = lambda p: p.rsplit("-", 1)[0]  # noqa: E731
    idx = {p: r for p, r in idx.items() if vol(p) in vols}
    bnd = {p: v for p, v in bnd.items() if vol(p) in vols}
    print(f"철 {sorted(vols)}\n정답 시작 쪽 {len(idx)}건, 경계 확인 쪽 {len(bnd)}쪽, 플랫폼 문서 {len(docs)}건\n")

    starts = set(docs)
    tp = sum(1 for p, s in bnd.items() if s and p in starts)
    print(f"## 문서 시작 판별: recall {pct(tp, sum(bnd.values()))}, "
          f"precision {pct(tp, sum(1 for p in bnd if p in starts))}")
    missed = sorted(p for p in idx if p not in docs)
    print(f"   정답 시작 쪽인데 플랫폼 문서가 없음: {missed}\n")

    def b_val(d, f):
        if f == "recipient":
            return d["fields"]["recipient"]  # 수신 보정은 B 단계에 포함
        return d["b"][f]["value"]

    print("## 색인 4칸 (정답 시작 쪽 전체, 못 찾은 문서 = 틀림)")
    print(f"{'':8s}" + "".join(f"{f:>16s}" for f in FIELDS))
    for name, get in [("B", b_val), ("최종", lambda d, f: d["fields"][f])]:
        cells = []
        for f in FIELDS:
            ok = sum(p in docs and ok_field(f, get(docs[p], f), idx[p][f]) for p in idx)
            cells.append(pct(ok, len(idx)))
        print(f"{name:8s}" + "".join(f"{c:>16s}" for c in cells))
    print()

    print("## 일련번호·일자와 검토 표시")
    for fld in ["serial", "date"]:
        ps = [p for p in idx if (fld == "date" or gt_serial(idx[p]["docno"]) is not None)]
        gt = {p: gt_serial(idx[p]["docno"]) if fld == "serial" else idx[p]["date"] for p in ps}
        conv = (lambda x: x) if fld == "serial" else pdate
        b_key = "b_serial" if fld == "serial" else "b_date"
        ok_b = sum(p in docs and match(conv(docs[p]["info"][b_key]), gt[p]) for p in ps)
        okf = {p: p in docs and match(conv(docs[p]["fields"][fld]), gt[p]) for p in ps}
        flagged = [p for p in ps if p in docs and docs[p]["flags"].get(fld)]
        wrong = [p for p in ps if not okf[p]]
        caught = [p for p in wrong if p in docs and docs[p]["flags"].get(fld)]
        print(f"{fld:7s} B {pct(ok_b, len(ps))} → B+hw {pct(sum(okf.values()), len(ps))} | "
              f"표시 {pct(len(flagged), len(ps))}, 틀린 것 중 표시 {pct(len(caught), len(wrong))}")
        if a.detail:
            for p in wrong:
                d = docs.get(p)
                print(f"    x {p} gt={gt[p]!r} got={d['fields'][fld] if d else None!r} "
                      f"hw={d['hw'][('docno' if fld == 'serial' else 'date')] if d else None} flags={d['flags'].get(fld) if d else None}")
    print()

    print("## 문서 단위")
    all_ok = {p: p in docs and all(ok_field(f, docs[p]["fields"][f], idx[p][f]) for f in FIELDS) for p in idx}
    flagged = {p for p in idx if p in docs and docs[p]["flags"]}
    silent = [p for p in idx if not all_ok[p] and p not in flagged]
    print(f"4칸 모두 정답 {pct(sum(all_ok.values()), len(idx))}, 고쳐야 함 {pct(len(idx) - sum(all_ok.values()), len(idx))}")
    print(f"검토 표시된 문서 {pct(len(flagged), len(idx))}, 표시 없는데 틀린 문서 {pct(len(silent), len(idx))}")
    for p in silent:
        d = docs.get(p)
        bad = [f for f in FIELDS if not d or not ok_field(f, d["fields"][f], idx[p][f])]
        print(f"    표시 없이 틀림 {p}: {bad}" + ("" if d else " (문서 못 찾음)"))
    if a.detail:
        for p in idx:
            if p in docs and not all_ok[p]:
                for f in FIELDS:
                    if not ok_field(f, docs[p]["fields"][f], idx[p][f]):
                        print(f"    x {p} {f}: got={docs[p]['fields'][f]!r} gt={idx[p][f]!r}")


if __name__ == "__main__":
    main()
