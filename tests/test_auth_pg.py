"""인증을 실제 Postgres에 돌린다: local 로그인·잠금·세션·로그아웃, OIDC 전체 흐름(가짜 제공자), 수정 이력.
TEST_PG_URL이 없으면 건너뛴다. 표를 지우므로 운영 DB에 대고 돌리지 말 것."""
import base64
import hashlib
import io
import json
import os
import time
import urllib.parse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app import auth

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_PG_URL"), reason="TEST_PG_URL 없음")
CSRF = {"x-docflow-csrf": "1"}
PW = "correct-horse-battery"
ISS, CID = "https://idp.test/realms/x", "docflow"


@pytest.fixture
def db(pg_store):
    with pg_store.pool.connection() as c:
        for t in ("sessions", "oidc_pending", "edits", "review_time", "users"):
            c.execute(f"DELETE FROM {t}")  # noqa: S608 — 고정된 표 이름
    return pg_store


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setattr(auth, "MODES", frozenset({"local", "oidc"}))
    app = FastAPI(dependencies=[Depends(auth.guard)])
    app.include_router(auth.router)

    @app.get("/p")
    def p(u: auth.Viewer):
        return u.public()

    @app.post("/w")
    def w(u: auth.Reviewer):
        return {"ok": u.username}

    return TestClient(app, follow_redirects=False)


def add_user(monkeypatch, name, role, pw=PW):
    from app import users
    monkeypatch.setattr("sys.stdin", io.StringIO(pw + "\n"))
    users.main(["add", name, "--role", role, "--password-stdin"])


def login(c, name, pw=PW, headers=CSRF):
    return c.post("/auth/login", json={"username": name, "password": pw}, headers=headers)


def test_local_login_session_logout(client, db, monkeypatch):
    add_user(monkeypatch, "alice", "reviewer")
    with db.pool.connection() as c:
        h = c.execute("SELECT password_hash FROM users WHERE username = 'alice'").fetchone()["password_hash"]
    assert h.startswith("$argon2id$") and PW not in h

    assert client.get("/p").status_code == 401
    assert login(client, "alice", headers={}).status_code == 403  # CSRF 표시 없는 로그인
    r = login(client, "alice")
    assert r.status_code == 200 and r.json()["user"]["role"] == "reviewer"
    sc = r.headers["set-cookie"].lower()
    assert "httponly" in sc and "samesite=lax" in sc
    token = client.cookies.get(auth.COOKIE)
    with db.pool.connection() as c:
        stored = c.execute("SELECT token_hash FROM sessions").fetchall()
    assert [s["token_hash"] for s in stored] == [hashlib.sha256(token.encode()).hexdigest()]  # 토큰 원문은 저장 안 함

    assert client.get("/auth/me").json()["user"]["username"] == "alice"
    assert client.post("/w").status_code == 403  # 쿠키 + CSRF 표시 없음
    assert client.post("/w", headers=CSRF).json() == {"ok": "alice"}
    assert client.post("/w", headers={"sec-fetch-site": "cross-site"}).status_code == 403

    client.post("/auth/logout", headers=CSRF)
    assert client.get("/p").status_code == 401
    assert client.get("/p", headers={"authorization": f"Bearer {token}"}).status_code == 401  # 로그아웃한 토큰 재사용


def test_bearer_and_expiry(client, db, monkeypatch):
    add_user(monkeypatch, "bob", "viewer")
    login(client, "bob")
    token = client.cookies.get(auth.COOKIE)
    client.cookies.clear()
    hdr = {"authorization": f"Bearer {token}"}
    assert client.get("/p", headers=hdr).status_code == 200
    assert client.post("/w", headers=hdr).status_code == 403  # viewer
    with db.pool.connection() as c:
        c.execute("UPDATE sessions SET expires_at = now() - interval '1 second'")
    assert client.get("/p", headers=hdr).status_code == 401


def test_lockout_and_generic_message(client, db, monkeypatch):
    add_user(monkeypatch, "carol", "viewer")
    nobody = login(client, "nobody", "whatever-123")
    wrong = login(client, "carol", "wrong-password")
    assert nobody.status_code == wrong.status_code == 401
    assert nobody.json() == wrong.json()  # 계정이 있는지 드러내지 않음
    for _ in range(auth.MAX_FAILS - 1):
        login(client, "carol", "wrong-password")
    locked = login(client, "carol")  # 맞는 비밀번호라도 잠김
    assert locked.status_code == 401 and locked.json() == wrong.json()
    from app import users
    users.main(["unlock", "carol"])
    assert login(client, "carol").status_code == 200


def test_disable_drops_sessions(client, db, monkeypatch):
    add_user(monkeypatch, "dave", "reviewer")
    login(client, "dave")
    assert client.get("/p").status_code == 200
    from app import users
    users.main(["disable", "dave"])
    assert client.get("/p").status_code == 401
    assert login(client, "dave").status_code == 401


def test_short_password_rejected(db, monkeypatch):
    with pytest.raises(SystemExit):
        add_user(monkeypatch, "erin", "viewer", pw="short")


def test_local_off_hides_login(client, monkeypatch):
    monkeypatch.setattr(auth, "MODES", frozenset({"oidc"}))
    assert login(client, "x").status_code == 404
    assert client.get("/auth/config").json()["modes"] == ["oidc"]


