"""저장된 철 결과(/volumes/{vol}/documents JSON)의 LLM·손글씨 판독 값(b·hw)으로 merge·validate만 다시 돌린다 — LLM 호출 없음.
색인 칸을 스키마로 옮기기 전·후 코드에서 각각 돌려 같은 칸 값·표시가 나오는지, eval/score.py 점수가 같은지 본다
(https://github.com/reddol18/docflow/issues/9). 모델 비교 하네스(eval/promptfoo/gate.py --expect-stored)의 기준값도 이것.

실행 (doc_platform 폴더, 시험 환경):
  uv run --no-project --with-requirements requirements-test.txt python -I eval/schema_replay.py \
      <앱 폴더> <출력 폴더> ../work/platform/*.json
<앱 폴더>는 app/가 든 폴더 (옮기기 전 코드는 main의 git worktree). 출력은 입력과 같은 이름의 JSON.
"""
import json
import sys
from pathlib import Path


def replay(graph, docs, b_of=None, recipient_of=None):
    """한 철의 저장 결과 → 지금 코드의 merge·validate로 다시 조립한 문서 목록.

    b_of(d) → b         저장된 LLM 추출 값 d["b"] 대신 쓸 값 (모델 비교 하네스가 후보 응답을 넣는다). 없으면 저장 값
    recipient_of(d, b) → (수신, 보정 사유)  수신을 b로 다시 보정 (app/graph.py extract와 같은 layout.fix_recipient).
                        없으면 저장된 수신·보정 사유 — b를 바꿀 때는 꼭 넘긴다 (수신 보정은 b의 수신 값에서 나온다)
    """
    merged = []
    # 저장된 수신은 rule: recipient 칸에서 (기관 매핑이 칸 이름을 바꿔도 — https://github.com/reddol18/docflow/issues/72)
    by_rule = getattr(graph.fields, "by_rule", None)  # 없으면 옮기기 전 코드(앱 폴더 인자) — 그때는 김해 칸 이름
    rec_name = (by_rule(graph.SCHEMA, "recipient") or {}).get("name") if by_rule else "recipient"
    for d in docs:
        b = b_of(d) if b_of else d["b"]
        rec, why = recipient_of(d, b) if recipient_of else (d["fields"].get(rec_name, ""), d["info"]["recipient_fix"])
        s = {"vol": d["vol"], "doc_id": d["doc_id"], "pages": d["pages"], "b": b, "hw": d["hw"],
             "recipient": rec, "recipient_fix": why,
             "ratio_i": graph.HEAD_RATIOS.index(d["info"]["head_ratio"]), "box_fail": d["info"]["box_fail"]}
        merged.extend(graph.merge(s)["docs"])
    return graph.validate({"docs": merged})["docs"]


def main():
    root, out, *paths = sys.argv[1:]
    sys.path.insert(0, str(Path(root).resolve()))
    from app import graph  # noqa: PLC0415 — 앱 폴더를 받은 뒤에 불러온다

    Path(out).mkdir(parents=True, exist_ok=True)
    for p in paths:
        merged = replay(graph, json.loads(Path(p).read_text(encoding="utf-8")))
        Path(out, Path(p).name).write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{Path(p).name}: {len(merged)}건")


if __name__ == "__main__":
    main()
