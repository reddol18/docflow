"""검색 순위 계산 — /search·/ask(app/main.py)와 합성 평가(eval/embed_compare.py·eval/search_synth.py·
eval/answer/gen_answers.py)가 같이 쓴다 (https://github.com/reddol18/docflow/issues/61).

DB·Qdrant·LLM을 부르지 않는 순수 함수만 둔다. 후보(벡터·낱말·문서번호 칸 값)는 부르는 쪽이 가져온다:
운영은 app/store.py(Postgres·Qdrant), 평가는 메모리 안 합성 문서.

  순위 = 문서번호 정확 일치(고정, 질의에 쓴 순서) → 나머지는 RRF(벡터 30 + 낱말 30) 순
  문서번호 일치 = 질의 안 문서번호(문장 안에 있어도)의 번호 부분(5자리 분류번호-일련번호)이 문서번호 칸 값의 번호 부분과
                 글자 그대로 같다 ('…-804'는 '…-8040'과 다르다). 같은 번호가 여러 부서에 있으면 부서 약칭까지 같은 것만,
                 부서까지 같은 문서가 없으면 번호만 같은 문서 (부서 약칭 판독이 틀린 경우).
"""
import re
from dataclasses import dataclass, field

# 문서번호 꼴: 부서 약칭(한글 1~4자) + 분류번호(4~5자리) + '-' + 일련번호(1~5자리). 문장 안에 있어도 잡는다.
# 앞뒤가 숫자·'-'로 이어지면(더 긴 번호의 일부) 문서번호로 보지 않는다
DOCNO_RX = re.compile(r"([가-힣]{1,4})\s*(?<![\d-])(\d{4,5})\s*-\s*(\d{1,5})(?![\d-])")
# 문서번호 칸 값의 번호 부분 (부서 약칭 없이 번호만 남은 값도)
NUMBER_RX = re.compile(r"(?<![\d-])(\d{4,5})\s*-\s*(\d{1,5})(?![\d])")
YEAR_RX = re.compile(r"\d{4}년?")
RRF_K = 60


def query_words(q):
    """낱말 검색에 넣을 낱말: 두 글자 이상, 연도(1997·1997년)는 뺀다"""
    return [w for w in re.split(r"\s+", q) if len(w) >= 2 and not YEAR_RX.fullmatch(w)]


def key_of(r):
    """문서 키 (철, doc_id) — 검색 행(dict) 또는 이미 키(tuple)인 것 모두 받는다
    (같은 쪽 이름이 다른 철에도 있을 수 있다, https://github.com/reddol18/docflow/issues/50)"""
    return (r["vol"], r["doc_id"]) if isinstance(r, dict) else tuple(r)


def rrf(*lists):
    """Reciprocal Rank Fusion 점수 {(vol, doc_id): Σ 1/(60+순위)} (순위는 1부터)"""
    score = {}
    for lst in lists:
        for rank, r in enumerate(lst):
            k = key_of(r)
            score[k] = score.get(k, 0) + 1 / (RRF_K + rank + 1)
    return score


def docnos_in(q):
    """질의 안 문서번호 [(부서 약칭, 번호 'NNNNN-N')] — 질의에 쓴 순서, 같은 것은 한 번"""
    out = []
    for m in DOCNO_RX.finditer(q or ""):
        d = (m.group(1), f"{m.group(2)}-{m.group(3)}")
        if d not in out:
            out.append(d)
    return out


def docno_number(value):
    """문서번호 칸 값의 번호 부분 'NNNNN-N' (공백 무시). 없으면 ''"""
    m = NUMBER_RX.search(value or "")
    return f"{m.group(1)}-{m.group(2)}" if m else ""


def docno_head(value):
    """문서번호 칸 값의 부서 약칭 (번호 앞 한글, 공백 무시). 없으면 ''"""
    m = re.search(r"([가-힣]{1,4})\s*(?=\d{4,5}\s*-)", value or "")
    return m.group(1) if m else ""


def docno_field(schema):
    """스키마에서 문서번호 칸 이름 (rule: docno). 없으면 None — 그 스키마는 문서번호 고정을 하지 않는다"""
    for f in schema.get("fields", []):
        if f.get("rule") == "docno":
            return f["name"]
    return None


def docno_matches(q, candidates):
    """질의의 문서번호와 번호가 정확히 같은 문서 키 — candidates = [(키, 문서번호 칸 값)] (부르는 쪽이 번호로 좁혀 온 것).
    질의 문서번호마다: 부서 약칭까지 같은 문서가 있으면 그것만, 없으면 번호만 같은 문서. 질의에 쓴 순서, 같은 번호 안은
    candidates 순서"""
    out = []
    cands = [(key_of(k), v, docno_number(v), docno_head(v)) for k, v in candidates]
    for head, num in docnos_in(q):
        same = [c for c in cands if c[2] == num]
        exact = [c for c in same if c[3] == head]
        for c in exact or same:
            if c[0] not in out:
                out.append(c[0])
    return out


@dataclass
class Ranking:
    """한 질의의 순위 — vector·keyword는 후보 그대로(행 또는 키), order가 화면 순서"""
    vector: list
    keyword: list
    score: dict  # RRF 점수 (고정 문서도 들어 있다 — 후보에 없으면 0)
    order: list  # 문서번호 고정 → RRF 순 키
    pinned: list = field(default_factory=list)  # 문서번호 정확 일치로 앞에 둔 키
    docnos: list = field(default_factory=list)  # 질의 안 문서번호

    @property
    def docno_miss(self):
        """문서번호 질의인데 그 번호의 문서가 없다 — /search는 '관련 문서 없음'(빈 목록)"""
        return bool(self.docnos) and not self.pinned


def fuse(vector, keyword, pinned=()):
    """(RRF 점수, 순서). 순서 = pinned(문서번호 정확 일치, 그 순서) → 나머지 RRF 내림차순 (같은 점수는 먼저 나온 후보 순 —
    예전 main.ranked와 같다)"""
    score = rrf(vector, keyword)
    pinned = [key_of(k) for k in pinned]
    for k in pinned:
        score.setdefault(k, 0.0)
    rest = [k for k in sorted(score, key=score.get, reverse=True) if k not in set(pinned)]
    return score, pinned + rest


def rank(q, vector, keyword, docno_candidates=()):
    """후보 → Ranking. docno_candidates = [(키, 문서번호 칸 값)] (질의에 문서번호가 없으면 비워도 된다)"""
    docnos = docnos_in(q)
    pinned = docno_matches(q, docno_candidates) if docnos else []
    score, order = fuse(vector, keyword, pinned)
    return Ranking(vector, keyword, score, order, pinned, docnos)


def no_match(ranking, top_sim=None, threshold=None):
    """'관련 문서 없음'으로 볼지 (/search만 — /ask는 그대로, 2026-11-24 결정).
    지금: 문서번호 질의인데 그 번호의 문서가 없을 때만. threshold(벡터 1위 유사도 하한)는 모델을 정한 뒤 정한다
    (https://github.com/reddol18/docflow/issues/61 7단계) — 주면 top_sim < threshold도 '없음'. 문서번호가 맞은 질의는 '없음'이 아니다"""
    if ranking.docno_miss:
        return True
    if ranking.pinned or threshold is None or top_sim is None:
        return False
    return top_sim < threshold
