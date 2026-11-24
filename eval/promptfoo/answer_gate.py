"""검색 답변 행렬 — 후보(답변 모델 × 답변 지시문 판)마다 Ragas 지표·'모른다'·기준과의 쌍별 승률·건당 비용·지연, 통과 기준 판정
(https://github.com/reddol18/docflow/issues/22 조건 3·4·5·6을 답변에).

실행 (LLM 호출 없음 — 이미 만든 파일만 읽는다):
  python -I -X utf8 eval/promptfoo/answer_gate.py --cand <답변.json> <심판.json> [--cand ...] [--pair <쌍별.json> ...] `
      [--criteria criteria.yaml] [--out 폴더]
  답변 = eval/answer/gen_answers.py, 심판 = eval/answer/judge.py (그 답변 파일을 채점한 것), 쌍별 = eval/answer/pairwise.py.
후보 이름 = (답변 파일 chat_model, 답변 지시문 판 answer_prompt — 옛 파일은 지시문 해시로 찾음). 기준 = criteria.yaml answer.baseline.
지표 (#21에서 사람과 일치도를 확인한 것만): faithfulness·answer_relevancy 통과율(P/(P+F))·평균, '모른다'(답 없는 질의),
  답 있는 질의 회피율, 문맥 정밀도·재현율(검색 — 표에만). 인용 정확도는 뺌(https://github.com/reddol18/docflow/issues/157).
승률: 기준과 후보를 담은 쌍별 결과의 후보 쪽 win_rate (무승부 0.5). 쌍별 결과가 없으면 '-'.
건당 비용 = 채점한 질의의 답변 호출 비용 평균(검색 임베딩은 후보끼리 같아 뺌, 로컬·원격 GPU 모델은 0 — GPU 시간은 따로),
지연 = 답변 호출 한 번의 시간(gen_answers latency_ms) 평균·p95. 이 값이 없는 옛 파일은 '-'.
같은 비교가 되게 확인 (어기면 종료 코드 2): 모든 답변 파일의 질의 세트 해시가 같음, 심판 결과가 그 답변 파일을 채점했고 끝까지 돎,
  모든 후보의 심판 모델·심판 지시문 해시·채점한 질의 id가 같음, 쌍별 결과의 질의 id도 같음, 기준이 꼭 하나, 후보 이름이 겹치지 않음,
  기준값을 켠 지표를 채점하지 않은 후보, 승률 기준을 켰는데 쌍별 결과가 없는 후보.
종료 코드: 0 모두 통과, 1 기준을 어긴 후보가 있음, 2 입력 문제
결과: --out(기본: 첫 답변 파일 옆 matrix/)에 answer_gate.md·answer_gate.json — 집계값뿐 (답 글은 넣지 않는다)
"""
import argparse
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_ROOT = HERE.parents[1]
sys.path.insert(0, str(APP_ROOT / "eval" / "answer"))  # python -I: gen_answers.prompt_name_of (옛 파일의 지시문 판 찾기)
sys.path.insert(0, str(APP_ROOT / "eval"))
sys.path.insert(0, str(APP_ROOT))

import yaml  # noqa: E402

# 기준값 이름 → (지표, 무엇을 보나). pass_drop = 통과율이 떨어진 %p, rise = 늘어난 %p
VALIDATED = ("faithfulness", "answer_relevancy", "refusal")  # #21 사용 판정, 심판 오류를 세는 지표
SHOWN = ("context_precision", "context_recall")  # 검색 지표 — 판정에 쓰지 않음
CRIT_METRIC = {"faithfulness_pass_drop_pp_max": "faithfulness", "answer_relevancy_pass_drop_pp_max": "answer_relevancy",
               "dont_know_drop_pp_max": "refusal", "refusal_answerable_rise_pp_max": "refusal"}


class InputError(Exception):
    """같은 비교가 아님 — 종료 코드 2"""


