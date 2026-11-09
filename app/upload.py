"""쪽 이미지 올리기 검사 (https://github.com/reddol18/docflow/issues/26, ASVS V12.1·API4 자원 소비).

받는 것: 진짜 JPEG만. 검사 순서 — 펼치기(디코드) 전에 값싼 것부터:
  1) 앞 바이트(SOI 시그니처)  2) 표지 구조를 끝(EOI)까지 걷기(jpeg_info): 크기(SOF)·스캔(SOS) 수, 끝이 잘렸으면 거절,
     EOI 뒤에 붙은 것(카메라 트레일러·0 채움)은 받는다  3) 픽셀 수·스캔 수 상한  4) 실제 디코드(1/8 흑백, 동시 실행 제한)
상한(환경변수로 바꿈, 기본값 근거):
  UPLOAD_MAX_MB      파일 하나 20MB → 413. 600dpi 컬러 A3 스캔 JPEG(품질 90 안팎)도 10~15MB라 여유가 있다
  UPLOAD_MAX_FILES   요청 하나 50장 → 400. 바이트가 아니라 요청 모양의 규칙이라 413(본문 크기)이 아니라 400
                     (바이트는 아래 본문 상한이 413). 쪽 한 장이 1MB 안팎이면 50장도 본문 상한 안에 든다
  UPLOAD_MAX_BODY_MB 요청 본문 210MB → 413. 화면(UploadPanel)이 한 요청에 10장씩 보낸다 → 10 × 20MB + 여유 10MB
  UPLOAD_MAX_MPX     픽셀 수 100MP → 400 (압축 폭탄). A3 600dpi = 7016×9921 ≈ 70MP에 약 40% 여유.
                     컬러로 펼치면 약 300MB — OCR 단계(ocr.read_image)가 실제로 펼치는 크기의 상한이기도 하다
  UPLOAD_MAX_SCANS   스캔(SOS) 64개 → 400 (스캔 폭탄: 프로그레시브 JPEG는 스캔마다 쪽 전체를 다시 훑는다 — 작은 파일에
                     스캔 수천 개면 디코드가 수 분). 보통 프로그레시브는 10개 안팎. 이 PC 실측: 100MP 프로그레시브 6스캔
                     0.35초, 64스캔 4.4초, 300스캔 17.9초 (2026-11-09)
  UPLOAD_DECODE_SLOTS 동시에 디코드 검사하는 파일 2개. 프로그레시브는 계수 버퍼를 통째로 잡아 100MP면 약 570MB —
                     둘이면 약 1.1GB로 docflow 메모리 상한(5Gi) 안. 프로그레시브 픽셀 상한을 따로 낮추지는 않았다
                     (스캔 소프트웨어가 프로그레시브로 내보낸 A3 600dpi도 받아야 하고, 메모리는 이 슬롯이 묶는다)
  UPLOAD_TMP_MB      동시에 받는 업로드 본문 합계 1536MB → 429. Starlette가 1MB 넘는 파일 조각을 /tmp에 흘려 쓰는데
                     k8s의 /tmp는 emptyDir 2Gi(넘으면 파드를 내쫓음)
본문 읽기: 처리 함수가 로그인·역할·CSRF 확인 뒤에 직접 읽는다(main.upload) → 로그인 안 한 요청은 본문을 /tmp에 쓰지 않는다.
  BodyCap은 실제 받은 바이트를 세며 본문 상한·/tmp 예산에서 끊는다 (Content-Length가 없어도 — 프록시가 chunked로 바꿀 수 있다).
"""
import os
import re
import tempfile
import threading

import cv2
import numpy as np
from fastapi import HTTPException

MB = 1024 * 1024
MAX_BYTES = int(float(os.environ.get("UPLOAD_MAX_MB", "20")) * MB)
MAX_FILES = int(os.environ.get("UPLOAD_MAX_FILES", "50"))
MAX_BODY = int(float(os.environ.get("UPLOAD_MAX_BODY_MB", "210")) * MB)
MAX_PIXELS = int(float(os.environ.get("UPLOAD_MAX_MPX", "100")) * 1_000_000)
MAX_SCANS = int(os.environ.get("UPLOAD_MAX_SCANS", "64"))
DECODE_SLOTS = threading.BoundedSemaphore(int(os.environ.get("UPLOAD_DECODE_SLOTS", "2")))
TMP_BUDGET = int(float(os.environ.get("UPLOAD_TMP_MB", "1536")) * MB)
JPEG_MAGIC = b"\xff\xd8\xff"
CHUNK = MB
# 쪽 파일 이름 규칙 (여기 한 곳): 글자·숫자·_ . - 로 된 1~96자 + .jpg (\w는 한글도 받는다)
# 확장자는 .jpg·.jpeg를 대소문자 없이 받고(화면은 /\.jpe?g$/i로 고른다), 저장은 늘 <쪽 이름>.jpg —
# OCR·목록·쪽 경로(store.page_path)가 모두 '*.jpg'로 찾으므로 저장 이름 규칙은 그대로 둔다
PAGE_RX = re.compile(r"([\w.-]{1,96})\.(?i:jpe?g)")
PAGE_MAX_UTF8 = 160  # 쪽 이름 바이트 상한: 임시 파일 이름(.<이름>.jpg.<8자>.part)이 파일 시스템 한도 255바이트를 넘지 않게
PAGE_RULE = "글자·숫자·_ . - 1~96자(UTF-8 160바이트 안) + .jpg/.jpeg"


