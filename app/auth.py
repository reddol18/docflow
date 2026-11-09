"""로그인·세션·역할 (https://github.com/reddol18/docflow/issues/6).

설치 환경마다 AUTH_MODES로 고른다.
  local        docflow 자체 계정. 비밀번호는 argon2id 해시만 저장. 계정은 `python -m app.users`로 만든다
  oidc         OIDC 제공자(기관 SSO·Keycloak)에 로그인을 맡긴다. docflow는 비밀번호를 받지 않는다
  local,oidc   둘 다
  off          인증 끄기. AUTH_DEV_ALLOW_OFF=1 이 함께 있어야만 된다 (개발용)

세션: 무작위 토큰을 쿠키(HttpOnly, SameSite=Lax)로 주고 DB에는 SHA-256 해시만 둔다 → 로그아웃·만료·강제 종료 가능.
스크립트는 같은 토큰을 `Authorization: Bearer`로 보내도 된다. 쿠키가 아니므로 CSRF 확인을 하지 않는다.
쿠키로 온 상태 변경 요청(POST·PATCH·DELETE)은 X-Docflow-CSRF 헤더나 Sec-Fetch-Site=same-origin 이 있어야 한다.

OIDC 설정 (oidc를 켤 때)
  OIDC_ISSUER            발급자 URL (discovery 문서의 issuer와 정확히 같아야 함)
  OIDC_CLIENT_ID         클라이언트 ID
  OIDC_CLIENT_SECRET_FILE / OIDC_CLIENT_SECRET   기밀 클라이언트면 비밀값 (파일 우선, k8s Secret 마운트)
  OIDC_REDIRECT_URI      제공자에 등록한 콜백 주소 (예: https://docflow.example/api/auth/oidc/callback)
  OIDC_SCOPES            기본 "openid profile email"
  OIDC_ROLE_CLAIM        역할을 읽을 클레임. 점으로 내려간다 (Keycloak: realm_access.roles). 비우면 DB 역할 유지
  OIDC_ROLE_MAP          {"제공자 값": "docflow 역할"} JSON. 지도에 없는 값은 무시
  OIDC_DEFAULT_ROLE      클레임으로 역할을 못 정했을 때. 기본 none(로그인 거절). viewer 등으로 열려면 명시
  OIDC_ALLOW_HTTP        1이면 http 발급자 허용 (클러스터 안 시험 제공자 전용). 기본은 https만

그 밖에
  COOKIE_SECURE          기본 1(Secure 쿠키). http로 데모할 때만 0. https 콜백인데 0이면 시작을 거부한다
  LOGIN_RATE_USER        같은 계정 이름으로 5분에 로그인 몇 번까지 (기본 10)
  TRUST_PROXY            X-Forwarded-For를 덮어쓰는 프록시 뒤에서만 1 → IP별 창(LOGIN_RATE_MAX, 기본 20)도 쓴다.
                         Next rewrites는 덮어쓰지 않으므로 지금 구성에서는 0
  OIDC_LABEL             로그인 버튼 글자 (기본 "SSO")
  AUDIT_ANON_MAX         계정에 묶이지 않는 감사 행(로그인 안 한 요청의 거절 등)을 60초에 몇 행까지 (기본 30, README)
"""
import base64
import collections
import dataclasses
import functools
import hashlib
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
from typing import Annotated

import httpx
import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from app import errors, store

ROLES = ("viewer", "reviewer", "inspector", "admin")  # 뒤로 갈수록 넓다. reviewer=입력자, inspector=검사자 (https://github.com/reddol18/docflow/issues/10)
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
COOKIE = "docflow_session"
STATE_COOKIE = "docflow_oidc_state"
SESSION_SEC = int(float(os.environ.get("SESSION_HOURS", "12")) * 3600)
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1") != "0"  # 기본 Secure. http로 데모할 때만 명시적으로 0
LOGIN_RATE = (int(os.environ.get("LOGIN_RATE_MAX", "20")), 300)  # 같은 IP에서 300초에 20번까지 (/auth/login, /auth/oidc/login)
# X-Forwarded-For를 덮어쓰는 프록시 뒤에서만 1 (Next rewrites는 클라이언트 값을 그대로 넘기므로 안 됨)
TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"
MAX_NEXT = 2048
MAX_FAILS, LOCK_MIN = 5, 15
CLAIM_SEC = 60  # 로그인 시도 자리를 잡고 검증을 끝내기까지 이보다 길 수 없다 (argon2 검증은 1초 안)
PENDING_SEC = 600  # OIDC 로그인 시작부터 콜백까지
ID_TOKEN_ALGS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"]  # none·HS*는 받지 않는다
FAIL_MSG = "아이디 또는 비밀번호가 맞지 않습니다"  # 계정이 있는지·잠겼는지 드러내지 않는다


