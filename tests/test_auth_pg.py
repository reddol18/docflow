"""인증을 실제 Postgres에 돌린다: local 로그인·잠금·세션·로그아웃, OIDC 전체 흐름(가짜 제공자), 수정 이력.
TEST_PG_URL이 없으면 건너뛴다. 표를 지우므로 운영 DB에 대고 돌리지 말 것."""
import base64
import collections
import hashlib
import io
import json
import os
import queue
import threading
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
    auth.audit_flush()  # 앞 시험이 미뤄 둔 감사 행이 이 시험의 표에 끼지 않게
    with pg_store.pool.connection() as c:
        for t in ("sessions", "oidc_pending", "edits", "review_time", "audit", "users"):
            c.execute(f"DELETE FROM {t}")  # noqa: S608 — 고정된 표 이름
    return pg_store


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setattr(auth, "MODES", frozenset({"local", "oidc"}))
    monkeypatch.setattr(auth, "_hits", {})  # 시험마다 IP 제한 창을 비운다
    monkeypatch.setattr(auth, "_anon", collections.deque())  # 감사 로그 계정 없는 행 상한 창도
    monkeypatch.setattr(auth, "_anon_state", {"dropped": 0, "overflow": 0})
    app = FastAPI(dependencies=[Depends(auth.guard)])
    app.include_router(auth.router)

    @app.get("/p")
    def p(u: auth.Viewer):
        return u.public()

    @app.post("/w")
    def w(u: auth.Reviewer):
        return {"ok": u.username}

    # https 주소 — 운영과 같이 __Host-·Secure 쿠키를 받아 다시 보낸다 (https://github.com/reddol18/docflow/issues/48)
    return TestClient(app, base_url="https://docflow.test", follow_redirects=False)


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
    sc = r.headers["set-cookie"]
    assert sc.startswith(auth.COOKIE + "=") and auth.COOKIE == "__Host-docflow_session"
    attrs = {a.strip().split("=")[0].lower() for a in sc.split(";")[1:]}
    assert {"httponly", "secure", "path"} <= attrs and "domain" not in attrs and "; Path=/" in sc  # __Host- 조건
    assert "samesite=lax" in sc.lower()
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


def audit_rows(db):
    auth.audit_flush()  # 로그인·로그아웃 행은 감사 쓰기 스레드가 응답 뒤에 쓴다
    with db.pool.connection() as c:
        return c.execute("SELECT * FROM audit ORDER BY id").fetchall()


def test_auth_events_audited(client, db, monkeypatch):
    """로그인 성공·실패·로그아웃·잠금 각 1회 → 감사 행 4개, 비밀번호·세션 토큰 문자열 0건
    (https://github.com/reddol18/docflow/issues/63)"""
    monkeypatch.setattr(auth, "MAX_FAILS", 2)  # 두 번째 실패에서 잠금 → 실패 1회 + 잠금 1회
    add_user(monkeypatch, "alice", "reviewer")
    add_user(monkeypatch, "bob", "viewer")
    assert login(client, "alice").status_code == 200
    token = client.cookies.get(auth.COOKIE)
    assert client.post("/auth/logout", headers=CSRF).status_code == 200
    assert login(client, "bob", "wrong-password-1").status_code == 401
    r = login(client, "bob", "wrong-password-2")
    assert r.status_code == 401 and r.json()["detail"] == auth.FAIL_MSG  # 응답은 잠겼는지 드러내지 않는다

    rows = audit_rows(db)
    assert [(r["action"], r["result"], r["username"], r["info"]) for r in rows] == [
        ("login", "ok", "alice", {"via": "local"}),
        ("logout", "ok", "alice", {"via": "cookie"}),
        ("login", "failed", "bob", {"via": "local", "reason": "bad_password"}),
        ("lockout", "locked", "bob", {"via": "local", "minutes": auth.LOCK_MIN, "fails": 2})]
    assert all(r["user_id"] and r["ts"] for r in rows)
    text = json.dumps(rows, ensure_ascii=False, default=str)
    for bad in (PW, "wrong-password-1", "wrong-password-2", token, hashlib.sha256(token.encode()).hexdigest()):
        assert bad not in text


