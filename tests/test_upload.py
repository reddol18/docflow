"""업로드 검증 (https://github.com/reddol18/docflow/issues/26): 진짜 JPEG만, 크기·개수·픽셀 상한, 덮어쓰기 409·명시 교체.
시험 이미지는 여기서 만든 8×8 JPEG (실제 기록물 이미지를 쓰지 않는다)."""
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import auth, store
from app import upload as uploads

ADMIN = {"authorization": "Bearer a", "x-docflow-csrf": "1"}


def jpeg(value=0, size=8):
    ok, buf = cv2.imencode(".jpg", np.full((size, size, 3), value, np.uint8))
    assert ok
    return buf.tobytes()


def bomb():
    """머리(SOF0)만 20000×20000(4억 픽셀)이라고 적은 JPEG — 펼치기 전에 막아야 한다"""
    b = bytearray(jpeg())
    i = b.index(b"\xff\xc0")
    b[i + 5:i + 9] = (20000).to_bytes(2, "big") * 2
    return bytes(b)


@pytest.fixture
def main(monkeypatch, tmp_path):
    from app import main
    monkeypatch.setattr(auth, "MODES", frozenset({"local"}))
    monkeypatch.setattr(auth, "session_user", lambda t: auth.User(1, "a", "a", "admin") if t == "a" else None)
    monkeypatch.setattr(store, "DATA", tmp_path)
    return main


def post(c, files, **params):
    return c.post("/volumes/v1/pages", headers=ADMIN, params=params,
                  files=[("files", (n, b, "image/jpeg")) for n, b in files])


