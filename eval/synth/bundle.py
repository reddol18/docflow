"""합성 기록물 철 만들기 — 문서 여러 건(첫 쪽·이어지는 쪽·붙임 서식)을 한 철로 (https://github.com/reddol18/docflow/issues/139).

출력 (<out>/<철 이름>/ — 철 이름은 vol_name: gimhae SYNTH<seed>-<연도>-<번호>, 새 양식은 시드 뒤에 A·D)
  <쪽>.jpg          낡게 만든 쪽 (docflow에 그대로 올릴 수 있는 이름: <철>-<4자리 쪽 번호>)
  <쪽>.json         AI Hub 88 라벨과 같은 꼴(images + annotations, bbox [x,y,w,h]). 손글씨는 ttype handwritten,
                    칸 값 단어에는 annotation.field (docno·date·recipient·title),
                    손글씨 칸 단어에는 annotation.cell (render.HAND_CELLS — 같은 칸 단어를 이으면 정답, 상자를 합치면 칸 상자)
  gt.csv            채점 정답 — PoC 확정 정답 파일(eval/score.py가 읽는 것)과 같은 열.
                    모든 쪽 확인='경계'(문서 시작 판별 채점), 시작 쪽만 4칸.
                    note는 '합성; docno:판독 불가(25.3); date:판독 불가(12.0)' 꼴 — 낡아서 사람도 읽기 어려운 칸
                    (밝기 차 < age.LEGIBLE_MIN). PoC 채점기의 어려운 칸 형식(';' 분리, '칸:키워드')과 같다
  meta.json         쪽별 낡음 정도·기울기·글꼴·칸별 밝기 차, 생성기 판
  check/<쪽>.jpg    (--check) 단어 상자를 그린 확인용 이미지 (절반 크기)
실제 자료는 읽지 않는다. 같은 --seed·--level이면 같은 철이 나온다 (같은 글꼴 목록일 때).
같은 이름 철 폴더가 이미 있으면 옛 쪽 파일(.jpg·.json·check/)을 지우고 다시 만든다 (clear_old_pages).

실행 (Augraphy 환경, eval/synth/requirements.txt):
  uv run --no-project --with-requirements eval/synth/requirements.txt python -I eval/synth/bundle.py out_dir --seed 1 --docs 5 --level mix
  양식: --form gimhae(기본)·agency1990·draft2000·mix (render.FORMS)
"""
import argparse
import csv
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # python -I는 스크립트 폴더를 path에 넣지 않는다

import age  # noqa: E402
import fonts as font_pins  # noqa: E402
import forms  # noqa: E402
import render  # noqa: E402
from PIL import ImageDraw  # noqa: E402

# synth-0.2: 손글씨 칸 이름(annotation.cell)을 라벨에 남긴다 (https://github.com/reddol18/docflow/issues/80).
# 같은 시드면 쪽 이미지·정답은 0.1과 같다
# synth-0.3: 양식 고르기(--form: gimhae 기본·agency1990·draft2000·mix, https://github.com/reddol18/docflow/issues/139 조건 3),
# meta.json에 form. --form gimhae(기본)면 같은 시드의 쪽 이미지·라벨·정답은 0.2와 같다
VERSION = "synth-0.3"
FORM_CHOICES = (*render.FORMS, "mix")  # mix: 철마다 시드로 양식을 고른다
GT_COLS = ["page", "확인", "is_start", "docno", "date", "recipient", "title", "check", "note", "saved"]


SEED_MAX = 4_000_000  # 0 ≤ seed < SEED_MAX. 낡게 하기 시드(seed*1000+쪽)·이름(SYNTH<seed>)이 감당하는 범위


def check_seed(seed):
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < SEED_MAX:
        raise ValueError(f"seed는 0 이상 {SEED_MAX:,} 미만 정수: {seed!r}")
    return seed


def seed_arg(s):
    """argparse용 --seed 형식 검사."""
    try:
        return check_seed(int(s))
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def hard_note(hard):
    """gt.csv note. 판독 어려운 칸은 '; 칸:판독 불가(밝기 차)' — PoC 채점기 hard_fields 형식(';' 분리, '칸:키워드')."""
    return "합성" + "".join(f"; {f}:판독 불가({v:.1f})" for f, v in sorted(hard.items()))


