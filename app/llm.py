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

모든 호출은 store.usage 표에 토큰·비용을 남긴다 (PRICE에 없는 모델 = 로컬 모델은 0원).
openai 제공자는 오늘 쓴 돈이 DAILY_BUDGET_USD를 넘으면 호출하지 않는다. local(내부 주소만)은 돈이 들지 않아 보지 않는다.
동시에 여러 호출이 한도 직전에 통과하면 조금 넘을 수 있다 (예약 없이 사후 정산).
429·5xx·시간 초과는 SDK가 지수 백오프로 재시도한다 (max_retries).
"""
import base64
import ipaddress
import math
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from openai import OpenAI

from app import fields, store

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
BUDGET = float(os.environ.get("DAILY_BUDGET_USD", "3.0"))
# 출력 상한 (비용·폭주 응답 막기)
# 추출: 모델 비교 하네스(eval/promptfoo/promptfooconfig.yaml)와 같은 값 — 2026-11-07 전체 행렬·로컬 실행에서 정상 응답 최대 310토큰
EXTRACT_MAX_TOKENS = 512
# 답변: 지시문이 "짧게 답한다", 근거는 상위 5건. 운영 답변 길이 측정 자료는 운영 DB(usage 표)에만 있어 이번에 보지 못했다 →
# 추출 상한의 2배로 넉넉히 두고, 잘리면(finish_reason=length) 서버 로그에 남긴다
ANSWER_MAX_TOKENS = 1024
INTERNAL_NETS = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]


class BudgetExceeded(RuntimeError):
    pass


class LLMOff(RuntimeError):
    """LLM_PROVIDER=off인데 LLM·임베딩을 부름"""


class ProviderConfigError(ValueError):
    pass


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
        return Provider("openai", models={k: (env.get(e) or "").strip() or OPENAI_MODELS[k] for k, e in MODEL_ENV.items()})
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


def _guard():
    if PROVIDER.name == "local":  # 내부 주소만 허용(check_local_url) — 돈이 들지 않는다 (사용량은 0원으로 남긴다)
        return
    spent = store.spent_today()
    if spent >= BUDGET:
        raise BudgetExceeded(f"오늘 사용액 ${spent:.4f} ≥ 한도 ${BUDGET}")


def _log(vol, kind, model, usage):
    tin = getattr(usage, "prompt_tokens", 0) or 0  # 로컬 서버는 usage를 안 줄 수 있다
    tout = getattr(usage, "completion_tokens", 0) or 0
    pi, po = PRICE.get(model, (0, 0))
    store.add_usage(vol, kind, model, tin, tout, (tin * pi + tout * po) / 1e6)


def extract(vol, lines, schema=None):
    """문서 첫 쪽 줄 텍스트 → 칸별 {value, confidence}. 칸 목록·지시문은 색인 스키마(app/fields.py)의 prompt 칸에서.
    prompt 칸이 없는 스키마면 부르지 않고 빈 dict. 제공자가 off면 LLMOff"""
    schema = schema or fields.SCHEMA
    prompt = fields.extract_prompt(schema)
    if prompt is None:
        return {}
    model = PROVIDER.model("extract")
    _guard()
    r = client().chat.completions.create(
        model=model,
        temperature=0,
        max_tokens=EXTRACT_MAX_TOKENS,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": prompt},
                  {"role": "user", "content": "\n".join(lines[:80])}],
    )
    _log(vol, "extract", model, r.usage)
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
    _guard()
    url = "data:image/png;base64," + base64.b64encode(png).decode()
    r = client().chat.completions.create(
        model=model,
        temperature=0,
        logprobs=True,
        max_tokens=40,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": CELL_PROMPT[field]},
            {"type": "image_url", "image_url": {"url": url, "detail": "high"}},
        ]}],
    )
    _log(vol, f"read_{field}", model, r.usage)
    ch = r.choices[0]
    lps = [t.logprob for t in (ch.logprobs.content or [])] if ch.logprobs else []
    conf = math.exp(sum(lps) / len(lps)) if lps else 0.0
    return (ch.message.content or "").strip(), round(conf, 4)


def embed(vol, texts):
    """제공자가 off면 LLMOff"""
    model = PROVIDER.model("embed")
    _guard()
    r = client().embeddings.create(model=model, input=texts)
    _log(vol, "embed", model, r.usage)
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


def answer(question, docs, schema=None):
    """docs = 검색 결과(vol·doc_id, fields 아래 보이는 칸 + body). 칸 표시명·순서는 색인 스키마에서. 제공자가 off면 LLMOff"""
    schema = schema or fields.SCHEMA
    lab = fields.labels(schema)
    model = PROVIDER.model("chat")
    _guard()
    ctx = "\n\n".join(
        f"[{cite_key(d['vol'], d['doc_id'])}] " + " / ".join(f"{lab[k]} {(d.get('fields') or {}).get(k, '')}" for k in fields.shown(schema)) + "\n"
        f"본문 앞부분: {d['body'][:600]}" for d in docs)
    r = client().chat.completions.create(
        model=model, temperature=0, max_tokens=ANSWER_MAX_TOKENS,
        messages=[{"role": "system", "content": ANSWER_PROMPT},
                  {"role": "user", "content": f"[문서]\n{ctx}\n\n[질문] {question}"}],
    )
    _log("-", "answer", model, r.usage)
    if getattr(r.choices[0], "finish_reason", None) == "length":  # 상한을 다시 볼 근거
        print(f"[llm] answer 응답이 max_tokens {ANSWER_MAX_TOKENS}에서 잘림", file=sys.stderr, flush=True)
    return r.choices[0].message.content
