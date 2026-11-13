"""입력 검증 (https://github.com/reddol18/docflow/issues/49): 철 이름 경로 이탈, 칸·검색어 상한, 업로드 형식, API 목록."""
import re
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import auth, store

ADMIN = {"authorization": "Bearer a", "x-docflow-csrf": "1"}
INTR_ID = "0" * 32  # 지어낸 interrupt id (xxh3 128 hex 형식)


@pytest.fixture
def main(monkeypatch, tmp_path):
    from app import main
    monkeypatch.setattr(auth, "MODES", frozenset({"local"}))
    monkeypatch.setattr(auth, "session_user", lambda t: auth.User(1, "a", "a", "admin") if t == "a" else None)
    monkeypatch.setattr(store, "DATA", tmp_path)
    return main


def _routes(main):
    return [r for r in main.app.routes if isinstance(r, APIRoute) and not r.path.startswith("/auth")]


@pytest.mark.parametrize("bad", ["%2E", "%2E%2E", "%2E%2E%2E", "v1%0A"])  # 끝 줄바꿈: '$'는 그 앞에서도 맞는다
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


def test_volume_names_skips_bad_dirs(main, tmp_path):
    for n in ("v1", "a b", "5350109-1999-0002"):
        (tmp_path / "pages" / n).mkdir(parents=True)
    assert main.volume_names() == ["5350109-1999-0002", "v1"]  # 규칙에 안 맞는 폴더 하나가 목록 전체를 막지 않는다


def _review_ready(main, monkeypatch):
    doc = {"doc_id": "d1", "pages": ["p1"], "flags": {"title": ["확신도"]},
           "fields": {"docno": "가-1", "date": "", "recipient": "긴" * 600, "title": "옛 제목"}, "hw": {}}
    snap = SimpleNamespace(interrupts=[SimpleNamespace(value={"flagged": [doc]}, id=INTR_ID)])
    monkeypatch.setattr(main, "G", SimpleNamespace(get_state=lambda cfg: snap))
    got, ids = [], []
    monkeypatch.setattr(main, "resume_review",
                        lambda vol, flagged, fixes, user, interrupt_id: (got.append(fixes), ids.append(interrupt_id)))
    return got, ids


def test_review_form_value_limit(main, monkeypatch):
    got, _ = _review_ready(main, monkeypatch)
    c = TestClient(main.app)
    res = c.post("/review/v1", data={"d1__title": "가" * 501}, headers=ADMIN, follow_redirects=False)
    assert res.status_code == 422 and not got
    # 고치지 않은 칸은 기존 값이 500자를 넘어도(OCR·GPT가 낸 값) 막지 않는다 — 바뀐 값만 본다
    res = c.post("/review/v1", data={"d1__title": "가" * 500, "d1__recipient": "긴" * 600}, headers=ADMIN,
                 follow_redirects=False)
    assert res.status_code == 303 and got == [{"d1": {"title": "가" * 500}}]


def test_review_empty_submit_both_routes(main, monkeypatch):
    """고친 칸 없는 제출(폼 '그대로 승인'·JSON {})도 두 경로 모두 대기 중인 interrupt id와 함께 resume_review로 간다.
    빈 fixes로 흐름이 끝까지 가는지는 test_llm_provider.test_empty_review_submit_reaches_index
    (https://github.com/reddol18/docflow/issues/120)"""
    got, ids = _review_ready(main, monkeypatch)
    c = TestClient(main.app)
    res = c.post("/review/v1", data={"d1__title": "옛 제목"}, headers=ADMIN, follow_redirects=False)
    assert res.status_code == 303
    assert c.post("/review/v1/json", json={}, headers=ADMIN).status_code == 200
    assert got == [{}, {}] and ids == [INTR_ID, INTR_ID]


def test_search_ask_limits(main, monkeypatch, ask_inline):
    monkeypatch.setattr(main, "hybrid", lambda q, yf, yt, k, user=None: [])
    monkeypatch.setattr(main.llm, "answer", lambda q, hits, user_id=None: ("답", []))
    monkeypatch.setattr(main.store, "take_quota", lambda *a: None)  # 호출 제한은 test_budget_pg에서
    c = TestClient(main.app)
    ok = "가" * main.Q_MAX
    assert c.get("/search", params={"q": ok}, headers=ADMIN).status_code == 200
    assert c.get("/search", params={"q": ok + "가"}, headers=ADMIN).status_code == 422
    assert c.get("/search", params={"q": ""}, headers=ADMIN).status_code == 422
    for k, want in [(0, 422), (1, 200), (50, 200), (51, 422)]:
        assert c.get("/search", params={"q": "농지", "k": k}, headers=ADMIN).status_code == want, k
    assert c.get("/ask", params={"q": ok}, headers=ADMIN).status_code == 200
    assert c.get("/ask", params={"q": ok + "가"}, headers=ADMIN).status_code == 422


