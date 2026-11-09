"""LLM 호출: 색인 칸 추출(텍스트, 칸은 app/fields.py 스키마), 손글씨 칸 판독(비전), 임베딩, 검색 답변.

제공자는 환경변수 LLM_PROVIDER로 고른다 (https://github.com/reddol18/docflow/issues/13):
  openai  (기본값) OpenAI API. 키는 OPENAI_KEY_FILE 파일 또는 OPENAI_API_KEY
  local   현장 내부 서버 전용 — OpenAI 호환 API를 내는 Ollama, vLLM 등. LLM_BASE_URL 필수 (예: http://ollama:11434/v1).
          호스트는 루프백·사설 IP(10/8, 172.16/12, 192.168/16)·클러스터 내부 이름(점 없는 이름, .svc, .cluster.local)만,
          스킴은 http/https만 — 아니면 시작할 때 멈춘다. 키는 LLM_API_KEY_FILE(주면 파일이 있어야 함), 없으면 키 없이.
          모델 기본값이 없다 — 쓰는 기능의 모델 이름이 비면 시작할 때 멈춘다 (graph가 require_models로 확인)
  off     LLM·임베딩을 부르지 않는다. 부르면 LLMOff — graph는 그 단계를 건너뛰고 검토 표시로, API는 503으로 (main)
기능별 모델: EXTRACT_MODEL(4칸 추출)·CELL_MODEL(손글씨 칸 판독)·EMBED_MODEL(임베딩)·CHAT_MODEL(검색 답변).
임베딩 차원: Qdrant 모음은 1536차원 고정(app/store.py init). local 임베딩 모델의 차원이 다르면 넣을 때 Qdrant가 거절해
색인이 실패한다(조용히 섞이지 않음). 로컬 임베딩 교체는 #13 완료 조건 4에서 다룬다.

모든 호출은 store.usage 표에 토큰·비용을 남긴다 (local은 0원).
비용 한도 (https://github.com/reddol18/docflow/issues/7) — openai 제공자만. local(내부 주소만)은 돈이 들지 않아 보지 않는다:
  예산 둘: index(색인·수정 — 추출·판독·임베딩) DAILY_BUDGET_USD, query(검색·질문 — 질의 임베딩·답변) DAILY_QUERY_BUDGET_USD.
          검색·질문이 그날 예산을 다 써도 색인·수정은 멈추지 않는다. 날짜는 KST
  예약:   부르기 전에 예상 비용의 상한(입력 토큰 ≤ UTF-8 바이트 수 + 형식 토큰, 이미지는 모델별 최대 토큰,
          출력은 max_tokens)을 Postgres 행 잠금으로 예약한다(store.reserve_budget). 정산액 + 예약 + 이번 예상이 한도를 넘으면
          부르지 않고 BudgetExceeded. 동시 호출이 몇 개든 정산 합계가 한도를 넘지 않는다 (실제 ≤ 예상이므로)
  정산:   끝나면 실제 토큰으로 계산한 비용으로 바꾼다. 응답에 usage가 없으면 예상 비용 그대로 (모자라게 세지 않게).
          호출이 실패하면 예약만 지운다. 정산 쓰기가 실패하거나 호출 중 프로세스가 죽으면 예약이 남아,
          store.HOLD_TTL 뒤 예상 비용 그대로 정산된다 (보수 정산, usage kind '<종류>:unsettled')
  가격:   openai 모델은 PRICE에 있어야 한다 — 없으면 시작할 때(load_provider) 멈추고, 부를 때도 거부한다(UnpricedModel)
429·5xx·시간 초과는 SDK가 지수 백오프로 재시도한다 (max_retries).
"""
import base64
import ipaddress
import math
import os
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from openai import OpenAI

from app import errors, fields, store