def parse_modes(raw, allow_off):
    modes = {m.strip() for m in (raw or "local").split(",") if m.strip()}
    if modes == {"off"}:
        if not allow_off:
            raise RuntimeError("AUTH_MODES=off 는 AUTH_DEV_ALLOW_OFF=1 이 함께 있어야 한다")
        return frozenset()
    if not modes or modes - {"local", "oidc"}:
        raise RuntimeError(f"AUTH_MODES 값이 잘못됨: {raw!r} (local, oidc, local,oidc, off)")
    return frozenset(modes)


MODES = parse_modes(os.environ.get("AUTH_MODES"), os.environ.get("AUTH_DEV_ALLOW_OFF") == "1")
if not MODES:
    print("경고: 인증이 꺼져 있음 (AUTH_MODES=off). 개발용으로만 쓸 것", flush=True)


@dataclasses.dataclass(frozen=True)
class User:
    id: int | None
    username: str
    name: str
    role: str

    def public(self):
        return {"username": self.username, "name": self.name, "role": self.role}


DEV_USER = User(None, "dev", "개발 모드", "admin")


def role_at_least(role, need):
    return role in ROLES and ROLES.index(role) >= ROLES.index(need)


# ---------- 비밀번호 ----------

ph = PasswordHasher()  # argon2id, 라이브러리 기본 비용 (RFC 9106 권장값)


@functools.cache
def _dummy_hash():
    return ph.hash(secrets.token_hex(16))


def hash_password(pw):
    return ph.hash(pw)


def _verify(h, pw):
    try:
        return ph.verify(h, pw)
    except (VerificationError, InvalidHashError):
        return False


def login_local(username, password, outcome=None):
    """맞으면 User, 틀리거나 잠겼거나 없는 계정이면 None. 없는 계정에도 해시 검증을 한 번 돌려 시간 차를 줄인다.
    outcome(dict)을 주면 실패 사유 분류(reason)와 해당 계정(account: 있는 local 계정일 때만)을 채운다 — 감사 로그용.
    사유는 응답에 나가지 않는다 (응답은 늘 FAIL_MSG — 계정이 있는지·잠겼는지 드러내지 않는다)"""
    out = outcome if outcome is not None else {}
    with store.pool.connection() as c:
        # 검증 전에 시도 한 번을 원자적으로 잡는다 → 동시에 여러 요청을 보내도 잠금 전 MAX_FAILS번까지만 검증한다.
        # 자리를 잡은 뒤 요청이 죽으면(풀 오류·프로세스 종료) failed가 MAX_FAILS에 남는다 → 마지막 시도가 CLAIM_SEC보다
        # 오래됐으면 남은 자리로 보지 않고 1부터 다시 센다 (검증 중인 정상 요청은 그보다 짧다)
        u = c.execute("UPDATE users SET failed = CASE WHEN failed >= %s THEN 1 ELSE failed + 1 END, attempt_at = now() "
                      "WHERE username = %s AND issuer IS NULL AND NOT disabled AND password_hash IS NOT NULL "
                      "AND (locked_until IS NULL OR locked_until < now()) "
                      "AND (failed < %s OR attempt_at IS NULL OR attempt_at < now() - make_interval(secs => %s)) "
                      "RETURNING id, username, name, role, password_hash, failed",
                      (MAX_FAILS, username, MAX_FAILS, CLAIM_SEC)).fetchone()
    if not u:  # 없음·꺼짐·잠김·이미 MAX_FAILS번 시도 중
        _verify(_dummy_hash(), password)
        _why_refused(username, out)
        return None
    ok = _verify(u["password_hash"], password)
    with store.pool.connection() as c:
        if not ok:
            out["account"] = User(u["id"], u["username"], u["name"] or u["username"], u["role"])
            out["reason"] = "bad_password"
            if u["failed"] >= MAX_FAILS:  # MAX_FAILS번째 실패 → 잠그고 횟수를 되돌린다 (풀린 뒤 다시 MAX_FAILS번)
                c.execute("UPDATE users SET failed = 0, locked_until = now() + make_interval(mins => %s) WHERE id = %s",
                          (LOCK_MIN, u["id"]))
                out["reason"] = "lockout"  # 이 실패로 잠겼다 — 감사 로그에 'lockout' 행
            return None
        c.execute("UPDATE users SET failed = 0, locked_until = NULL, last_login = now() WHERE id = %s", (u["id"],))
        if ph.check_needs_rehash(u["password_hash"]):
            c.execute("UPDATE users SET password_hash = %s WHERE id = %s", (ph.hash(password), u["id"]))
    return User(u["id"], u["username"], u["name"] or u["username"], u["role"])