def page_of(filename):
    """올린 파일 이름 → 쪽 이름(확장자 뺀 것). 규칙에 안 맞으면 None"""
    m = PAGE_RX.fullmatch(filename)
    return m.group(1) if m and len(m.group(1).encode("utf-8")) <= PAGE_MAX_UTF8 else None
# 프레임 시작(SOF) 표지 — 크기가 여기 있다. C4(허프만 표)·C8(예약)·CC(산술 조건)는 SOF가 아니다
SOF = set(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
# 스캔의 압축 데이터 다음 표지: FF(+채움 FF) 뒤 00(바이트 채움)·D0~D7(RST, 스캔 안)이 아닌 것
# FF 하나만 본다 — '\xff+'는 FF가 길게 이어진 입력에서 자리마다 되짚어 길이의 제곱이 된다(FF 4만 개에 7.5초).
# 채움 FF가 앞에 더 있어도 마지막 FF에서 맞고, 걷기(jpeg_info)는 거기서 채움을 건너뛰며 표지를 읽는다
SCAN_END = re.compile(rb"\xff[^\x00\xd0-\xd7\xff]")


class BodyCap:
    """업로드 경로의 요청 본문을 받은 바이트로 묶는다 (순수 ASGI — 본문 읽기(receive)만 감싼다).
    Content-Length가 상한을 넘으면 처음 읽을 때 바로 413. 없거나 거짓이어도 실제 받은 바이트가 상한을 넘는 순간 413.
    reserved: 지금 받고 있는 업로드 본문의 합 (한 프로세스 안, 이벤트 루프 하나라 잠금이 필요 없다) — /tmp 예산을 넘으면 429.
    거절은 receive 안에서 HTTPException으로 낸다 → 본문을 읽는 처리 함수(main.upload)의 감사·예외 처리기를 그대로 탄다"""
    reserved = 0

    def __init__(self, app, match):
        self.app = app
        self.match = match  # (method, path) → 이 경로인가

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.match(scope["method"], scope["path"]):
            return await self.app(scope, receive, send)
        raw = dict(scope["headers"]).get(b"content-length")
        state = {"first": True, "seen": 0, "held": 0}

        async def capped():
            if state["first"]:
                state["first"] = False
                if raw is not None and raw.isdigit() and int(raw) > MAX_BODY:
                    raise HTTPException(413, f"요청이 너무 큼 (상한 {MAX_BODY // MB}MB)")
            msg = await receive()
            if msg["type"] == "http.request":
                n = len(msg.get("body", b""))
                state["seen"] += n
                if state["seen"] > MAX_BODY:
                    raise HTTPException(413, f"요청이 너무 큼 (상한 {MAX_BODY // MB}MB)")
                if BodyCap.reserved + n > TMP_BUDGET:
                    raise HTTPException(429, "다른 업로드를 받는 중 — 잠시 뒤 다시")
                BodyCap.reserved += n
                state["held"] += n
            return msg

        try:
            await self.app(scope, capped, send)
        finally:
            BodyCap.reserved -= state["held"]


def jpeg_info(data):
    """JPEG 표지를 SOI부터 첫 EOI까지 걷는다 — 이미지를 펼치지 않고. {w, h, scans, progressive} 또는 None
    (JPEG 구조가 아님·SOF 없음·EOI 전에 끝남 = 잘린 파일). 스캔이 MAX_SCANS를 넘으면 거기서 멈추고 scans = MAX_SCANS + 1.
    APP 조각(EXIF 썸네일 등)은 길이만큼 건너뛰므로 안에 든 다른 JPEG의 표지를 잘못 읽지 않는다. EOI 뒤는 보지 않는다"""
    n = len(data)
    if not data.startswith(b"\xff\xd8"):
        return None
    i, info = 2, {"w": 0, "h": 0, "scans": 0, "progressive": False}
    while True:
        if i >= n or data[i] != 0xFF:
            return None  # 표지 자리에 다른 바이트 — 깨진 구조, 또는 EOI 없이 끝남
        while i < n and data[i] == 0xFF:  # 채움 바이트
            i += 1
        if i >= n:
            return None
        m = data[i]
        i += 1
        if m == 0xD9:  # EOI
            return info if info["w"] and info["h"] and info["scans"] else None
        if m == 0x01 or 0xD0 <= m <= 0xD8:  # 길이 없는 표지
            continue
        if i + 2 > n:
            return None
        ln = int.from_bytes(data[i:i + 2], "big")
        if ln < 2 or i + ln > n:
            return None
        if m in SOF:
            if ln < 7 or info["w"]:
                return None  # 짧은 SOF, 프레임 두 개
            info["h"], info["w"] = int.from_bytes(data[i + 3:i + 5], "big"), int.from_bytes(data[i + 5:i + 7], "big")
            info["progressive"] = m in (0xC2, 0xC6, 0xCA, 0xCE)
            if not info["w"] or not info["h"]:
                return None  # 높이 0(DNL로 뒤에서 정함)은 받지 않는다
        i += ln
        if m == 0xDA:
            if not info["w"]:
                return None  # SOF보다 먼저 온 스캔
            info["scans"] += 1
            if info["scans"] > MAX_SCANS:
                return info
            end = SCAN_END.search(data, i)
            if not end:
                return None  # 압축 데이터 중간에서 끝남 (잘린 파일)
            i = end.start()


def decodes(data):
    """실제로 펼쳐지는가. 1/8 크기 흑백으로 펼친다(libjpeg DCT 축소) — 압축 데이터는 끝까지 읽으면서 출력 메모리는 1/192.
    양자화 표가 없거나 프레임이 깨진 파일은 None (허프만 표가 없으면 libjpeg가 표준 표를 쓴다).
    스캔 데이터 중간의 손상은 libjpeg가 경고만 내고 회색으로 채워 통과할 수 있다 (끝 잘림은 jpeg_info가 막는다)"""
    with DECODE_SLOTS:
        img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_REDUCED_GRAYSCALE_8)
    return img is not None and img.size > 0