PROVIDERS = ("openai", "local", "off")
MODEL_ENV = {"extract": "EXTRACT_MODEL", "cell": "CELL_MODEL", "embed": "EMBED_MODEL", "chat": "CHAT_MODEL"}
OPENAI_MODELS = {
    # 4칸 추출: 1994-0001 철 정답 22건 비교에서 gpt-4.1-mini + OCR 오독 교정 지시문이 제목 18/22 (gpt-4o-mini 11~14/22),
    # gpt-4.1은 19/22지만 비용 5배. eval/extract_variants.py
    "extract": "gpt-4.1-mini",
    # 손글씨 칸: 1997-0002 철 20칸 비교에서 gpt-4.1-mini(high)가 일련번호 7/10·일자 8/10으로 가장 잘 읽고 가장 쌌다
    # (gpt-4o-mini 4~5/10·6/10, gpt-4o 5/10·7/10, gpt-4.1 7/10·7/10). eval/cell_variants.py
    "cell": "gpt-4.1-mini",
    "embed": "text-embedding-3-small",
    "chat": "gpt-4o-mini",  # 검색 답변
}
# USD / 1M 토큰 (입력, 출력)
PRICE = {"gpt-4o-mini": (0.15, 0.60), "gpt-4.1-mini": (0.40, 1.60), "text-embedding-3-small": (0.02, 0.0)}
BUDGET = float(os.environ.get("DAILY_BUDGET_USD", "3.0"))  # 색인·수정 (index)
QUERY_BUDGET = float(os.environ.get("DAILY_QUERY_BUDGET_USD", "0.6"))  # 검색·질문 (query) — 색인 예산과 따로
LIMITS = {"index": BUDGET, "query": QUERY_BUDGET}
# 출력 상한 (비용·폭주 응답 막기, https://github.com/reddol18/docflow/issues/7)
# 추출: 운영 모델 gpt-4.1-mini 응답 길이 실측의 2배 근처. 2026-11-07 모델 비교 결과(work/promptfoo/results_full_20261107.json,
#   지금 지시문으로 900건)의 completion 토큰: 중앙값 100, p90 110, 최대 138 → 256 (최대의 1.9배).
#   잘리면(finish_reason=length) JSON이 깨지므로 빈 값(확신도 0 → 검토 표시)으로 두고 서버 로그에 남긴다.
#   모델 비교 하네스(eval/promptfoo)는 후보 모델(로컬 최대 431토큰)까지 재므로 512 그대로
EXTRACT_MAX_TOKENS = 256
# 답변: 지시문이 "짧게 답한다", 근거는 상위 5건. 통제자 결정 800 (2026-11-10). 잘리면 서버 로그에 남긴다
ANSWER_MAX_TOKENS = 800
CELL_MAX_TOKENS = 40  # 손글씨 칸 한 줄
# 예약용 입력 토큰 상한: BPE 토큰은 1바이트 이상이라 토큰 수 ≤ UTF-8 바이트 수. 메시지 형식 토큰은 넉넉히 더한다
CALL_TOKENS = 16
MESSAGE_TOKENS = 8
# 이미지 한 장(detail high)의 입력 토큰 상한 — OpenAI 이미지 토큰 계산 규칙의 최댓값
#   gpt-4.1-mini: 32px 조각 최대 1536개 × 1.62 = 2489, gpt-4o-mini: 기본 2833 + 512px 타일(768×2048 → 최대 8개) × 5667
IMAGE_TOKENS = {"gpt-4.1-mini": 2500, "gpt-4o-mini": 2833 + 8 * 5667}
INTERNAL_NETS = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]


class BudgetExceeded(RuntimeError):
    pass


class LLMOff(RuntimeError):
    """LLM_PROVIDER=off인데 LLM·임베딩을 부름"""


class ProviderConfigError(ValueError):
    pass


class UnpricedModel(ProviderConfigError):
    """openai 제공자인데 PRICE에 없는 모델 — 비용을 셀 수 없어 부르지 않는다"""