def _why_refused(username, out):
    """검증 자리를 못 잡은 까닭 (감사 로그용). 없는 계정은 이름을 남기지 않는다 — 아이디 칸에 비밀번호를 잘못 넣은 것일 수 있다"""
    with store.pool.connection() as c:
        r = c.execute("SELECT id, username, name, role, disabled, password_hash IS NULL AS no_pw, "
                      "coalesce(locked_until > now(), false) AS locked FROM users WHERE username = %s AND issuer IS NULL",
                      (username,)).fetchone()
    if not r:
        out["reason"] = "unknown_user"
        return
    out["account"] = User(r["id"], r["username"], r["name"] or r["username"], r["role"])
    out["reason"] = ("disabled" if r["disabled"] else "no_password" if r["no_pw"] else "locked" if r["locked"]
                     else "busy")  # busy: 다른 요청들이 이미 MAX_FAILS번 검증 중


# ---------- 인증 사건 감사 로그 (https://github.com/reddol18/docflow/issues/63) ----------
# 남기는 것: 행위(login·lockout·logout·거절된 요청)·결과·사유 분류·방식(local/oidc)·계정(있는 계정일 때만).
# 남기지 않는 것: 비밀번호, 세션 토큰·쿠키·헤더, 요청 본문, 없는 계정으로 시도한 아이디(비밀번호를 잘못 넣은 것일 수 있다).
# 계정에 묶이지 않는 행(로그인 안 한 요청의 거절·없는 계정 시도)은 누구나 무한히 만들 수 있다 → 프로세스 전체에서
# AUDIT_ANON_MAX행/60초까지만 쓰고, 넘친 개수는 다음에 쓸 수 있을 때 'audit_suppressed' 한 행으로 남긴다.
# 계정에 묶인 행은 계정별 시도 제한(USER_RATE)·잠금(MAX_FAILS)이 이미 묶는다.

ANON_AUDIT = (int(os.environ.get("AUDIT_ANON_MAX", "30")), 60)
_anon: collections.deque = collections.deque()
_anon_state = {"dropped": 0}
_anon_lock = threading.Lock()


def _anon_slot():
    """(쓸 수 있나, 그동안 버린 개수). 버린 개수는 쓸 수 있게 된 첫 행이 가져가 요약 행으로 남긴다"""
    n, win = ANON_AUDIT
    now = time.monotonic()
    with _anon_lock:
        while _anon and _anon[0] < now - win:
            _anon.popleft()
        if len(_anon) >= n:
            if not _anon_state["dropped"]:
                print("감사 로그: 계정 없는 거절 기록이 많아 줄여 남김 (AUDIT_ANON_MAX)", file=sys.stderr, flush=True)
            _anon_state["dropped"] += 1
            return False, 0
        _anon.append(now)
        dropped, _anon_state["dropped"] = _anon_state["dropped"], 0
        return True, dropped


def audit_event(user, action, result, vol=None, doc_id=None, *, limited=False, **info):
    """인증 사건·거절된 요청 한 행. limited면 계정 없는 행으로 보고 프로세스 전체 상한을 건다.
    기록 실패는 요청 결과를 바꾸지 않는다 (서버 로그로). 썼으면 True"""
    try:
        if limited:
            ok, dropped = _anon_slot()
            if not ok:
                return False
            if dropped:
                store.audit(None, "audit_suppressed", None, None, "dropped", count=dropped)
        store.audit(user, action, vol, doc_id, result, **info)
        return True
    except Exception:
        errors.log_exception(f"audit {action} {result}")
        return False


