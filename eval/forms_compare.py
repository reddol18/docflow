"""양식별 경계·4칸 정답률 — 합성 work 하나와 그 work로 돌린 promptfoo 결과(eval/promptfoo/run.ps1 -Work)로
(https://github.com/reddol18/docflow/issues/8 조건 1).

  python -I -X utf8 eval/forms_compare.py --work <합성 work> --results <work>/promptfoo/results.json --out <저장소 밖 폴더>
         [--provider gpt-4.1-mini] [--prompt current]

후보 응답으로 철을 다시 조립하는 길은 모델 비교 하네스(eval/promptfoo/gate.py rebuild)와 같고, 채점은 eval/score.py metrics()다
(정답 = work 폴더 gt/review.csv, 철 양식 = synth_manifest.json). 이 도구가 더하는 것은 양식별로 나눠 세는 것과
'규칙이 틀린 쪽' 목록(조건 2 비전 판별 대상)뿐이다. 합성 work만 받는다 (실제 자료 work는 멈춤).
출력
  화면                 양식 × 경계 재현율·정밀도·4칸(B)·4칸 모두 정답 — 수만
  <out>/forms_<이름>.json  같은 수 + 틀린 쪽 목록 (합성 쪽 이름·틀린 칸 이름만, 값 없음)
    boundary_wrong  [{page, gt_start, pred_start}]   운영 split(layout.is_start)이 틀린 쪽
    field_wrong     [{page, fields: [칸...]}]        정답 시작 쪽 중 4칸(B)이 하나라도 틀린 쪽 (문서를 못 찾은 쪽 포함)
"""
import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_ROOT = HERE.parent
for _p in (HERE / "promptfoo", HERE, HERE / "synth", APP_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def pct(pair):
    a, b = pair
    return f"{100.0 * a / b:.1f}% ({a}/{b})" if b else "-"


def by_form(gate, score, docs, gt):
    """{양식: metrics} — 철 양식(manifest)별로 문서를 나눠 같은 metrics()로"""
    out = {}
    forms_of = {}
    for vol in gate.common.vols():
        forms_of.setdefault(gate.common.form_of(vol), []).append(vol)
    for form, vols in sorted(forms_of.items()):
        sub = {k: d for k, d in docs.items() if d["vol"] in vols}
        if not sub:
            continue
        out[form] = score.metrics(sub, gt, form_of=gate.common.form_of)
    return out


def wrong_pages(score, m, docs, bnd):
    idx = m["_idx"]
    bw = []
    starts = set(docs)
    for p, g in sorted(bnd.items()):
        if p.rsplit("-", 1)[0] not in m["vols"]:
            continue
        y = p in starts
        if g != y:
            bw.append({"page": p, "gt_start": g, "pred_start": y})
    fw = []
    for p in sorted(idx):
        d = docs.get(p)
        bad = [f for f in score.FIELDS if not d or not score.ok_field(f, score.b_val(d, f), idx[p][f])]
        if bad:
            fw.append({"page": p, "fields": bad, "found": d is not None})
    return bw, fw


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--work", required=True)
    ap.add_argument("--results", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--name", default="")
    ap.add_argument("--provider", default="gpt-4.1-mini")
    ap.add_argument("--prompt", default="current")
    a = ap.parse_args(argv)
    work, out = Path(a.work).resolve(), Path(a.out).resolve()
    if out.is_relative_to(APP_ROOT) or work.is_relative_to(APP_ROOT):
        raise SystemExit("work·결과는 저장소 밖에 둔다")
    os.environ["DOCFLOW_EVAL_WORK"] = str(work)
    os.environ.pop("DOCFLOW_EVAL_VOLS", None)
    import gate  # noqa: PLC0415 — DOCFLOW_EVAL_WORK를 넣은 뒤 (common.WORK)
    import score  # noqa: PLC0415

    bad = gate.common.non_synthetic()
    if bad or not gate.common.vols():
        raise SystemExit(f"합성 철이 아닌 철 {len(bad)}개 — 합성 work만 받는다")
    rows, _, _ = gate.load_results(a.results)
    groups = gate.group(rows)
    res = groups.get((a.provider, a.prompt))
    if not res:
        raise SystemExit(f"결과에 후보 {a.provider}·{a.prompt}가 없다: {sorted(groups)}")
    data = {v: gate.common.load_vol(v) for v in gate.common.vols()}
    docs, _, errors = gate.rebuild(res, data)
    gt = gate.common.load_gt()
    cost = gate.cost_latency(res)
    report = {"work": work.name, "provider": a.provider, "prompt": a.prompt, "read_errors": errors,
              "usd": cost["usd"], "n_calls": cost["n"], "forms": {}}
    print("| 양식 | 철 | 경계 확인 쪽 | 정답 문서 | 경계 재현율 | 경계 정밀도 | " + " | ".join(score.FIELDS) + " | 4칸 모두 |")
    print("|---|---|---|---|---|---|" + "---|" * len(score.FIELDS) + "---|")
    for form, m in by_form(gate, score, docs, gt).items():
        bw, fw = wrong_pages(score, m, {k: d for k, d in docs.items() if d["vol"] in m["vols"]}, gt[2])
        report["forms"][form] = {
            "vols": len(m["vols"]), "pages": m["n_bnd"], "docs_gt": m["n_gt"], "docs_split": m["n_docs"],
            "boundary": m["boundary"], "fields_b": m["fields"]["B"], "fields_final": m["fields"]["final"],
            "all_ok": m["doc"]["all_ok"], "boundary_wrong": bw, "field_wrong": fw}
        b = m["fields"]["B"]
        print(f"| {form} | {len(m['vols'])} | {m['n_bnd']} | {m['n_gt']} | {pct(m['boundary']['recall'])} | "
              f"{pct(m['boundary']['precision'])} | " + " | ".join(pct(b[f]) for f in score.FIELDS)
              + f" | {pct(m['doc']['all_ok'])} |")
    print(f"읽기 실패 {errors}, 호출 {cost['n']}건 ${cost['usd']:.4f}")
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"forms_{a.name or work.name}.json"
    p.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"→ {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
