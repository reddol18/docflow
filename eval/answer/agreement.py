"""사람 채점과 심판 채점의 일치도 (https://github.com/reddol18/docflow/issues/21 5단계).

사람 채점표(human_sheet.py로 만든 xlsx·csv를 사람이 채운 것)와 심판 결과(judge.py)를 id로 잇고, 지표마다
둘 다 P/F를 준 질의만으로 일치율·Cohen kappa를 낸다 (N·'-'·빈칸은 뺀다 — 빠진 수도 낸다).
통과 기준 (2026-11-22 사용자 결정, https://github.com/reddol18/docflow/issues/21#issuecomment-7070886287):
  일치율 ≥ 80% 이고 kappa ≥ 0.6 → 그 지표를 쓴다. 아니면 심판 지시문을 한 번 고쳐 재채점(--revision 0일 때),
  고친 뒤에도(--revision 1) 못 넘으면 그 지표를 뺀다. kappa를 셀 수 없으면(한쪽이 모두 같은 값) 기준 미달로 본다.
결과: 표준 출력에 마크다운 표, --out에 JSON. 심판 결과에 metrics_judged(judge.py --metrics)가 있으면 그 지표만 본다.

실행 (표준 라이브러리 + openpyxl(xlsx일 때)):
  & $py -I -X utf8 eval/answer/agreement.py --human <human.xlsx> --judge <judge\\qwen3_30b.json> --revision 0
"""
import argparse
import csv
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # python -I: 같은 폴더

from human_sheet import RUBRIC  # noqa: E402

MIN_AGREEMENT = 0.8
MIN_KAPPA = 0.6
LABELS = {"p": "P", "pass": "P", "통과": "P", "o": "P", "1": "P",
          "f": "F", "fail": "F", "실패": "F", "x": "F", "0": "F",
          "n": "N", "해당없음": "N", "해당 없음": "N", "-": "N", "": ""}


def norm(v):
    """사람이 적은 값 → 'P'·'F'·'N'·''(빈칸). 모르는 값이면 ValueError"""
    s = str(v if v is not None else "").strip().lower()
    if s.endswith(".0"):  # 엑셀이 1·0을 숫자로 읽은 경우
        s = s[:-2]
    if s not in LABELS:
        raise ValueError(f"채점 값을 모름: {v!r} (P/F/N)")
    return LABELS[s]


def read_human(path):
    """채점표 → {id: {지표: P/F/N/''}}. 머리 '키 (한국어)'의 괄호 앞을 지표 키로"""
    path = Path(path)
    if path.suffix.lower() == ".xlsx":
        from openpyxl import load_workbook  # noqa: PLC0415
        ws = load_workbook(path, read_only=True, data_only=True)["채점"]
        it = ws.iter_rows(values_only=True)
        head = [str(h or "") for h in next(it)]
        rows = [dict(zip(head, r)) for r in it if any(c is not None for c in r)]
    else:
        with path.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
    keys = {h: h.split(" (", 1)[0] for h in (rows[0] if rows else {}) if h.split(" (", 1)[0] in {k for k, _, _ in RUBRIC}}
    out = {}
    for r in rows:
        rid = str(r.get("id") or "").strip()
        if not rid:
            continue
        if rid in out:
            raise ValueError(f"채점표에 id가 겹친다: {rid}")
        try:
            out[rid] = {k: norm(r.get(h)) for h, k in keys.items()}
        except ValueError as e:
            raise ValueError(f"{rid}: {e}") from None
    return out


def kappa(pairs):
    """이진(P/F) Cohen kappa — pairs [(사람, 심판)]. 우연 일치가 1이면(둘 다 한 값만) NaN"""
    n = len(pairs)
    if not n:
        return math.nan
    po = sum(a == b for a, b in pairs) / n
    ph = sum(a == "P" for a, _ in pairs) / n
    pj = sum(b == "P" for _, b in pairs) / n
    pe = ph * pj + (1 - ph) * (1 - pj)
    return math.nan if pe >= 1 else (po - pe) / (1 - pe)


