"""데이터셋 — 저장된 철 결과의 문서마다 시험 하나 (vars: vol, doc_id). OCR 글자는 지시문 함수가 읽는다.
정답이 있는 문서(정답 시작 쪽)만이 아니라 철의 모든 문서를 넣는다: 검토 표시가 앞뒤 문서를 함께 보기 때문 (app/flags.py)."""
import common


def generate_tests(config=None):
    return [{"description": d["doc_id"], "vars": {"vol": vol, "doc_id": d["doc_id"]}}
            for vol in common.vols() for d in common.stored_docs(vol)]
