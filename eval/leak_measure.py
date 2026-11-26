"""지시문 새기 검사(app/llm.py leaks_prompt)가 정상 답을 막는지 — 저장된 답변 파일로만 (LLM·API 호출 없음, 이 PC 안에서).

  python -I -X utf8 eval/leak_measure.py <답변 json> [<답변 json> …]
답변 파일 = eval/answer/gen_answers.py 결과(rows[].answer·raw_answer·retrieved). 답 글과 검색으로 넘긴 문서 글(retrieved의 글자 값)에서
검사가 걸리는 개수만 출력한다 (글자는 출력하지 않음). 기대값 0. https://github.com/reddol18/docflow/issues/27
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _strings(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from _strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from _strings(v)


def measure(paths):
    from app import llm  # noqa: PLC0415
    c = {"answers": 0, "answers_blocked": 0, "raw_blocked": 0, "doc_texts": 0, "doc_texts_blocked": 0}
    for p in paths:
        rows = json.loads(Path(p).read_text(encoding="utf-8")).get("rows") or []
        for r in rows:
            c["answers"] += 1
            c["answers_blocked"] += llm.leaks_prompt(r.get("answer") or "")
            c["raw_blocked"] += llm.leaks_prompt(r.get("raw_answer") or "")
            for s in _strings(r.get("retrieved")):
                c["doc_texts"] += 1
                c["doc_texts_blocked"] += llm.leaks_prompt(s)
    return c


def main(argv=None):
    paths = (argv if argv is not None else sys.argv[1:])
    if not paths:
        print(__doc__)
        return 2
    c = measure(paths)
    print(f"답변 파일 {len(paths)}개: 저장된 답 {c['answers']}건 중 막힘 {c['answers_blocked']}건 (원 답 {c['raw_blocked']}건), "
          f"넘긴 문서 글 {c['doc_texts']}개 중 걸림 {c['doc_texts_blocked']}개")
    return 0 if not (c["answers_blocked"] or c["raw_blocked"] or c["doc_texts_blocked"]) else 1


if __name__ == "__main__":
    sys.exit(main())