def test_failed_login_reasons(client, db, monkeypatch):
    """실패 사유 분류. 없는 계정으로 시도한 아이디는 남기지 않는다 (아이디 칸에 비밀번호를 넣은 것일 수 있다)"""
    add_user(monkeypatch, "carol", "viewer")
    typo = "correct-horse-battery-typed-as-id"
    assert login(client, typo).status_code == 401
    assert login(client, "carol", headers={}).status_code == 403  # CSRF 표시 없음
    with db.pool.connection() as c:
        c.execute("UPDATE users SET locked_until = now() + interval '5 minutes' WHERE username = 'carol'")
    assert login(client, "carol").status_code == 401  # 잠긴 동안 (맞는 비밀번호여도)
    with db.pool.connection() as c:
        c.execute("UPDATE users SET locked_until = NULL, disabled = true WHERE username = 'carol'")
    assert login(client, "carol").status_code == 401
    monkeypatch.setattr(auth, "USER_RATE", (0, 300))
    assert login(client, "carol").status_code == 429
    rows = audit_rows(db)
    assert [(r["action"], r["result"], r["username"], r["info"]["reason"]) for r in rows] == [
        ("login", "failed", None, "unknown_user"), ("login", "failed", None, "csrf"),
        ("login", "failed", "carol", "locked"), ("login", "failed", "carol", "disabled"),
        ("login", "failed", None, "rate_limited")]
    assert typo not in json.dumps(rows, ensure_ascii=False, default=str)


def test_failed_login_time_independent_of_account(client, db, monkeypatch):
    """실패 응답이 계정 유무·감사 상한에 따라 달라지지 않는다 (https://github.com/reddol18/docflow/issues/63 리뷰):
    요청 스레드의 DB 왕복 수가 없는 계정·잠긴 계정·꺼진 계정·상한이 찬 때 모두 같고, 감사 쓰기를 기다리지 않는다"""
    add_user(monkeypatch, "dave", "viewer")
    add_user(monkeypatch, "erin", "viewer")
    with db.pool.connection() as c:
        c.execute("UPDATE users SET locked_until = now() + interval '5 minutes' WHERE username = 'dave'")
        c.execute("UPDATE users SET disabled = true WHERE username = 'erin'")
    real = db.pool.connection
    trips = []

    def counting(*a, **k):
        if threading.current_thread().name != "audit-writer":
            trips.append(1)
        return real(*a, **k)

    monkeypatch.setattr(db.pool, "connection", counting)
    hold = threading.Event()
    auth.audit_later(hold.wait, 10)  # 감사 쓰기 스레드를 붙잡아 둔다 — 응답은 그래도 바로 와야 한다
    counts = {}
    for name in ("nobody-x", "dave", "erin"):
        trips.clear()
        assert login(client, name).status_code == 401
        counts[name] = len(trips)
    assert not hold.is_set() and auth._audit_q.unfinished_tasks == 4  # 세 응답 모두 감사 쓰기 전에 왔다
    hold.set()
    auth.audit_flush()
    monkeypatch.setattr(auth, "ANON_AUDIT", (0, 60))  # 상한이 찬 때
    trips.clear()
    assert login(client, "nobody-y").status_code == 401
    counts["capped"] = len(trips)
    assert set(counts.values()) == {1}, counts  # 자리잡기 UPDATE 한 번
    monkeypatch.setattr(db.pool, "connection", real)
    rows = audit_rows(db)
    assert [(r["username"], r["info"]["reason"]) for r in rows] == [
        (None, "unknown_user"), ("dave", "locked"), ("erin", "disabled")]  # 상한이 찬 넷째는 버림


def test_login_ok_only_after_session(client, db, monkeypatch):
    """세션을 만들다 실패하면 login/ok가 남지 않는다"""
    add_user(monkeypatch, "fay", "viewer")

    def boom(*a):
        raise RuntimeError("세션 표 쓰기 실패 (가상)")

    monkeypatch.setattr(auth, "new_session", boom)
    with pytest.raises(RuntimeError):
        login(client, "fay")
    assert audit_rows(db) == []


def fresh_writer(monkeypatch, size=100):
    """이 시험만의 감사 쓰기 대기열·스레드 (전역 스레드의 차례를 건드리지 않게)"""
    monkeypatch.setattr(auth, "_audit_q", queue.Queue(maxsize=size))
    monkeypatch.setattr(auth, "_worker", [])
    monkeypatch.setattr(auth, "_anon_state", {"dropped": 0, "overflow": 0})


def actions(db):
    return [(r["action"], r["result"], r["info"]) for r in audit_rows(db)]


