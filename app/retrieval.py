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
# 주소 번지('외동 1234-5번지')·전화('전화 0553-1234')는 문서번호로 보지 않는다
DOCNO_RX = re.compile(r"([가-힣]{1,4})\s*(?<![\d-])(\d{4,5})\s*-\s*(\d{1,5})(?![\d-])(?!\s*(?:번지|번길|호))")
NOT_DOCNO_HEAD = ("전화", "전송", "팩스", "번지", "우편")
# 문서번호 칸 값의 번호 부분 (부서 약칭 없이 번호만 남은 값도)
NUMBER_RX = re.compile(r"(?<![\d-])(\d{4,5})\s*-\s*(\d{1,5})(?![\d])")
YEAR_RX = re.compile(r"\d{4}년?")
RRF_K = 60


def query_words(q):
    """낱말 검색에 넣을 낱말: 두 글자 이상, 연도(1997·1997년)는 뺀다"""
    return [w for w in re.split(r"\s+", q) if len(w) >= 2 and not YEAR_RX.fullmatch(w)]


@dataclass(frozen=True)
class Config:
    """검색 설정 (https://github.com/reddol18/docflow/issues/61 6단계에서 합성 세트로 고른다).
    clean      질의 다듬기: 괄호·문장부호 떼기, 낱말 끝 조사 떼기, 질문 틀 낱말(누구인가·문서번호 …) 빼기, 월·일 빼기
    year       질의 안 연도('1995년' 하나)를 연도 칸으로 (화면 연도 칸이 비었을 때만)
    kw_weight  RRF에서 낱말 순위의 가중 (1 = 예전과 같음, 0 = 낱말 순위를 합치지 않고 벡터만 — 문서번호 고정은 그대로)"""
    clean: bool = False
    year: bool = False
    kw_weight: float = 1.0

    @property
    def name(self):
        return f"{'clean_' if self.clean else ''}{'year_' if self.year else ''}kw{self.kw_weight:g}"


PUNCT_RX = re.compile(r"[()\[\]{}<>「」『』\"'“”‘’?!.,·:;~]")
# 낱말 끝 조사 (긴 것 먼저). 떼고 두 글자 이상 남을 때만 — 낱말 검색은 부분 일치라 덜 떼어도 맞고 더 떼도 맞는다
PARTICLES = ("에서는", "에서", "으로", "에게", "께서", "부터", "까지", "에는", "의", "에", "을", "를", "이", "가", "은", "는", "로",
             "와", "과", "도")
# 질문 틀 낱말 (조사 뗀 뒤) — 어느 문서의 제목·수신·문서번호에도 뜻 없이 걸리거나 아무 데도 안 걸린다
STOPWORDS = frozenset({"문서", "문서번호", "무엇인가", "누구인가", "언제", "어디인가", "어느", "각각", "모두", "찾아", "알려",
                       "요약해", "시행되었나", "냈나", "관련", "신청인", "대표자", "수신자", "처리결과", "위치", "주소", "시행일자",
                       "내용", "건이고", "있나", "되었나", "처리되었나", "어떻게"})
DAY_RX = re.compile(r"\d{1,2}(월|일)")
QUERY_YEAR_RX = re.compile(r"(?<!\d)((?:19|20)\d{2})년")


NO_STRIP = ("허가", "인가", "평가", "추가", "참가", "증가")  # '가'로 끝나는 낱말 (조사 아님)


def strip_particle(w):
    if w.endswith(NO_STRIP):
        return w
    for p in PARTICLES:
        if w.endswith(p) and len(w) - len(p) >= 2:
            return w[:-len(p)]
    return w


def clean_words(q):
    """다듬은 낱말 (Config.clean): 괄호·문장부호 → 공백, 연도·월·일 빼기, 조사 떼기, 질문 틀 낱말 빼기, 같은 낱말 한 번.
    문서번호(번호 부분의 '-')는 그대로 둔다"""
    out = []
    for w in PUNCT_RX.sub(" ", q or "").split():
        w = strip_particle(w)
        if YEAR_RX.fullmatch(w) or DAY_RX.fullmatch(w):
            continue
        if len(w) >= 2 and w not in STOPWORDS and w not in out:
            out.append(w)
    return out


def query_terms(q, cfg=None):
    """낱말 검색에 넣을 낱말 — 설정에 따라 query_words 또는 clean_words"""
    return clean_words(q) if (cfg or SEARCH).clean else query_words(q)


def query_year(q, cfg=None):
    """질의 안 연도 (Config.year일 때, 'NNNN년'이 한 가지일 때만). 아니면 None"""
    if not (cfg or SEARCH).year:
        return None
    ys = set(QUERY_YEAR_RX.findall(q or ""))
    return int(ys.pop()) if len(ys) == 1 else None


def key_of(r):
    """문서 키 (철, doc_id) — 검색 행(dict) 또는 이미 키(tuple)인 것 모두 받는다
    (같은 쪽 이름이 다른 철에도 있을 수 있다, https://github.com/reddol18/docflow/issues/50)"""
    return (r["vol"], r["doc_id"]) if isinstance(r, dict) else tuple(r)