@dataclass(frozen=True)
class Provider:
    name: str  # openai | local | off
    base_url: str | None = None
    models: dict = field(default_factory=dict)  # 기능(extract·cell·embed·chat) → 모델 이름
    key_file: str | None = None  # local 서버 키 파일 (없으면 키 없이)

    @property
    def on(self):
        return self.name != "off"

    def model(self, kind):
        if not self.on:
            raise LLMOff(f"LLM_PROVIDER=off — {kind} 호출 안 함")
        m = self.models.get(kind)
        if not m:
            raise ProviderConfigError(f"LLM_PROVIDER={self.name}: {MODEL_ENV[kind]} 필요")
        return m


def internal_host(host):
    """현장 내부 주소인가: 루프백·사설 IP(10/8, 172.16/12, 192.168/16)·클러스터 내부 이름(점 없는 이름, .svc, .cluster.local)"""
    host = (host or "").lower().rstrip(".")
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host or host.endswith((".svc", ".cluster.local"))
    return ip.is_loopback or any(ip in n for n in INTERNAL_NETS)


def check_local_url(url):
    """LLM_BASE_URL 검사: http/https + 내부 호스트만. 아니면 ProviderConfigError"""
    try:
        u = urlsplit(url)
        host = u.hostname
        _ = u.port  # 포트가 숫자가 아니면 ValueError
    except ValueError as e:
        raise ProviderConfigError(f"LLM_BASE_URL을 읽을 수 없음: {e}") from None
    if u.scheme not in ("http", "https"):
        raise ProviderConfigError(f"LLM_BASE_URL 스킴은 http/https만 (받은 값: {u.scheme or '없음'})")
    if not internal_host(host):
        raise ProviderConfigError(f"LLM_PROVIDER=local은 현장 내부 서버 전용 — 호스트 {host!r}는 "
                                  "루프백·사설 IP·클러스터 내부 이름이 아님")


def load_provider(env=None):
    """환경변수 → Provider. 모르는 제공자, local인데 주소 없음·내부 주소 아님·키 파일 없음은 시작할 때 멈춘다"""
    env = os.environ if env is None else env
    name = (env.get("LLM_PROVIDER") or "openai").strip().lower()
    if name not in PROVIDERS:
        raise ProviderConfigError(f"LLM_PROVIDER={name!r} — 가능: {', '.join(PROVIDERS)}")
    if name == "off":
        return Provider("off")
    if name == "openai":
        models = {k: (env.get(e) or "").strip() or OPENAI_MODELS[k] for k, e in MODEL_ENV.items()}
        unpriced = sorted(f"{MODEL_ENV[k]}={m}" for k, m in models.items() if m not in PRICE)
        if unpriced:
            raise UnpricedModel(f"가격표(app/llm.py PRICE)에 없는 모델: {', '.join(unpriced)} — 비용을 셀 수 없어 쓰지 않는다")
        return Provider("openai", models=models)
    base = (env.get("LLM_BASE_URL") or "").strip()
    if not base:
        raise ProviderConfigError("LLM_PROVIDER=local이면 LLM_BASE_URL 필요 (예: http://127.0.0.1:11434/v1)")
    check_local_url(base)
    key_file = (env.get("LLM_API_KEY_FILE") or "").strip() or None
    if key_file and not Path(key_file).is_file():
        raise ProviderConfigError("LLM_API_KEY_FILE을 줬는데 파일이 없음")
    return Provider("local", base, {k: (env.get(e) or "").strip() or None for k, e in MODEL_ENV.items()}, key_file)


PROVIDER = load_provider()


def enabled():
    return PROVIDER.on


def require_models(kinds):
    """local 제공자에서 쓰는 기능(kinds)의 모델 이름이 다 있는지 — 시작할 때(graph import) 확인한다"""
    if PROVIDER.name != "local":
        return
    missing = [MODEL_ENV[k] for k in MODEL_ENV if k in kinds and not PROVIDER.models.get(k)]
    if missing:
        raise ProviderConfigError(f"LLM_PROVIDER=local: 모델 이름 필요 — {', '.join(missing)}")


