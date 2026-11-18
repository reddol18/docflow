"""합성 기록물 생성기 (eval/synth, https://github.com/reddol18/docflow/issues/139).

쪽 그리기·칸 정답·판독 잣대·글꼴 해시는 검사 환경(Pillow)에서 돈다. 낡게 하기·철 만들기는 Augraphy가 있어야 해서
그 환경에서만 돈다 (없으면 건너뜀). 그 환경에는 앱 의존성이 없어 conftest를 끈다:
  uv run --no-project --with-requirements eval/synth/requirements.txt --with pytest==9.1.1 \
    python -m pytest -q --noconftest tests/test_synth.py
"""
import csv
import random
import re
import sys
from pathlib import Path

import numpy as np
import pytest

SYNTH = Path(__file__).resolve().parents[1] / "eval" / "synth"
sys.path.insert(0, str(SYNTH))

import fonts  # noqa: E402
import render  # noqa: E402

pytestmark = pytest.mark.skipif(not Path(render.Fonts().print).is_file(), reason="바탕 글꼴(Windows)이 없음")
NOFONTS = render.Fonts(hand=[])  # 손글씨 글꼴 없이 — 받기에 기대지 않고 같은 결과


def page(seed, **kw):
    r = random.Random(seed)
    c = render.new_case(r)
    return c, render.first_page(c, NOFONTS, random.Random(f"{seed}-p"), **kw)


def test_same_seed_same_page():
    c1, p1 = page(7, typewriter=True)
    c2, p2 = page(7, typewriter=True)
    assert c1 == c2
    assert p1.ann == p2.ann
    assert np.array_equal(np.asarray(p1.img), np.asarray(p2.img))
    assert page(8)[0] != c1


@pytest.mark.parametrize("seed", range(12))
def test_field_words_match_answer(seed):
    """칸 값 단어(annotation.field)를 이으면 정답이 된다 — 정답과 그림이 어긋나지 않는다."""
    c, p = page(seed)
    words = {f: [a["text"] for a in p.ann if a.get("field") == f] for f in render.FIELDS}
    gt = c.fields
    assert "".join(words["docno"]) == gt["docno"].replace(" ", "")
    assert " ".join(words["recipient"]) == gt["recipient"]
    assert " ".join(words["title"]) == gt["title"]
    yy, mm, dd = (int(x) for x in re.findall(r"\d+", " ".join(words["date"])))
    assert (1900 + yy, mm, dd) == tuple(int(x) for x in gt["date"].split("-"))


def test_boxes_inside_page_and_not_empty():
    for kind in ("first", "cont", "attach"):
        r = random.Random(3)
        c = render.new_case(r)
        p = {"first": render.first_page, "cont": render.cont_page, "attach": render.attach_page}[kind](c, NOFONTS, r)
        assert p.ann
        for a in p.ann:
            x0, y0, x1, y1 = a["box"]
            assert 0 <= x0 < x1 <= render.W and 0 <= y0 < y1 <= render.H, (kind, a)
        assert (kind == "first") == any("field" in a for a in p.ann)  # 4칸은 첫 쪽에만


def test_place_names_are_not_real_and_no_real_data_paths():
    """지명은 지어낸 것만, 생성기 코드는 실제 자료 경로를 모른다."""
    assert set(render.CITY) == {"한빛시", "새솔시", "가람군", "누리시"}
    files = list(SYNTH.glob("*.py"))
    assert {f.name for f in files} >= {"render.py", "age.py", "bundle.py", "fonts.py"}
    for f in files:
        src = f.read_text(encoding="utf-8").lower().replace("\\", "/")
        for bad in ("aihub_extracted", "71299", "work/gt", "라벨링데이터", "원천데이터"):
            assert bad not in src, (f.name, bad)


def test_legibility_contrast():
    import age  # Augraphy는 함수 안에서만 불러온다

    _, p = page(1)
    clean = np.asarray(p.img.convert("L"))
    full = age.field_legibility(p.ann, clean, clean)
    assert set(full) == set(render.FIELDS) and min(full.values()) > 150
    blank = np.full_like(clean, 230)
    assert max(age.field_legibility(p.ann, clean, blank).values()) < 5
    faded = (clean.astype(np.float32) * 0.1 + 230 * 0.9).astype(np.uint8)  # 잉크가 거의 날아간 쪽 (밝기 차 약 20)
    assert max(age.field_legibility(p.ann, clean, faded).values()) < age.LEGIBLE_MIN


def test_fonts_ignores_wrong_hash(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    d = fonts.font_dir()
    d.mkdir(parents=True)
    (d / "Gaegu-Regular.ttf").write_bytes(b"not a font")
    assert fonts.installed() == []
    assert all(re.fullmatch(r"[0-9a-f]{64}", h) for _, h in fonts.PINS.values())
    assert all(p.startswith(fonts.BASE) for p in (fonts.BASE + v[0] for v in fonts.PINS.values()))
    assert fonts.BASE.startswith("https://")


def test_bundle_end_to_end(tmp_path):
    pytest.importorskip("augraphy")
    import bundle

    d, rows, meta = bundle.make(tmp_path / "a", seed=4, n_docs=3, level="mix", fonts=NOFONTS)
    d2, rows2, _ = bundle.make(tmp_path / "b", seed=4, n_docs=3, level="mix", fonts=NOFONTS)
    assert rows == rows2  # 같은 시드 → 같은 정답
    with (d / "gt.csv").open(encoding="utf-8-sig") as f:
        got = list(csv.DictReader(f))
    assert list(got[0]) == bundle.GT_COLS
    assert sum(r["is_start"] == "Y" for r in got) == 3 and got[0]["is_start"] == "Y"
    for r in got:
        assert re.fullmatch(r"SYNTH0004-19\d\d-0001-\d{4}", r["page"])
        assert r["확인"] == "경계" and r["note"].startswith("합성")
        assert (d / f"{r['page']}.jpg").is_file() and (d / f"{r['page']}.json").is_file()
        assert bool(r["docno"]) == (r["is_start"] == "Y")
    assert len(meta["pages"]) == len(got)