def test_writer_survives_errors_and_respawns(db, monkeypatch):
    """감사 쓰기 스레드는 일·로거가 터져도 죽지 않고, 죽어 있으면 다음 audit_later가 새로 띄운다"""
    fresh_writer(monkeypatch)

    def bad():
        raise RuntimeError("일 실패 (가상)")

    def bad_log(*a):
        raise RuntimeError("로거 실패 (가상)")

    monkeypatch.setattr(auth.errors, "log_exception", bad_log)
    auth.audit_later(bad)
    auth.audit_later(auth.audit_event, None, "after_error", "ok")
    assert auth.audit_flush(5) and auth._worker[-1].is_alive()
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    monkeypatch.setattr(auth, "_audit_q", queue.Queue(maxsize=100))
    auth._worker[:] = [dead]
    auth.audit_later(auth.audit_event, None, "after_respawn", "ok")
    assert auth.audit_flush(5) and auth._worker[-1] is not dead
    assert [a for a, _, _ in actions(db)] == ["after_error", "after_respawn"]


def test_flush_has_deadline(db, monkeypatch, capsys):
    """DB가 느려도 종료가 막히지 않는다: 마감이 지나면 남은 개수를 서버 로그에 남기고 그만 기다린다"""
    fresh_writer(monkeypatch)
    hold = threading.Event()
    auth.audit_later(hold.wait, 5)
    auth.audit_later(auth.audit_event, None, "late", "ok")
    t0 = time.monotonic()
    assert auth.audit_flush(0.2) is False
    assert time.monotonic() - t0 < 2
    assert "못 쓴 감사 행" in capsys.readouterr().err
    hold.set()
    assert auth.audit_flush(5)


def test_queue_overflow_counted_apart(db, monkeypatch, capsys):
    """대기열이 차서 버린 것은 상한 창에서 버린 것과 따로 세고 요약 행도 따로 (queue_full)"""
    fresh_writer(monkeypatch, size=1)
    hold = threading.Event()
    auth.audit_later(hold.wait, 5)
    for _ in range(100):  # 감사 스레드가 붙잡는 일을 꺼내 갈 때까지
        if auth._audit_q.qsize() == 0:
            break
        time.sleep(0.02)
    auth.audit_later(auth.audit_event, None, "t1", "ok")
    auth.audit_later(auth.audit_event, None, "t2", "ok")  # 넘침
    assert auth._anon_state == {"dropped": 0, "overflow": 1}
    assert "대기열이 차서" in capsys.readouterr().err
    hold.set()
    assert actions(db) == [("audit_suppressed", "queue_full", {"count": 1}), ("t1", "ok", None)]


def test_deferred_row_keeps_request_time(db, monkeypatch):
    """응답 뒤에 쓰는 행의 시각은 요청 때 — 나중에 바로 쓴 행보다 뒤로 가지 않는다"""
    fresh_writer(monkeypatch)
    hold = threading.Event()
    auth.audit_later(hold.wait, 5)
    auth.audit_later(auth.audit_event, None, "deferred", "ok")
    time.sleep(0.05)
    db.audit(None, "direct", result="ok")
    hold.set()
    rows = {r["action"]: r for r in audit_rows(db)}
    assert rows["deferred"]["id"] > rows["direct"]["id"] and rows["deferred"]["ts"] < rows["direct"]["ts"]


def test_logout_without_session_not_audited(client, db):
    """세션 없는 로그아웃은 아무 일도 안 한다 — 누구나 감사 행을 만들지 못하게 남기지 않는다"""
    assert client.post("/auth/logout", headers=CSRF).status_code == 200
    assert client.post("/auth/logout", headers={"authorization": "Bearer not-a-session"}).status_code == 200
    assert audit_rows(db) == []


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


def test_oidc_login_audited(client, db, idp):
    seen, claims = idp
    state = start(client, seen)
    assert client.get("/auth/oidc/callback", params={"code": "c1", "state": state}).status_code == 303
    assert client.get("/auth/oidc/callback", params={"code": "c1", "state": state}).status_code == 400  # 다시 쓰기 (state 쿠키 없음)
    client.cookies.set(auth.STATE_COOKIE, state)
    assert client.get("/auth/oidc/callback", params={"code": "c1", "state": state}).status_code == 400  # 쓴 state
    assert client.get("/auth/oidc/callback", params={"error": "access_denied"}).status_code == 303
    claims["groups"] = []
    auth.OIDC.default_role = None  # 역할을 못 정하면 거절
    state = start(client, seen)
    assert client.get("/auth/oidc/callback", params={"code": "c2", "state": state}).headers["location"] == "/login?error=role"
    rows = audit_rows(db)
    assert [(r["action"], r["result"], r["username"], r["info"]) for r in rows] == [
        ("login", "ok", "kim", {"via": "oidc"}),
        ("login", "failed", None, {"via": "oidc", "reason": "oidc_state"}),
        ("login", "failed", None, {"via": "oidc", "reason": "oidc_expired"}),
        ("login", "failed", None, {"via": "oidc", "reason": "oidc_error"}),
        ("login", "failed", None, {"via": "oidc", "reason": "oidc_role"})]
    text = json.dumps(rows, default=str)
    assert "access_denied" not in text and seen["data"]["code_verifier"] not in text  # 제공자 오류 글·PKCE 값 없음


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


