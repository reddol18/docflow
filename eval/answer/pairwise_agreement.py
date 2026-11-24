"""쌍별 심판과 사람의 일치도 (https://github.com/reddol18/docflow/issues/22 조건 3, 2026-11-24 사용자 결정 3).

사람 채점표(pairwise_sheet.py의 CSV를 채운 것 — 채점 페이지의 'CSV 저장' 파일)의 A/B/비김을 자리 열쇠(_key/…_pw_key.json)로
a·b·tie로 바꾸고, 쌍별 결과(pairwise.py)의 질의별 result(두 순서가 같아야 승, 아니면 tie)와 id로 잇는다.
비김 처리 (이 구현에서 정함 — 사용자 권고대로): 사람이 '비김'이라고 한 쌍은 일치율·kappa에서 뺀다 — 사람이 둘 중 하나를
  고르지 못한 쌍은 심판이 맞혔는지 볼 수 없다. 대신 그 수와 그 쌍들의 심판 판정(a·b·tie 수)을 따로 낸다.
  심판의 tie는 빼지 않는다 — 사람이 고른 쌍에서 심판이 tie면 불일치로 센다 (심판이 가르지 못한 것).
  그래서 kappa는 범주 {a, b, tie}의 Cohen kappa (사람 쪽에는 tie가 없다).
통과 기준 = #21과 같다 (agreement.py MIN_AGREEMENT·MIN_KAPPA·MAX_SKEW, verdict):
  일치율 ≥ 80% 그리고 kappa ≥ 0.6 → 승률 사용. 일치율 ≥ 80%이고 한쪽 값이 90% 넘게 한 범주로 몰렸으면 일치율만으로 사용.
  아니면 쌍별 지시문을 한 번 고쳐 재채점(--revision 0), 고친 뒤에도(--revision 1) 못 넘으면 승률은 참고값으로만 (win_rate_min null 유지).
결과: 표준 출력에 마크다운, --out에 JSON.

실행 (표준 라이브러리만):
  python -I -X utf8 eval/answer/pairwise_agreement.py --human <채운 CSV> --key <_key\\…_pw_key.json> --pairwise <쌍별.json> --revision 0
"""
import argparse
import csv
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # python -I: 같은 폴더

from agreement import MAX_SKEW, MIN_AGREEMENT, MIN_KAPPA, verdict  # noqa: E402
from pairwise_sheet import VERDICT_COL  # noqa: E402

CATS = ("a", "b", "tie")
HUMAN = {"a": "A", "답 a": "A", "b": "B", "답 b": "B",
         "비김": "tie", "tie": "tie", "무승부": "tie", "동점": "tie", "t": "tie", "": ""}


def norm(v):
    s = str(v if v is not None else "").strip().lower()
    if s not in HUMAN:
        raise ValueError(f"판정 값을 모름: {v!r} (A/B/비김)")
    return HUMAN[s]


def read_human(path):
    """채운 CSV → {id: 'A'·'B'·'tie'·''(빈칸)}"""
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if rows and VERDICT_COL not in rows[0]:
        raise ValueError(f"'{VERDICT_COL}' 칸이 없다 — pairwise_sheet.py 표인지 확인")
    out = {}
    for r in rows:
        rid = str(r.get("id") or "").strip()
        if not rid:
            continue
        if rid in out:
            raise ValueError(f"채점표에 id가 겹친다: {rid}")
        try:
            out[rid] = norm(r.get(VERDICT_COL))
        except ValueError as e:
            raise ValueError(f"{rid}: {e}") from None
    return out


def to_side(pos, a_is):
    """사람이 고른 자리 'A'·'B'·'tie' → 후보 'a'·'b'·'tie' (a_is = 답 A에 놓인 쪽)"""
    if pos == "tie":
        return "tie"
    other = "b" if a_is == "a" else "a"
    return a_is if pos == "A" else other


def kappa(pairs, cats=CATS):
    """범주형 Cohen kappa — pairs [(사람, 심판)]. 우연 일치가 1이면 NaN"""
    n = len(pairs)
    if not n:
        return math.nan
    po = sum(h == j for h, j in pairs) / n
    pe = sum((sum(h == c for h, _ in pairs) / n) * (sum(j == c for _, j in pairs) / n) for c in cats)
    return math.nan if pe >= 1 else (po - pe) / (1 - pe)


def skew(pairs):
    """사람·심판 각각에서 가장 많은 범주의 비율 중 큰 쪽"""
    if not pairs:
        return None
    return max(max(sum(p[s] == c for p in pairs) for c in CATS) / len(pairs) for s in (0, 1))


