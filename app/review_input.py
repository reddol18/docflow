"""사람 검토 입력 거르기. 외부 의존 없음 → CI 0층에서 바로 시험한다."""

EDITABLE = ("docno", "date", "recipient", "title")


def clean_fixes(flagged, pairs):
    """검토 대기 문서의 고칠 수 있는 칸 중 실제로 바뀐 값만 남긴다. pairs = [(doc_id, 칸, 값)]
    폼 경로와 JSON 경로가 같은 거름을 쓴다 → 모르는 문서·칸은 흐름에도 리포트 집계에도 들어가지 않는다."""
    cur = {d["doc_id"]: d["fields"] for d in flagged}
    fixes = {}
    for did, k, val in pairs:
        val = str(val).strip()
        if did in cur and k in EDITABLE and val != (cur[did].get(k) or ""):
            fixes.setdefault(did, {})[k] = val
    return fixes
