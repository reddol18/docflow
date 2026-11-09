"""인증 (app/auth.py) — DB 없이 되는 부분: 설정, 돌아갈 주소, 역할 지도, id_token 검증, CSRF, 경로 보호.
DB가 필요한 로그인·잠금·세션·OIDC 전체 흐름은 tests/test_auth_pg.py."""
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from app import auth

ISS, CID = "https://idp.test/realms/x", "docflow"


def test_parse_modes():
    assert auth.parse_modes(None, False) == {"local"}
    assert auth.parse_modes("oidc", False) == {"oidc"}
    assert auth.parse_modes(" local , oidc ", False) == {"local", "oidc"}
    assert auth.parse_modes("off", True) == frozenset()
    with pytest.raises(RuntimeError):
        auth.parse_modes("off", False)  # 끄려면 개발 표시가 함께 있어야
    for bad in ("ldap", "local,off", ","):
        with pytest.raises(RuntimeError):
            auth.parse_modes(bad, True)


@pytest.mark.parametrize("n,want", [
    ("/volumes/a", "/volumes/a"), ("/report?day_from=2026-11-01", "/report?day_from=2026-11-01"),
    (None, "/"), ("", "/"), ("https://evil.test/", "/"), ("//evil.test/", "/"), ("/\\evil.test", "/"),
    ("javascript:alert(1)", "/"), ("/a\r\nSet-Cookie: x=1", "/"), ("evil.test", "/"),
])
def test_safe_next(n, want):
    assert auth.safe_next(n) == want


@pytest.fixture
def oidc(monkeypatch):
    cfg = auth.OidcConfig(issuer=ISS, client_id=CID, redirect_uri="http://localhost:3000/api/auth/oidc/callback",
                          role_claim="realm_access.roles", role_map={"docflow-admin": "admin", "docflow-review": "reviewer"})
    monkeypatch.setattr(auth, "OIDC", cfg)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="k1", use="sig", alg="RS256")
    disc = {"issuer": ISS, "jwks_uri": ISS + "/certs", "authorization_endpoint": ISS + "/auth", "token_endpoint": ISS + "/token"}
    monkeypatch.setattr(auth, "_get_json", lambda url: {"keys": [jwk]} if url == disc["jwks_uri"] else disc)
    auth._cache.clear()
    yield key, disc
    auth._cache.clear()


def token(key, **over):
    now = int(time.time())
    claims = {"iss": ISS, "aud": CID, "sub": "u-1", "iat": now, "exp": now + 300, "nonce": "n1", **over}
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, key, algorithm="RS256", headers={"kid": "k1"})


def test_id_token_ok(oidc):
    key, disc = oidc
    assert auth.verify_id_token(token(key), disc, "n1")["sub"] == "u-1"


@pytest.mark.parametrize("over", [
    {"nonce": "other"}, {"nonce": None}, {"aud": "someone-else"}, {"iss": "https://evil.test"},
    {"exp": int(time.time()) - 3600}, {"sub": None}, {"aud": [CID, "other"], "azp": "other"},
])
def test_id_token_rejects(oidc, over):
    key, disc = oidc
    with pytest.raises(HTTPException) as e:
        auth.verify_id_token(token(key, **over), disc, "n1")
    assert e.value.status_code == 401


def test_id_token_rejects_bad_signature_and_alg(oidc):
    key, disc = oidc
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    claims = {"iss": ISS, "aud": CID, "sub": "u", "iat": now, "exp": now + 60, "nonce": "n1"}
    forged = [
        jwt.encode(claims, other, algorithm="RS256", headers={"kid": "k1"}),  # 남의 키로 서명
        jwt.encode(claims, "secret-0123456789-0123456789-0123", algorithm="HS256", headers={"kid": "k1"}),  # 대칭키로 바꿔치기
        jwt.encode(claims, key, algorithm="RS256", headers={"kid": "unknown"}),
        jwt.encode(claims, None, algorithm="none"),
    ]
    for raw in forged:
        with pytest.raises(HTTPException):
            auth.verify_id_token(raw, disc, "n1")


def test_oidc_role(oidc):
    assert auth.oidc_role({"realm_access": {"roles": ["x", "docflow-review"]}}) == "reviewer"
    assert auth.oidc_role({"realm_access": {"roles": ["docflow-review", "docflow-admin"]}}) == "admin"
    assert auth.oidc_role({"realm_access": {"roles": ["admin"]}}) is None  # 지도에 없는 값은 무시 → 기본(거절)
    assert auth.oidc_role({}) is None
    auth.OIDC.default_role = "viewer"  # 명시하면 열린다
    assert auth.oidc_role({}) == "viewer"