def test_search_ask_rate_and_budget_responses(main, monkeypatch, ask_inline):
    """두 경로 모두 호출 제한을 거치고(넘으면 429 + Retry-After), 검색·질문 예산이 차면 503 + 공개 문구
    (https://github.com/reddol18/docflow/issues/7). 실제 셈은 test_budget_pg"""
    seen = []
    monkeypatch.setattr(main.store, "take_quota", lambda key, route, wins: seen.append((route, [w[0] for w in wins])) or "min")
    c = TestClient(main.app)
    # 질의 검증(422)에 걸린 요청은 세지 않는다 — 경로 함수 안에서 센다
    assert c.get("/search", params={"q": ""}, headers=ADMIN).status_code == 422
    assert c.get("/ask", params={"q": "가" * (main.Q_MAX + 1)}, headers=ADMIN).status_code == 422
    assert seen == []
    for path in ("/search", "/ask"):
        r = c.get(path, params={"q": "가상"}, headers=ADMIN)
        assert r.status_code == 429 and 1 <= int(r.headers["retry-after"]) <= 60  # 지금 분이 끝날 때까지
    assert seen == [("search", ["min", "day"]), ("ask", ["min", "day"])]
    refunds = []
    monkeypatch.setattr(main.store, "take_quota", lambda *a: None)
    monkeypatch.setattr(main.store, "refund_quota", lambda key, route, wins: refunds.append(route))

    def over(*a, **k):
        raise main.llm.BudgetExceeded("query 예산 한도 (가상)")
    monkeypatch.setattr(main, "hybrid", over)
    r = c.get("/ask", params={"q": "가상"}, headers=ADMIN)
    assert r.status_code == 503 and r.json()["code"] == "budget" and "가상" not in r.text and int(r.headers["retry-after"]) > 0
    assert refunds == ["ask"]  # 예산 503은 그 사용자의 호출 1회를 되돌린다


def test_minute_retry_after_is_rest_of_window(main, monkeypatch):
    """분당 429의 Retry-After = 지금 분이 끝날 때까지 남은 초"""
    import datetime as dt
    monkeypatch.setattr(main, "_now", lambda: dt.datetime(2001, 3, 1, 10, 0, 45, tzinfo=main.store.KST))
    monkeypatch.setattr(main.store, "take_quota", lambda *a: "min")
    r = TestClient(main.app).get("/search", params={"q": "가상"}, headers=ADMIN)
    assert r.status_code == 429 and r.headers["retry-after"] == "15"


def test_upload_requires_jpeg(main, tmp_path, monkeypatch):
    # 내용까지 검사하므로 진짜 JPEG, 쓰는 업로드는 철 단계를 본다 (https://github.com/reddol18/docflow/issues/26)
    snap = SimpleNamespace(values={}, next=(), interrupts=[])
    monkeypatch.setattr(main, "G", SimpleNamespace(get_state=lambda cfg: snap))
    c = TestClient(main.app)
    jpg = cv2.imencode(".jpg", np.zeros((8, 8, 3), np.uint8))[1].tobytes()
    jpg2 = cv2.imencode(".jpg", np.full((8, 8, 3), 200, np.uint8))[1].tobytes()
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    files = [("files", ("a.jpg", jpg, "image/jpeg")), ("files", ("b.jpg", png, "image/jpeg"))]
    res = c.post("/volumes/v1/pages", files=files, headers=ADMIN)
    assert res.status_code == 400 and "b.jpg" in res.text
    assert not any((tmp_path / "pages" / "v1").iterdir())  # 하나라도 틀리면 아무것도 저장하지 않는다 (임시 파일도)
    res = c.post("/volumes/v1/pages", files=[("files", ("t.jpg", b"text", "image/jpeg"))], headers=ADMIN)
    assert res.status_code == 400
    res = c.post("/volumes/v1/pages", files=[("files", ("a.jpg", jpg, "image/jpeg"))], headers=ADMIN)
    assert res.status_code == 200 and (tmp_path / "pages" / "v1" / "a.jpg").read_bytes() == jpg
    # 틀린 파일이 섞인 다시 올리기는 같은 이름의 기존 쪽을 건드리지 않고, 임시 파일도 남기지 않는다
    res = c.post("/volumes/v1/pages", params={"replace": "true"},
                 files=[("files", ("a.jpg", jpg2, "image/jpeg")), ("files", ("b.jpg", png, "image/jpeg"))], headers=ADMIN)
    assert res.status_code == 400 and (tmp_path / "pages" / "v1" / "a.jpg").read_bytes() == jpg
    assert sorted(p.name for p in (tmp_path / "pages" / "v1").iterdir()) == ["a.jpg"]


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
