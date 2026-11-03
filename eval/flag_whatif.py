"""저장된 결과로 '검토 표시' 규칙·기준값을 바꿨을 때를 미리 계산한다 (재실행·LLM 없음).

규칙은 app/flags.py 하나를 그래프와 같이 쓴다. 표시는 저장된 값이 아니라 여기서 다시 계산한다.
실행 (records_poc 폴더에서):
  .venv/Scripts/python.exe -I -X utf8 doc_platform/eval/flag_whatif.py work/platform/*.json [--sweep] [--detail 이름]

  기본      지금 기본값(DEFAULT)과 추천 설정을 칸별·문서별로 비교
  --sweep   확신도 기준값 × 추가 규칙 조합을 훑어 '표시 없이 틀린 문서'와 '표시 비율'을 표로
  --detail  지정한 설정 이름에서 표시 없이 틀린 문서를 칸별로 나열
목표 (https://github.com/reddol18/docflow/issues/2): 표시 없이 틀린 문서 3% 이하, 표시된 문서 80% 이하.
"""
import argparse
import itertools
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(HERE))
from rescore import load_gt, ok_field  # noqa: E402

from app import flags as F  # noqa: E402

FIELDS = ["docno", "date", "recipient", "title"]
FLAG_OF = {"docno": "serial", "date": "date", "recipient": "recipient", "title": "title"}

ALL_RULES = {**F.DEFAULT, "rules": tuple(F.RULES)}


def load(paths):
    vols = {}
    for p in paths:
        for d in json.loads(Path(p).read_text(encoding="utf-8")):
            vols.setdefault(d["vol"], {})[d["doc_id"]] = d
    return {v: [ds[k] for k in sorted(ds)] for v, ds in vols.items()}


def recompute(vols, cfg):
    out = {}
    for docs in vols.values():
        for d, fl in zip(docs, F.flags(docs, cfg)):
            out[d["doc_id"]] = fl
    return out


def evaluate(docs, idx, fl):
    """칸별(틀린 것 중 표시 / 표시 비율)과 문서 단위(표시 비율 / 표시 없이 틀림)."""
    res = {}
    for f in FIELDS:
        wrong = [p for p in idx if p not in docs or not ok_field(f, docs[p]["fields"][f], idx[p][f])]
        flagged = [p for p in idx if p in docs and fl[p].get(FLAG_OF[f])]
        caught = [p for p in wrong if p in docs and fl[p].get(FLAG_OF[f])]
        res[f] = (len(caught), len(wrong), len(flagged))
    n = len(idx)
    bad = {p for p in idx if p not in docs or not all(ok_field(f, docs[p]["fields"][f], idx[p][f]) for f in FIELDS)}
    doc_fl = {p for p in idx if p in docs and fl[p]}
    silent = sorted(bad - doc_fl)
    return res, len(doc_fl) / n, len(silent) / n, silent


def report(name, docs, idx, fl):
    res, rate, silent_rate, silent = evaluate(docs, idx, fl)
    n = len(idx)
    print(f"### {name}")
    for f in FIELDS:
        c, w, fl_n = res[f]
        print(f"  {f:9s} 틀린 것 중 표시 {c:3d}/{w:3d} ({c / max(w, 1):4.0%})  표시 {fl_n:3d}/{n} ({fl_n / n:4.0%})")
    print(f"  문서: 표시 {rate:4.0%}, 표시 없이 틀림 {len(silent)}/{n} ({silent_rate:4.1%})")
    return silent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results", nargs="+")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--detail", choices=["이전", "기본", "전체 규칙"])
    a = ap.parse_args()
    vols = load(a.results)
    docs = {d["doc_id"]: d for ds in vols.values() for d in ds}
    _, idx, _, _ = load_gt()
    idx = {p: r for p, r in idx.items() if p.rsplit("-", 1)[0] in vols}

    # 저장된 표시(이전 규칙으로 실행한 결과)와 LEGACY 재계산이 같은지 = 규칙을 옮기며 바뀐 것이 없는지
    legacy = recompute(vols, F.LEGACY)
    diff = [p for p in docs if legacy[p] != docs[p]["flags"]]
    print(f"저장된 표시와 이전 규칙 재계산 차이: {len(diff)}건 / {len(docs)}건\n")

    sets = {"이전": F.LEGACY, "기본": F.DEFAULT, "전체 규칙": ALL_RULES}
    for name, cfg in sets.items():
        fl = recompute(vols, cfg)
        silent = report(name, docs, idx, fl)
        if a.detail == name:
            for p in silent:
                d = docs.get(p)
                bad = [f for f in FIELDS if not d or not ok_field(f, d["fields"][f], idx[p][f])]
                for f in bad:
                    print(f"    x {p} {f}: got={d['fields'][f] if d else None!r} gt={idx[p][f]!r}")
        if name == "기본":  # 철마다 (한 철에만 맞춘 규칙인지 보려고)
            for v in sorted(vols):
                sub = {p: r for p, r in idx.items() if p.startswith(v + "-")}
                if sub:
                    _, rate, sr, s = evaluate(docs, sub, fl)
                    print(f"    {v}: {len(sub):3d}건 표시 {rate:4.0%}, 표시 없이 틀림 {len(s)}")
        print()

    if a.sweep:
        names = list(F.RULES)
        print("conf_s conf_d conf_llm 규칙                                   표시   표시없이틀림")
        rows = []
        for cs, cd, cl in itertools.product([0.9, 0.95], [0.8, 0.9, 0.95], [0.8, 0.85, 0.9]):
            for k in range(len(names) + 1):
                for rs in itertools.combinations(names, k):
                    cfg = {**F.DEFAULT, "conf_serial": cs, "conf_date": cd, "conf_llm": cl, "rules": rs}
                    _, rate, sr, _ = evaluate(docs, idx, recompute(vols, cfg))
                    rows.append((sr, rate, cs, cd, cl, rs))
        # 표시 비율별로 '표시 없이 틀림'이 가장 낮은 설정 (파레토)
        best, seen = [], 1.0
        for r in sorted(rows, key=lambda r: (r[1], r[0])):
            if r[0] < seen - 1e-9:
                best.append(r)
                seen = r[0]
        for sr, rate, cs, cd, cl, rs in best:
            print(f"{cs:5.2f} {cd:6.2f} {cl:7.2f}  {','.join(rs):38s} {rate:5.0%}   {sr:5.1%}")


if __name__ == "__main__":
    main()
