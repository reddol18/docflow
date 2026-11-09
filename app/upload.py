"""쪽 이미지 올리기 검사 (https://github.com/reddol18/docflow/issues/26, ASVS V12.1·API4 자원 소비).

받는 것: 진짜 JPEG만 — 앞 바이트(SOI 시그니처) + 끝(EOI, 잘린 전송) + 머리의 SOF 크기(픽셀 수 상한, 펼치기 전에)
  + 실제 디코드 성공. 검사는 이 순서 — 압축 폭탄은 머리에서 걸러 펼치지 않는다.
상한(환경변수로 바꿈, 기본값 근거):
  UPLOAD_MAX_MB     파일 하나 20MB → 413. 600dpi 컬러 A3 스캔 JPEG(품질 90 안팎)도 10~15MB라 여유가 있다
  UPLOAD_MAX_FILES  요청 하나 50장 → 400. 바이트가 아니라 요청 모양의 규칙이라 413(본문 크기)이 아니라 400.
                    화면(UploadPanel)은 10장씩 나눠 보낸다
  UPLOAD_MAX_MPX    픽셀 수 100MP → 400 (압축 폭탄). A3 600dpi = 7016×9921 ≈ 70MP에 약 40% 여유.
                    컬러로 펼치면 약 300MB — OCR 단계(ocr.read_image)가 실제로 펼치는 크기의 상한이기도 하다
  UPLOAD_TMP_MB     동시에 받는 업로드 본문 합계 1536MB → 429. Starlette가 1MB 넘는 파일 조각을 /tmp에 흘려 쓰는데
                    k8s의 /tmp는 emptyDir 2Gi(넘으면 파드를 내쫓음) — 상한 크기 요청 하나(약 1GB)는 늘 들어가고 둘은 안 든다
본문 상한: 요청 본문은 처리 함수보다 먼저(로그인 확인보다도 먼저) /tmp로 읽힌다 → BodyCap이 Content-Length를 먼저 보고
  (파일 수 × 파일 상한 + 1MB) 넘으면 413, 없으면 411, 읽는 동안 선언보다 많이 오면 400으로 끊는다.
"""
import os
import tempfile

import cv2
import numpy as np
from fastapi import HTTPException

MB = 1024 * 1024
MAX_BYTES = int(float(os.environ.get("UPLOAD_MAX_MB", "20")) * MB)
MAX_FILES = int(os.environ.get("UPLOAD_MAX_FILES", "50"))
MAX_PIXELS = int(float(os.environ.get("UPLOAD_MAX_MPX", "100")) * 1_000_000)
TMP_BUDGET = int(float(os.environ.get("UPLOAD_TMP_MB", "1536")) * MB)
JPEG_MAGIC = b"\xff\xd8\xff"
CHUNK = MB
# 프레임 시작(SOF) 표지 — 크기가 여기 있다. C4(허프만 표)·C8(예약)·CC(산술 조건)는 SOF가 아니다
SOF = {m for m in range(0xC0, 0xD0)} - {0xC4, 0xC8, 0xCC}


def body_cap():
    """한 요청 본문 상한: 파일 상한 × 개수 상한 + 1MB(조각 머리·경계 문자열)"""
    return MAX_FILES * MAX_BYTES + MB


class BodyCap:
    """업로드 경로의 요청 본문을 처리 함수 앞에서 묶는다 (순수 ASGI — 본문 읽기(receive)만 감싼다).
    거절은 처음 receive에서 HTTPException으로 낸다 → FastAPI 예외 처리기·보안 헤더를 그대로 탄다.
    reserved: 지금 받고 있는 업로드 본문의 선언 크기 합 (한 프로세스 안, 이벤트 루프 하나라 잠금이 필요 없다)"""
    reserved = 0

    def __init__(self, app, match):
        self.app = app
        self.match = match  # (method, path) → 이 경로인가

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.match(scope["method"], scope["path"]):
            return await self.app(scope, receive, send)
        raw = dict(scope["headers"]).get(b"content-length")
        state = {"seen": 0, "held": 0}

        async def capped():
            if state["seen"] == 0 and not state["held"]:
                if raw is None or not raw.isdigit():
                    raise HTTPException(411, "Content-Length 필요")
                size = int(raw)
                if size > body_cap():
                    raise HTTPException(413, f"요청이 너무 큼 (상한 {body_cap() // MB}MB)")
                if BodyCap.reserved + size > TMP_BUDGET:
                    raise HTTPException(429, "다른 업로드를 받는 중 — 잠시 뒤 다시")
                BodyCap.reserved += size
                state["held"] = size
            msg = await receive()
            if msg["type"] == "http.request":
                state["seen"] += len(msg.get("body", b""))
                if state["seen"] > state["held"]:
                    raise HTTPException(400, "본문이 Content-Length보다 김")
            return msg

        try:
            await self.app(scope, capped, send)
        finally:
            BodyCap.reserved -= state["held"]


