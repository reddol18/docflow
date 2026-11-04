"""응답 보안 헤더 (https://github.com/reddol18/docflow/issues/47): 모든 경로·모든 응답에 붙는지."""
from types import SimpleNamespace

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import auth

NEED = {"cache-control": "no-store", "x-content-type-options": "nosniff", "x-frame-options": "DENY"}


@pytest.fixture(scope="module")
def main():
    from app import main
    return main


def _url(path):
    return path.replace("{", "").replace("}", "")


def _check(res, html=False):
    for k, v in NEED.items():
        assert res.headers.get(k) == v, (res.request.url, k, res.headers.get(k))
    csp = res.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp
    assert "script-src" not in csp and "'unsafe-eval'" not in csp  # default-src 'none'이 스크립트를 막는다
    assert csp.startswith("default-src 'none'")
    if html:
        assert "img-src 'self'" in csp and "form-action 'self'" in csp
    else:
        assert csp.startswith("default-src 'none'; frame-ancestors 'none'")


def test_every_route_has_security_headers(main, monkeypatch):
    """로그인 없이 부른 모든 경로(401)와 공개 경로에도 헤더가 붙는다 — 새 경로가 빠지면 실패."""
    monkeypatch.setattr(auth, "MODES", frozenset({"local"}))
    monkeypatch.setattr(auth, "session_user", lambda t: None)
    c = TestClient(main.app)  # with 없이 → lifespan(DB)을 돌리지 않는다
    routes = [r for r in main.app.routes if isinstance(r, APIRoute)]
    assert len(routes) > 10
    for r in routes:
        for m in r.methods:
            if r.path in auth.PUBLIC and m != "GET":
                continue  # 공개 POST(로그인 등)는 본문이 필요하다 — 아래 healthz·404로 공개 쪽을 본다
            _check(c.request(m, _url(r.path), headers={"x-docflow-csrf": "1"}))
    _check(c.get("/healthz"))
    _check(c.get("/없는-경로"))


def test_unhandled_error_500_has_headers(main, monkeypatch):
    """처리 안 된 예외(500)에도 헤더가 붙고, 예외 문구는 응답에 나가지 않는다."""
    monkeypatch.setattr(auth, "MODES", frozenset({"local"}))
    monkeypatch.setattr(auth, "session_user", lambda t: auth.User(1, "v", "v", "viewer"))

    def boom(path):
        raise RuntimeError("내부 경로 /secret/x")
    monkeypatch.setattr(main.ocr, "read_image", boom)
    c = TestClient(main.app, raise_server_exceptions=False)
    res = c.get("/head/vol/p1", headers={"authorization": "Bearer v"})
    assert res.status_code == 500 and "secret" not in res.text
    _check(res)


def _snap(flagged):
    return SimpleNamespace(interrupts=[SimpleNamespace(value={"flagged": flagged})] if flagged else [])


def test_review_page_csp_and_no_inline_script(main, monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "MODES", frozenset({"local"}))
    monkeypatch.setattr(auth, "session_user", lambda t: auth.User(1, "v", "v", "viewer"))
    monkeypatch.setattr(main.store, "DATA", tmp_path)
    doc = {"doc_id": "d1", "pages": ["p1"], "flags": {}, "fields": {"docno": "가-1", "date": "", "recipient": "", "title": ""},
           "hw": {"docno": {"text": "1", "conf": 0.9}, "date": {"text": "2", "conf": 0.8}}}
    (tmp_path / "crops" / "vol").mkdir(parents=True)
    (tmp_path / "crops" / "vol" / "d1_docno.png").write_bytes(b"x")  # date 자른 이미지는 없다
    monkeypatch.setattr(main, "G", SimpleNamespace(get_state=lambda cfg: _snap([doc])))
    c = TestClient(main.app)
    res = c.get("/review/vol", headers={"authorization": "Bearer v"})
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/html")
    _check(res, html=True)
    body = res.text
    assert "onerror" not in body and "<script" not in body  # CSP가 막을 인라인 스크립트가 없다
    assert "/crops/vol/d1_docno.png" in body and "/crops/vol/d1_date.png" not in body  # 있는 것만 그린다
    assert "손글씨 판독: 2" in body  # 이미지가 없어도 판독 값은 보인다
    monkeypatch.setattr(main, "G", SimpleNamespace(get_state=lambda cfg: _snap([])))
    _check(c.get("/review/vol", headers={"authorization": "Bearer v"}), html=True)
