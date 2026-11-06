"""되돌려 주기 제공자 — LLM을 부르지 않고, 저장된 철 결과의 추출 값(b)을 그 모델 응답처럼 돌려준다.
채점 연결 확인용: 이 제공자로 돌린 gate.py 점수는 저장 결과에 eval/score.py를 돌린 점수와 같아야 한다 (gate.py --expect-stored)."""
import json

import common

_cache = {}


def _b(vol, doc_id):
    if vol not in _cache:
        _cache[vol] = {d["doc_id"]: d["b"] for d in common.stored_docs(vol)}
    return _cache[vol][doc_id]


def call_api(prompt, options, context):
    v = context["vars"]
    return {"output": json.dumps(_b(v["vol"], v["doc_id"]), ensure_ascii=False),
            "tokenUsage": {"prompt": 0, "completion": 0, "total": 0}, "cost": 0}
