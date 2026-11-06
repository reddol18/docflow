"""플랫폼 결과(/volumes/{vol}/documents JSON)를 PoC 확정 정답(work/gt/review.csv)으로 채점한다. LLM은 쓰지 않는다.

실행 (records_poc 폴더에서): .venv/Scripts/python.exe -I -X utf8 doc_platform/eval/score.py work/platform/*.json [--detail]
채점 규칙은 PoC scripts/rescore.py를 그대로 쓴다 (정규화, '?' 와일드카드, 일련번호 빈칸, 날짜 YYYY-MM).
결과 JSON에 들어 있는 철만 채점한다.
  경계      확인 열에 '경계'가 있는 쪽: 시작 쪽 판정의 재현율·정밀도
  4칸       정답 시작 쪽마다 B(GPT 텍스트 추출+수신 보정) / 최종(B+손글씨 판독) 정답률. 못 찾은 문서 = 틀림
  일련번호·일자  B → B+hw
  검토 표시  틀린 칸 중 표시된 비율, 전체 중 표시 비율
  문서 단위  4칸 모두 맞은 문서, 표시 없는데 틀린 문서
숫자는 metrics()가 한 번에 계산한다 — 모델 비교 하네스(eval/promptfoo/gate.py)도 같은 함수를 쓴다.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(1, str(Path(__file__).resolve().parents[1]))  # doc_platform (색인 스키마)
from rescore import gt_serial, load_gt, match, ok_field, pct, pdate  # noqa: E402

from app import fields  # noqa: E402

# 채점 칸 = 김해 매핑에서 사람이 고칠 수 있는 칸 (https://github.com/reddol18/docflow/issues/9). 정답은 이 4칸만 있다
FIELDS = list(fields.editable(fields.load(fields.DIR / "gimhae.yaml")))


def b_val(d, f):
    if f == "recipient":
        return d["fields"]["recipient"]  # 수신 보정은 B 단계에 포함
    return d["b"][f]["value"]


def metrics(docs, gt=None):
    """docs = {doc_id: 문서 결과}. 결과에 든 철만 채점. 반환 dict의 (맞은 수, 전체) 쌍과 목록으로 화면·비교를 만든다."""
    rows, idx, bnd, _ = gt or load_gt()
    vols = {d["vol"] for d in docs.values()}
    vol = lambda p: p.rsplit("-", 1)[0]  # noqa: E731
    idx = {p: r for p, r in idx.items() if vol(p) in vols}
    bnd = {p: v for p, v in bnd.items() if vol(p) in vols}
    m = {"vols": sorted(vols), "n_gt": len(idx), "n_bnd": len(bnd), "n_docs": len(docs)}
    starts = set(docs)
    tp = sum(1 for p, s in bnd.items() if s and p in starts)
    m["boundary"] = {"recall": (tp, sum(bnd.values())), "precision": (tp, sum(1 for p in bnd if p in starts))}
    m["missed"] = sorted(p for p in idx if p not in docs)

    m["fields"] = {}
    for name, get in [("B", b_val), ("final", lambda d, f: d["fields"][f])]:
        m["fields"][name] = {f: (sum(p in docs and ok_field(f, get(docs[p], f), idx[p][f]) for p in idx), len(idx))
                             for f in FIELDS}

    m["flag"] = {}
    for fld in ["serial", "date"]:
        ps = [p for p in idx if (fld == "date" or gt_serial(idx[p]["docno"]) is not None)]
        gtv = {p: gt_serial(idx[p]["docno"]) if fld == "serial" else idx[p]["date"] for p in ps}
        conv = (lambda x: x) if fld == "serial" else pdate
        b_key = "b_serial" if fld == "serial" else "b_date"
        ok_b = sum(p in docs and match(conv(docs[p]["info"][b_key]), gtv[p]) for p in ps)
        okf = {p: p in docs and match(conv(docs[p]["fields"][fld]), gtv[p]) for p in ps}
        flagged = [p for p in ps if p in docs and docs[p]["flags"].get(fld)]
        wrong = [p for p in ps if not okf[p]]
        caught = [p for p in wrong if p in docs and docs[p]["flags"].get(fld)]
        m["flag"][fld] = {"B": (ok_b, len(ps)), "final": (sum(okf.values()), len(ps)),
                          "flagged": (len(flagged), len(ps)), "recall": (len(caught), len(wrong)),
                          "wrong": wrong, "gt": gtv}

    all_ok = {p: p in docs and all(ok_field(f, docs[p]["fields"][f], idx[p][f]) for f in FIELDS) for p in idx}
    flagged = {p for p in idx if p in docs and docs[p]["flags"]}
    silent = [p for p in idx if not all_ok[p] and p not in flagged]
    m["doc"] = {"all_ok": (sum(all_ok.values()), len(idx)), "fix": (len(idx) - sum(all_ok.values()), len(idx)),
                "flagged": (len(flagged), len(idx)), "silent": (len(silent), len(idx))}
    m["silent"] = silent
    m["_idx"], m["_all_ok"] = idx, all_ok
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results", nargs="+")
    ap.add_argument("--detail", action="store_true")
    a = ap.parse_args()
    docs = {}
    for path in a.results:
        for d in json.loads(Path(path).read_text(encoding="utf-8")):
            docs[d["doc_id"]] = d
    m = metrics(docs)
    idx = m["_idx"]
    print(f"철 {m['vols']}\n정답 시작 쪽 {m['n_gt']}건, 경계 확인 쪽 {m['n_bnd']}쪽, 플랫폼 문서 {m['n_docs']}건\n")
    print(f"## 문서 시작 판별: recall {pct(*m['boundary']['recall'])}, precision {pct(*m['boundary']['precision'])}")
    print(f"   정답 시작 쪽인데 플랫폼 문서가 없음: {m['missed']}\n")

    print("## 색인 4칸 (정답 시작 쪽 전체, 못 찾은 문서 = 틀림)")
    print(f"{'':8s}" + "".join(f"{f:>16s}" for f in FIELDS))
    for name, key in [("B", "B"), ("최종", "final")]:
        print(f"{name:8s}" + "".join(f"{pct(*m['fields'][key][f]):>16s}" for f in FIELDS))
    print()

    print("## 일련번호·일자와 검토 표시")
    for fld in ["serial", "date"]:
        r = m["flag"][fld]
        print(f"{fld:7s} B {pct(*r['B'])} → B+hw {pct(*r['final'])} | "
              f"표시 {pct(*r['flagged'])}, 틀린 것 중 표시 {pct(*r['recall'])}")
        if a.detail:
            for p in r["wrong"]:
                d = docs.get(p)
                print(f"    x {p} gt={r['gt'][p]!r} got={d['fields'][fld] if d else None!r} "
                      f"hw={d['hw'][('docno' if fld == 'serial' else 'date')] if d else None} flags={d['flags'].get(fld) if d else None}")
    print()

    print("## 문서 단위")
    print(f"4칸 모두 정답 {pct(*m['doc']['all_ok'])}, 고쳐야 함 {pct(*m['doc']['fix'])}")
    print(f"검토 표시된 문서 {pct(*m['doc']['flagged'])}, 표시 없는데 틀린 문서 {pct(*m['doc']['silent'])}")
    for p in m["silent"]:
        d = docs.get(p)
        bad = [f for f in FIELDS if not d or not ok_field(f, d["fields"][f], idx[p][f])]
        print(f"    표시 없이 틀림 {p}: {bad}" + ("" if d else " (문서 못 찾음)"))
    if a.detail:
        for p in idx:
            if p in docs and not m["_all_ok"][p]:
                for f in FIELDS:
                    if not ok_field(f, docs[p]["fields"][f], idx[p][f]):
                        print(f"    x {p} {f}: got={docs[p]['fields'][f]!r} gt={idx[p][f]!r}")


if __name__ == "__main__":
    main()