def left(tmp_path):
    """쪽 폴더에 남은 파일 전부 (임시 .part 포함)"""
    d = tmp_path / "pages" / "v1"
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def test_fake_and_broken_jpeg_rejected(main, tmp_path):
    c = TestClient(main.app)
    cases = [
        ("text.jpg", b"just text, not an image"),            # 확장자만 .jpg
        ("sig.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 64),     # 시그니처는 맞지만 JPEG 구조가 아님
        ("cut.jpg", jpeg()[:40]),                             # 머리 중간에서 잘림
        ("bomb.jpg", bomb()),                                 # 픽셀 수 상한 초과 (압축 폭탄)
    ]
    for name, data in cases:
        res = post(c, [("ok.jpg", jpeg()), (name, data)])
        assert res.status_code == 400, name
        assert name in res.text and "just text" not in res.text  # 이름은 규칙을 통과한 것만, 내용은 되돌려 주지 않는다
        assert left(tmp_path) == []  # 하나라도 틀리면 아무것도 남지 않는다 (임시 파일도)


def no_quant():
    """시그니처·크기(SOF)·끝(EOI)은 멀쩡한데 양자화 표(DQT)를 뺀 JPEG — 펼칠 수 없다"""
    b, out, i = jpeg(), bytearray(b"\xff\xd8"), 2
    while b[i + 1] != 0xDA:  # 스캔 시작 전까지 조각을 옮기며 DQT(DB)만 뺀다
        n = int.from_bytes(b[i + 2:i + 4], "big")
        if b[i + 1] != 0xDB:
            out += b[i:i + 2 + n]
        i += 2 + n
    return bytes(out + b[i:])


def test_signature_ok_but_not_decodable(main, tmp_path):
    """머리는 멀쩡하지만 펼쳐지지 않는 파일, 끝이 잘린 파일 → 400"""
    c = TestClient(main.app)
    broken = no_quant()
    assert broken.startswith(b"\xff\xd8\xff") and broken.endswith(b"\xff\xd9")
    p = tmp_path / "x.jpg"
    p.write_bytes(broken)
    assert uploads.jpeg_dims(p) == (8, 8) and not uploads.decodes(p)
    for name, data in [("b.jpg", broken), ("cut.jpg", jpeg()[:-20])]:
        res = post(c, [(name, data)])
        assert res.status_code == 400 and name in res.text and left(tmp_path) == [], name
    assert post(c, [("pad.jpg", jpeg() + b"\x00" * 16)]).status_code == 200  # EOI 뒤 0 채움은 받는다


def test_bad_name_not_echoed(main, tmp_path):
    res = post(TestClient(main.app), [("ok.jpg", jpeg()), ("<script>.jpg", jpeg())])
    assert res.status_code == 400 and "<script>" not in res.text and "2번째" in res.text
    assert left(tmp_path) == []


def test_oversize_file_413(main, tmp_path, monkeypatch):
    data = jpeg()
    monkeypatch.setattr(uploads, "MAX_BYTES", len(data) - 1)  # 상한 + 1바이트
    res = post(TestClient(main.app), [("a.jpg", data)])
    assert res.status_code == 413 and left(tmp_path) == []
    monkeypatch.setattr(uploads, "MAX_BYTES", len(data))  # 상한과 같으면 받는다
    assert post(TestClient(main.app), [("a.jpg", data)]).status_code == 200


def test_too_many_files_400(main, tmp_path, monkeypatch):
    monkeypatch.setattr(uploads, "MAX_FILES", 3)
    c = TestClient(main.app)
    res = post(c, [(f"p{i}.jpg", jpeg(i)) for i in range(4)])  # 상한 + 1장
    assert res.status_code == 400 and left(tmp_path) == []
    assert post(c, [(f"p{i}.jpg", jpeg(i)) for i in range(3)]).status_code == 200


def test_body_cap_before_parsing(main, tmp_path, monkeypatch):
    """본문 전체가 상한을 넘으면 처리 함수(로그인 확인 포함)보다 먼저 413 — /tmp에 흘려 쓰기 전에"""
    monkeypatch.setattr(uploads, "MAX_FILES", 1)
    monkeypatch.setattr(uploads, "MAX_BYTES", 1024)
    c = TestClient(main.app)
    big = [("a.jpg", b"\xff\xd8\xff" + b"\x00" * (2 * 1024 * 1024))]
    assert post(c, big).status_code == 413
    res = c.post("/volumes/v1/pages", files=[("files", ("a.jpg", big[0][1], "image/jpeg"))])  # 로그인 없이도 413
    assert res.status_code == 413 and left(tmp_path) == []
    # 다른 업로드가 /tmp 예산을 쓰고 있으면 429
    monkeypatch.setattr(uploads, "MAX_BYTES", 20 * 1024 * 1024)
    monkeypatch.setattr(uploads.BodyCap, "reserved", uploads.TMP_BUDGET)
    assert post(c, [("a.jpg", jpeg())]).status_code == 429


def _done(main, monkeypatch, stage="done"):
    snaps = {"done": SimpleNamespace(values={"indexed": 1}, next=(), interrupts=[]),
             "review": SimpleNamespace(values={}, next=("review",), interrupts=[object()]),
             "stopped": SimpleNamespace(values={}, next=("split",), interrupts=[])}
    monkeypatch.setattr(main, "G", SimpleNamespace(get_state=lambda cfg: snaps[stage]))


def test_overwrite_409_then_explicit_replace(main, tmp_path, monkeypatch):
    c = TestClient(main.app)
    old, new = jpeg(10), jpeg(200)
    assert post(c, [("a.jpg", old), ("b.jpg", jpeg(50))]).status_code == 200
    page = tmp_path / "pages" / "v1" / "a.jpg"
    # 그 쪽을 이미 OCR했고 손글씨 칸도 잘라 두었다
    store.ocr_path("v1", "a").parent.mkdir(parents=True)
    store.ocr_path("v1", "a").write_text('{"page": "a", "items": [], "lines": []}', encoding="utf-8")
    store.ocr_path("v1", "b").write_text('{"page": "b", "items": [], "lines": []}', encoding="utf-8")
    store.crop_path("v1", "a", "docno").parent.mkdir(parents=True)
    store.crop_path("v1", "a", "docno").write_bytes(b"png")

    # 같은 이름 다른 내용 → 409, 기존 바이트·OCR 그대로, 같이 보낸 새 쪽도 저장 안 됨
    res = post(c, [("a.jpg", new), ("c.jpg", jpeg(90))])
    assert res.status_code == 409 and "a.jpg" in res.text and "replace=true" in res.text
    assert page.read_bytes() == old and store.ocr_path("v1", "a").exists()
    assert left(tmp_path) == ["a.jpg", "b.jpg"]
    # 같은 이름 같은 내용 → 그대로 둠 (끊긴 업로드를 다시 보내도 된다)
    res = post(c, [("a.jpg", old)])
    assert res.status_code == 200 and res.json()["unchanged"] == 1 and res.json()["saved"] == 0
    assert store.ocr_path("v1", "a").exists()

    # 검토 대기·중간에 멈춘 철은 바꾸지 않는다
    for stage in ("review", "stopped"):
        _done(main, monkeypatch, stage)
        assert post(c, [("a.jpg", new)], replace="true").status_code == 409
        assert page.read_bytes() == old and store.ocr_path("v1", "a").exists()
    main.running["v1"] = "running"
    try:
        _done(main, monkeypatch)
        assert post(c, [("a.jpg", new)], replace="true").status_code == 409
    finally:
        main.running.pop("v1", None)

    # 명시 교체 → 바뀌고 그 쪽 OCR·잘라낸 칸만 지워짐, 끝난 철이라 다시 돌려야 함(rerun)
    res = post(c, [("a.jpg", new)], replace="true")
    assert res.status_code == 200, res.text
    assert res.json() | {"total": 0} == {"vol": "v1", "saved": 1, "replaced": 1, "unchanged": 0, "rerun": True,
                                         "total": 0}
    assert page.read_bytes() == new and "v1" not in main.running
    assert not store.ocr_path("v1", "a").exists() and not store.crop_path("v1", "a", "docno").exists()
    assert store.ocr_path("v1", "b").exists()
    assert left(tmp_path) == ["a.jpg", "b.jpg"]

    # 다음 실행의 OCR 노드는 바뀐 쪽만 다시 읽는다
    seen = []
    monkeypatch.setattr(main.flow.ocr, "ocr_page", lambda p: seen.append(p.name) or {"items": [], "lines": []})
    monkeypatch.setattr(store, "event", lambda *a, **k: None)
    assert main.flow.ocr_pages({"vol": "v1"}) == {"pages": ["a", "b"]}
    assert seen == ["a.jpg"]


def test_jpeg_dims(tmp_path):
    p = tmp_path / "x.jpg"
    p.write_bytes(jpeg(size=8))
    assert uploads.jpeg_dims(p) == (8, 8)
    p.write_bytes(bomb())
    assert uploads.jpeg_dims(p) == (20000, 20000)
    for bad in (b"", b"\xff\xd8", b"\xff\xd8\xff\xd9", b"\xff\xd8\x00\x00", b"GIF89a"):
        p.write_bytes(bad)
        assert uploads.jpeg_dims(p) is None, bad