def rrf(*lists, weights=None):
    """Reciprocal Rank Fusion 점수 {(vol, doc_id): Σ w/(60+순위)} (순위는 1부터, 가중 w 기본 1).
    가중이 0인 목록은 합치지 않는다 (그 목록에만 있는 문서도 넣지 않는다)"""
    score = {}
    for i, lst in enumerate(lists):
        w = 1 if weights is None else weights[i]
        if not w:
            continue
        for rank, r in enumerate(lst):
            k = key_of(r)
            score[k] = score.get(k, 0) + w / (RRF_K + rank + 1)
    return score


def docnos_in(q):
    """질의 안 문서번호 [(부서 약칭, 번호 'NNNNN-N')] — 질의에 쓴 순서, 같은 것은 한 번"""
    out = []
    for m in DOCNO_RX.finditer(q or ""):
        if m.group(1).endswith(NOT_DOCNO_HEAD):
            continue
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
        # 질의 머리는 앞 글자가 붙어 길게 잡힐 수 있다('문서번호지경' → '번호지경') — 칸 값 약칭이 그 끝과 같으면 같은 부서
        exact = [c for c in same if c[3] and head.endswith(c[3])]
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
    year: int | None = None  # 걸러 쓴 연도 (화면 연도 칸 또는 질의 연도)

    @property
    def docno_miss(self):
        """문서번호 질의인데 그 번호의 문서가 없다 — /search는 '관련 문서 없음'(빈 목록)"""
        return bool(self.docnos) and not self.pinned


def fuse(vector, keyword, pinned=(), kw_weight=1.0):
    """(RRF 점수, 순서). 순서 = pinned(문서번호 정확 일치, 그 순서) → 나머지 RRF 내림차순 (같은 점수는 먼저 나온 후보 순 —
    예전 main.ranked와 같다). kw_weight = 낱말 순위의 가중 (0이면 벡터만)"""
    score = rrf(vector, keyword, weights=(1.0, kw_weight))
    pinned = [key_of(k) for k in pinned]
    for k in pinned:
        score.setdefault(k, 0.0)
    rest = [k for k in sorted(score, key=score.get, reverse=True) if k not in set(pinned)]
    return score, pinned + rest


def rank(q, vector, keyword, docno_candidates=(), cfg=None, year=None, docno=True):
    """후보 → Ranking. docno_candidates = [(키, 문서번호 칸 값)] (질의에 문서번호가 없으면 비워도 된다).
    후보(vector·keyword)는 부르는 쪽이 query_terms·query_year로 가져온 것.
    docno=False(스키마에 문서번호 칸이 없음)면 문서번호 고정도 '없음'도 하지 않는다"""
    cfg = cfg or SEARCH
    docnos = docnos_in(q) if docno else []
    pinned = docno_matches(q, docno_candidates) if docnos else []
    score, order = fuse(vector, keyword, pinned, cfg.kw_weight)
    return Ranking(vector, keyword, score, order, pinned, docnos, year)


def no_match(ranking, top_sim=None, threshold=None):
    """'관련 문서 없음'으로 볼지 (/search만 — /ask는 그대로, 2026-11-24 결정).
    문서번호 질의인데 그 번호의 문서가 없거나, 후보가 하나도 없거나(화면 연도 칸으로 걸러 비었을 때 — 질의 연도로 비면
    부르는 쪽이 연도 없이 다시 찾는다), threshold(벡터 1위 유사도 하한)를
    주면 top_sim < threshold일 때. 문서번호가 맞은 질의는 '없음'이 아니다"""
    if ranking.docno_miss or not ranking.order:
        return True
    if ranking.pinned or threshold is None or top_sim is None:
        return False
    return top_sim < threshold


# 운영 검색 설정 (https://github.com/reddol18/docflow/issues/61 6단계, 합성 세트 A·B로 고름): 질의 연도 → 연도 칸, 낱말 가중 0.5,
# 질의 다듬기는 끔. text-embedding-3-small 기준 A 근거 모두 상위5 20 → 28/32, B 주제·바꿔 말하기·조건 R@5는 그대로.
# 다듬기(clean)는 모델·글마다 들쭉날쭉(±)이고, 가중 0(벡터만)은 A가 더 오르지만 B 바꿔 말하기 R@5가 떨어진다
SEARCH = Config(clean=False, year=True, kw_weight=0.5)
# 벡터 1위 유사도 '없음' 하한 {임베딩 모델: 값} — 모델마다 유사도 분포가 달라 모델별로. 없는 모델은 임계값을 쓰지 않는다
# (7단계, 합성 세트 A·B 62질의, 운영 설정 SEARCH·문서 글 current). 보수적으로: 답 있는 질의를 하나도 '없음'으로 보지 않는
# 가장 큰 값 (eval/search_synth.py --pool). 김해 실제 문서로는 재지 않은 값 — 운영 기본은 끔(2026-11-24 결정),
# SEARCH_NO_MATCH_THRESHOLD=auto로 켠다 (app/main.no_match_threshold)
#   text-embedding-3-small 0.34: 합성 답 없음 12건 중 이 값으로 더 잡는 것 없음 (문서번호 규칙으로 2건). 1위 유사도가
#                          답 있음·없음에 겹쳐 있다 (답 있는 바꿔 말하기 질의 0.343 < 답 없는 질의 0.39~0.62)
#   bge-m3 0.578: 답 없음 9/12, 잘못 '없음' 0 (하나 빼고 고르기 9/12, 잘못 1)
NO_MATCH_THRESHOLD = {"text-embedding-3-small": 0.34, "bge-m3": 0.578}


def threshold_for(model):
    return NO_MATCH_THRESHOLD.get(model)