def audit_as(action):
    """경로 함수에 감사 행위 이름을 붙인다. GET이 아닌 경로는 모두 이것이 있어야 한다 (시험이 app.routes를 돈다).
    처리 함수 앞(로그인·역할·CSRF·본문 검증)에서 거절된 요청은 main의 예외 처리기가 이 이름으로 rejected_<상태> 행을 남긴다"""
    def deco(fn):
        fn.audit_action = action
        return fn
    return deco


def _reason(e, reason):
    e.reason = reason  # 감사 로그 사유 분류 (응답에는 나가지 않는다)
    return e


def _audited(e):
    e.audited = True  # 이미 감사 로그에 남겼다 — 예외 처리기가 다시 남기지 않는다
    return e


# ---------- 시도 제한 (인증 전 경로) ----------
# 계정 이름별 창(대입)과 IP별 창(TRUST_PROXY일 때만). 전체 창은 두지 않는다: 누구나 채워 모든 사람의 로그인을 막을 수 있다.
# Next 프록시는 X-Forwarded-For를 덮어쓰지 않고 클라이언트가 보낸 값을 그대로 넘긴다 → Next 뒤에서는 TRUST_PROXY를 켜지 않는다.
# 남는 위험(IP를 못 믿을 때 이름을 바꿔 가며 뿌리기)은 https://github.com/reddol18/docflow/issues/40

_hits: dict[str, collections.deque] = {}
_hits_lock = threading.Lock()
USER_RATE = (int(os.environ.get("LOGIN_RATE_USER", "10")), 300)


def client_ip(request):
    if TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for", "")
        if xff.strip():
            return xff.split(",")[-1].strip()
    return request.client.host if request.client else "?"


def rate_limit(request, username=None):
    """로그인 대입·로그인 시작 반복(DB 행·argon2 CPU)을 막는다. 한 프로세스 안의 메모리 창."""
    keys = []
    if username is not None:
        keys.append((f"user:{username.lower()}", USER_RATE))
    if TRUST_PROXY:  # 앞단 프록시가 X-Forwarded-For를 덮어쓴다고 확인된 경우만 (Next는 아님)
        keys.append((f"ip:{client_ip(request)}", LOGIN_RATE))
    now = time.monotonic()
    with _hits_lock:
        if len(_hits) > 10000:
            oldest = max(USER_RATE[1], LOGIN_RATE[1])
            for k in [k for k, q in _hits.items() if not q or q[-1] < now - oldest]:
                del _hits[k]
        qs = []
        for key, (n, win) in keys:
            q = _hits.setdefault(key, collections.deque())
            while q and q[0] < now - win:
                q.popleft()
            if len(q) >= n:
                raise _reason(HTTPException(429, "시도가 너무 많습니다. 잠시 뒤 다시 하세요"), "rate_limited")
            qs.append(q)
        for q in qs:
            q.append(now)


# ---------- 세션 ----------

def _h(token):
    return hashlib.sha256(token.encode()).hexdigest()


def new_session(user_id, via):
    token = secrets.token_urlsafe(32)  # 256비트
    with store.pool.connection() as c:
        c.execute("DELETE FROM sessions WHERE expires_at < now()")
        c.execute("INSERT INTO sessions (token_hash, user_id, expires_at, via) "
                  "VALUES (%s, %s, now() + make_interval(secs => %s), %s)", (_h(token), user_id, SESSION_SEC, via))
    return token


def session_user(token):
    with store.pool.connection() as c:
        r = c.execute("SELECT u.id, u.username, u.name, u.role FROM sessions s JOIN users u ON u.id = s.user_id "
                      "WHERE s.token_hash = %s AND s.expires_at > now() AND NOT u.disabled", (_h(token),)).fetchone()
        if r:  # 이미지마다 쓰지 않도록 5분에 한 번만
            c.execute("UPDATE sessions SET last_seen = now() WHERE token_hash = %s AND last_seen < now() - interval '5 minutes'",
                      (_h(token),))
    return User(r["id"], r["username"], r["name"] or r["username"], r["role"]) if r else None


def drop_session(token):
    """세션을 지운다. 지운 세션의 사용자(User), 없던 토큰이면 None"""
    with store.pool.connection() as c:
        r = c.execute("DELETE FROM sessions s USING users u WHERE s.token_hash = %s AND u.id = s.user_id "
                      "RETURNING u.id, u.username, u.name, u.role", (_h(token),)).fetchone()
    return User(r["id"], r["username"], r["name"] or r["username"], r["role"]) if r else None


