"""양식별 경계·4칸 정답률 — 합성 work 하나와 그 work로 돌린 promptfoo 결과(eval/promptfoo/run.ps1 -Work)로
(https://github.com/reddol18/docflow/issues/8 조건 1).

  python -I -X utf8 eval/forms_compare.py --work <합성 work> --results <work>/promptfoo/results.json --out <저장소 밖 폴더>
         [--provider gpt-4.1-mini] [--prompt current]

후보 응답으로 철을 다시 조립하는 길은 모델 비교 하네스(eval/promptfoo/gate.py rebuild)와 같고, 채점은 eval/score.py metrics()다
(정답 = work 폴더 gt/review.csv, 철 양식 = synth_manifest.json). 이 도구가 더하는 것은 양식별로 나눠 세는 것과
'규칙이 틀린 쪽' 목록(조건 2 비전 판별 대상)뿐이다. 합성 work만 받는다 (실제 자료 work·aihub_extracted 경로는 멈춤 —
eval/compare_common.reject_real_data). 양식은 synth_manifest의 철 목록으로 나눈다. 채점은 결과에 행이 있는 철(실행 대상,
run.ps1 -Vols로 일부만 돌렸을 수 있다)만 — 실행 대상인데 문서를 하나도 못 찾은 양식은 0으로, 철이 하나도 실행 대상이 아닌 양식은
'실행 안 됨'(not_run)으로 따로 낸다 (0점·비전 대상으로 잡지 않는다).
단가를 모르는 후보면 gate.py처럼 종료 코드 2 (로컬 모델은 결과 config의 apiBaseUrl로 0원).
출력
  화면                 양식 × 경계 재현율·정밀도·4칸(B)·4칸 모두 정답 — 수만
  <out>/forms_<work>_<후보>_<판>.json (--name을 주면 forms_<이름>.json, 이미 있으면 멈춤)
                       같은 수 + 틀린 쪽 목록 (합성 쪽 이름·틀린 칸 이름만, 값 없음)
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
import compare_common as cc  # noqa: E402


def pct(pair):
    a, b = pair
    return f"{100.0 * a / b:.1f}% ({a}/{b})" if b else "-"


def vol_of(doc_id):
    return doc_id.rsplit("-", 1)[0]


def run_vols(res, have):
    """결과에 행이 하나라도 있는 철 (= 이번 실행 대상, run.ps1 -Vols로 일부만 돌렸을 수 있다). work에 없는 철은 뺀다"""
    return sorted({vol_of(k) for k in res} & set(have))


def by_form(gate, score, docs, gt, ran):
    """({양식: metrics}, {양식: 철 수}) — 철 양식(synth_manifest의 철 목록)별로, 실행 대상 철(ran)의 정답·문서만 같은 metrics()로.
    실행 대상인데 split 문서가 하나도 없는 양식은 그 철의 정답으로 채점해 0으로 남기고(표에서 빠지지 않게),
    철이 하나도 실행 대상이 아닌 양식은 채점하지 않고 '실행 안 됨'으로 따로 돌려준다 (0점·비전 대상으로 잡지 않게)"""
    out, not_run = {}, {}
    forms_of = {}
    for vol in sorted(gate.common.synthetic_vols()):
        forms_of.setdefault(gate.common.form_of(vol), []).append(vol)
    for form, vols in sorted(forms_of.items()):
        vols = [v for v in vols if v in ran]
        if not vols:
            not_run[form] = len(forms_of[form])
            continue
        sub = {k: d for k, d in docs.items() if d["vol"] in vols}
        out[form] = score.metrics(sub, gt, form_of=gate.common.form_of, vols=vols)
        out[form]["vols_not_run"] = len(forms_of[form]) - len(vols)
    return out, not_run


def safe_name(s):
    return "".join(c if c.isalnum() or c in "-._" else "_" for c in s)


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
    cc.reject_real_data(work, "--work")
    cc.reject_real_data(a.results, "--results")
    os.environ["DOCFLOW_EVAL_WORK"] = str(work)
    os.environ.pop("DOCFLOW_EVAL_VOLS", None)
    import gate  # noqa: PLC0415 — DOCFLOW_EVAL_WORK를 넣은 뒤 (common.WORK)
    import score  # noqa: PLC0415

    if not gate.common.vols():
        raise SystemExit(f"work에 철 결과가 없다 ({work / 'platform'}/*.json) — eval/synth/to_work.py로 만든 합성 work를 준다")
    bad = gate.common.non_synthetic()
    if bad:
        raise SystemExit(f"합성 철이 아닌 철 {len(bad)}개 — 합성 work만 받는다")
    rows, cfg, _ = gate.load_results(a.results)
    groups = gate.group(rows)
    res = groups.get((a.provider, a.prompt))
    if not res:
        raise SystemExit(f"결과에 후보 {a.provider}·{a.prompt}가 없다: {sorted(groups)}")
    unknown = gate.unpriced(list(res.values()), cfg)  # gate.main과 같이 — 단가 모르는 원격 모델이면 채점 전에 멈춘다
    if unknown:
        print(f"단가를 모르는 모델: {unknown} — app/llm.py PRICE에 넣거나 로컬 모델이면 apiBaseUrl을 127.0.0.1로", file=sys.stderr)
        return 2
    ran = run_vols(res, gate.common.vols())
    if not ran:
        raise SystemExit(f"후보 {a.provider}·{a.prompt}의 결과에 이 work 철의 문서가 하나도 없다 — 다른 work로 돌린 결과인지 본다")
    # 결과에 행이 있는 철만 조립·채점 (run.ps1 -Vols로 일부만 돌렸으면 나머지 철을 0점·읽기 실패로 세지 않게)
    p = out / (f"forms_{safe_name(a.name)}.json" if a.name
               else f"forms_{safe_name(work.name)}_{safe_name(a.provider)}_{safe_name(a.prompt)}.json")
    if p.exists():  # 다른 후보·판 결과를 조용히 덮어쓰지 않는다
        raise SystemExit(f"출력 파일이 이미 있다: {p} — 지우거나 --name을 바꾼다")
    data = {v: gate.common.load_vol(v) for v in ran}
    docs, _, errors = gate.rebuild(res, data)
    gt = gate.common.load_gt()
    cost = gate.cost_latency(res, cfg)
    scored, not_run = by_form(gate, score, docs, gt, set(ran))
    report = {"work": work.name, "provider": a.provider, "prompt": a.prompt, "read_errors": errors,
              "usd": cost["usd"], "n_calls": cost["n"], "vols_run": len(ran), "forms": {}, "not_run": not_run}
    print("| 양식 | 철 | 경계 확인 쪽 | 정답 문서 | 경계 재현율 | 경계 정밀도 | " + " | ".join(score.FIELDS) + " | 4칸 모두 |")
    print("|---|---|---|---|---|---|" + "---|" * len(score.FIELDS) + "---|")
    for form, m in scored.items():
        bw, fw = wrong_pages(score, m, {k: d for k, d in docs.items() if d["vol"] in m["vols"]}, gt[2])
        report["forms"][form] = {
            "vols": len(m["vols"]), "vols_not_run": m["vols_not_run"], "pages": m["n_bnd"], "docs_gt": m["n_gt"],
            "docs_split": m["n_docs"], "boundary": m["boundary"], "fields_b": m["fields"]["B"],
            "fields_final": m["fields"]["final"], "all_ok": m["doc"]["all_ok"], "boundary_wrong": bw, "field_wrong": fw}
        b = m["fields"]["B"]
        skipped = f" (+실행 안 됨 {m['vols_not_run']})" if m["vols_not_run"] else ""
        print(f"| {form} | {len(m['vols'])}{skipped} | {m['n_bnd']} | {m['n_gt']} | {pct(m['boundary']['recall'])} | "
              f"{pct(m['boundary']['precision'])} | " + " | ".join(pct(b[f]) for f in score.FIELDS)
              + f" | {pct(m['doc']['all_ok'])} |")
    for form, n in not_run.items():
        print(f"| {form} | 실행 안 됨 (철 {n}) |" + " |" * (5 + len(score.FIELDS)))
    print(f"실행 대상 철 {len(ran)}, 읽기 실패 {errors}, 호출 {cost['n']}건 ${cost['usd']:.4f}")
    out.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"→ {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
