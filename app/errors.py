"""화면·API로 내보낼 오류 분류 (https://github.com/reddol18/docflow/issues/28, ASVS V7.4.1·API3).

화면(events, 응답)에는 분류 코드와 짧은 문구만 — 예외 이름·메시지·경로는 서버 로그(표준 오류)에만 남긴다.
서버 로그에 남기는 traceback도 키·비밀번호 꼴 문자열과, 위치·소스 줄이 아닌 줄의 한글·한자 묶음·주민등록번호·전화번호 꼴
(예외 문구에 실린 문서 값)은 가린다(mask). 영문으로 적힌 이름 같은 값은 가리지 못한다 — 로그 줄에 문서 값을 넣지 않는 것이
먼저다(README).
"""
import logging
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
# 앞 이름은 ASCII 64자까지 — \w*로 두면 한글처럼 \w인 글자가 길게 이어진 글에서 위치마다 끝까지 되짚어 제곱 시간이 된다
_NAME = r"(?i)(?<![A-Za-z0-9])([A-Za-z0-9_]{0,64}(?:api[_-]?key|password|passwd|secret|token|authorization))"
_KV_QUOTED = re.compile(_NAME + r"(['\"]?\s*[=:]\s*)(['\"])(?:(?!\3).)*\3")  # 'password': 'a b c' — 따옴표 안 전체
_KV = re.compile(_NAME + r"(['\"]?\s*[=:]\s*)[^\s'\"}]+")
_URI = re.compile(r"(://[^:/\s@]+:)[^@\s]+(@)")  # postgresql://user:비밀번호@host
# 예외 문구에 실린 문서 값(실명·번호)을 가린다 (https://github.com/reddol18/docflow/issues/27). traceback 줄을 먼저 나눈다
# (ExceptionGroup의 '  | ' 머리는 떼고 본다):
#   위치 줄   File "…", line N          — 그대로
#   소스 줄   위치 줄 바로 다음의 4칸 들여쓴 줄 — 그대로 (코드다. 소스 없는 프레임이면 다음 줄은 메시지라 아래로)
#   메시지 줄 그 밖의 모든 줄 — 따옴표와 상관없이 한글·한자 글자 묶음과 주민등록번호·전화번호 꼴을 *** 로
# 경로 속 한글·우리 앱의 한국어 메시지도 가려지지만, 어디서 났는지는 위치 줄이 남아 알 수 있다. 모두 선형 시간 정규식이다
_GROUP_PREFIX = re.compile(r"^[ \t]*(?:\| ?)+")  # ExceptionGroup 하위 예외 줄의 '  | ' 머리 (그 뒤 들여쓰기는 남긴다)
_SOURCE_LOC = re.compile(r'^\s*File ".*", line \d+')
_HAN = re.compile(r"[가-힣ㄱ-ㅎㅏ-ㅣ㐀-䶿一-鿿豈-﫿]+")
_IDISH = re.compile(r"(?<!\d)(?:\d{6}-?[1-8]\d{6}|0\d{1,2}-?\d{3,4}-?\d{4})(?!\d)")  # 주민등록번호·전화번호 꼴


def _mask_values(text):
    """traceback 줄을 위치·소스·메시지로 나눠 메시지 줄의 한글·한자·번호 꼴만 가린다"""
    out, after_loc = [], False
    for line in text.split("\n"):
        m = _GROUP_PREFIX.match(line)
        body = line[m.end():] if m else line
        if _SOURCE_LOC.match(body):
            out.append(line)
            after_loc = True
            continue
        if after_loc and body.startswith("    "):
            out.append(line)
        else:
            out.append(_IDISH.sub("***", _HAN.sub("***", line)))
        after_loc = False
    return "\n".join(out)

def mask(text):
    text = _KEY.sub("***", text)
    text = _KV_QUOTED.sub(r"\1\2\3***\3", text)
    text = _KV.sub(r"\1\2***", text)
    text = _mask_values(text)
    return _URI.sub(r"\1***\2", text)


class AccessLogNoQuery(logging.Filter):
    """uvicorn 접근 로그 줄에서 질의 문자열을 뗀다: 'GET /ask?q=… HTTP/1.1' → 'GET /ask?… HTTP/1.1'.
    /search·/ask의 q(검색어·질문)에 실명 등이 들어갈 수 있다 (https://github.com/reddol18/docflow/issues/27). 접근 로그 자체
    (누가·언제·어느 경로·상태 코드)는 그대로 남긴다. 모든 요청에서 뗀다 — 다른 경로의 질의(vol 등)는 감사 로그에 따로 남는다.
    uvicorn은 경로를 args 세 번째(client, method, path, http_version, status)로 넘긴다"""

    def filter(self, record):
        a = record.args
        if isinstance(a, tuple) and len(a) >= 3 and isinstance(a[2], str) and "?" in a[2]:
            record.args = (*a[:2], a[2].split("?", 1)[0] + "?…", *a[3:])
        return True


def install_access_log_filter():
    """uvicorn.access 로거에 AccessLogNoQuery를 한 번 건다 (app/main.py가 불러올 때). uvicorn의 logging dictConfig는
    로거의 기존 필터를 지우지 않아 순서와 상관없이 남는다 (tests/test_llm_boundary.py가 실제 uvicorn으로 확인)"""
    lg = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, AccessLogNoQuery) for f in lg.filters):
        lg.addFilter(AccessLogNoQuery())


def log_exception(where):
    """지금 처리 중인 예외의 traceback을 가려서 서버 로그로"""
    text = "".join(traceback.format_exception(*sys.exc_info()))
    print(f"[error] {where}\n{mask(text)}", file=sys.stderr, flush=True)