def drop_user_sessions(user_id):
    with store.pool.connection() as c:
        return c.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,)).rowcount


def set_session_cookie(resp, token):
    resp.set_cookie(COOKIE, token, max_age=SESSION_SEC, httponly=True, samesite="lax", secure=COOKIE_SECURE, path="/")


# ---------- 요청마다: 누구인가, 무엇을 할 수 있나 ----------

PUBLIC = {"/healthz", "/auth/config", "/auth/login", "/auth/logout", "/auth/oidc/login", "/auth/oidc/callback"}


def _token(request):
    a = request.headers.get("authorization", "")
    if a[:7].lower() == "bearer ":
        return a[7:].strip(), False
    return request.cookies.get(COOKIE), True


def check_csrf(request):
    """쿠키로 인증된 상태 변경 요청은 같은 사이트에서 온 것만. 다른 사이트 폼·fetch는 이 헤더를 붙일 수 없다(CORS 없음)."""
    if request.headers.get("x-docflow-csrf") == "1":
        return
    if request.headers.get("sec-fetch-site") in ("same-origin", "none"):
        return
    raise _reason(HTTPException(403, "다른 사이트에서 온 요청은 받지 않음"), "csrf")


def resolve(request: Request):
    """로그인한 사용자. 없으면 401. 한 요청 안에서는 한 번만 DB를 본다."""
    if hasattr(request.state, "user"):
        return request.state.user
    if not MODES:
        request.state.user = DEV_USER
        return DEV_USER
    token, via_cookie = _token(request)
    u = session_user(token) if token else None
    if not u:  # 사유: 토큰 없음 / 맞지 않거나 끝난 토큰 (토큰 자체는 어디에도 남기지 않는다)
        raise _reason(HTTPException(401, "로그인 필요"), "bad_session" if token else "no_session")
    if via_cookie and request.method not in SAFE_METHODS:
        try:
            check_csrf(request)
        except HTTPException as e:
            e.user = u  # 세션 쿠키가 실린 다른 사이트 요청 — 감사 로그에 그 쿠키의 사용자로 (CSRF 시도 흔적)
            raise
    request.state.user = u
    return u


def guard(request: Request):
    """앱 전체에 거는 의존성: PUBLIC 경로를 빼고 모두 로그인 필요 (기본 거부). 역할은 경로마다 require()로 더 좁힌다."""
    if request.url.path in PUBLIC:
        return
    resolve(request)


def require(role):
    if role not in ROLES:
        raise ValueError(role)

    def dep(request: Request):
        u = resolve(request)
        if not role_at_least(u.role, role):
            raise _reason(HTTPException(403, f"{role} 역할 필요"), "role")
        return u

    dep.role = role  # 경로 보호 시험이 읽는다
    return Depends(dep)


Viewer = Annotated[User, require("viewer")]
Reviewer = Annotated[User, require("reviewer")]
Inspector = Annotated[User, require("inspector")]
Admin = Annotated[User, require("admin")]


# ---------- OIDC ----------

def _env_secret(name):
    f = os.environ.get(name + "_FILE")
    if f and os.path.exists(f):
        with open(f, encoding="utf-8") as fh:
            return fh.read().strip()
    return os.environ.get(name, "")


