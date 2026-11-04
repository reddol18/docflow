"""입력 검증 (https://github.com/reddol18/docflow/issues/49): 철 이름 경로 이탈, 칸·검색어 상한, 업로드 형식, API 목록."""
import re
from types import SimpleNamespace

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import auth, store

ADMIN = {"authorization": "Bearer a", "x-docflow-csrf": "1"}


@pytest.fixture
def main(monkeypatch, tmp_path):
    from app import main
    monkeypatch.setattr(auth, "MODES", frozenset({"local"}))
    monkeypatch.setattr(auth, "session_user", lambda t: auth.User(1, "a", "a", "admin") if t == "a" else None)
    monkeypatch.setattr(store, "DATA", tmp_path)
    return main


def _routes(main):
    return [r for r in main.app.routes if isinstance(r, APIRoute) and not r.path.startswith("/auth")]


@pytest.mark.parametrize("bad", ["%2E", "%2E%2E", "%2E%2E%2E"])
def test_dot_volume_rejected_on_every_route(main, bad):
    """점만으로 된 철 이름으로 철 경로를 모두 부르면 400 — 본문이 필요한 경로(업로드 등)도 본문 검증보다 먼저."""
    c = TestClient(main.app)
    vol_routes = [r for r in _routes(main) if "{vol}" in r.path]
    assert len(vol_routes) >= 12
    for r in vol_routes:
        url = r.path.replace("{vol}", bad).replace("{doc_id}", "d").replace("{name}", "d_docno.png").replace("{page}", "p")
        for m in r.methods:
            res = c.request(m, url, headers=ADMIN)
            assert res.status_code == 400, f"{m} {url} → {res.status_code}"


def test_under_keeps_paths_inside(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA", tmp_path)
    assert store.under("pages", "v1", "p.jpg") == tmp_path / "pages" / "v1" / "p.jpg"
    assert store.pages_dir("5350109-1999-0002") == tmp_path / "pages" / "5350109-1999-0002"
    for parts in [("..",), (".",), ("v", "..", "w"), ("a/../../x",), ("../ocr",), ("v", "../../etc")]:
        with pytest.raises(store.PathEscape):
            store.under("pages", *parts)


def _review_ready(main, monkeypatch):
    doc = {"doc_id": "d1", "pages": ["p1"], "flags": {"title": ["확신도"]},
           "fields": {"docno": "가-1", "date": "", "recipient": "", "title": "옛 제목"}, "hw": {}}
    snap = SimpleNamespace(interrupts=[SimpleNamespace(value={"flagged": [doc]})])
    monkeypatch.setattr(main, "G", SimpleNamespace(get_state=lambda cfg: snap))
    got = []
    monkeypatch.setattr(main, "resume_review", lambda vol, flagged, fixes, user: got.append(fixes))
    return got


def test_review_form_value_limit(main, monkeypatch):
    got = _review_ready(main, monkeypatch)
    c = TestClient(main.app)
    res = c.post("/review/v1", data={"d1__title": "가" * 501}, headers=ADMIN, follow_redirects=False)
    assert res.status_code == 422 and not got
    res = c.post("/review/v1", data={"d1__title": "가" * 500}, headers=ADMIN, follow_redirects=False)
    assert res.status_code == 303 and got == [{"d1": {"title": "가" * 500}}]


def test_search_ask_limits(main, monkeypatch):
    monkeypatch.setattr(main, "hybrid", lambda q, yf, yt, k: [])
    monkeypatch.setattr(main.llm, "answer", lambda q, hits: "답")
    c = TestClient(main.app)
    ok = "가" * main.Q_MAX
    assert c.get("/search", params={"q": ok}, headers=ADMIN).status_code == 200
    assert c.get("/search", params={"q": ok + "가"}, headers=ADMIN).status_code == 422
    assert c.get("/search", params={"q": ""}, headers=ADMIN).status_code == 422
    for k, want in [(0, 422), (1, 200), (50, 200), (51, 422)]:
        assert c.get("/search", params={"q": "농지", "k": k}, headers=ADMIN).status_code == want, k
    assert c.get("/ask", params={"q": ok}, headers=ADMIN).status_code == 200
    assert c.get("/ask", params={"q": ok + "가"}, headers=ADMIN).status_code == 422


def test_upload_requires_jpeg(main, tmp_path):
    c = TestClient(main.app)
    jpg = b"\xff\xd8\xff\xe0" + b"\x00" * 16
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    files = [("files", ("a.jpg", jpg, "image/jpeg")), ("files", ("b.jpg", png, "image/jpeg"))]
    res = c.post("/volumes/v1/pages", files=files, headers=ADMIN)
    assert res.status_code == 400 and "b.jpg" in res.text
    assert not (tmp_path / "pages" / "v1").exists()  # 하나라도 틀리면 아무것도 저장하지 않는다
    res = c.post("/volumes/v1/pages", files=[("files", ("t.jpg", b"text", "image/jpeg"))], headers=ADMIN)
    assert res.status_code == 400
    res = c.post("/volumes/v1/pages", files=[("files", ("a.jpg", jpg, "image/jpeg"))], headers=ADMIN)
    assert res.status_code == 200 and (tmp_path / "pages" / "v1" / "a.jpg").read_bytes() == jpg


def test_header_api_list_matches_routes(main):
    """`app/main.py` 머리말 목록과 실제 라우트의 (메서드, 경로) 집합이 같다 — 경로를 더하거나 빼면 목록도 고친다."""
    listed = set()
    for line in main.__doc__.splitlines():
        m = re.match(r"^\s+(GET|POST|PATCH|PUT|DELETE)\s+(/\S+)", line)
        if m:
            listed.add((m.group(1), m.group(2)))
    actual = {(meth, r.path) for r in _routes(main) for meth in r.methods}
    assert listed - actual == set(), f"목록에만 있음: {listed - actual}"
    assert actual - listed == set(), f"목록에 없음: {actual - listed}"
