"""플랫폼 결과(/volumes/{vol}/documents JSON)를 정답 CSV로 채점한다. LLM은 쓰지 않는다.

실행 (records_poc 폴더에서): .venv/Scripts/python.exe -I -X utf8 doc_platform/eval/score.py work/platform/*.json
  [--gt 정답.csv ...] [--detail]
  --gt  정답 CSV (PoC 확정 정답 review.csv 또는 합성 철 안의 gt.csv — 여럿 가능). 저장소 밖에 둔다.
        안 주면 결과 파일 옆 work 폴더의 gt/review.csv (<work>/platform/<철>.json → <work>/gt/review.csv)
채점 규칙은 eval/scoring.py (PoC scripts/rescore.py에서 옮김 — 정규화, '?' 와일드카드, 일련번호 빈칸, 날짜 YYYY-MM,
https://github.com/reddol18/docflow/issues/8 조건 3). 저장소 밖 PoC 폴더 없이 돈다.
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

HERE = Path(__file__).resolve().parent
for _i, _p in enumerate((HERE, HERE.parent, HERE / "synth")):  # eval(scoring), doc_platform(색인 스키마), forms.py(합성 양식)
    if str(_p) not in sys.path:
        sys.path.insert(_i, str(_p))
import forms  # noqa: E402
from scoring import default_gt, gt_serial, load_gt, match, ok_field, pct, pdate  # noqa: E402

from app import fields  # noqa: E402


def _schema(form):
    return fields.load(fields.DIR / forms.SCHEMA_FILE[form])


def form_fields():
    """양식별 채점 칸 = 그 양식 칸 정의에서 사람이 고칠 수 있는 칸 (https://github.com/reddol18/docflow/issues/9) → {양식: (칸...)}"""
    return {f: tuple(fields.editable(_schema(f))) for f in forms.FORMS}


HAND_FLAGS = ("serial", "date")  # 손글씨 판독 검토 표시를 집계하는 칸 = 칸 정의 rule 이름 (app/graph.py RULES)


def hand_flag_forms():
    """{표시 칸: 그 칸 규칙(rule)이 있는 양식 집합}. draft2000처럼 규칙이 없는 양식(문서번호·시행일자가 인쇄 글자, LLM 추출만)의
    문서는 손글씨 표시 집계에서 뺀다 — 최종 칸에 serial이 없고 date 표시도 붙지 않는다 (https://github.com/reddol18/docflow/issues/139)"""
    rules = {f: {x.get("rule") for x in _schema(f)["fields"]} for f in forms.FORMS}
    return {fld: {f for f, r in rules.items() if fld in r} for fld in HAND_FLAGS}


HAND_FORMS = hand_flag_forms()


def manifest_forms(results):
    """결과 파일(<work>/platform/<철>.json) 옆 work의 synth_manifest.json → {철: 양식}. 없으면 빈 dict (실제 철 = 김해)"""
    out = {}
    for p in {Path(x).resolve().parent.parent / "synth_manifest.json" for x in results}:
        if p.exists():
            out.update({v["vol"]: forms.form_of(v) for v in json.loads(p.read_text(encoding="utf-8")).get("vols", [])
                        if v.get("vol")})
    return out


def scoring_fields():
    """채점 칸 — 정답(gt.csv·review.csv)은 docno·date·recipient·title 4칸 열뿐이라, 모든 양식(김해·합성 새 양식,
    https://github.com/reddol18/docflow/issues/139)의 고칠 수 있는 칸이 같아야 한 실행에 섞인 철을 같은 칸으로 채점할 수 있다.
    어느 양식이 다른 칸을 갖게 되면 조용히 김해 칸으로 채점하지 않고 멈춘다"""
    by_form = form_fields()
    base = by_form[forms.DEFAULT_FORM]
    other = {f: v for f, v in by_form.items() if v != base}
    if other:
        raise SystemExit(f"양식마다 채점 칸이 다르다 (김해 {base}): {other} — eval/score.py를 철 양식별 칸으로 고친다")
    return list(base)


FIELDS = scoring_fields()


def b_val(d, f):
    if f == "recipient":
        return d["fields"]["recipient"]  # 수신 보정은 B 단계에 포함
    return d["b"][f]["value"]


def metrics(docs, gt, form_of=None, vols=None):
    """docs = {doc_id: 문서 결과}, gt = scoring.load_gt(정답 CSV) 결과. 결과에 든 철만 채점.
    vols를 주면 그 철들의 정답으로 채점한다 (문서가 하나도 없는 철도 — 못 찾은 문서 = 틀림).
    반환 dict의 (맞은 수, 전체) 쌍과 목록으로 화면·비교를 만든다.
    form_of(철) → 양식 (없으면 모두 김해). 손글씨 표시(serial·date)는 그 칸 규칙이 있는 양식의 문서만 센다 (HAND_FORMS)"""
    form_of = form_of or (lambda v: forms.DEFAULT_FORM)
    rows, idx, bnd, _ = gt
    vols = set(vols) if vols is not None else {d["vol"] for d in docs.values()}
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
    for fld in HAND_FLAGS:
        ps = [p for p in idx if form_of(vol(p)) in HAND_FORMS[fld]
              and (fld == "date" or gt_serial(idx[p]["docno"]) is not None)]
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


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("results", nargs="+")
    ap.add_argument("--gt", action="append",  # nargs='+'면 뒤 결과 파일(위치 인자)까지 삼킨다
                    help="정답 CSV (저장소 밖), 여럿이면 --gt를 여러 번. 안 주면 결과 파일 옆 work의 gt/review.csv")
    ap.add_argument("--detail", action="store_true")
    a = ap.parse_args(argv)
    docs = {}
    for path in a.results:
        for d in json.loads(Path(path).read_text(encoding="utf-8")):
            docs[d["doc_id"]] = d
    by_form = manifest_forms(a.results)
    m = metrics(docs, load_gt(a.gt or default_gt(a.results)), form_of=lambda v: by_form.get(v, forms.DEFAULT_FORM))
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