def read_json(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def prompt_name(ans):
    if ans.get("answer_prompt"):
        return ans["answer_prompt"]
    from gen_answers import prompt_name_of  # noqa: PLC0415 — 옛 파일(#21)만: 지시문 해시로 판 이름
    return prompt_name_of(ans)


def pct(x):
    return None if x is None else 100.0 * x


def p95(xs):
    xs = sorted(xs)
    return xs[max(0, math.ceil(0.95 * len(xs)) - 1)] if xs else None


def candidate(ans_path, judge_path):
    """답변·심판 파일 한 쌍 → 후보 줄 (집계값만)"""
    ans, jdg = read_json(ans_path), read_json(judge_path)
    if jdg.get("answers_file") != Path(ans_path).name:
        raise InputError(f"{Path(judge_path).name}은 {jdg.get('answers_file')}를 채점한 결과다 (넘긴 답변 {Path(ans_path).name})")
    if not jdg.get("complete"):
        raise InputError(f"{Path(judge_path).name}: 심판이 끝까지 돌지 않았다 ({jdg.get('judged')}/{jdg.get('planned')})")
    ids = [r["id"] for r in jdg["rows"]]
    rows = {r["id"]: r for r in ans["rows"]}
    missing = [i for i in ids if i not in rows]
    if missing:
        raise InputError(f"{Path(ans_path).name}에 없는 심판 질의: {missing[:5]}")
    sel = [rows[i] for i in ids]
    if any("error" in r for r in sel):
        raise InputError(f"{Path(ans_path).name}: 실패한 답변이 있다")
    s = jdg["summary"]
    judged = set(jdg.get("metrics_judged") or s)
    m = {k: {"pass": pct(s[k]["pass_rate"]) if k in judged else None, "n": s[k]["n"] if k in judged else 0,
             "mean": s[k]["mean"] if k in judged else None} for k in (*VALIDATED, *SHOWN)}
    lat = [r["latency_ms"] for r in sel if r.get("latency_ms") is not None]
    return {"model": ans.get("chat_model"), "prompt": prompt_name(ans), "answers_file": Path(ans_path).name,
            "judge_file": Path(judge_path).name, "queries_sha256": ans.get("queries_sha256"), "ids": sorted(ids),
            "judge_model": (jdg.get("judge") or {}).get("model"), "judge_digest": (jdg.get("judge") or {}).get("ollama_digest"),
            "judge_prompts": (jdg.get("prompts") or {}).get("sha256"), "judge_version": (jdg.get("prompts") or {}).get("version"),
            "judged": judged, "m": m,
            "dont_know": pct(s["refusal"].get("dont_know_rate_unanswerable")),
            "refusal_answerable": pct(s["refusal"].get("refusal_rate_answerable")),
            "judge_errors": sum(s[k]["errors"] for k in VALIDATED if k in s),
            "priced": ans.get("priced", True),
            "usd_per_query": sum(r.get("usd") or 0 for r in sel) / len(sel) if sel else 0.0,
            "ms_mean": sum(lat) / len(lat) if lat else None, "ms_p95": p95(lat), "n": len(sel),
            "win": None, "pair": None, "verdict": ""}


def check_same(cands):
    """후보끼리 같은 비교인가 — 아니면 InputError"""
    for key, what in (("queries_sha256", "질의 세트"), ("judge_model", "심판 모델"), ("judge_digest", "심판 모델 다이제스트"),
                      ("judge_prompts", "심판 지시문 해시"), ("ids", "채점한 질의")):
        vals = {json.dumps(c[key]) for c in cands}
        if len(vals) > 1:
            raise InputError(f"후보마다 {what}가 다르다: " + ", ".join(f"{c['answers_file']}={str(c[key])[:40]}" for c in cands))
    files = [c["answers_file"] for c in cands]  # 쌍별 결과는 파일 이름으로 후보를 찾는다
    same = sorted({f for f in files if files.count(f) > 1})
    if same:
        raise InputError(f"답변 파일 이름이 같은 후보가 둘 이상 (폴더가 달라도 쌍별 결과를 구분 못 함): {same}")
    names = [(c["model"], c["prompt"]) for c in cands]
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        raise InputError(f"같은 후보(모델, 지시문 판)가 둘 이상: {dup}")


def attach_pairs(cands, base, pair_paths):
    """쌍별 결과에서 기준 대 후보 승률을 후보 줄에 붙인다. 모든 쌍별 결과의 요약도 돌려준다"""
    by_file = {c["answers_file"]: c for c in cands}
    pairs = []
    for p in pair_paths:
        pw = read_json(p)
        fa, fb = pw["a"]["file"], pw["b"]["file"]
        if fa not in by_file or fb not in by_file:
            raise InputError(f"{Path(p).name}: 후보에 없는 답변 파일 ({fa}, {fb})")
        if not pw.get("complete"):
            raise InputError(f"{Path(p).name}: 쌍별 심판이 끝까지 돌지 않았다")
        if pw.get("queries_sha256") != base["queries_sha256"]:
            raise InputError(f"{Path(p).name}: 쌍별 질의 세트가 후보의 질의 세트와 다르다")
        if sorted(r["id"] for r in pw["rows"]) != base["ids"]:
            raise InputError(f"{Path(p).name}: 쌍별 질의가 심판 채점 질의와 다르다")
        s = pw["summary"]
        pairs.append({"file": Path(p).name, "a": by_file[fa], "b": by_file[fb], "s": s, "judge_model": pw["judge"]["model"],
                      "version": pw["prompts"]["version"]})
        for side, other in (("a", fb), ("b", fa)):
            c = by_file[pw[side]["file"]]
            if other == base["answers_file"] and c is not base:
                if c["win"] is not None:
                    raise InputError(f"{c['answers_file']}의 기준 대 쌍별 결과가 둘 이상")
                c["win"], c["pair"] = s[f"win_rate_{side}"], {"wins": s[f"{side}_wins"], "ties": s["ties"],
                                                             "losses": s[f"{'b' if side == 'a' else 'a'}_wins"],
                                                             "n": s["n"], "errors": s.get("errors", 0),
                                                             "file": Path(p).name}
    return pairs


def judge(c, base, crit):
    """기준을 어긴 이유 목록 (빈 목록 = 통과). 지표 값이 없으면(판정 0건) 실패로 본다"""
    why = []

    def drop(name, label, cur, ref):
        lim = crit.get(name)
        if lim is None:
            return
        if cur is None or ref is None:
            why.append(f"{label} 값 없음 (판정 0건)")
        elif round(ref - cur, 6) > lim:  # 부동소수 오차(0.14→0.04 = 10.000000000000002)로 경계값이 실패하지 않게
            why.append(f"{label} {ref:.1f}% → {cur:.1f}% (-{lim}%p 초과)")

    drop("faithfulness_pass_drop_pp_max", "근거 충실도 통과율", c["m"]["faithfulness"]["pass"], base["m"]["faithfulness"]["pass"])
    drop("answer_relevancy_pass_drop_pp_max", "질문 적합도 통과율", c["m"]["answer_relevancy"]["pass"],
         base["m"]["answer_relevancy"]["pass"])
    drop("dont_know_drop_pp_max", "답 없는 질의 '모른다'", c["dont_know"], base["dont_know"])
    lim = crit.get("refusal_answerable_rise_pp_max")
    if lim is not None:
        if c["refusal_answerable"] is None or base["refusal_answerable"] is None:
            why.append("답 있는 질의 회피율 값 없음")
        elif round(c["refusal_answerable"] - base["refusal_answerable"], 6) > lim:
            why.append(f"답 있는 질의 회피 {base['refusal_answerable']:.1f}% → {c['refusal_answerable']:.1f}% (+{lim}%p 초과)")
    lim = crit.get("win_rate_min")
    if lim is not None and c["win"] is not None and c["win"] < lim:
        why.append(f"기준 대비 승률 {c['win']:.2f} (< {lim})")
    err_max = crit.get("judge_error_max")
    if lim is not None and c["pair"] is not None and err_max is not None and c["pair"]["errors"] > err_max:
        why.append(f"쌍별 심판 오류 {c['pair']['errors']}건 (> {err_max}) — 승률이 일부 쌍만으로 나옴")
    return why


def errors_why(c, crit):
    lim = crit.get("judge_error_max")
    return [f"심판 오류 {c['judge_errors']}건 (> {lim})"] if lim is not None and c["judge_errors"] > lim else []


def precheck(cands, crit):
    """켠 기준에 필요한 지표·쌍별 결과가 있는가"""
    for name, metric in CRIT_METRIC.items():
        if crit.get(name) is not None:
            bad = [c["answers_file"] for c in cands if metric not in c["judged"]]
            if bad:
                raise InputError(f"{name}을 켰는데 {metric}을 채점하지 않은 후보: {bad}")


def _md(head, body):
    return "\n".join(["| " + " | ".join(head) + " |", "|" + "---|" * len(head)] + ["| " + " | ".join(r) + " |" for r in body])


def _p(x):
    return "-" if x is None else f"{x:.1f}%"


def _ms(x):
    return "-" if x is None else f"{x / 1000:.1f}s"


def _metric(m):
    return "-" if m["pass"] is None else f"{m['pass']:.1f}% ({m['n']}건, 평균 {m['mean']:.2f})" if m["mean"] is not None \
        else f"{m['pass']:.1f}% ({m['n']}건)"


def table(cands):
    head = ["후보", "판", "근거 충실도", "질문 적합도", "'모른다'(답 없음)", "회피(답 있음)", "문맥 정밀도", "문맥 재현율",
            "기준 대비 승률 (승/무/패)", "심판 오류", "건당 비용", "평균 지연", "p95 지연", "판정"]
    body = []
    for c in cands:
        win = "-" if c["win"] is None else f"{c['win']:.2f} ({c['pair']['wins']}/{c['pair']['ties']}/{c['pair']['losses']})"
        cost = f"${c['usd_per_query']:.5f}" if c["priced"] else "$0 (GPU 시간 따로)"
        body.append([str(c["model"]), str(c["prompt"]), _metric(c["m"]["faithfulness"]), _metric(c["m"]["answer_relevancy"]),
                     _p(c["dont_know"]), _p(c["refusal_answerable"]), _p(c["m"]["context_precision"]["pass"]),
                     _p(c["m"]["context_recall"]["pass"]), win, str(c["judge_errors"]), cost, _ms(c["ms_mean"]),
                     _ms(c["ms_p95"]), c["verdict"]])
    return _md(head, body)


def pair_table(pairs):
    head = ["쌍 (a 대 b)", "판정 수", "a 승", "b 승", "무승부", "같은 답", "순서 불일치", "첫 자리 선택률", "다른 검색", "오류",
            "심판·지시문"]
    return _md(head, [[f"{p['a']['model']}·{p['a']['prompt']} 대 {p['b']['model']}·{p['b']['prompt']}", str(p["s"]["n"]),
                       str(p["s"]["a_wins"]), str(p["s"]["b_wins"]), str(p["s"]["ties"]), str(p["s"]["identical"]),
                       str(p["s"]["inconsistent"]),
                       "-" if p["s"]["first_position_rate"] is None else f"{p['s']['first_position_rate']:.2f}",
                       str(p["s"]["different_retrieval"]), str(p["s"]["errors"]), f"{p['judge_model']} {p['version']}"]
                      for p in pairs])


def run(cand_args, pair_paths, crit_all):
    """→ (종료 코드, 마크다운, JSON용 목록). 입력 문제는 InputError"""
    crit = crit_all.get("answer") or {}
    want = crit.get("baseline") or {}
    cands = [candidate(a, j) for a, j in cand_args]
    check_same(cands)
    base = [c for c in cands if c["model"] == want.get("model") and c["prompt"] == want.get("prompt")]
    if len(base) != 1:
        raise InputError(f"기준 후보 {want}가 {len(base)}개다 — 꼭 하나")
    base = base[0]
    precheck(cands, crit)
    pairs = attach_pairs(cands, base, pair_paths)
    if crit.get("win_rate_min") is not None:
        bad = [c["answers_file"] for c in cands if c is not base and c["win"] is None]
        if bad:
            raise InputError(f"win_rate_min을 켰는데 기준과의 쌍별 결과가 없는 후보: {bad}")
    code = 0
    for c in cands:
        why = errors_why(c, crit) + ([] if c is base else judge(c, base, crit))
        c["verdict"] = ("기준" if c is base else "통과") if not why else (("기준 — " if c is base else "실패: ") + "; ".join(why))
        code |= 1 if why else 0
    md = table(cands)
    if pairs:
        md += "\n\n쌍별 비교 (두 순서 모두 같은 쪽을 골라야 승, 아니면 무승부):\n\n" + pair_table(pairs)
    c0 = cands[0]
    md += (f"\n\n심판: {c0['judge_model']} (지시문 {c0['judge_version']}), 채점 질의 {len(c0['ids'])}건. "
           f"통과율 = P/(P+F), 근거 충실도는 답을 피한 답을 빼고 센다. 기준값: "
           + json.dumps({k: v for k, v in crit.items() if k != "baseline"}, ensure_ascii=False))
    out = [{k: c[k] for k in ("model", "prompt", "answers_file", "judge_file", "m", "dont_know", "refusal_answerable", "win",
                              "pair", "judge_errors", "priced", "usd_per_query", "ms_mean", "ms_p95", "n", "verdict")}
           for c in cands]
    return code, md, {"candidates": out, "pairs": [{"file": p["file"], "summary": p["s"]} for p in pairs]}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cand", nargs=2, action="append", required=True, metavar=("ANSWERS", "JUDGE"))
    ap.add_argument("--pair", action="append", default=[])
    ap.add_argument("--criteria", default=str(HERE / "criteria.yaml"))
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)
    crit = yaml.safe_load(Path(a.criteria).read_text(encoding="utf-8"))
    try:
        code, md, data = run(a.cand, a.pair, crit)
    except InputError as e:
        print(str(e), file=sys.stderr)
        return 2
    out = Path(a.out) if a.out else Path(a.cand[0][0]).resolve().parent / "matrix"
    if out.resolve().is_relative_to(APP_ROOT):
        print("결과는 저장소 밖에 쓴다", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    print(md)
    (out / "answer_gate.md").write_text(md + "\n", encoding="utf-8")
    (out / "answer_gate.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n결과: {out / 'answer_gate.md'} — 종료 코드 {code}")
    return code


if __name__ == "__main__":
    sys.exit(main())