def pick_form(seed, form="gimhae"):
    """철 양식. mix면 시드로 고른다 (철 하나 = 기관 하나라 양식도 하나). 양식 난수는 따로 — gimhae 철의 난수 순서를 바꾸지 않는다"""
    if form == "mix":
        return random.Random(f"{seed}-form").choice(render.FORMS)
    if form not in render.FORMS:
        raise ValueError(f"모르는 양식 {form!r} (가능: {FORM_CHOICES})")
    return form


def plan(seed, n_docs, form="gimhae"):
    """문서별 쪽 계획 → (city, year, pages). pages는 쪽 순서대로 [(Case, kind, cont, attach)]:
    kind는 first·cont·attach, cont는 첫 쪽 본문이 다음 쪽으로 이어지는지, attach는 붙임 서식 쪽이 따르는지
    (first·cont 쪽에서만 참일 수 있다). 그리기 전 단계라 시험에서 따로 본다.
    form: render.FORMS 중 하나 (mix는 pick_form으로 먼저 푼다). gimhae는 예전과 같은 난수 순서.
    mix·모르는 양식이 바로 오면 ValueError (양식 난수를 plan 난수와 섞지 않게 — make처럼 pick_form을 먼저 부른다)"""
    if form not in render.FORMS:
        raise ValueError(f"plan 양식은 {render.FORMS} 중 하나 (mix는 pick_form으로 먼저 푼다): {form!r}")
    r = random.Random(seed)
    lo, hi = render.YEARS[form]
    city, year = r.choice(render.CITY), r.randint(lo, hi)
    org = render.new_org(r, form, city) if form != "gimhae" else None
    pages = []
    for _ in range(n_docs):
        c = render.new_case(r, city=city, year=year) if form == "gimhae" else \
            render.new_case_form(r, form, city=city, year=year, org=org)
        cont, attach = r.random() < 0.3, r.random() < 0.35
        pages.append((c, "first", cont, attach))
        if cont:
            pages.append((c, "cont", False, attach))
        if attach:
            pages.append((c, "attach", False, False))
    return city, year, pages


def draw(c, kind, cont, attach, fonts, r, typewriter):
    if kind == "first":
        return render.FIRST_PAGE[c.form](c, fonts, r, typewriter, cont=cont, attach=attach)
    if kind == "cont":
        return render.cont_page(c, fonts, r, typewriter, attach=attach)
    return render.attach_page(c, fonts, r, typewriter)


def label(page_id, img_w, img_h, year, ann, boxes, form="gimhae"):
    return {
        # AI Hub 88 라벨처럼 images는 배열 하나. 만든 시각은 같은 시드 같은 결과를 위해 비워 둔다
        "images": [{"image.make.code": "SYNTH", "image.make.year": str(year),
                    "image.category": "합성.기안문" if form == "draft2000" else "합성.시행문",
                    "image.width": img_w, "image.height": img_h, "image.file.name": f"{page_id}.jpg",
                    "image.create.time": ""}],
        "annotations": [
            {"id": i, "annotation.type": "rectangle", "annotation.text": a["text"], "annotation.ttype": a["ttype"],
             "annotation.bbox": [b[0], b[1], b[2] - b[0], b[3] - b[1]], **({"annotation.field": a["field"]} if "field" in a else {}),
             **({"annotation.cell": a["cell"]} if "cell" in a else {})}
            for i, (a, b) in enumerate(zip(ann, boxes))],
    }


def vol_name(seed, year, bundle_no, form="gimhae"):
    """철 이름. gimhae는 예전 이름 그대로 SYNTH<seed>-<연도>-<번호> (w47 재현), 새 양식은 시드 뒤에 양식 표시
    (forms.VOL_TAG: agency1990 'A', draft2000 'D' — 예: SYNTH0011A-1995-0001). 같은 시드 다른 양식 철이 같은 폴더에 겹치지 않는다"""
    return f"SYNTH{seed:04d}{forms.VOL_TAG[form]}-{year}-{bundle_no:04d}"


def clear_old_pages(d, vol):
    """같은 이름 철 폴더를 다시 만들 때 옛 쪽 파일(<철>-<4자리>.jpg·.json, check/<철>-<4자리>.jpg)을 지운다 — 쪽 수가 줄면
    옛 쪽이 남아 to_work가 OCR·문서로 섞는다 (gt.csv·meta.json은 새로 쓴다). 합성 철이 아닌 폴더(meta.json synthetic이 참이
    아님)면 아무것도 지우지 않고 멈춘다. 지운 파일 수를 돌려준다"""
    d = Path(d)
    if not d.is_dir():
        return 0
    meta_p = d / "meta.json"
    if meta_p.exists() and json.loads(meta_p.read_text(encoding="utf-8")).get("synthetic") is not True:
        raise SystemExit(f"합성 철이 아닌 폴더라 덮어쓰지 않는다 (meta.json synthetic): {d}")
    old = [p for p in (*d.glob(f"{vol}-[0-9][0-9][0-9][0-9].*"), *(d / "check").glob(f"{vol}-[0-9][0-9][0-9][0-9].jpg"))
           if p.is_file() and p.suffix in (".jpg", ".json")]
    for p in old:
        p.unlink()
    return len(old)