def _key():
    p = Path(os.environ.get("OPENAI_KEY_FILE", "/etc/openai/api-key"))
    return p.read_text().strip() if p.exists() else os.environ["OPENAI_API_KEY"]


_client = None


def make_client(p):
    """제공자(openai·local) → OpenAI SDK 클라이언트 (local도 OpenAI 호환 API라 같은 SDK, 주소만 다름).
    local 키가 없으면 자리 채움 값 (SDK가 빈 키를 받지 않는다)"""
    if p.name == "local":
        key = Path(p.key_file).read_text().strip() if p.key_file else "unused"
        return OpenAI(api_key=key, base_url=p.base_url, timeout=120, max_retries=2)
    return OpenAI(api_key=_key(), timeout=60, max_retries=4)


def client():
    global _client
    if not PROVIDER.on:  # off면 클라이언트를 만들지 않는다
        raise LLMOff("LLM_PROVIDER=off")
    if _client is None:
        _client = make_client(PROVIDER)
    return _client


def tokens_max(*texts, messages=1):
    """입력 토큰 상한: UTF-8 바이트 수 + 형식 토큰"""
    return sum(len(t.encode("utf-8")) for t in texts) + CALL_TOKENS + MESSAGE_TOKENS * messages


def price_of(model):
    p = PRICE.get(model)
    if p is None:
        raise UnpricedModel(f"가격표(PRICE)에 없는 모델 {model!r} — 부르지 않음")
    return p


def estimate_usd(model, tin_max, tout_max):
    """예약할 비용 상한 (USD)"""
    pi, po = price_of(model)
    return (tin_max * pi + tout_max * po) / 1e6


class _Meter:
    """호출 하나의 사용량 — 부른 쪽이 응답의 usage를 넣는다"""
    usage = None


@contextmanager
def _metered(vol, kind, model, scope, tin_max, tout_max, user_id=None):
    """예약 → 호출 → 정산. local은 예약 없이 0원으로 남긴다. openai는 PRICE에 없는 모델이면 UnpricedModel,
    예약이 한도를 넘으면 BudgetExceeded — 둘 다 클라이언트를 만들거나 부르기 전에"""
    m = _Meter()
    if PROVIDER.name == "local":  # 내부 주소만 허용(check_local_url) — 돈이 들지 않는다 (사용량은 0원으로 남긴다)
        yield m
        tin, tout = _tokens(m.usage)
        store.add_usage(vol, kind, model, tin, tout, 0.0, scope, user_id)
        return
    est = estimate_usd(model, tin_max, tout_max)
    day, start = store.kst_day()
    hold = store.reserve_budget(scope, day, start, est, LIMITS[scope], (vol, kind, model, user_id))
    if hold is None:
        raise BudgetExceeded(f"{scope} 예산 한도 ${LIMITS[scope]} — 이번 호출 예상 ${est:.6f}를 예약할 수 없음")
    try:
        yield m
    except BaseException:
        try:
            store.release_budget(hold)
        except Exception:  # 예약을 못 지워도 원래 오류를 가리지 않는다 (남은 예약은 store.HOLD_TTL 뒤 예상 비용으로 보수 정산)
            print(f"[llm] 예약 {hold} 지우기 실패", file=sys.stderr, flush=True)
        raise
    tin, tout = _tokens(m.usage)
    if m.usage is None:  # 비용을 모르면 예상 그대로 (모자라게 세지 않게)
        usd = est
    else:
        pi, po = price_of(model)
        usd = (tin * pi + tout * po) / 1e6
        if usd > est:  # 상한 계산이 틀렸다는 뜻 — 실제대로 정산하고 알린다
            print(f"[llm] {kind} 실제 비용 ${usd:.6f} > 예약 ${est:.6f}", file=sys.stderr, flush=True)
    try:
        store.settle_budget(hold, scope, day, usd, (vol, kind, model, tin, tout, user_id))
    except Exception:  # 호출은 끝났다 — 결과는 돌려준다. 예약은 남아 HOLD_TTL 뒤 예상 비용으로 보수 정산된다
        errors.log_exception(f"[llm] {kind} 정산 실패 (예약 {hold}은 만료 뒤 예상 비용으로 정산)")


