"""업로드 검증 (https://github.com/reddol18/docflow/issues/26): 진짜 JPEG만, 크기·개수·픽셀·스캔 상한, 본문 상한,
로그인 확인 뒤 본문 읽기, 덮어쓰기 409·명시 교체, 철 단계·실행 표시 경합.
시험 이미지는 여기서 만든 작은 JPEG (실제 기록물 이미지를 쓰지 않는다)."""
import time
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import auth, store
from app import upload as uploads

ADMIN = {"authorization": "Bearer a", "x-docflow-csrf": "1"}
SNAPS = {"new": SimpleNamespace(values={}, next=(), interrupts=[]),
         "done": SimpleNamespace(values={"indexed": 1}, next=(), interrupts=[]),
         "review": SimpleNamespace(values={}, next=("review",), interrupts=[object()]),
         "stopped": SimpleNamespace(values={}, next=("split",), interrupts=[])}


def jpeg(value=0, size=8, progressive=False):
    img = np.full((size, size, 3), value, np.uint8)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_PROGRESSIVE, int(progressive)])
    assert ok
    return buf.tobytes()


def bomb():
    """머리(SOF0)만 20000×20000(4억 픽셀)이라고 적은 JPEG — 펼치기 전에 막아야 한다"""
    b = bytearray(jpeg())
    i = b.index(b"\xff\xc0")
    b[i + 5:i + 9] = (20000).to_bytes(2, "big") * 2
    return bytes(b)


def scan_bomb(n):
    """프로그레시브 JPEG의 마지막 스캔(앞의 DHT 포함)을 되풀이해 스캔 n개로 — 스캔마다 쪽 전체를 다시 훑는다"""
    b = jpeg(30, size=16, progressive=True)
    last = b.rindex(b"\xff\xda")
    start = b.rfind(b"\xff\xc4", 0, last)  # 그 스캔 앞의 허프만 표
    start = start if start != -1 else last
    seg = b[start:-2]
    have = uploads.jpeg_info(b)["scans"]
    return b[:-2] + seg * (n - have) + b"\xff\xd9"


def stage(main, monkeypatch, name="new", hook=None):
    def get_state(cfg):
        if hook:
            hook()
        return SNAPS[name]
    monkeypatch.setattr(main, "G", SimpleNamespace(get_state=get_state))


@pytest.fixture
def main(monkeypatch, tmp_path):
    from app import main
    monkeypatch.setattr(auth, "MODES", frozenset({"local"}))
    monkeypatch.setattr(auth, "session_user", lambda t: auth.User(1, "a", "a", "admin") if t == "a" else None)
    monkeypatch.setattr(store, "DATA", tmp_path)
    stage(main, monkeypatch)
    main.running.pop("v1", None)
    return main


