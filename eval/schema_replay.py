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


def replay(graph, docs):
    """한 철의 저장 결과 → 지금 코드의 merge·validate로 다시 조립한 문서 목록"""
    merged = []
    for d in docs:
        s = {"vol": d["vol"], "doc_id": d["doc_id"], "pages": d["pages"], "b": d["b"], "hw": d["hw"],
             "recipient": d["fields"]["recipient"], "recipient_fix": d["info"]["recipient_fix"],
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