def test_oidc_config_check(monkeypatch):
    ok = {"issuer": ISS, "client_id": CID, "redirect_uri": "https://docflow.test/api/auth/oidc/callback"}
    auth.OidcConfig(**ok).check()
    assert auth.OidcConfig().default_role is None
    monkeypatch.delenv("OIDC_DEFAULT_ROLE", raising=False)
    assert auth.OidcConfig.from_env().default_role is None  # 환경 변수 기본값도 거절
    bad = [
        {"issuer": ISS},  # 필수값 없음
        {**ok, "role_map": {"g": "root"}},  # 없는 역할
        {**ok, "issuer": "http://idp.test/realms/x"},  # http 발급자
    ]
    for kw in bad:
        with pytest.raises(RuntimeError):
            auth.OidcConfig(**kw).check()
    auth.OidcConfig(**{**ok, "issuer": "http://keycloak:8080/realms/x", "allow_http": True}).check()
    with pytest.raises(RuntimeError):
        auth.OidcConfig(**ok).check(cookie_secure=False)  # https 콜백인데 Secure 쿠키를 끔


def test_check_discovery_hosts():
    """discovery 문서의 jwks_uri·token_endpoint·authorization_endpoint는 issuer와 같은 scheme·호스트·포트만 (https://github.com/reddol18/docflow/issues/48)"""
    good = {"issuer": ISS, "jwks_uri": ISS + "/protocol/openid-connect/certs", "token_endpoint": "https://IDP.test:443/t",
            "authorization_endpoint": ISS + "/auth?prompt=login", "userinfo_endpoint": "https://other.test/u"}  # 안 쓰는 주소는 보지 않음
    assert auth.check_discovery(good, ISS) is good
    for k in auth.DISCOVERY_URLS:
        for bad in ("https://evil.test/x", "https://idp.test.evil.test/x", "https://idp.test@evil.test/x", "https://idp.test:444/x",
                    "http://idp.test/x", "//idp.test/x", "/relative", "https://idp.test /x", "", None, 1, ["https://idp.test/x"]):
            with pytest.raises(RuntimeError):
                auth.check_discovery({**good, k: bad}, ISS)
        with pytest.raises(RuntimeError):
            auth.check_discovery({k2: v for k2, v in good.items() if k2 != k}, ISS)  # 빠짐
    with pytest.raises(RuntimeError):
        auth.check_discovery({**good, "issuer": "https://evil.test/realms/x"}, ISS)
    h = "http://keycloak:8080/realms/x"  # 클러스터 안 시험 제공자 (OIDC_ALLOW_HTTP=1)
    local = {"issuer": h, **{k: h + "/" + k for k in auth.DISCOVERY_URLS}}
    with pytest.raises(RuntimeError):
        auth.check_discovery(local, h)
    assert auth.check_discovery(local, h, allow_http=True) is local
    with pytest.raises(RuntimeError):
        auth.check_discovery({**local, "jwks_uri": "http://keycloak:8081/certs"}, h, allow_http=True)


def test_rate_limit(monkeypatch):
    monkeypatch.setattr(auth, "USER_RATE", (3, 300))
    monkeypatch.setattr(auth, "LOGIN_RATE", (2, 300))
    monkeypatch.setattr(auth, "_hits", {})

    class R:
        def __init__(self, ip, xff=None):
            self.client = type("C", (), {"host": ip})()
            self.headers = {"x-forwarded-for": xff} if xff else {}

    for _ in range(3):
        auth.rate_limit(R("127.0.0.1"), "Admin")
    with pytest.raises(HTTPException) as e:
        auth.rate_limit(R("127.0.0.1", xff="1.2.3.4"), "admin")  # 대소문자·XFF를 바꿔도 같은 이름 창
    assert e.value.status_code == 429
    for i in range(20):
        auth.rate_limit(R("127.0.0.1", xff=f"9.9.9.{i}"), f"u{i}")  # 전체 창은 없다 (남이 채워 모두를 막지 못하게)
    assert not any(k.startswith("ip:") for k in auth._hits)  # 프록시를 믿지 않으면 IP 창을 쓰지 않는다
    monkeypatch.setattr(auth, "_hits", {})
    monkeypatch.setattr(auth, "TRUST_PROXY", True)
    auth.rate_limit(R("127.0.0.1", xff="9.9.9.9, 10.0.0.9"))
    assert "ip:10.0.0.9" in auth._hits