def post(c, files, headers=ADMIN, **params):
    return c.post("/volumes/v1/pages", headers=headers, params=params,
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
        ("tail.jpg", jpeg()[:-20]),                           # 스캔 데이터 끝이 잘림 (EOI 없음)
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
    """머리·끝은 멀쩡하지만 펼쳐지지 않는 파일 → 400"""
    broken = no_quant()
    assert uploads.jpeg_info(broken)["w"] == 8 and not uploads.decodes(broken)
    res = post(TestClient(main.app), [("b.jpg", broken)])
    assert res.status_code == 400 and "b.jpg" in res.text and left(tmp_path) == []


def test_trailer_after_eoi_accepted(main, tmp_path):
    """EOI 뒤에 붙은 것(카메라 트레일러·긴 0 채움)은 받는다 — 끝이 잘린 파일만 거절"""
    c = TestClient(main.app)
    for name, data in [("pad.jpg", jpeg() + b"\x00" * 5000), ("trailer.jpg", jpeg(1) + b"TRAILER\xff\xd8junk" * 300)]:
        assert post(c, [(name, data)]).status_code == 200, name
    assert left(tmp_path) == ["pad.jpg", "trailer.jpg"]


def test_scan_bomb_rejected_before_decode(main, tmp_path, monkeypatch):
    c = TestClient(main.app)
    ok = scan_bomb(uploads.MAX_SCANS)
    assert uploads.jpeg_info(ok)["scans"] == uploads.MAX_SCANS
    over = scan_bomb(uploads.MAX_SCANS + 1)
    decoded = []
    monkeypatch.setattr(uploads, "decodes", lambda data: decoded.append(1) or True)
    res = post(c, [("scan.jpg", over)])
    assert res.status_code == 400 and "스캔" in res.text and not decoded and left(tmp_path) == []
    assert post(c, [("ok.jpg", ok)]).status_code == 200 and decoded


def test_bad_name_not_echoed(main, tmp_path):
    c = TestClient(main.app)
    for bad in ("<script>.jpg", "a" * 97 + ".jpg", "x.png", "x.jpg.exe"):
        res = post(c, [("ok.jpg", jpeg()), (bad, jpeg())])
        assert res.status_code == 400 and bad not in res.text and "2번째" in res.text, bad
    assert post(c, [("a" * 96 + ".jpg", jpeg())]).status_code == 200  # 96자는 받는다


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
    assert res.status_code == 400 and "3장" in res.text and left(tmp_path) == []
    assert post(c, [(f"p{i}.jpg", jpeg(i)) for i in range(3)]).status_code == 200


def test_body_read_after_login(main, tmp_path, monkeypatch):
    """로그인·역할·CSRF 확인이 본문 읽기보다 먼저 — 로그인 안 한 요청은 본문을 읽지 않는다(/tmp 예산도 안 씀)"""
    seen = []
    real = uploads.BodyCap.__call__

    async def spy(self, scope, receive, send):
        async def rec():
            seen.append(1)
            return await receive()
        return await real(self, scope, rec if self.match(scope["method"], scope["path"]) else receive, send)
    monkeypatch.setattr(uploads.BodyCap, "__call__", spy)
    users = {"a": auth.User(1, "a", "a", "admin"), "v": auth.User(2, "v", "v", "viewer")}
    monkeypatch.setattr(auth, "session_user", users.get)
    c = TestClient(main.app)
    assert post(c, [("a.jpg", jpeg())], headers={}).status_code == 401 and not seen
    viewer = {"authorization": "Bearer v", "x-docflow-csrf": "1"}
    assert post(c, [("a.jpg", jpeg())], headers=viewer).status_code == 403 and not seen  # 역할
    assert post(c, [("a.jpg", jpeg())]).status_code == 200 and seen


def test_body_cap(main, tmp_path, monkeypatch):
    """본문 상한: Content-Length가 넘으면 바로, 없으면(chunked) 받은 바이트로 413. /tmp 예산이 차면 429"""
    monkeypatch.setattr(uploads, "MAX_BODY", 64 * 1024)
    c = TestClient(main.app)
    big = b"\xff\xd8\xff" + b"\x00" * (128 * 1024)
    assert post(c, [("a.jpg", big)]).status_code == 413
    body = b"--x\r\nContent-Disposition: form-data; name=\"files\"; filename=\"a.jpg\"\r\n\r\n" + big + b"\r\n--x--\r\n"
    res = c.post("/volumes/v1/pages", headers={**ADMIN, "content-type": "multipart/form-data; boundary=x"},
                 content=(body[i:i + 8192] for i in range(0, len(body), 8192)))  # 생성기 → chunked, Content-Length 없음
    assert res.status_code == 413 and left(tmp_path) == []
    monkeypatch.setattr(uploads, "MAX_BODY", 210 * 1024 * 1024)
    assert post(c, [("a.jpg", jpeg())]).status_code == 200  # chunked가 아니어도 Content-Length는 필수가 아니다
    monkeypatch.setattr(uploads.BodyCap, "reserved", uploads.TMP_BUDGET)
    assert post(c, [("b.jpg", jpeg(1))]).status_code == 429


def test_overwrite_409_then_explicit_replace(main, tmp_path, monkeypatch):
    c = TestClient(main.app)
    old, new = jpeg(10), jpeg(200)
    assert post(c, [("a.jpg", old), ("b.jpg", jpeg(50))]).json()["rerun"] is False  # 처음 철
    page = tmp_path / "pages" / "v1" / "a.jpg"
    # 그 쪽을 이미 OCR했고 손글씨 칸도 잘라 두었다
    store.ocr_path("v1", "a").parent.mkdir(parents=True)
    store.ocr_path("v1", "a").write_text('{"page": "a", "items": [], "lines": []}', encoding="utf-8")
    store.ocr_path("v1", "b").write_text('{"page": "b", "items": [], "lines": []}', encoding="utf-8")
    store.crop_path("v1", "a", "docno").parent.mkdir(parents=True)
    store.crop_path("v1", "a", "docno").write_bytes(b"png")
    stage(main, monkeypatch, "done")

    # 같은 이름 다른 내용 → 409, 기존 바이트·OCR 그대로, 같이 보낸 새 쪽도 저장 안 됨
    res = post(c, [("a.jpg", new), ("c.jpg", jpeg(90))])
    assert res.status_code == 409 and "a.jpg" in res.text and "replace=true" in res.text
    assert page.read_bytes() == old and store.ocr_path("v1", "a").exists()
    assert left(tmp_path) == ["a.jpg", "b.jpg"]
    # 같은 이름 같은 내용 → 그대로 둠 (끊긴 업로드를 다시 보내도 된다)
    res = post(c, [("a.jpg", old)])
    assert res.status_code == 200 and res.json()["unchanged"] == 1 and res.json()["saved"] == 0
    assert store.ocr_path("v1", "a").exists()

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


@pytest.mark.parametrize("name", ["review", "stopped", "running"])
def test_write_blocked_mid_flow(main, tmp_path, monkeypatch, name):
    """실행 중·검토 대기·중간에 멈춘 철에는 새 쪽 추가도 교체도 409 — 같은 내용 재업로드는 그대로 200"""
    c = TestClient(main.app)
    assert post(c, [("a.jpg", jpeg(10))]).status_code == 200
    if name == "running":
        main.running["v1"] = run = main.Busy("running")
        stage(main, monkeypatch, "done")
    else:
        stage(main, monkeypatch, name)
    try:
        assert post(c, [("n.jpg", jpeg(5))]).status_code == 409
        assert post(c, [("a.jpg", jpeg(200))], replace="true").status_code == 409
        assert post(c, [("a.jpg", jpeg(10))]).json()["unchanged"] == 1
        assert left(tmp_path) == ["a.jpg"]
        if name == "running":
            assert main.running["v1"] is run  # 다른 일이 세운 실행 표시를 지우지 않는다
    finally:
        main.running.pop("v1", None)


def test_new_page_on_done_volume_needs_rerun(main, tmp_path, monkeypatch):
    c = TestClient(main.app)
    assert post(c, [("a.jpg", jpeg(10))]).json()["rerun"] is False
    stage(main, monkeypatch, "done")
    res = post(c, [("b.jpg", jpeg(20))])
    assert res.status_code == 200 and res.json()["saved"] == 1 and res.json()["rerun"] is True


def test_unchanged_upload_audited_as_unchanged(main, monkeypatch):
    rows = []
    monkeypatch.setattr(auth, "audit_event", lambda user, action, result, *a, **k: rows.append((action, result)))
    c = TestClient(main.app)
    post(c, [("a.jpg", jpeg(10))])
    post(c, [("a.jpg", jpeg(10))])
    post(c, [("a.jpg", jpeg(99))])
    assert rows == [("upload", "ok"), ("upload", "unchanged"), ("upload", "rejected_409")]


def test_run_cannot_slip_in_while_uploading(main, tmp_path, monkeypatch):
    """업로드가 실행 표시를 세운 뒤 단계를 읽는다 — 그사이 /run의 예약은 실패하고, 업로드가 끝나면 표시가 풀린다"""
    tried = []
    stage(main, monkeypatch, "new", hook=lambda: tried.append(main.reserve("v1", "queued")))
    assert post(TestClient(main.app), [("a.jpg", jpeg())]).status_code == 200
    assert tried == [None] and "v1" not in main.running


def test_release_keeps_others_mark(main, tmp_path, monkeypatch):
    """예전처럼 확인 없이 덮어쓴 표시가 있어도, 업로드는 자기가 세운 표시만 지운다"""
    def overwrite():
        main.running["v1"] = "queued"
    stage(main, monkeypatch, "review", hook=overwrite)
    try:
        assert post(TestClient(main.app), [("a.jpg", jpeg())]).status_code == 409
        assert main.running.get("v1") == "queued"
    finally:
        main.running.pop("v1", None)
    tok = main.reserve("v1", "queued")
    assert tok == "queued" and main.reserve("v1", "uploading") is None
    main.release("v1", main.Busy("queued"))  # 같은 글자라도 다른 표시면 지우지 않는다
    assert main.running["v1"] is tok
    main.release("v1", tok)
    assert "v1" not in main.running


def test_place_rolls_back_new_pages_on_error(main, tmp_path, monkeypatch):
    """새 쪽을 놓다가 디스크 오류 등으로 실패하면 이번에 놓은 새 쪽을 모두 되돌린다"""
    calls = []
    real = uploads.place_new

    def flaky(tmp, dest):
        calls.append(dest)
        if len(calls) == 2:
            raise OSError("disk full (가상)")
        real(tmp, dest)
    monkeypatch.setattr(uploads, "place_new", flaky)
    res = post(TestClient(main.app), [("a.jpg", jpeg(1)), ("b.jpg", jpeg(2))])
    assert res.status_code == 500 and "disk" not in res.text
    assert left(tmp_path) == [] and "v1" not in main.running


def test_scan_end_linear_time():
    """스캔 데이터 안의 FF가 아주 길게 이어져도 표지 걷기는 한 번 훑기 — 예전 '\\xff+' 정규식은 길이의 제곱이었다
    (FF 4만 개에 7.5초, 20MB면 수십 시간 동안 GIL을 쥐어 /healthz까지 멈춤)"""
    head = jpeg()[:jpeg().index(b"\xff\xda")]
    sos = jpeg()[jpeg().index(b"\xff\xda"):]
    sos = sos[:4 + int.from_bytes(sos[2:4], "big") - 2]
    for n in (40_000, 2_000_000):
        data = head + sos + b"\xff" * n + b"\x00"  # EOI 없음 → 잘린 파일
        t = time.perf_counter()
        assert uploads.jpeg_info(data) is None
        assert time.perf_counter() - t < 1.0, n
        t = time.perf_counter()
        assert uploads.jpeg_info(data + b"\xff\xd9")["scans"] == 1  # 채움 FF 뒤의 EOI는 표지로 읽는다
        assert time.perf_counter() - t < 1.0, n


def test_extension_and_name_bytes(main, tmp_path):
    """.JPG·.jpeg도 받아 <쪽>.jpg로 저장, 같은 쪽 이름 두 번은 400. 쪽 이름은 UTF-8 160바이트까지
    (한글 96자 = 288바이트면 임시 파일 이름이 255바이트를 넘어 500이 나던 것)"""
    c = TestClient(main.app)
    res = post(c, [("A.JPG", jpeg(1)), ("b.jpeg", jpeg(2)), ("c.Jpg", jpeg(3))])
    assert res.status_code == 200, res.text
    assert left(tmp_path) == ["A.jpg", "b.jpg", "c.jpg"]
    assert post(c, [("d.jpg", jpeg(4)), ("d.JPEG", jpeg(5))]).status_code == 400
    assert post(c, [("가" * 53 + ".jpg", jpeg(6))]).status_code == 200  # 159바이트
    res = post(c, [("가" * 54 + ".jpg", jpeg(7))])  # 162바이트
    assert res.status_code == 400 and "1번째" in res.text


def test_review_resume_reserves_first(main, monkeypatch):
    """검토 제출은 이력을 쓰기 전에 실행 표시를 세우고, 실패하면 자기 표시를 푼다"""
    seen, submitted = [], []
    monkeypatch.setattr(main, "record_review", lambda vol, fl, fx, user: seen.append(main.running.get(vol)))
    monkeypatch.setattr(main, "submit", lambda vol, arg, user=None, reserved=False: submitted.append(reserved))
    main.resume_review("v1", [], {}, None)
    assert seen == ["queued"] and submitted == [True]
    main.running.pop("v1", None)

    def boom(vol, fl, fx, user):
        raise RuntimeError("가상 저장 실패")
    monkeypatch.setattr(main, "record_review", boom)
    with pytest.raises(RuntimeError):
        main.resume_review("v1", [], {}, None)
    assert "v1" not in main.running
    main.running["v1"] = "running"
    try:
        with pytest.raises(main.HTTPException):
            main.resume_review("v1", [], {}, None)
        assert main.running["v1"] == "running"
    finally:
        main.running.pop("v1", None)


def test_jpeg_info():
    assert uploads.jpeg_info(jpeg(size=8)) | {"scans": 0} == {"w": 8, "h": 8, "scans": 0, "progressive": False}
    p = uploads.jpeg_info(jpeg(size=16, progressive=True))
    assert p["progressive"] and 1 < p["scans"] <= 20
    assert (uploads.jpeg_info(bomb())["w"], uploads.jpeg_info(bomb())["h"]) == (20000, 20000)
    for bad in (b"", b"\xff\xd8", b"\xff\xd8\xff\xd9", b"\xff\xd8\x00\x00", b"GIF89a", jpeg()[:-2]):
        assert uploads.jpeg_info(bad) is None, bad
