"""저장된 결과로 '검토 표시' 규칙·기준값을 바꿨을 때를 미리 계산한다 (재실행·LLM 없음).

규칙은 app/flags.py 하나를 그래프와 같이 쓴다. 표시는 저장된 값이 아니라 여기서 다시 계산한다.
실행 (records_poc 폴더에서):
  .venv/Scripts/python.exe -I -X utf8 doc_platform/eval/flag_whatif.py work/platform/*.json [--sweep] [--loo] [--detail 이름]

  기본      이전 규칙(LEGACY)·기본값(DEFAULT)·전체 규칙을 칸별·문서별로 비교
  --sweep   확신도 기준값(이전보다 낮은 값, 0=끔 포함) × 일자 불일치 켬/끔 × 추가 규칙 조합의 파레토 경계,
            표시 80% 이하에서의 최선, 이론 하한
  --loo     철 하나 빼고 고르기: 6개 철로 설정을 고르고 남은 철에서 잰다 (표본 맞춤 정도 확인)
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
    ap.add_argument("--loo", action="store_true")
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

    if a.sweep or a.loo:
        cfgs, flagged = sweep(vols, docs, idx)
        bad = {p for p in idx if p not in docs or not all(ok_field(f, docs[p]["fields"][f], idx[p][f]) for f in FIELDS)}
        print(f"설정 {len(cfgs)}개 (기준값은 이전보다 낮은 값과 0=끔 포함, 일자 불일치 규칙 끔 포함)")
        print(f"틀린 문서 {len(bad)}/{len(idx)}, 맞는 문서 {len(idx) - len(bad)}\n")
        if a.sweep:
            print("파레토 경계 (표시 비율마다 '표시 없이 틀림'이 가장 낮은 설정)")
            print("  표시   표시없이틀림  설정")
            best, seen = [], len(idx) + 1
            stats = [(len(flagged[i]), len(bad - flagged[i]), i) for i in range(len(cfgs))]
            for nf, ns, i in sorted(stats):
                if ns < seen:
                    best.append((nf, ns, i))
                    seen = ns
            for nf, ns, i in best:
                print(f"  {nf / len(idx):4.0%}  {ns:3d} ({ns / len(idx):4.1%})   {label(cfgs[i])}")
            under80 = [s for s in stats if s[0] <= 0.8 * len(idx)]
            nf, ns, i = min(under80, key=lambda s: (s[1], s[0]))
            print(f"\n표시 80% 이하 중 '표시 없이 틀림' 최소: 표시 {nf / len(idx):.0%}, {ns}건 ({ns / len(idx):.1%}) — {label(cfgs[i])}")
            print(f"이론 하한: 틀린 문서를 모두 표시하면 표시 {len(bad) / len(idx):.0%}. "
                  f"80% 이하 + 3% 이하가 되려면 맞는 문서 오표시가 "
                  f"{int(0.8 * len(idx)) - (len(bad) - int(0.03 * len(idx)))}건 이하여야 한다")
        if a.loo:
            loo(vols, idx, bad, cfgs, flagged)


GRID = {"conf_serial": [0, 0.9, 0.95], "conf_date": [0, 0.8, 0.9], "conf_llm": [0, 0.8, 0.9], "mismatch_date": [True, False]}


def label(c):
    return (f"serial {c['conf_serial']:g} / date {c['conf_date']:g} / llm {c['conf_llm']:g}"
            f"{'' if c['mismatch_date'] else ' / 일자불일치 끔'} / 규칙 {','.join(c['rules']) or '-'}")


def sweep(vols, docs, idx):
    """모든 설정에 대해 '표시된 문서' 집합을 한 번만 계산한다 (--loo가 다시 쓴다)."""
    names = list(F.RULES)
    cfgs, flagged = [], []
    for vals in itertools.product(*GRID.values()):
        for k in range(len(names) + 1):
            for rs in itertools.combinations(names, k):
                cfg = {**F.LEGACY, **dict(zip(GRID, vals)), "rules": rs}
                fl = recompute(vols, cfg)
                cfgs.append(cfg)
                flagged.append(frozenset(p for p in idx if p in docs and fl[p]))
    return cfgs, flagged


def choose(ids, bad, flagged, target=0.03):
    """ids 안에서 '표시 없이 틀림' ≤ target 중 표시가 가장 적은 설정. 없으면 표시 없이 틀림이 가장 적은 것."""
    n = len(ids)
    stats = [(len((bad & ids) - f), len(f & ids), i) for i, f in enumerate(flagged)]
    ok = [s for s in stats if s[0] <= target * n]
    return min(ok, key=lambda s: (s[1], s[0]))[2] if ok else min(stats, key=lambda s: (s[0], s[1]))[2]


def loo(vols, idx, bad, cfgs, flagged):
    """철 하나 빼고 고르기: 나머지 6개 철로 설정을 고르고, 뺀 철에서 잰다. 고른 설정이 표본에 맞춰졌는지 보는 확인."""
    print("철 하나 빼고 고르기 (나머지 철로 설정 선택 → 뺀 철에서 측정)")
    tot_n = tot_s = tot_f = 0
    for v in sorted(vols):
        test = frozenset(p for p in idx if p.startswith(v + "-"))
        if not test:
            continue
        i = choose(frozenset(idx) - test, bad, flagged)
        s, f = len((bad & test) - flagged[i]), len(flagged[i] & test)
        tot_n, tot_s, tot_f = tot_n + len(test), tot_s + s, tot_f + f
        print(f"  {v}: {len(test):3d}건 표시 {f / len(test):4.0%}, 표시 없이 틀림 {s}  ← {label(cfgs[i])}")
    print(f"  합계: 표시 {tot_f / tot_n:.0%}, 표시 없이 틀림 {tot_s}/{tot_n} ({tot_s / tot_n:.1%})")


if __name__ == "__main__":
    main()
