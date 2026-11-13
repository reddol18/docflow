"""화면·API로 내보낼 오류 분류 (https://github.com/reddol18/docflow/issues/28, ASVS V7.4.1·API3).

화면(events, 응답)에는 분류 코드와 짧은 문구만 — 예외 이름·메시지·경로는 서버 로그(표준 오류)에만 남긴다.
서버 로그에 남기는 traceback도 키·비밀번호 꼴 문자열과 한글이 든 따옴표 글자(예외 repr에 실린 문서 값)는 가린다(mask).
따옴표 없이 실린 실명 같은 값은 가리지 못한다 — 로그 줄에 문서 값을 넣지 않는 것이 먼저다(README).
"""
import re
import sys
import traceback

MESSAGES = {
    "budget": "오늘 LLM 사용 한도에 걸려 멈췄습니다",
    "llm": "LLM·임베딩 호출이 실패했습니다",
    "llm_off": "이 설치에서는 검색·답변 등 LLM이 필요한 기능을 쓸 수 없습니다",
    "db": "데이터베이스 처리에 실패했습니다",
    "vector": "벡터 색인 처리에 실패했습니다",
    "network": "외부 연결에 실패했습니다",
    "storage": "파일을 읽거나 쓰지 못했습니다",
    "internal": "내부 오류가 났습니다",
}


def code_of(e):
    from app.llm import BudgetExceeded, LLMOff
    if isinstance(e, BudgetExceeded):
        return "budget"
    if isinstance(e, LLMOff):
        return "llm_off"
    mod = type(e).__module__ or ""
    if mod.startswith("openai"):
        return "llm"
    if mod.startswith("psycopg"):
        return "db"
    if mod.startswith("qdrant_client"):
        return "vector"
    if mod.startswith("httpx") or isinstance(e, (ConnectionError, TimeoutError)):
        return "network"
    if isinstance(e, OSError):
        return "storage"
    return "internal"


def public(e):
    """events·응답에 넣을 값: {code, message}"""
    code = code_of(e)
    return {"code": code, "message": MESSAGES[code]}


# API 키, Bearer·Basic 인증 값, JWT
_KEY = re.compile(r"sk-[A-Za-z0-9_\-]{8,}|(?i:bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}"
                  r"|eyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]*")
# 이름이 key·secret·token·password로 끝나면(client_secret, access_token, PGPASSWORD …) 값을 가린다
_NAME = r"(?i)(?<![A-Za-z0-9])(\w*(?:api[_-]?key|password|passwd|secret|token|authorization))"
_KV_QUOTED = re.compile(_NAME + r"(['\"]?\s*[=:]\s*)(['\"])(?:(?!\3).)*\3")  # 'password': 'a b c' — 따옴표 안 전체
_KV = re.compile(_NAME + r"(['\"]?\s*[=:]\s*)[^\s'\"}]+")
_URI = re.compile(r"(://[^:/\s@]+:)[^@\s]+(@)")  # postgresql://user:비밀번호@host
# 한글이 든 따옴표 글자 — 예외 repr(KeyError('…'), 라이브러리가 싣는 input_value='…' 등)에 실린 문서 값(실명 등)을 가린다
# (https://github.com/reddol18/docflow/issues/27). traceback 속 소스 줄의 한글 글자도 같이 가려지지만 줄 번호로 찾을 수 있다.
# 따옴표 없이 실린 값은 못 가린다 — 그래서 로그 줄에는 애초에 문서 값을 넣지 않는다 (README '로그·추적에 남는 원문')
_HANGUL_QUOTED = re.compile(r"(['\"])(?:(?!\1)[^\n])*[가-힣ㄱ-ㅎㅏ-ㅣ](?:(?!\1)[^\n])*\1")


def mask(text):
    text = _KEY.sub("***", text)
    text = _KV_QUOTED.sub(r"\1\2\3***\3", text)
    text = _KV.sub(r"\1\2***", text)
    text = _HANGUL_QUOTED.sub(r"\1***\1", text)
    return _URI.sub(r"\1***\2", text)


def log_exception(where):
    """지금 처리 중인 예외의 traceback을 가려서 서버 로그로"""
    text = "".join(traceback.format_exception(*sys.exc_info()))
    print(f"[error] {where}\n{mask(text)}", file=sys.stderr, flush=True)
