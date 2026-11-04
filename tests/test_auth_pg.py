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
    monkeypatch.setattr(auth, "COOKIE_SECURE", False)  # 시험 클라이언트는 http → Secure 쿠키를 다시 보내지 않는다
    monkeypatch.setattr(auth, "_hits", {})  # 시험마다 IP 제한 창을 비운다
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


def test_lockout_holds_under_concurrency(db, monkeypatch):
    """동시에 여러 번 틀려도 잠금 전까지 실제 비밀번호 검증은 MAX_FAILS번만 (시도 자리를 원자적으로 잡는다)."""
    from concurrent.futures import ThreadPoolExecutor
    add_user(monkeypatch, "ivan", "viewer")
    real = []
    orig = auth._verify

    def counting(h, pw):
        if h != auth._dummy_hash():
            real.append(1)
        return orig(h, pw)

    monkeypatch.setattr(auth, "_verify", counting)
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(lambda _: auth.login_local("ivan", "wrong-password-x"), range(20)))
    assert not any(res)
    assert len(real) <= auth.MAX_FAILS
    assert auth.login_local("ivan", PW) is None  # 잠김


def test_login_rate_limited(client, db, monkeypatch):
    monkeypatch.setattr(auth, "USER_RATE", (3, 300))
    monkeypatch.setattr(auth, "_hits", {})
    codes = [login(client, "nobody", "x-123456789").status_code for _ in range(4)]
    assert codes == [401, 401, 401, 429]  # 같은 이름
    # X-Forwarded-For를 바꿔도 이름 창은 그대로 (프록시를 믿지 않음)
    assert login(client, "nobody", "x-123456789", headers={**CSRF, "x-forwarded-for": "10.9.9.9"}).status_code == 429
    # 전체 창이 없으므로 남이 이름을 바꿔 가며 보내도 다른 사람의 로그인은 막히지 않는다
    for i in range(5):
        login(client, f"u{i}", "x-123456789")
    add_user(monkeypatch, "nora", "viewer")
    assert login(client, "nora").status_code == 200


def test_stuck_attempt_counter_heals(client, db, monkeypatch):
    """시도 자리를 잡은 뒤 요청이 죽어 failed가 MAX_FAILS에 남아도 영원히 막히지 않는다 — 이전에 잠겼던 계정도."""
    add_user(monkeypatch, "kai", "viewer")
    for past_lock in ("NULL", "now() - interval '1 hour'"):
        with db.pool.connection() as c:
            c.execute(f"UPDATE users SET failed = %s, locked_until = {past_lock}, "  # noqa: S608 — 고정된 두 값
                      "attempt_at = now() WHERE username = %s", (auth.MAX_FAILS, "kai"))
        assert login(client, "kai").status_code == 401  # 방금 잡힌 자리(검증 중일 수 있음)는 그대로 존중
        with db.pool.connection() as c:
            c.execute("UPDATE users SET attempt_at = now() - make_interval(secs => %s) WHERE username = %s",
                      (auth.CLAIM_SEC + 1, "kai"))
        assert login(client, "kai").status_code == 200  # 오래된 자리는 남은 것으로 보지 않음


def test_inflight_fifth_attempt_does_not_lock(client, db, monkeypatch):
    """5번째 시도가 검증 중일 때 들어온 요청이 계정을 잠그지 않는다 (예전 자가 회복 UPDATE의 문제)."""
    add_user(monkeypatch, "ona", "viewer")
    with db.pool.connection() as c:
        c.execute("UPDATE users SET failed = %s, attempt_at = now() WHERE username = %s", (auth.MAX_FAILS, "ona"))
    login(client, "ona", "wrong-password-z")
    with db.pool.connection() as c:
        assert c.execute("SELECT locked_until FROM users WHERE username = %s", ("ona",)).fetchone()["locked_until"] is None


def test_password_stdin_strips_crlf(db, monkeypatch):
    add_user(monkeypatch, "lee", "viewer", pw=PW + "\r")
    assert auth.login_local("lee", PW) is not None


def test_password_stdin_strips_bom(db, monkeypatch):
    add_user(monkeypatch, "bom", "viewer", pw="\ufeff" + PW + "\r")  # Windows PowerShell 5.1 파이프
    assert auth.login_local("bom", PW) is not None


def test_login_drops_previous_session(client, db, monkeypatch):
    add_user(monkeypatch, "jin", "viewer")
    login(client, "jin")
    login(client, "jin")
    with db.pool.connection() as c:
        assert c.execute("SELECT count(*) AS n FROM sessions").fetchone()["n"] == 1


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


def test_local_only_mode(client, db, monkeypatch):
    monkeypatch.setattr(auth, "MODES", frozenset({"local"}))
    add_user(monkeypatch, "gus", "viewer")
    assert client.get("/auth/config").json()["modes"] == ["local"]
    assert login(client, "gus").status_code == 200
    assert client.get("/p").status_code == 200
    assert client.post("/auth/logout", headers=CSRF).status_code == 200
    assert client.get("/p").status_code == 401


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


def test_oidc_only_mode_login_logout(client, db, idp, monkeypatch):
    monkeypatch.setattr(auth, "MODES", frozenset({"oidc"}))
    seen, _ = idp
    state = start(client, seen)
    assert client.get("/auth/oidc/callback", params={"code": "c", "state": state}).status_code == 303
    assert client.get("/p").status_code == 200
    assert client.post("/auth/logout", headers=CSRF).status_code == 200
    assert client.get("/p").status_code == 401
    with db.pool.connection() as c:
        assert c.execute("SELECT count(*) AS n FROM sessions").fetchone()["n"] == 0  # 서버에서도 폐기


