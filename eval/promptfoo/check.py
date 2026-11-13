"""문서마다 붙는 채점 (promptfoo 비교 화면용).
  통과 = 응답이 JSON 객체 (app/fields.validate_extract — 운영 app/llm.extract와 같은 판정,
         https://github.com/reddol18/docflow/issues/27). 모양이 틀린 칸은 운영처럼 빈 값(추출 실패)으로 채점하고 이유에 칸 이름을 적는다
  칸 점수(namedScores) = 정답 시작 쪽이면 칸마다 B 값(LLM 추출 + 수신 보정, 손글씨 판독 전)이 정답과 맞는지
                         — eval/score.py의 B 열과 같은 정의·같은 규칙 (수신은 layout.fix_recipient를 거친 값)
철 단위 점수(최종 정답률·검토 표시 재현율·표시 없이 틀린 비율)와 통과 기준은 gate.py가 낸다.
promptfoo가 문서마다 Python을 새로 띄우므로 openai·DB·그래프 모듈은 불러오지 않는다 (app.fields·app.layout만)."""
import common
from prompts import SCHEMA
from rescore import load_gt, ok_field
from score import FIELDS

from app import fields, layout

# 수신 보정의 재료 = rule: recipient 칸의 LLM 값 (app/graph.py _llm_of와 같은 찾기, graph는 무거워 불러오지 않는다)
RECIPIENT = next((f["name"] for f in SCHEMA["fields"] if f.get("rule") == "recipient"), "recipient")


def b_values(b, vol, doc_id):
    """eval/score.py b_val과 같은 값: 칸은 LLM 추출 값, 수신만 보정 뒤 값"""
    vals = {f: (b.get(f) or {}).get("value") or "" for f in FIELDS}
    if "recipient" in vals:
        vals["recipient"], _ = layout.fix_recipient(common.ocr(vol, doc_id), (b.get(RECIPIENT) or {}).get("value") or "")
    return vals


def get_assert(output, context):
    try:
        b = fields.validate_extract(SCHEMA, output)
    except (ValueError, TypeError) as e:  # fields.ExtractInvalid는 ValueError
        return {"pass": False, "score": 0, "reason": f"JSON 객체 아님: {e}"}
    invalid = f" (추출 실패 칸: {', '.join(b.failed)})" if b.failed else ""
    v = context["vars"]
    g = load_gt()[1].get(v["doc_id"])
    if g is None:
        return {"pass": True, "score": 1, "reason": "정답 없는 문서 (형식만 확인)" + invalid}
    got = b_values(b, v["vol"], v["doc_id"])
    named = {f: float(ok_field(f, got[f], g[f])) for f in FIELDS}
    bad = [f for f, ok in named.items() if not ok]
    return {"pass": True, "score": sum(named.values()) / len(named), "namedScores": named,
            "reason": ("추출 칸 모두 맞음" if not bad else f"틀린 칸: {', '.join(bad)}") + invalid}