@dataclasses.dataclass
class OidcConfig:
    issuer: str = ""
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""
    scopes: str = "openid profile email"
    role_claim: str = ""
    role_map: dict = dataclasses.field(default_factory=dict)
    default_role: str | None = None  # 기본은 거절: 제공자에 로그인할 수 있다고 모두 문서를 보게 하지 않는다
    label: str = "SSO"
    allow_http: bool = False

    @classmethod
    def from_env(cls):
        d = os.environ.get("OIDC_DEFAULT_ROLE", "none").strip()
        return cls(issuer=os.environ.get("OIDC_ISSUER", "").strip(), client_id=os.environ.get("OIDC_CLIENT_ID", "").strip(),
                   client_secret=_env_secret("OIDC_CLIENT_SECRET"), redirect_uri=os.environ.get("OIDC_REDIRECT_URI", "").strip(),
                   scopes=os.environ.get("OIDC_SCOPES", "openid profile email"), role_claim=os.environ.get("OIDC_ROLE_CLAIM", "").strip(),
                   role_map=json.loads(os.environ.get("OIDC_ROLE_MAP") or "{}"),
                   default_role=None if d in ("", "none") else d, label=os.environ.get("OIDC_LABEL", "SSO"),
                   allow_http=os.environ.get("OIDC_ALLOW_HTTP") == "1")

    def check(self, cookie_secure=True):
        missing = [k for k in ("issuer", "client_id", "redirect_uri") if not getattr(self, k)]
        if missing:
            raise RuntimeError(f"oidc를 켰는데 설정이 없음: {', '.join('OIDC_' + k.upper() for k in missing)}")
        if not self.allow_http and not self.issuer.startswith("https://"):
            raise RuntimeError("OIDC_ISSUER는 https여야 한다 (클러스터 안 시험 제공자만 OIDC_ALLOW_HTTP=1)")
        if self.redirect_uri.startswith("https://") and not cookie_secure:
            raise RuntimeError("https로 서비스하면서 COOKIE_SECURE=0 — 세션 쿠키가 http로도 나간다")
        if self.default_role is not None and self.default_role not in ROLES:
            raise RuntimeError(f"OIDC_DEFAULT_ROLE 값이 잘못됨: {self.default_role}")
        bad = {v for v in self.role_map.values() if v not in ROLES}
        if bad:
            raise RuntimeError(f"OIDC_ROLE_MAP의 역할이 잘못됨: {bad}")


OIDC = OidcConfig.from_env()
if "oidc" in MODES:
    OIDC.check(COOKIE_SECURE)


def _get_json(url):
    r = httpx.get(url, timeout=10, follow_redirects=False)
    r.raise_for_status()
    return r.json()


def _post_form(url, data, auth):
    r = httpx.post(url, data=data, auth=auth, timeout=10, follow_redirects=False)
    if r.status_code != 200:
        raise HTTPException(502, "OIDC 토큰 교환 실패")  # 응답 본문(오류 설명)은 사용자에게 보이지 않는다
    return r.json()


_cache: dict = {}


def _cached(key, fn, ttl=3600):
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    v = fn()
    _cache[key] = (time.monotonic() + ttl, v)
    return v


def discovery():
    def load():
        d = _get_json(OIDC.issuer.rstrip("/") + "/.well-known/openid-configuration")
        if d.get("issuer") != OIDC.issuer:
            raise RuntimeError("discovery 문서의 issuer가 설정과 다름")
        return d
    return _cached("discovery", load)


def _signing_key(jwks_uri, kid, refresh=False):
    if refresh:
        _cache.pop(("jwks", jwks_uri), None)
    keys = _cached(("jwks", jwks_uri), lambda: _get_json(jwks_uri).get("keys", []))
    for k in keys:
        if k.get("kid") == kid and k.get("use", "sig") == "sig":
            return jwt.PyJWK(k).key
    return None


def verify_id_token(raw, disc, nonce):
    """서명(제공자 JWKS)·발급자·대상·만료·nonce 확인. 실패하면 HTTPException(401)."""
    try:
        hdr = jwt.get_unverified_header(raw)
        if hdr.get("alg") not in ID_TOKEN_ALGS:
            raise jwt.InvalidAlgorithmError(hdr.get("alg"))
        key = _signing_key(disc["jwks_uri"], hdr.get("kid")) or _signing_key(disc["jwks_uri"], hdr.get("kid"), refresh=True)
        if key is None:
            raise jwt.InvalidKeyError("kid")
        claims = jwt.decode(raw, key=key, algorithms=[hdr["alg"]], audience=OIDC.client_id, issuer=disc["issuer"],
                            leeway=60, options={"require": ["exp", "iat", "iss", "aud", "sub"]})
    except jwt.PyJWTError as e:
        raise HTTPException(401, "OIDC 토큰 검증 실패") from e
    aud = claims["aud"]
    if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != OIDC.client_id:
        raise HTTPException(401, "OIDC 토큰 검증 실패")
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise HTTPException(401, "OIDC 토큰 검증 실패")
    return claims


def _claim(claims, path):
    v = claims
    for p in path.split("."):
        v = v.get(p) if isinstance(v, dict) else None
    return v


