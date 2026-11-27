"""저장된 추출 값(b)에 지금 코드의 수신 보정(layout.fix_recipient)을 다시 걸어 채점한다 — LLM·API 호출 없음
(https://github.com/reddol18/docflow/issues/1 조건 4).

  저장   저장된 철 결과의 칸 값 그대로 (eval/score.py work/platform/*.json 과 같은 숫자 — 기준선)
  재조립 저장된 b·hw로 수신을 지금 규칙으로 다시 보정하고 merge·validate (eval/promptfoo/gate.py assemble과 같은 길)
칸마다 B·최종 맞은 수와, 수신이 저장 대비 바뀐 문서 수(틀림→맞음, 맞음→틀림)만 찍는다. 문서 글자는 찍지 않는다.

실행 (doc_platform 폴더): ..\\.venv\\Scripts\\python.exe -I -X utf8 eval/recipient_replay.py
철·정답 위치는 eval/promptfoo/common.py (DOCFLOW_EVAL_WORK·DOCFLOW_EVAL_VOLS).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "promptfoo"))  # python -I 로 돌려도 하네스 모듈을 찾게
import common  # noqa: E402
import gate  # noqa: E402
from rescore import ok_field  # noqa: E402  (common이 records_poc/scripts를 경로에 넣는다)


def counts(m):
    return {name: {f: m["fields"][name][f][0] for f in gate.FIELDS} for name in ("B", "final")}


def main():
    data = {vol: common.load_vol(vol) for vol in common.vols()}
    stored = {d["doc_id"]: d for docs, _ in data.values() for d in docs}
    replayed, _ = gate.assemble(data)
    m0, m1 = gate.metrics(stored), gate.metrics(replayed)
    n = m0["n_gt"]
    print(f"철 {len(data)}개, 정답 시작 쪽 {n}건, 문서 {len(stored)}건\n")
    c0, c1 = counts(m0), counts(m1)
    print(f"{'칸':10s}{'저장 B':>10s}{'재조립 B':>10s}{'저장 최종':>10s}{'재조립 최종':>10s}")
    for f in gate.FIELDS:
        print(f"{f:10s}{c0['B'][f]:>10d}{c1['B'][f]:>10d}{c0['final'][f]:>10d}{c1['final'][f]:>10d}   (/{n})")
    for key, name in [("all_ok", "4칸 모두 정답"), ("silent", "표시 없이 틀린 문서")]:
        print(f"{name:10s}{m0['doc'][key][0]:>10d}{m1['doc'][key][0]:>10d}   (/{n}, 저장 / 재조립)")
    idx = m0["_idx"]
    up = down = 0
    for p, g in idx.items():
        if p not in stored:
            continue
        a = ok_field("recipient", stored[p]["fields"]["recipient"], g["recipient"])
        b = ok_field("recipient", replayed[p]["fields"]["recipient"], g["recipient"])
        up += (not a) and b
        down += a and not b
    print(f"\n수신 저장 대비: 틀림→맞음 {up}건, 맞음→틀림 {down}건")
    for label, keep in [("정답 문서", lambda i: i in idx), ("전체 문서", lambda i: True)]:
        fixes = {}
        for d in replayed.values():
            if keep(d["doc_id"]):
                why = d["info"].get("recipient_fix") or "(없음)"
                fixes[why] = fixes.get(why, 0) + 1
        print(f"{label}의 수신 보정 규칙: " + ", ".join(f"{k} {v}" for k, v in sorted(fixes.items())))
    # 잘못 뗀 결재란 낱말: LLM 값 끝 낱말을 뗐는데 정답의 끝 낱말도 바로 그 낱말인 문서 ('가상위생과장'의 '과장'은 아님)
    cut = wrong_cut = 0
    for p, g in idx.items():
        if p not in stored:
            continue
        val = gate.graph._llm_of(stored[p]["b"], "recipient")
        if "결재란" in (replayed[p]["info"].get("recipient_fix") or ""):
            cut += 1
            tail = val.split()[-1]
            gw = (g["recipient"] or "").split()
            wrong_cut += bool(gw) and gate.layout.norm(gw[-1]) == gate.layout.norm(tail)
    print(f"값 끝 결재란 낱말을 뗀 정답 문서 {cut}건, 그중 정답도 그 낱말로 끝남(잘못 뗌) {wrong_cut}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