def make(out, seed, n_docs=5, level="mix", fonts=None, check=False, bundle_no=1, form="gimhae"):
    check_seed(seed)
    form = pick_form(seed, form)
    fonts = fonts or render.Fonts(hand=font_pins.installed())
    city, year, pages = plan(seed, n_docs, form)
    vol = vol_name(seed, year, bundle_no, form)
    d = Path(out) / vol
    clear_old_pages(d, vol)
    d.mkdir(parents=True, exist_ok=True)
    rows, meta = [], {"vol": vol, "generator": VERSION, "seed": seed, "level": level, "form": form, "synthetic": True,
                      "pages": []}
    lr = random.Random(seed * 7 + 3)
    for n, (c, kind, cont, attach) in enumerate(pages, 1):
        pid = f"{vol}-{n:04d}"
        lv = lr.choice(age.LEVELS) if level == "mix" else level
        r = random.Random(f"{seed}-{n}")
        p = draw(c, kind, cont, attach, fonts, r, typewriter=lv != "light")
        img, boxes, legib, ang = age.age(p.img, p.ann, lv, seed * 1000 + n)
        img.save(d / f"{pid}.jpg", quality=age.QUALITY[lv])  # 판독 밝기 차를 잰 품질과 같게
        (d / f"{pid}.json").write_text(json.dumps(label(pid, img.size[0], img.size[1], year, p.ann, boxes, form),
                                                  ensure_ascii=False, indent=1), encoding="utf-8")
        start = kind == "first"
        hard = {f: v for f, v in legib.items() if v < age.LEGIBLE_MIN} if start else {}
        row = {k: "" for k in GT_COLS}
        row.update(page=pid, 확인="경계", is_start="Y" if start else "N",
                   note=hard_note(hard))
        if start:
            row.update(c.fields)
        rows.append(row)
        meta["pages"].append({"page": pid, "kind": kind, "level": lv, "rotation_deg": round(ang, 3),
                              "hand_font": Path(p.hand_font).name, "legibility": legib if start else {},
                              "hard_fields": sorted(hard)})
        if check:
            chk = img.copy()
            dr = ImageDraw.Draw(chk)
            for a, b in zip(p.ann, boxes):
                dr.rectangle(b, outline=(0, 160, 0) if a["ttype"] == "textType1" else (220, 0, 160), width=3)
            (d / "check").mkdir(exist_ok=True)
            chk.resize((img.size[0] // 2, img.size[1] // 2)).save(d / "check" / f"{pid}.jpg", quality=80)
    with (d / "gt.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=GT_COLS)
        w.writeheader()
        w.writerows(rows)
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return d, rows, meta


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out")
    ap.add_argument("--seed", type=seed_arg, default=1, help=f"0 이상 {SEED_MAX:,} 미만")
    ap.add_argument("--docs", type=int, default=5)
    ap.add_argument("--level", choices=[*age.LEVELS, "mix"], default="mix")
    ap.add_argument("--check", action="store_true", help="상자 확인용 이미지도 만든다")
    ap.add_argument("--form", choices=FORM_CHOICES, default="gimhae",
                    help="첫 쪽 양식 (mix: 시드로 고름). 기본 gimhae = 예전 철과 같음")
    a = ap.parse_args(argv)
    hand = font_pins.installed()  # 해시 계산은 여기서 한 번만
    if not hand:
        print("손글씨 글꼴이 없습니다: python -I eval/synth/fonts.py 로 먼저 받으세요", file=sys.stderr)
        return 1
    d, rows, meta = make(a.out, a.seed, a.docs, a.level, fonts=render.Fonts(hand=hand), check=a.check, form=a.form)
    starts = sum(r["is_start"] == "Y" for r in rows)
    hard = sum(bool(p["hard_fields"]) for p in meta["pages"])
    print(f"{d}: 쪽 {len(rows)}, 문서 {starts}, 판독 어려운 칸이 있는 문서 {hard}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