def oidc_role(claims):
    """역할 클레임을 OIDC_ROLE_MAP으로 옮겨 가장 넓은 역할. 못 정하면 기본 역할(None이면 거절)."""
    if OIDC.role_claim:
        vals = _claim(claims, OIDC.role_claim)
        vals = [vals] if isinstance(vals, str) else vals if isinstance(vals, list) else []
        got = [OIDC.role_map[v] for v in vals if isinstance(v, str) and v in OIDC.role_map]
        if got:
            return max(got, key=ROLES.index)
    return OIDC.default_role


def upsert_oidc_user(claims):
    """(issuer, sub)로 사용자를 찾거나 만든다. 역할 클레임을 쓰면 로그인할 때마다 역할을 맞춘다. 거절이면 None."""
    role = oidc_role(claims)
    username = str(claims.get("preferred_username") or claims.get("email") or claims["sub"])[:80]
    name = str(claims.get("name") or username)[:80]
    with store.pool.connection() as c:
        u = c.execute("SELECT id, role, disabled FROM users WHERE issuer = %s AND subject = %s",
                      (claims["iss"], claims["sub"])).fetchone()
        if u is None:
            if role is None:
                return None
            u = c.execute("INSERT INTO users (username, name, role, issuer, subject, last_login) "
                          "VALUES (%s, %s, %s, %s, %s, now()) RETURNING id, role, disabled",
                          (username, name, role, claims["iss"], claims["sub"])).fetchone()
        else:
            if u["disabled"]:
                return None
            if OIDC.role_claim:
                if role is None:
                    return None
                u["role"] = role
            c.execute("UPDATE users SET username = %s, name = %s, role = %s, last_login = now() WHERE id = %s",
                      (username, name, u["role"], u["id"]))
    return User(u["id"], username, name, u["role"])


def safe_next(n):
    """로그인 뒤 돌아갈 곳: 같은 사이트의 상대 경로만 (오픈 리다이렉트 방지)."""
    if not n or len(n) > MAX_NEXT or not n.startswith("/") or n.startswith("//") or "\\" in n or any(ord(ch) < 32 for ch in n):
        return "/"
    p = urllib.parse.urlsplit(n)
    return "/" if p.scheme or p.netloc else n


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


# ---------- 경로 ----------

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/config")
def auth_config():
    """로그인 화면이 켜진 방식만 보여 주도록."""
    return {"modes": sorted(MODES), "off": not MODES, "oidc_label": OIDC.label if "oidc" in MODES else None}


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


def login_failed(reason, via, account=None):
    """로그인 실패 한 행: 사유 분류만. 있는 계정이면 그 계정(id·이름), 없으면 계정 칸을 비운다 (상한 걸림)"""
    audit_event(account, "login", "failed", limited=account is None, via=via, reason=reason)


@router.post("/login")
@audit_as("login")
def login(body: LoginBody, request: Request):
    if "local" not in MODES:
        raise HTTPException(404)
    try:
        check_csrf(request)  # 로그인 CSRF(남의 계정으로 로그인시키기) 방지
        rate_limit(request, body.username)
    except HTTPException as e:
        login_failed(getattr(e, "reason", f"rejected_{e.status_code}"), "local")
        raise _audited(e) from None
    out = {}
    u = login_local(body.username, body.password, out)
    if not u:
        if out.get("reason") == "lockout":  # 이 실패로 계정이 잠겼다 — 실패 행 대신 잠금 행 하나
            audit_event(out["account"], "lockout", "locked", via="local", minutes=LOCK_MIN, fails=MAX_FAILS)
        else:
            login_failed(out.get("reason", "unknown"), "local", out.get("account"))
        raise _audited(HTTPException(401, FAIL_MSG))
    audit_event(u, "login", "ok", via="local")
    _drop_previous(request)
    resp = JSONResponse({"user": u.public()})
    set_session_cookie(resp, new_session(u.id, "local"))
    return resp


def _drop_previous(request):
    """로그인할 때 이 브라우저가 들고 있던 이전 세션은 서버에서도 지운다 (쿠키만 덮어쓰지 않고)."""
    old = request.cookies.get(COOKIE)
    if old:
        drop_session(old)


@router.post("/logout")
@audit_as("logout")
def logout(request: Request):
    token, via_cookie = _token(request)
    if via_cookie:
        check_csrf(request)  # 거절은 main의 예외 처리기가 rejected_403으로 (계정 없는 행 상한)
    # 실제로 지운 세션이 있을 때만 남긴다 — 세션 없는 로그아웃은 아무 일도 안 하므로 누구나 행을 만들지 못하게
    u = drop_session(token) if token else None
    if u:
        audit_event(u, "logout", "ok", via="bearer" if not via_cookie else "cookie")
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