def join(human, judge_rows, metrics):
    """{지표: {pairs, missing_human, only_one}} — 둘 다 P/F인 질의만 짝. 심판 결과에 없는 사람 id는 멈춘다"""
    jr = {r["id"]: r for r in judge_rows}
    extra = sorted(set(human) - set(jr))
    if extra:
        raise ValueError(f"심판 결과에 없는 id: {extra} — 같은 답변 파일인지 확인")
    out = {}
    for m in metrics:
        pairs, missing, one = [], [], []
        for rid in sorted(set(human) & set(jr)):
            h, j = human[rid].get(m, ""), jr[rid]["binary"].get(m, "N")
            if h == "" and j in ("P", "F"):
                missing.append(rid)
            elif h in ("P", "F") and j in ("P", "F"):
                pairs.append((h, j))
            elif (h in ("P", "F")) != (j in ("P", "F")):
                one.append(rid)  # 한쪽만 해당 없음
        out[m] = {"pairs": pairs, "missing_human": missing, "only_one": one}
    return out


def verdict(agreement, k, revision):
    if agreement is not None and agreement >= MIN_AGREEMENT and not math.isnan(k) and k >= MIN_KAPPA:
        return "사용"
    return "지시문 고쳐 재채점" if revision == 0 else "제외"


def evaluate(human, judge_rows, revision=0, metrics=None):
    metrics = metrics or [k for k, _, _ in RUBRIC]
    res = {}
    for m, j in join(human, judge_rows, metrics).items():
        pairs = j["pairs"]
        agr = sum(a == b for a, b in pairs) / len(pairs) if pairs else None
        k = kappa(pairs)
        conf = {f"{a}{b}": sum(1 for p in pairs if p == (a, b)) for a in "PF" for b in "PF"}  # 사람·심판 순
        res[m] = {"n": len(pairs), "agreement": None if agr is None else round(agr, 3),
                  "kappa": None if math.isnan(k) else round(k, 3), "confusion_human_judge": conf,
                  "missing_human": j["missing_human"], "only_one_na": j["only_one"], "verdict": verdict(agr, k, revision)}
    return res


def table(res):
    lines = ["| 지표 | n | 일치율 | kappa | 사람P·심판P | P·F | F·P | F·F | 빈칸 | 한쪽만 N | 결론 |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for m, r in res.items():
        c = r["confusion_human_judge"]
        agr = "-" if r["agreement"] is None else f"{r['agreement']:.0%}"
        lines.append(f"| {m} | {r['n']} | {agr} | {r['kappa'] if r['kappa'] is not None else '계산 불가'} | {c['PP']} | {c['PF']} | "
                     f"{c['FP']} | {c['FF']} | {len(r['missing_human'])} | {len(r['only_one_na'])} | {r['verdict']} |")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--human", required=True)
    ap.add_argument("--judge", required=True)
    ap.add_argument("--revision", type=int, default=0, help="심판 지시문 고친 횟수 (0: 처음 → 미달이면 재채점, 1: 미달이면 제외)")
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)
    judge = json.loads(Path(a.judge).read_text(encoding="utf-8"))
    judged = judge.get("metrics_judged")  # judge.py --metrics로 일부만 채점했으면 그 지표만 본다
    metrics = [k for k, _, _ in RUBRIC if judged is None or k in judged]
    res = evaluate(read_human(a.human), judge["rows"], a.revision, metrics)
    print(f"심판 {judge['judge']['model']} · 지시문 {judge['prompts']['version']} ({judge['prompts']['sha256'][:12]}) · "
          f"기준 일치율 ≥ {MIN_AGREEMENT:.0%} 그리고 kappa ≥ {MIN_KAPPA}")
    print(table(res))
    if a.out:
        Path(a.out).write_text(json.dumps({"judge_label": judge["label"], "prompts": judge["prompts"], "revision": a.revision,
                                           "gate": {"agreement": MIN_AGREEMENT, "kappa": MIN_KAPPA}, "metrics": res},
                                          ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