def check(data, label):
    """다 받은 파일 바이트를 검사한다. 틀리면 HTTPException(400). label: 오류 문구에 넣을 이름(규칙을 통과한 이름만)"""
    info = jpeg_info(data)
    if info is None:
        raise HTTPException(400, f"JPEG가 아님 (구조가 깨졌거나 끝이 잘림): {label}")
    if info["w"] * info["h"] > MAX_PIXELS:
        raise HTTPException(400, f"픽셀 수가 너무 많음 (상한 {MAX_PIXELS // 1_000_000}MP): {label}")
    if info["scans"] > MAX_SCANS:
        raise HTTPException(400, f"스캔이 너무 많음 (상한 {MAX_SCANS}): {label}")
    if not decodes(data):
        raise HTTPException(400, f"이미지를 펼칠 수 없음: {label}")


def spool(src, folder, name, label):
    """올린 파일 하나를 folder 안 임시 파일(.<name>.*.part — *.jpg 목록에 안 잡힘)에 조각 단위로 쓰고 검사한다.
    folder는 최종 위치와 같은 파일 시스템(쪽 폴더) → 검사 뒤 옮기기는 원자적 링크·이름 바꾸기다.
    검사에 떨어지면 임시 파일을 지우고 HTTPException. 통과하면 임시 파일 경로.
    label: 오류 문구에 넣을 이름 (이름 규칙을 통과한 이름만 — 파일 내용은 넣지 않는다)"""
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=f".{name}.", suffix=".part")
    try:
        size = 0
        with os.fdopen(fd, "wb") as out:
            while chunk := src.read(CHUNK):
                if size == 0 and not chunk.startswith(JPEG_MAGIC[:len(chunk)]):
                    raise HTTPException(400, f"JPEG가 아님: {label}")
                size += len(chunk)
                if size > MAX_BYTES:
                    raise HTTPException(413, f"파일이 너무 큼 (상한 {MAX_BYTES // MB}MB): {label}")
                out.write(chunk)
        if size < len(JPEG_MAGIC):
            raise HTTPException(400, f"JPEG가 아님: {label}")
        with open(tmp, "rb") as f:
            check(f.read(), label)  # MAX_BYTES 안이라 한 번에 읽는다
        return tmp
    except BaseException:
        os.unlink(tmp)
        raise


def place_new(tmp, dest):
    """같은 이름이 없을 때만 놓는다 — 하드 링크는 이미 있으면 실패하므로 확인과 놓기 사이에 끼어든 같은 이름을 덮지 않는다.
    하드 링크를 못 쓰는 파일 시스템이면 확인 뒤 이름 바꾸기로. 이미 있으면 FileExistsError"""
    try:
        os.link(tmp, dest)
    except FileExistsError:
        raise
    except OSError:
        if os.path.exists(dest):
            raise FileExistsError(dest) from None
        os.replace(tmp, dest)
        return
    os.unlink(tmp)