def test_docs_disabled(main_app):
    from fastapi.testclient import TestClient
    app, _ = main_app
    c = TestClient(app)
    assert c.get("/openapi.json").status_code == 404 and c.get("/docs").status_code == 404


class FakeReq:
    def __init__(self, headers):
        self.headers = headers


def test_csrf():
    auth.check_csrf(FakeReq({"x-docflow-csrf": "1"}))
    auth.check_csrf(FakeReq({"sec-fetch-site": "same-origin"}))
    for h in ({}, {"sec-fetch-site": "cross-site"}, {"sec-fetch-site": "same-site"}, {"x-docflow-csrf": "0"}):
        with pytest.raises(HTTPException) as e:
            auth.check_csrf(FakeReq(h))
        assert e.value.status_code == 403


# ---------- 경로 보호: 앱의 모든 경로를 돌며 확인 → 새 경로에서 보호를 빠뜨리면 실패 ----------

ADMIN_ROUTES = {("POST", "/volumes/{vol}/pages"), ("POST", "/volumes/{vol}/run"), ("POST", "/volumes/{vol}/resume"),
                ("GET", "/export.xlsx"), ("DELETE", "/volumes/{vol}"),  # 납품 출력·일괄 삭제 (https://github.com/reddol18/docflow/issues/12)
                ("GET", "/export/errors.xlsx")}  # 검수 오류목록 (https://github.com/reddol18/docflow/issues/88)


def _roles(dependant):
    out = []
    for d in dependant.dependencies:
        if getattr(d.call, "role", None):
            out.append(d.call.role)
        out += _roles(d)
    return out


@pytest.fixture(scope="module")
def main_app():
    from fastapi.routing import APIRoute

    from app import main
    return main.app, [r for r in main.app.routes if isinstance(r, APIRoute)]


def test_every_route_has_role(main_app):
    app, routes = main_app
    assert any(d.dependency is auth.guard for d in app.router.dependencies)
    for r in routes:
        if r.path in auth.PUBLIC:
            continue
        roles = _roles(r.dependant)
        assert roles, f"{r.path} 역할 없음"
        need = max(roles, key=auth.ROLES.index)
        for m in r.methods:
            if (m, r.path) in ADMIN_ROUTES:
                assert need == "admin", (m, r.path)
            elif m not in auth.SAFE_METHODS:
                assert auth.role_at_least(need, "reviewer"), f"{m} {r.path}: 바꾸는 요청인데 {need}"


def _url(path):
    return path.replace("{", "").replace("}", "")  # /volumes/{vol} → /volumes/vol


def test_every_route_401_without_login(main_app, monkeypatch):
    from fastapi.testclient import TestClient
    app, routes = main_app
    monkeypatch.setattr(auth, "MODES", frozenset({"local"}))
    monkeypatch.setattr(auth, "session_user", lambda t: None)
    c = TestClient(app)  # with 없이 → lifespan(DB 연결)을 돌리지 않는다
    for r in routes:
        if r.path in auth.PUBLIC:
            continue
        for m in r.methods:
            res = c.request(m, _url(r.path), headers={"x-docflow-csrf": "1"})
            assert res.status_code == 401, f"{m} {r.path} → {res.status_code}"
            res = c.request(m, _url(r.path), headers={"authorization": "Bearer bogus"})
            assert res.status_code == 401, f"{m} {r.path} (가짜 토큰) → {res.status_code}"


def test_role_and_csrf_enforced(main_app, monkeypatch):
    from fastapi.testclient import TestClient
    app, routes = main_app
    monkeypatch.setattr(auth, "MODES", frozenset({"local"}))
    who = {}
    monkeypatch.setattr(auth, "session_user", lambda t: who.get(t))
    who["v"] = auth.User(1, "v", "v", "viewer")
    who["r"] = auth.User(2, "r", "r", "reviewer")
    c = TestClient(app)
    for r in routes:
        for m in r.methods - set(auth.SAFE_METHODS):
            if r.path in auth.PUBLIC:
                continue
            res = c.request(m, _url(r.path), headers={"authorization": "Bearer v"})
            assert res.status_code == 403, f"viewer {m} {r.path} → {res.status_code}"
            if (m, r.path) in ADMIN_ROUTES:
                res = c.request(m, _url(r.path), headers={"authorization": "Bearer r"})
                assert res.status_code == 403, f"reviewer {m} {r.path} → {res.status_code}"
    # 쿠키로 온 바꾸는 요청은 CSRF 표시가 없으면 거절 (역할이 맞아도)
    c.cookies.set(auth.COOKIE, "r")
    assert c.post("/volumes/vol/timings", json=[]).status_code == 403