# ---------- OIDC 전체 흐름 (가짜 제공자) ----------

@pytest.fixture
def idp(monkeypatch):
    cfg = auth.OidcConfig(issuer=ISS, client_id=CID, client_secret="s3cret",
                          redirect_uri="http://localhost:3000/api/auth/oidc/callback",
                          role_claim="groups", role_map={"docflow-admin": "admin"}, default_role="viewer")
    monkeypatch.setattr(auth, "OIDC", cfg)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="k1", use="sig")
    disc = {"issuer": ISS, "jwks_uri": ISS + "/certs", "authorization_endpoint": ISS + "/auth", "token_endpoint": ISS + "/token"}
    monkeypatch.setattr(auth, "_get_json", lambda url: {"keys": [jwk]} if url == disc["jwks_uri"] else disc)
    auth._cache.clear()
    seen = {}
    claims = {"sub": "sso-1", "preferred_username": "kim", "name": "김검토", "groups": ["docflow-admin"]}

    def post_form(url, data, basic):
        seen.update(data=data, basic=basic, url=url)
        now = int(time.time())
        idt = jwt.encode({"iss": ISS, "aud": CID, "iat": now, "exp": now + 300, "nonce": seen["nonce"], **claims},
                         key, algorithm="RS256", headers={"kid": "k1"})
        return {"id_token": idt, "access_token": "a"}

    monkeypatch.setattr(auth, "_post_form", post_form)
    yield seen, claims
    auth._cache.clear()


def start(client, seen, nxt="/volumes/a"):
    r = client.get("/auth/oidc/login", params={"next": nxt})
    assert r.status_code == 302
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r.headers["location"]).query))
    assert r.headers["location"].startswith(ISS + "/auth?")
    assert q["code_challenge_method"] == "S256" and q["client_id"] == CID
    seen["nonce"], seen["challenge"] = q["nonce"], q["code_challenge"]
    return q["state"]


def test_oidc_flow(client, db, idp):
    seen, _ = idp
    state = start(client, seen)
    r = client.get("/auth/oidc/callback", params={"code": "c1", "state": state})
    assert r.status_code == 303 and r.headers["location"] == "/volumes/a"
    assert client.get("/p").json() == {"username": "kim", "name": "김검토", "role": "admin"}
    v = seen["data"]["code_verifier"]  # PKCE: 보낸 verifier가 처음 challenge와 맞아야
    assert base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode() == seen["challenge"]
    assert seen["basic"] == (CID, "s3cret")
    with db.pool.connection() as c:
        u = c.execute("SELECT issuer, subject, password_hash FROM users").fetchone()
    assert (u["issuer"], u["subject"], u["password_hash"]) == (ISS, "sso-1", None)
    # 같은 state 다시 쓰기 → 거절
    assert client.get("/auth/oidc/callback", params={"code": "c1", "state": state}).status_code == 400


def test_oidc_state_must_match_browser(client, db, idp):
    seen, _ = idp
    state = start(client, seen)
    client.cookies.clear()  # 다른 브라우저: state 쿠키가 없거나 다름
    assert client.get("/auth/oidc/callback", params={"code": "c", "state": state}).status_code == 400
    client.cookies.set(auth.STATE_COOKIE, "someone-elses-state")
    assert client.get("/auth/oidc/callback", params={"code": "c", "state": state}).status_code == 400


def test_oidc_open_redirect_and_role_refresh(client, db, idp):
    seen, claims = idp
    state = start(client, seen, nxt="https://evil.test/")
    assert client.get("/auth/oidc/callback", params={"code": "c", "state": state}).headers["location"] == "/"
    claims["groups"] = []  # 제공자에서 관리자 그룹이 빠지면 다음 로그인 때 역할도 내려간다
    state = start(client, seen)
    client.get("/auth/oidc/callback", params={"code": "c", "state": state})
    assert client.get("/p").json()["role"] == "viewer"
    auth.OIDC.default_role = None  # 기본 역할 없음 → 그룹 없는 사람은 거절
    state = start(client, seen)
    assert client.get("/auth/oidc/callback", params={"code": "c", "state": state}).headers["location"] == "/login?error=role"


def test_oidc_off(client, monkeypatch):
    monkeypatch.setattr(auth, "MODES", frozenset({"local"}))
    assert client.get("/auth/oidc/login").status_code == 404
    assert client.get("/auth/oidc/callback", params={"code": "c", "state": "s"}).status_code == 404


def test_edit_history(db, monkeypatch):
    add_user(monkeypatch, "fay", "reviewer")
    with db.pool.connection() as c:
        uid = c.execute("SELECT id FROM users WHERE username = 'fay'").fetchone()["id"]
    db.add_edits("v1", "v1-0001", [("date", "1999.1.2", "1999.1.20"), ("title", None, "제목")], uid, "review")
    db.add_edits("v1", "v1-0001", [("date", "1999.1.20", "1999.1.21")], None, "edit")
    h = db.edit_history("v1", "v1-0001")
    assert [(r["field"], r["old"], r["new"], r["source"], r["username"]) for r in h] == [
        ("date", "1999.1.2", "1999.1.20", "review", "fay"), ("title", None, "제목", "review", "fay"),
        ("date", "1999.1.20", "1999.1.21", "edit", None)]
