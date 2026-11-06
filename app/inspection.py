"""2단계 검증 — 검사 목록 만들기 (https://github.com/reddol18/docflow/issues/10).

입력(검토·수정, reviewer)이 끝난 철에서 검사자(inspector)가 볼 문서를 고른다.
- 검토 표시가 붙은 문서: 전부 (reason=flagged)
- 표시 없는 문서: 비율 p만큼 무작위 표본, 수는 올림 ceil(b×p) — b>0·p>0이면 최소 1건 (reason=sample)
- 100% 검수 칸을 지정하면 나머지 문서도 전부 (reason=field100). 그 칸은 모든 문서에서 확인한다
한 문서에 이유가 겹치면 flagged > sample > field100 순. 철별 추정 오류율은 sample 문서만으로 낸다(k/n).
"""
import math
import random
from fractions import Fraction

REASONS = ("flagged", "sample", "field100")


def sample_size(b, rate):
    """표시 없는 문서 b건에서 비율 rate로 뽑을 수 — 올림. 0.3×10이 3.0000000000000004가 되어 4로 올라가지 않게 분수로 셈"""
    if b <= 0 or rate <= 0:
        return 0
    return min(b, math.ceil(Fraction(str(rate)) * b))


def plan(docs, rate, fields100=(), seed=0):
    """docs = 그래프 상태의 문서 목록 → ({doc_id: reason}, 요약). 같은 seed면 같은 표본"""
    flagged = sorted(d["doc_id"] for d in docs if d.get("flags"))
    rest = sorted(d["doc_id"] for d in docs if not d.get("flags"))
    n = sample_size(len(rest), rate)
    # 재현 가능한 표본이 목적(시드는 서버가 secrets로 정해 저장) — 암호용 난수가 아니다
    sample = sorted(random.Random(seed).sample(rest, n))  # noqa: S311
    rows = {d: "flagged" for d in flagged}
    rows.update({d: "sample" for d in sample})
    if fields100:
        for d in rest:
            rows.setdefault(d, "field100")
    summary = {"flagged": len(flagged), "unflagged": len(rest), "sample": n, "rate": rate,
               "fields100": list(fields100), "seed": seed}
    return rows, summary