def test_cli_can_disable_sso_user(client, db, idp):
    from app import users
    seen, _ = idp
    state = start(client, seen)
    client.get("/auth/oidc/callback", params={"code": "c", "state": state})
    assert client.get("/p").status_code == 200
    with pytest.raises(SystemExit):
        users.main(["disable", "kim"])  # --sso 없이는 local 계정만 찾는다
    users.main(["disable", "kim", "--sso"])
    assert client.get("/p").status_code == 401  # 세션이 바로 끊긴다
    state = start(client, seen)
    assert client.get("/auth/oidc/callback", params={"code": "c", "state": state}).headers["location"] == "/login?error=role"


def test_oidc_provider_down_goes_to_login(client, db, idp, monkeypatch):
    import httpx
    seen, _ = idp
    state = start(client, seen)

    def down(*a, **k):
        raise httpx.ConnectError("unreachable")

    monkeypatch.setattr(auth, "_post_form", down)
    r = client.get("/auth/oidc/callback", params={"code": "c", "state": state})
    assert r.status_code == 303 and r.headers["location"] == "/login?error=oidc"
    assert auth.STATE_COOKIE in r.headers.get("set-cookie", "")  # state 쿠키 지움


def test_oidc_error_param_clears_state(client, db, idp):
    seen, _ = idp
    start(client, seen)
    r = client.get("/auth/oidc/callback", params={"error": "access_denied"})
    assert r.status_code == 303 and r.headers["location"] == "/login?error=oidc"
    sc = r.headers.get("set-cookie", "")
    assert auth.STATE_COOKIE in sc and ("max-age=0" in sc.lower() or "expires=" in sc.lower())  # 지우는 쿠키


def test_last_seen_updated_at_most_every_5_minutes(client, db, monkeypatch):
    add_user(monkeypatch, "mia", "viewer")
    login(client, "mia")
    with db.pool.connection() as c:
        c.execute("UPDATE sessions SET last_seen = now() - interval '1 minute'")
        before = c.execute("SELECT last_seen FROM sessions").fetchone()["last_seen"]
    client.get("/p")
    with db.pool.connection() as c:
        assert c.execute("SELECT last_seen FROM sessions").fetchone()["last_seen"] == before  # 5분 안이면 쓰지 않음
        c.execute("UPDATE sessions SET last_seen = now() - interval '6 minutes'")
    client.get("/p")
    with db.pool.connection() as c:
        assert c.execute("SELECT last_seen > now() - interval '1 minute' AS fresh FROM sessions").fetchone()["fresh"]


def test_oidc_off(client, monkeypatch):
    monkeypatch.setattr(auth, "MODES", frozenset({"local"}))
    assert client.get("/auth/oidc/login").status_code == 404
    assert client.get("/auth/oidc/callback", params={"code": "c", "state": "s"}).status_code == 404


def test_edit_history(db, monkeypatch):
    add_user(monkeypatch, "fay", "reviewer")
    with db.pool.connection() as c:
        uid = c.execute("SELECT id FROM users WHERE username = 'fay'").fetchone()["id"]
    db.add_edits("v1", [("v1-0001", "date", "1999.1.2", "1999.1.20"), ("v1-0001", "title", None, "제목")], uid, "review")
    db.add_edits("v1", [("v1-0001", "date", "1999.1.20", "1999.1.21")], None, "edit")
    h = db.edit_history("v1", "v1-0001")
    assert [(r["field"], r["old"], r["new"], r["source"], r["username"]) for r in h] == [
        ("date", "1999.1.2", "1999.1.20", "review", "fay"), ("title", None, "제목", "review", "fay"),
        ("date", "1999.1.20", "1999.1.21", "edit", None)]


def test_review_submit_records_edits_and_user(db, monkeypatch):
    """검토 제출 경로(/review/{vol}, /review/{vol}/json 공통)가 고친 칸마다 이전 값·새 값·사용자를 남긴다."""
    from app import main
    add_user(monkeypatch, "hana", "reviewer")
    with db.pool.connection() as c:
        uid = c.execute("SELECT id FROM users WHERE username = 'hana'").fetchone()["id"]
    user = auth.User(uid, "hana", "hana", "reviewer")
    flagged = [{"doc_id": "v2-0001", "flags": {"date": ["확신도"]},
                "fields": {"docno": "가나 100-1", "date": "1999.3.4", "recipient": "가나시장", "title": "t"}}]
    fixes = main.clean_fixes(flagged, [("v2-0001", "date", "1999.3.14"), ("v2-0001", "title", "t"), ("nope", "date", "x")])
    main.record_review("v2", flagged, fixes, user)
    h = db.edit_history("v2", "v2-0001")
    assert [(r["field"], r["old"], r["new"], r["source"], r["username"]) for r in h] == [
        ("date", "1999.3.4", "1999.3.14", "review", "hana")]
    db.add_review_times("v2", [{"doc_id": "v2-0001", "mode": "review", "active_ms": 5000}], uid)
    with db.pool.connection() as c:
        assert c.execute("SELECT user_id FROM review_time WHERE vol = 'v2'").fetchone()["user_id"] == uid