@router.get("/me")
def me(user: Viewer):
    return {"user": user.public(), "modes": sorted(MODES)}


@router.get("/oidc/login")
def oidc_login(request: Request, next: str = "/"):
    if "oidc" not in MODES:
        raise HTTPException(404)
    rate_limit(request)
    d = discovery()
    state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    with store.pool.connection() as c:
        c.execute("DELETE FROM oidc_pending WHERE created_at < now() - make_interval(secs => %s)", (PENDING_SEC,))
        c.execute("INSERT INTO oidc_pending (state, nonce, verifier, next) VALUES (%s, %s, %s, %s)",
                  (state, nonce, verifier, safe_next(next)))
    q = urllib.parse.urlencode({
        "response_type": "code", "client_id": OIDC.client_id, "redirect_uri": OIDC.redirect_uri, "scope": OIDC.scopes,
        "state": state, "nonce": nonce,
        "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256"})
    ep = d["authorization_endpoint"]
    resp = RedirectResponse(ep + ("&" if "?" in ep else "?") + q, status_code=302)
    # 콜백의 state가 이 브라우저에서 시작한 로그인인지 확인하는 쿠키 (로그인 CSRF 방지)
    resp.set_cookie(STATE_COOKIE, state, max_age=PENDING_SEC, httponly=True, samesite="lax", secure=COOKIE_SECURE, path="/")
    return resp


@router.get("/oidc/callback")
@audit_as("login")
def oidc_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    if "oidc" not in MODES:
        raise HTTPException(404)
    if error:  # 제공자가 거절·취소 (error 값은 제공자가 정한 글이라 남기지 않는다)
        login_failed("oidc_error", "oidc")
        resp = RedirectResponse("/login?error=oidc", status_code=303)
        resp.delete_cookie(STATE_COOKIE, path="/")
        return resp
    mine = request.cookies.get(STATE_COOKIE)
    if not (code and state and mine and secrets.compare_digest(state, mine)):
        login_failed("oidc_state", "oidc")
        raise HTTPException(400, "로그인 요청이 맞지 않음 — 다시 시도")
    with store.pool.connection() as c:
        p = c.execute("DELETE FROM oidc_pending WHERE state = %s AND created_at > now() - make_interval(secs => %s) "
                      "RETURNING nonce, verifier, next", (state, PENDING_SEC)).fetchone()
    if not p:
        login_failed("oidc_expired", "oidc")
        raise HTTPException(400, "로그인 요청이 만료됨 — 다시 시도")
    try:
        d = discovery()
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": OIDC.redirect_uri,
                "client_id": OIDC.client_id, "code_verifier": p["verifier"]}
        tok = _post_form(d["token_endpoint"], data, (OIDC.client_id, OIDC.client_secret) if OIDC.client_secret else None)
        if "id_token" not in tok:
            raise HTTPException(502, "OIDC 응답에 id_token 없음")
        u = upsert_oidc_user(verify_id_token(tok["id_token"], d, p["nonce"]))
    except (httpx.HTTPError, HTTPException, RuntimeError, ValueError, KeyError, TypeError) as e:
        # 제공자 연결 실패·응답 오류·토큰 검증 실패 → 오류 화면 대신 로그인 화면으로 (원인은 서버 로그에만, 토큰은 남기지 않음)
        print(f"OIDC 콜백 실패: {type(e).__name__}", flush=True)
        login_failed("oidc_verify", "oidc")
        resp = RedirectResponse("/login?error=oidc", status_code=303)
        resp.delete_cookie(STATE_COOKIE, path="/")
        return resp
    if u is None:  # 역할을 못 정함·꺼진 계정 — 제공자 로그인은 됐지만 docflow가 거절
        login_failed("oidc_role", "oidc")
        resp = RedirectResponse("/login?error=role", status_code=303)
    else:
        audit_event(u, "login", "ok", via="oidc")
        _drop_previous(request)
        resp = RedirectResponse(p["next"] or "/", status_code=303)
        set_session_cookie(resp, new_session(u.id, "oidc"))
    resp.delete_cookie(STATE_COOKIE, path="/")
    return resp
