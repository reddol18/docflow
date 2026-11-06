"""문서마다 붙는 채점 (promptfoo 비교 화면용).
  통과 = 응답이 JSON이고 app/fields.parse_extract로 읽힘 (운영 app/llm.extract와 같은 해석)
  칸 점수(namedScores) = 정답 시작 쪽이면 칸마다 추출 값(B, 손글씨 판독·수신 보정 전)이 정답과 맞는지 (eval/score.py와 같은 규칙)
철 단위 점수(최종 정답률·검토 표시 재현율·표시 없이 틀린 비율)와 통과 기준은 gate.py가 낸다.
promptfoo가 문서마다 Python을 새로 띄우므로 openai·DB 모듈은 불러오지 않는다."""
import common  # noqa: F401 — 경로
from prompts import SCHEMA
from rescore import load_gt, ok_field
from score import FIELDS

from app import fields


def get_assert(output, context):
    try:
        b = fields.parse_extract(SCHEMA, output)
    except (ValueError, TypeError) as e:
        return {"pass": False, "score": 0, "reason": f"JSON 아님: {e}"}
    g = load_gt()[1].get(context["vars"]["doc_id"])
    if g is None:
        return {"pass": True, "score": 1, "reason": "정답 없는 문서 (형식만 확인)"}
    named = {f: float(ok_field(f, b[f]["value"], g[f])) for f in FIELDS}
    bad = [f for f, ok in named.items() if not ok]
    return {"pass": True, "score": sum(named.values()) / len(named), "namedScores": named,
            "reason": "추출 칸 모두 맞음" if not bad else f"틀린 칸: {', '.join(bad)}"}