def test_loopback_gets_http_cookie_and_names_do_not_mix(client, db, monkeypatch):
    """port-forward(파드 안 루프백)로 온 요청만 http 이름·Secure 없음 — 이 PC의 localhost:3000 개발 서버가 로그인하는 길.
    파드 IP에서 온 요청(https 프런트)은 http 이름 쿠키를 읽지 않고, 루프백 요청은 __Host- 쿠키를 읽지 않는다 (https://github.com/reddol18/docflow/issues/48)"""
    add_user(monkeypatch, "lia", "viewer")
    dev = TestClient(client.app, base_url="http://localhost:3000", client=("127.0.0.1", 50000))
    r = login(dev, "lia")
    sc = r.headers["set-cookie"]
    assert r.status_code == 200 and sc.startswith(auth.HTTP_COOKIE + "=") and "secure" not in sc.lower()
    assert dev.get("/p").status_code == 200  # http에서 쿠키가 다시 온다
    token = dev.cookies.get(auth.HTTP_COOKIE)
    for host in ("::1", "::ffff:127.0.0.1"):
        assert auth.cookie_mode(type("R", (), {"client": type("C", (), {"host": host})()})())[2] is False
    pod = TestClient(client.app, base_url="https://docflow.test", client=("10.42.0.9", 50000))
    assert pod.get("/p", headers={"cookie": f"{auth.HTTP_COOKIE}={token}"}).status_code == 401  # 다른 이름은 무시
    assert pod.get("/p", headers={"cookie": f"{auth.COOKIE}={token}"}).status_code == 200
    assert dev.get("/p", headers={"cookie": f"{auth.COOKIE}={token}"}).status_code == 401
    r = dev.post("/auth/logout", headers=CSRF)
    assert auth.HTTP_COOKIE in r.headers["set-cookie"] and "secure" not in r.headers["set-cookie"].lower()


def test_logout_clears_host_cookie_with_secure(client, db, monkeypatch):
    add_user(monkeypatch, "kai", "viewer")
    login(client, "kai")
    sc = client.post("/auth/logout", headers=CSRF).headers["set-cookie"]
    assert sc.startswith(auth.COOKIE + "=") and "secure" in sc.lower() and "; Path=/" in sc  # __Host- 지우기도 Secure·Path=/


@pytest.mark.parametrize("key,url", [
    ("jwks_uri", "https://evil.test/certs"),
    ("token_endpoint", "https://evil.test/token"),
    ("authorization_endpoint", "https://evil.test/auth"),
    ("token_endpoint", "http://169.254.169.254/latest/meta-data/"),
    ("jwks_uri", "https://idp.test:8443/realms/x/certs"),  # 포트가 다름
    ("token_endpoint", "https://idp.test@evil.test/token"),  # 사용자 정보로 호스트 숨기기
    ("authorization_endpoint", "http://idp.test/realms/x/auth"),  # https → http
])
def test_oidc_discovery_host_must_match_issuer(client, db, idp, monkeypatch, key, url):
    """discovery 문서의 주소 호스트가 issuer와 다르면 로그인 시작·콜백 모두 거절, 그 주소로 아무것도 보내지 않는다 (API7)"""
    seen, _ = idp
    state = start(client, seen)  # 처음엔 맞는 문서
    disc = {"issuer": ISS, "jwks_uri": ISS + "/certs", "authorization_endpoint": ISS + "/auth", "token_endpoint": ISS + "/token"}
    bad = {**disc, key: url}
    fetched = []

    def get_json(u):
        fetched.append(u)
        return bad

    monkeypatch.setattr(auth, "_get_json", get_json)
    auth._cache.clear()  # 문서가 바뀐 뒤 (캐시 만료·가로채기)
    r = client.get("/auth/oidc/callback", params={"code": "c", "state": state})
    assert r.status_code == 303 and r.headers["location"] == "/login?error=oidc"
    assert "data" not in seen  # 토큰 교환을 부르지 않았다
    r = client.get("/auth/oidc/login")
    assert r.status_code == 502 and "evil" not in r.headers.get("location", "")
    assert all("evil" not in u and "169.254" not in u for u in fetched)  # discovery 주소 말고는 받지 않았다
    assert client.get("/p").status_code == 401


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


