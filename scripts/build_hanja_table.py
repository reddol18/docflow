"""한자 → 한글 음 표 만들기 (색인 표기 규칙 hanja_hangul, https://github.com/reddol18/docflow/issues/9).

원천: Unicode Unihan 데이터베이스 16.0.0의 kHangul 항목 (Unihan_Readings.txt).
  https://www.unicode.org/Public/16.0.0/ucd/Unihan.zip
  SHA-256 B8F000DF69DE7828D21326A2FFEA462B04BC7560022989F7CC704F10521EF3E0
  라이선스: Unicode License v3 (https://www.unicode.org/license.txt) — 표 파일 머리에 고지를 남긴다.
음이 여럿인 글자(예: 樂 락·악·요)는 대표 음 하나만 쓴다 (pick). 두음법칙은 적용하지 않는다 (李 → 리).

실행 (doc_platform 폴더): python -I scripts/build_hanja_table.py <내려받은 Unihan.zip>
결과: app/field_schemas/hanja_hangul.tsv (한 줄에 '한자<TAB>한글')
"""
import hashlib
import sys
import zipfile
from pathlib import Path

SHA256 = "b8f000df69de7828d21326a2ffea462b04bc7560022989f7cc704f10521ef3e0"
OUT = Path(__file__).resolve().parents[1] / "app" / "field_schemas" / "hanja_hangul.tsv"
HEADER = """# 한자 → 한글 음 (대표 음 하나). 원천: Unicode Unihan 16.0.0 kHangul, scripts/build_hanja_table.py가 만든다.
# Copyright © 1991-2024 Unicode, Inc. Unicode License v3 — https://www.unicode.org/license.txt
"""


def pick(value):
    """kHangul 값('락:0E 낙:0 악:0N')에서 대표 음 하나. 출처 표시 E = 교육용 기초 한자 음, N = 두음법칙 변형이 아닌 음,
    표시 없음(낙:0) = 두음법칙 변형. E → N → 처음 순으로 고른다"""
    toks = [t.split(":") + [""] for t in value.split()]
    for flag in ("E", "N"):
        for reading, tags, *_ in toks:
            if flag in tags:
                return reading
    return toks[0][0]


def main():
    src = Path(sys.argv[1])
    data = src.read_bytes()
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise SystemExit(f"{src}: SHA-256이 다름 (16.0.0 Unihan.zip이 아님)")
    with zipfile.ZipFile(src) as z:
        text = z.read("Unihan_Readings.txt").decode("utf-8")
    rows = []
    for line in text.splitlines():
        if line.startswith("#") or "\tkHangul\t" not in line:
            continue
        code, _, value = line.split("\t")
        ch = chr(int(code[2:], 16))
        first = pick(value)
        if len(first) == 1 and "가" <= first <= "힣":
            rows.append(f"{ch}\t{first}")
    OUT.write_text(HEADER + "\n".join(rows) + "\n", encoding="utf-8", newline="\n")
    print(f"{OUT.name}: {len(rows)}자")


if __name__ == "__main__":
    main()