def _tokens(usage):
    # 로컬 서버는 usage를 안 줄 수 있다
    return (getattr(usage, "prompt_tokens", 0) or 0), (getattr(usage, "completion_tokens", 0) or 0)


def extract(vol, lines, schema=None):
    """문서 첫 쪽 줄 텍스트 → 칸별 {value, confidence}. 칸 목록·지시문은 색인 스키마(app/fields.py)의 prompt 칸에서.
    prompt 칸이 없는 스키마면 부르지 않고 빈 dict. 제공자가 off면 LLMOff.
    응답이 max_tokens에서 잘리면(JSON이 깨짐) 빈 값·확신도 0 — 검토 표시로 사람에게"""
    schema = schema or fields.SCHEMA
    prompt = fields.extract_prompt(schema)
    if prompt is None:
        return {}
    model = PROVIDER.model("extract")
    user = "\n".join(lines[:80])
    with _metered(vol, "extract", model, "index", tokens_max(prompt, user, messages=2), EXTRACT_MAX_TOKENS) as m:
        r = client().chat.completions.create(
            model=model,
            temperature=0,
            max_tokens=EXTRACT_MAX_TOKENS,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": prompt},
                      {"role": "user", "content": user}],
        )
        m.usage = r.usage
    if getattr(r.choices[0], "finish_reason", None) == "length":
        print(f"[llm] extract 응답이 max_tokens {EXTRACT_MAX_TOKENS}에서 잘림 ({vol}) — 빈 값으로", file=sys.stderr, flush=True)
        return fields.parse_extract(schema, "{}")
    return fields.parse_extract(schema, r.choices[0].message.content)


CELL_PROMPT = {
    # 처음 지시문은 "비어 있으면 '-'까지만"이라고 해서 흘림체 일련번호를 빈칸으로 답하는 일이 많았다 → 손글씨를 읽으라고 명시
    "docno": "1990년대 공문서 머리부의 '문서번호' 칸을 잘라낸 이미지다. 앞부분(부서 약칭, 5자리 분류번호, '-')은 인쇄 글자이고, "
             "'-' 뒤에는 사람이 손으로 쓴 일련번호 숫자 1~5자리가 흘림체로 적혀 있는 경우가 대부분이다. "
             "손글씨 획을 숫자로 읽어 '부서 분류번호-일련번호' 한 줄로 적어라 (예: 지경 55142-1558). "
             "흘려 써서 애매해도 가장 그럴듯한 숫자를 적는다. '-' 뒤에 아무 획도 없을 때만 '-'로 끝낸다. 값만 적고 설명은 쓰지 않는다.",
    "date": "1990년대 공문서 머리부의 '시행일자' 칸을 잘라낸 이미지다. 연·월·일 숫자는 사람이 손으로 쓴 흘림체인 경우가 많다. "
            "손글씨 획을 숫자로 읽어 '연. 월. 일.' 한 줄로 적어라 (예: 97. 5. 15.). 연도는 2자리일 수 있다. "
            "흘려 써서 애매해도 가장 그럴듯한 숫자를 적는다. 값만 적고 설명은 쓰지 않는다.",
}


