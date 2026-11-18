"""합성 기록물 손글씨 글꼴 받기 (모두 SIL OFL 1.1, Google Fonts 저장소) — https://github.com/reddol18/docflow/issues/139.

받은 파일은 SHA256을 고정 값과 맞춰 보고, 다르면 지우고 실패한다 (scripts/tool_pins.txt와 같은 방식).
저장 위치: %LOCALAPPDATA%\\docflow-tools\\fonts (저장소에는 넣지 않는다).
실행: python -I eval/synth/fonts.py
"""
import hashlib
import os
import sys
import urllib.request
from pathlib import Path

BASE = "https://raw.githubusercontent.com/google/fonts/5bd87dfc525c8a0d1d8529d3684716e00c3fc59a/ofl/"  # 커밋 고정 (2026-11-19)
PINS = {  # 파일 이름: (저장소 경로, sha256)
    "NanumPenScript-Regular.ttf": ("nanumpenscript/NanumPenScript-Regular.ttf",
                                   "6f0d1ab29c7894010dc88831fb7a0a51edb79136e450344183de5b1a8b52bd43"),
    "NanumBrushScript-Regular.ttf": ("nanumbrushscript/NanumBrushScript-Regular.ttf",
                                     "27ceaf578c96f594cdf07fe0181b251790acbb746a164e45c1f6473f89911a31"),
    "Gaegu-Regular.ttf": ("gaegu/Gaegu-Regular.ttf", "aa52c98336f7c62e2896fc8b12b56a75d5b476d88a2f104b0980f4f7ce0adfc3"),
    "HiMelody-Regular.ttf": ("himelody/HiMelody-Regular.ttf", "360d2c0a880918aa48328d1d9219f5390788d09a1c9353e12b471de018673ae6"),
    "GamjaFlower-Regular.ttf": ("gamjaflower/GamjaFlower-Regular.ttf",
                                "ece32819ed58536355a49a095b0cdfdd3b8ef9081c5ed9ca1cef8f5d999ae1ac"),
}


MAX_BYTES = 20 * 1024 * 1024  # 글꼴 하나 상한 (지금 가장 큰 것 약 12.6MB)


def font_dir():
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "docflow-tools" / "fonts"


def sha256(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def installed():
    """받아 두었고 해시가 맞는 글꼴 경로들. 해시가 틀린 파일은 쓰지 않는다."""
    d = font_dir()
    return [str(d / n) for n, (_, h) in sorted(PINS.items()) if (d / n).is_file() and sha256(d / n) == h]


def fetch():
    d = font_dir()
    d.mkdir(parents=True, exist_ok=True)
    bad = 0
    for name, (path, h) in sorted(PINS.items()):
        dst = d / name
        if dst.is_file() and sha256(dst) == h:
            continue
        tmp = dst.with_suffix(".part")
        try:
            with urllib.request.urlopen(BASE + path, timeout=60) as r:  # 고정 https 주소(커밋 고정)만
                data = r.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                print(f"!! 너무 큼: {name}", file=sys.stderr)
                bad += 1
                continue
            tmp.write_bytes(data)
            if sha256(tmp) != h:
                print(f"!! 해시 불일치: {name}", file=sys.stderr)
                bad += 1
                continue
            tmp.replace(dst)
            print(f"받음 {name}")
        finally:
            tmp.unlink(missing_ok=True)  # 실패·예외 때 반쯤 받은 파일을 남기지 않는다
    return bad


if __name__ == "__main__":
    sys.exit(1 if fetch() else 0)
