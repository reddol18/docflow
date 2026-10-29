"""OpenAI 호출: 색인 4칸 추출(텍스트), 손글씨 칸 판독(비전), 임베딩, 검색 답변.

모든 호출은 store.usage 표에 토큰·비용을 남기고, 오늘 쓴 돈이 DAILY_BUDGET_USD를 넘으면 호출하지 않는다.
동시에 여러 호출이 한도 직전에 통과하면 조금 넘을 수 있다 (예약 없이 사후 정산).
429·5xx·시간 초과는 SDK가 지수 백오프로 재시도한다 (max_retries).
"""
import base64
import json
import math
import os
from pathlib import Path

from openai import OpenAI

from app import store

CHAT = os.environ.get("CHAT_MODEL", "gpt-4o-mini")  # 검색 답변
# 4칸 추출: 1994-0001 철 정답 22건 비교에서 gpt-4.1-mini + OCR 오독 교정 지시문이 제목 18/22 (gpt-4o-mini 11~14/22),
# gpt-4.1은 19/22지만 비용 5배. eval/extract_variants.py
EXTRACT = os.environ.get("EXTRACT_MODEL", "gpt-4.1-mini")
# 손글씨 칸: 1997-0002 철 20칸 비교에서 gpt-4.1-mini(high)가 일련번호 7/10·일자 8/10으로 가장 잘 읽고 가장 쌌다
# (gpt-4o-mini 4~5/10·6/10, gpt-4o 5/10·7/10, gpt-4.1 7/10·7/10). eval/cell_variants.py
CELL = os.environ.get("CELL_MODEL", "gpt-4.1-mini")
EMBED = os.environ.get("EMBED_MODEL", "text-embedding-3-small")
# USD / 1M 토큰 (입력, 출력)
PRICE = {"gpt-4o-mini": (0.15, 0.60), "gpt-4.1-mini": (0.40, 1.60), "text-embedding-3-small": (0.02, 0.0)}
BUDGET = float(os.environ.get("DAILY_BUDGET_USD", "3.0"))


class BudgetExceeded(RuntimeError):
    pass


def _key():
    p = Path(os.environ.get("OPENAI_KEY_FILE", "/etc/openai/api-key"))
    return p.read_text().strip() if p.exists() else os.environ["OPENAI_API_KEY"]


_client = None


def client():
    global _client
    if _client is None:
        _client = OpenAI(api_key=_key(), timeout=60, max_retries=4)
    return _client


def _guard():
    spent = store.spent_today()
    if spent >= BUDGET:
        raise BudgetExceeded(f"오늘 사용액 ${spent:.4f} ≥ 한도 ${BUDGET}")


def _log(vol, kind, model, usage):
    tin = usage.prompt_tokens
    tout = getattr(usage, "completion_tokens", 0) or 0
    pi, po = PRICE.get(model, (0, 0))
    store.add_usage(vol, kind, model, tin, tout, (tin * pi + tout * po) / 1e6)


EXTRACT_PROMPT = """입력은 1990년대 지방자치단체 공문서(기안문·시행문) 첫 쪽을 OCR로 줄 단위로 옮긴 텍스트다.
왼쪽 머리 칸(문서번호·시행일자·수신·제목)과 오른쪽 결재란(시장·부시장·과장·보존·취급·선결 등)이 한 줄에 섞여 있을 수 있다.

아래 4칸을 뽑는다.
- docno: 문서번호 (예: "지경 55142-1558"). 부서 약칭 + 분류번호 + "-" + 일련번호
- date: 시행일자를 YYYY-MM-DD로. 2자리 연도는 1990년대/2000년대로 풀이. 일부가 없으면 아는 부분만 쓰고 확신도를 낮춘다 (예: "1997-03")
- recipient: 수신 (기관·사람·주소). "내부결재", "수신처 참조"도 그대로 값이다
- title: 제목

OCR 텍스트에는 글자를 잘못 읽은 곳이 많다 (예: '회담'→'회답', '필중'→'필증', '파른'→'따른', '실비'→'설비', '참유변상'→'장유면장'). 행정 문서 문맥상 명백한 오독은 바른 글자로 고쳐 적는다. 숫자·고유명사는 근거 없이 바꾸지 않는다.
제목은 '제목' 항목 줄의 끝까지 빠짐없이 적는다 (따옴표로 쓴 "건의", 괄호 안 내용 포함). 수신은 '수신' 항목의 값만 적고(주소·상호·'대표'까지), 결재란·담당자·참조 부서 글자는 넣지 않는다.
칸마다 confidence(0~1)를 붙인다. 결재란 단어(시장, 과장, 보존, 전결 등)는 값에 넣지 않는다. 텍스트에 없는 것을 지어내지 않는다.
출력: JSON 객체 하나
{"docno": {"value": "...", "confidence": 0.0}, "date": {...}, "recipient": {...}, "title": {...}}"""


def extract(vol, lines):
    """문서 첫 쪽 줄 텍스트 → 4칸 {value, confidence}."""
    _guard()
    r = client().chat.completions.create(
        model=EXTRACT,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": EXTRACT_PROMPT},
                  {"role": "user", "content": "\n".join(lines[:80])}],
    )
    _log(vol, "extract", EXTRACT, r.usage)
    out = json.loads(r.choices[0].message.content)
    return {k: {"value": str((out.get(k) or {}).get("value") or ""),
                "confidence": float((out.get(k) or {}).get("confidence") or 0)}
            for k in ["docno", "date", "recipient", "title"]}


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
    """잘라낸 손글씨 칸 이미지 → (글자, 확신도). 확신도 = 생성 토큰 확률의 기하평균 (PoC TrOCR와 같은 정의)."""
    _guard()
    url = "data:image/png;base64," + base64.b64encode(png).decode()
    r = client().chat.completions.create(
        model=CELL,
        temperature=0,
        logprobs=True,
        max_tokens=40,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": CELL_PROMPT[field]},
            {"type": "image_url", "image_url": {"url": url, "detail": "high"}},
        ]}],
    )
    _log(vol, f"read_{field}", CELL, r.usage)
    ch = r.choices[0]
    lps = [t.logprob for t in (ch.logprobs.content or [])] if ch.logprobs else []
    conf = math.exp(sum(lps) / len(lps)) if lps else 0.0
    return (ch.message.content or "").strip(), round(conf, 4)


def embed(vol, texts):
    _guard()
    r = client().embeddings.create(model=EMBED, input=texts)
    _log(vol, "embed", EMBED, r.usage)
    return [d.embedding for d in r.data]


ANSWER_PROMPT = """너는 1990년대 공문서 기록물 검색 도우미다. 아래 [문서] 목록만 근거로 질문에 답한다.
답의 근거가 된 문서는 [doc_id]로 표시한다. 목록에 답이 없으면 없다고 말한다. 짧게 답한다."""


def answer(question, docs):
    _guard()
    ctx = "\n\n".join(
        f"[{d['doc_id']}] 문서번호 {d['docno']} / 시행일자 {d['date']} / 수신 {d['recipient']} / 제목 {d['title']}\n"
        f"본문 앞부분: {d['body'][:600]}" for d in docs)
    r = client().chat.completions.create(
        model=CHAT, temperature=0,
        messages=[{"role": "system", "content": ANSWER_PROMPT},
                  {"role": "user", "content": f"[문서]\n{ctx}\n\n[질문] {question}"}],
    )
    _log("-", "answer", CHAT, r.usage)
    return r.choices[0].message.content
