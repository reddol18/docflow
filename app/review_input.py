"""사람 검토 입력 거르기. 외부 의존 없음(색인 스키마 YAML만) → CI 0층에서 바로 시험한다."""
from app import fields

# 고칠 수 있는 칸 = 스키마에서 숨김·고정값이 아닌 건 단위 칸 (김해: 문서번호·시행일자·수신·제목)
EDITABLE = fields.editable(fields.SCHEMA)


def clean_fixes(flagged, pairs):
    """검토 대기 문서의 고칠 수 있는 칸 중 실제로 바뀐 값만 남긴다. pairs = [(doc_id, 칸, 값)]
    폼 경로와 JSON 경로가 같은 거름을 쓴다 → 모르는 문서·칸은 흐름에도 리포트 집계에도 들어가지 않는다."""
    cur = {d["doc_id"]: d["fields"] for d in flagged}
    fixes = {}
    for did, k, val in pairs:
        val = str(val).strip()
        if k in EDITABLE:
            val = fields.normalize_input(fields.SCHEMA, {k: val})[k]  # LLM 값과 같은 표기 규칙
        if did in cur and k in EDITABLE and val != (cur[did].get(k) or ""):
            fixes.setdefault(did, {})[k] = val
    return fixes


def changes(flagged, fixes):
    """수정 이력용 [(doc_id, 칸, 이전 값, 새 값)]. 이전 값은 clean_fixes가 비교한 값과 같은 곳(검토 대기 문서)에서."""
    cur = {d["doc_id"]: d["fields"] for d in flagged}
    return [(did, k, cur[did].get(k), v) for did, f in fixes.items() for k, v in f.items()]