def _review_post(db, monkeypatch, via, snaps, fixes=None):
    """iris(검토자)가 v3 검토를 폼·JSON으로 제출. G.get_state는 snaps를 차례로 돌려주고(마지막은 계속), submit은 대신 받는다
    → (응답, 받은 submit 인자, iris id)"""
    from types import SimpleNamespace

    from app import main
    add_user(monkeypatch, "iris", "reviewer")
    with db.pool.connection() as c:
        uid = c.execute("SELECT id FROM users WHERE username = 'iris'").fetchone()["id"]
        c.execute("DELETE FROM review_submitters WHERE vol = 'v3'")
    user = auth.User(uid, "iris", "iris", "reviewer")
    monkeypatch.setattr(auth, "MODES", frozenset({"local"}))
    monkeypatch.setattr(auth, "session_user", lambda t: user if t == "iris-token" else None)
    left = list(snaps)
    monkeypatch.setattr(main, "G", SimpleNamespace(get_state=lambda cfg: left.pop(0) if len(left) > 1 else left[0]))
    sent = []
    monkeypatch.setattr(main, "submit", lambda vol, arg, user=None, reserved=False: sent.append(arg))
    c = TestClient(main.app)
    h = {"authorization": "Bearer iris-token", **CSRF}
    try:
        if via == "form":
            data = {f"v3-0001__{k}": v for k, v in (fixes or {"title": "가상 제목"}).items()}
            r = c.post("/review/v3", data=data, headers=h, follow_redirects=False)
        else:
            r = c.post("/review/v3/json", json={"v3-0001": fixes} if fixes else {}, headers=h)
    finally:
        main.running.pop("v3", None)  # submit을 대신 받아 실행 표시가 남는다
    return r, sent, uid


def _v3_waiting(intr_id):
    from types import SimpleNamespace
    doc = {"doc_id": "v3-0001", "pages": ["v3-0001"], "flags": {"title": ["확신도"]},
           "fields": {"docno": "가상 1-1", "date": "", "recipient": "가상 수신", "title": "가상 제목"}, "hw": {}}
    return SimpleNamespace(interrupts=[SimpleNamespace(value={"flagged": [doc]}, id=intr_id)])


@pytest.mark.parametrize("via", ["form", "json"])
def test_empty_review_submit_still_records(db, monkeypatch, via):
    """고친 칸 없는 검토 제출도 review_submitters 행과 감사 ok(fixed_cells=0)를 남긴다 — 교차검사 근거를 잃지 않게
    (https://github.com/reddol18/docflow/issues/120). 흐름(그래프)은 대신 받아 둔다"""
    r, sent, uid = _review_post(db, monkeypatch, via, [_v3_waiting("1" * 32)])
    assert r.status_code == (303 if via == "form" else 200), r.text
    assert [s.resume for s in sent] == [{"1" * 32: {}}]
    with db.pool.connection() as cn:
        assert [r["user_id"] for r in cn.execute("SELECT user_id FROM review_submitters WHERE vol = 'v3'")] == [uid]
    rows = [r for r in audit_rows(db) if r["action"] == "review" and r["vol"] == "v3"]
    assert [(r["result"], r["info"]["fixed_cells"], r["info"]["via"]) for r in rows] == [("ok", 0, via)]
    assert db.edit_history("v3", "v3-0001") == []


@pytest.mark.parametrize("via", ["form", "json"])
@pytest.mark.parametrize("now", ["ended", "changed"])
def test_review_submit_stale_interrupt_409(db, monkeypatch, via, now):
    """화면(경로)이 대기를 읽은 뒤 철이 다시 돌아 대기가 끝났거나 다른 대기로 바뀌었으면 409 — 고친 값·이력·
    review_submitters를 남기지 않고 흐름도 잇지 않는다. 감사는 기존 규칙대로 rejected_409"""
    from types import SimpleNamespace
    after = SimpleNamespace(interrupts=[]) if now == "ended" else _v3_waiting("f" * 32)
    r, sent, _ = _review_post(db, monkeypatch, via, [_v3_waiting("1" * 32), after], fixes={"title": "가상 새 제목"})
    assert r.status_code == 409, r.text
    assert sent == []
    with db.pool.connection() as cn:
        assert cn.execute("SELECT count(*) AS n FROM review_submitters WHERE vol = 'v3'").fetchone()["n"] == 0
    assert db.edit_history("v3", "v3-0001") == []
    rows = [r for r in audit_rows(db) if r["action"] == "review" and r["vol"] == "v3"]
    assert [r["result"] for r in rows] == ["rejected_409"]