def evaluate(human, key, pw, revision=0):
    if key.get("pairwise_label") != pw.get("label") or key.get("queries_sha256") != pw.get("queries_sha256"):
        raise ValueError("열쇠가 이 쌍별 결과로 만든 것이 아니다 (label·질의 세트)")
    pos = key["position_a_is"]
    judge = {r["id"]: r for r in pw["rows"]}
    extra = sorted(set(human) - set(pos))
    if extra:
        raise ValueError(f"열쇠에 없는 id: {extra}")
    missing = sorted(set(pos) - set(human))
    pairs, ties, blank, rows = [], [], [], []
    for rid in sorted(pos):
        if rid in missing:
            continue
        h = human[rid]
        j = judge[rid]["result"]
        if j not in CATS:
            raise ValueError(f"{rid}: 쌍별 결과에 판정이 없다 (오류 쌍)")
        if h == "":
            blank.append(rid)
            continue
        side = to_side(h, pos[rid])
        rows.append({"id": rid, "human": side, "judge": j, "judge_consistent": judge[rid].get("consistent")})
        if side == "tie":
            ties.append(j)
        else:
            pairs.append((side, j))
    agr = sum(h == j for h, j in pairs) / len(pairs) if pairs else None
    k = kappa(pairs)
    sk = skew(pairs)
    conf = {f"{h}{j}": sum(1 for p in pairs if p == (h, j)) for h in ("a", "b") for j in CATS}  # 사람·심판 순
    v = verdict(agr, k, revision, sk)
    return {"n": len(pairs), "agreement": None if agr is None else round(agr, 3),
            "kappa": None if math.isnan(k) else round(k, 3), "skew": None if sk is None else round(sk, 3),
            "confusion_human_judge": conf,
            "human_ties": {"n": len(ties), "judge": {c: ties.count(c) for c in CATS}},
            "blank": blank, "not_in_sheet": missing, "verdict": v,
            "win_rate": {"사용": "win_rate_min을 켤 수 있다", "사용(쏠림 규칙)": "win_rate_min을 켤 수 있다 (쏠림 규칙)",
                         "지시문 고쳐 재채점": "쌍별 지시문을 한 번 고쳐 다시 잰다",
                         "제외": "승률은 참고값으로만 (win_rate_min null 유지)"}[v],
            "rows": rows}


def table(res):
    c = res["confusion_human_judge"]
    agr = "-" if res["agreement"] is None else f"{res['agreement']:.0%}"
    sk = "-" if res["skew"] is None else f"{res['skew']:.0%}"
    head = ("| n | 일치율 | kappa | 쏠림 | 사람a·심판a | a·b | a·tie | b·a | b·b | b·tie | 사람 비김 (심판 a/b/tie) | 빈칸 | 결론 |\n"
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    t = res["human_ties"]
    return head + (f"\n| {res['n']} | {agr} | {res['kappa'] if res['kappa'] is not None else '계산 불가'} | {sk} | "
                   f"{c['aa']} | {c['ab']} | {c['atie']} | {c['ba']} | {c['bb']} | {c['btie']} | "
                   f"{t['n']} ({t['judge']['a']}/{t['judge']['b']}/{t['judge']['tie']}) | {len(res['blank'])} | "
                   f"{res['verdict']} — {res['win_rate']} |")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--human", required=True, help="채운 채점표 CSV (채점 페이지 'CSV 저장')")
    ap.add_argument("--key", required=True, help="pairwise_sheet.py가 만든 _key/…_pw_key.json")
    ap.add_argument("--pairwise", required=True, help="pairwise.py 결과 JSON (표를 만든 그 파일)")
    ap.add_argument("--revision", type=int, default=0, help="쌍별 지시문 고친 횟수 (0: 미달이면 재채점, 1: 미달이면 참고값)")
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)
    pw = json.loads(Path(a.pairwise).read_text(encoding="utf-8"))
    key = json.loads(Path(a.key).read_text(encoding="utf-8"))
    res = evaluate(read_human(a.human), key, pw, a.revision)
    print(f"쌍별 심판 {pw['judge']['model']} · 지시문 {pw['prompts']['version']} ({pw['prompts']['sha256'][:12]}) · "
          f"기준 일치율 ≥ {MIN_AGREEMENT:.0%} 그리고 kappa ≥ {MIN_KAPPA} (또는 쏠림 > {MAX_SKEW:.0%}이면 일치율만) · "
          "사람 비김은 일치율·kappa에서 뺌")
    print(table(res))
    if a.out:
        Path(a.out).write_text(json.dumps({"pairwise_label": pw["label"], "prompts": pw["prompts"], "revision": a.revision,
                                           "gate": {"agreement": MIN_AGREEMENT, "kappa": MIN_KAPPA, "skew_over": MAX_SKEW},
                                           "human_tie_rule": "사람 비김은 일치율·kappa에서 빼고 따로 보고, 심판 tie는 불일치로 셈",
                                           **res}, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