def read_cell(vol, field, png):
    """잘라낸 손글씨 칸 이미지 → (글자, 확신도). 확신도 = 생성 토큰 확률의 기하평균 (PoC TrOCR와 같은 정의).
    logprobs를 주지 않는 서버면 확신도 0 (검토 표시로 간다). 제공자가 off면 LLMOff"""
    model = PROVIDER.model("cell")
    url = "data:image/png;base64," + base64.b64encode(png).decode()
    # 이미지 토큰 상한을 모르는 모델(가격표에는 있는데 IMAGE_TOKENS에 없음)은 가장 큰 값으로
    tin_max = tokens_max(CELL_PROMPT[field]) + IMAGE_TOKENS.get(model, max(IMAGE_TOKENS.values()))
    with _metered(vol, f"read_{field}", model, "index", tin_max, CELL_MAX_TOKENS) as m:
        r = client().chat.completions.create(
            model=model,
            temperature=0,
            logprobs=True,
            max_tokens=CELL_MAX_TOKENS,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": CELL_PROMPT[field]},
                {"type": "image_url", "image_url": {"url": url, "detail": "high"}},
            ]}],
        )
        m.usage = r.usage
    ch = r.choices[0]
    lps = [t.logprob for t in (ch.logprobs.content or [])] if ch.logprobs else []
    conf = math.exp(sum(lps) / len(lps)) if lps else 0.0
    return (ch.message.content or "").strip(), round(conf, 4)


def embed(vol, texts, scope="index", user_id=None):
    """scope: index(색인·수정) | query(검색 질의 — user_id는 부른 사람). 제공자가 off면 LLMOff"""
    model = PROVIDER.model("embed")
    with _metered(vol, "embed", model, scope, tokens_max(*texts, messages=len(texts)), 0, user_id) as m:
        r = client().embeddings.create(model=model, input=texts)
        m.usage = r.usage
    return [d.embedding for d in r.data]


ANSWER_PROMPT = """너는 1990년대 공문서 기록물 검색 도우미다. 아래 [문서] 목록만 근거로 질문에 답한다.
답의 근거가 된 문서는 목록에 붙은 [철/문서] 표시를 그대로 옮겨 적는다. 목록에 답이 없으면 없다고 말한다. 짧게 답한다."""
# 근거 표시는 철까지 — 다른 철에 같은 쪽 이름이 있을 수 있다 (https://github.com/reddol18/docflow/issues/50).
# 철·doc_id에는 '/'·']'가 들어가지 않는다 (main.VOL_RX·쪽 파일 이름 규칙)
CITE_RX = re.compile(r"\[([\w.-]+)/([\w.-]+)\]")


def cite_key(vol, doc_id):
    """답변 근거 표시 [vol/doc_id]의 안쪽"""
    return f"{vol}/{doc_id}"


def citations(text):
    """답변 글의 근거 표시 → [(vol, doc_id)] (나온 순서, 중복 없이)"""
    return list(dict.fromkeys(CITE_RX.findall(text or "")))


def answer(question, docs, schema=None, user_id=None):
    """docs = 검색 결과(vol·doc_id, fields 아래 보이는 칸 + body). 칸 표시명·순서는 색인 스키마에서. 제공자가 off면 LLMOff.
    검색·질문 예산(query)을 쓴다. user_id: 부른 사람 (사용량 행에 남긴다)"""
    schema = schema or fields.SCHEMA
    lab = fields.labels(schema)
    model = PROVIDER.model("chat")
    ctx = "\n\n".join(
        f"[{cite_key(d['vol'], d['doc_id'])}] " + " / ".join(f"{lab[k]} {(d.get('fields') or {}).get(k, '')}" for k in fields.shown(schema)) + "\n"
        f"본문 앞부분: {d['body'][:600]}" for d in docs)
    user = f"[문서]\n{ctx}\n\n[질문] {question}"
    with _metered("-", "answer", model, "query", tokens_max(ANSWER_PROMPT, user, messages=2), ANSWER_MAX_TOKENS,
                  user_id) as m:
        r = client().chat.completions.create(
            model=model, temperature=0, max_tokens=ANSWER_MAX_TOKENS,
            messages=[{"role": "system", "content": ANSWER_PROMPT},
                      {"role": "user", "content": user}],
        )
        m.usage = r.usage
    if getattr(r.choices[0], "finish_reason", None) == "length":  # 상한을 다시 볼 근거
        print(f"[llm] answer 응답이 max_tokens {ANSWER_MAX_TOKENS}에서 잘림", file=sys.stderr, flush=True)
    return r.choices[0].message.content