def jpeg_dims(path):
    """JPEG 머리에서 (너비, 높이)를 읽는다 — 이미지를 펼치지 않고. JPEG 구조가 아니거나 SOF가 없으면 None.
    APP 조각(EXIF 썸네일 등)은 길이만큼 건너뛰므로 안에 든 다른 JPEG의 SOF를 잘못 읽지 않는다"""
    with open(path, "rb") as f:
        if f.read(2) != b"\xff\xd8":
            return None
        while True:
            b = f.read(1)
            if not b:
                return None
            if b != b"\xff":
                return None  # 표지 사이에 다른 바이트 — 깨진 구조
            m = f.read(1)
            while m == b"\xff":  # 채움 바이트
                m = f.read(1)
            if not m:
                return None
            m = m[0]
            if m == 0x01 or 0xD0 <= m <= 0xD8:  # 길이 없는 표지
                continue
            if m in (0xD9, 0xDA):  # 끝(EOI)·스캔 시작(SOS)이 SOF보다 먼저
                return None
            ln = f.read(2)
            if len(ln) < 2 or int.from_bytes(ln, "big") < 2:
                return None
            n = int.from_bytes(ln, "big") - 2
            if m in SOF:
                seg = f.read(5)
                if len(seg) < 5:
                    return None
                h, w = int.from_bytes(seg[1:3], "big"), int.from_bytes(seg[3:5], "big")
                return (w, h) if w and h else None  # 높이 0(DNL로 뒤에서 정함)은 받지 않는다
            f.seek(n, os.SEEK_CUR)


def ends_jpeg(path):
    """끝(EOI, FFD9)으로 끝나는가 — 뒤에 붙은 0 채움은 허용. OpenCV(libjpeg)는 중간에 잘린 파일도 남은 곳을 회색으로 채워
    '펼쳐짐'으로 돌려주므로(경고만 낸다), 전송 중 잘린 스캔이 반쪽 이미지로 OCR되지 않게 여기서 막는다"""
    with open(path, "rb") as f:
        f.seek(max(0, os.path.getsize(path) - 64))
        return f.read().rstrip(b"\x00").endswith(b"\xff\xd9")


def decodes(path):
    """실제로 펼쳐지는가. 1/8 크기 흑백으로 펼친다(libjpeg DCT 축소) — 압축 데이터는 끝까지 읽으면서 메모리는 1/192.
    양자화 표가 없거나 프레임이 깨진 파일은 None (허프만 표가 없으면 libjpeg가 표준 표를 쓴다).
    잘린 스캔 데이터는 libjpeg가 경고만 내므로 ends_jpeg가 따로 본다"""
    img = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_REDUCED_GRAYSCALE_8)
    return img is not None and img.size > 0


def spool(src, folder, name, label):
    """올린 파일 하나를 folder 안 임시 파일(.<name>.*.part — *.jpg 목록에 안 잡힘)에 조각 단위로 쓰고 검사한다.
    folder는 최종 위치와 같은 파일 시스템(쪽 폴더) → 검사 뒤 옮기기는 원자적 이름 바꾸기·링크다.
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
        if size < len(JPEG_MAGIC) or not ends_jpeg(tmp):
            raise HTTPException(400, f"JPEG가 아님: {label}")
        dims = jpeg_dims(tmp)
        if dims is None:
            raise HTTPException(400, f"JPEG가 아님: {label}")
        if dims[0] * dims[1] > MAX_PIXELS:
            raise HTTPException(400, f"픽셀 수가 너무 많음 (상한 {MAX_PIXELS // 1_000_000}MP): {label}")
        if not decodes(tmp):
            raise HTTPException(400, f"이미지를 펼칠 수 없음: {label}")
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
