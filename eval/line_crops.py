"""수신·제목 줄 자르기 판정 (https://github.com/reddol18/docflow/issues/35). LLM·OCR을 다시 부르지 않는다.

실행 (records_poc 폴더에서): .venv/Scripts/python.exe -I -X utf8 doc_platform/eval/line_crops.py [--detail] [--save DIR]
  정답    work/gt/review.csv (PoC 확정 정답) 시작 쪽의 recipient·title
  OCR     work/ocr_platform/{철}/{쪽}.json (플랫폼 OCR 결과 — 운영 /data/ocr와 같은 모양)
자른 상자는 app/layout.py line_box (운영 흐름·보충 스크립트와 같은 함수).

판정: 정답 문자열의 줄이 잘린 상자 안에 있나 — 글자 쌍(bigram, 비교용 정규화 뒤)으로 본다
  찾을 수 있는 쌍  정답 글자 쌍 가운데 그 칸이 있는 구역의 OCR 단어에 있는 것. 구역 = 제목은 쪽 위 60%(머리부),
                  수신은 정답 제목 줄 위(못 찾으면 쪽 위 60%), 수신처 참조 문서의 수신은 쪽 아래 40%(수신처 목록).
                  본문에 업체명 등이 다시 나와도 세지 않고, OCR 오독으로 없는 쌍도 셈에서 뺀다
  든다            그중 60% 이상이 잘린 상자 안 단어(중심점이 상자 안)에 있으면 — 정답의 주된 줄이 들어 있음
  덮음            (참고) 90% 이상 — 여러 줄 값을 다 담았나
찾을 수 있는 쌍이 2개 미만인 문서(머리부 OCR이 값을 거의 못 읽음)는 판정 불가로 따로 센다
(비율 분모에서 뺌, 엄격 비율에는 틀림으로 넣음).
'수신처 참조' 문서 = 머리부 수신 값(app/layout.py rule_recipient)이 '수신처 참조'인 문서 — 따로 센다.

출력은 집계 숫자만 (문서 번호·값은 --detail일 때만 화면에). --save DIR은 잘린 이미지를 로컬 폴더에 써서 눈으로 본다 (저장소에 넣지 않는다).
"""
import argparse
import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # records_poc
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # doc_platform
from app import layout  # noqa: E402

GT = ROOT / "work" / "gt" / "review.csv"
OCR = ROOT / "work" / "ocr_platform"
IMAGES = Path(r"E:\claude-dev\aihub_extracted\88\원천데이터\인.허가")
IN, COVER = 0.6, 0.9  # 정답 글자 쌍 중 잘린 상자 안에 든 비율: 줄이 듦 / 여러 줄 다 덮음
ZONE = 0.6  # 정답 줄을 찾는 구역: 쪽 위 60% (머리부), 수신처 참조 문서의 수신은 그 아래 (수신처 목록)


def bigrams(s):
    s = layout.norm(s)
    return [s[i:i + 2] for i in range(len(s) - 1)]


def center_in(box, crop):
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return crop[0] <= cx <= crop[2] and crop[1] <= cy <= crop[3]


def cy(it):
    return (it["box"][1] + it["box"][3]) / 2


def title_y(items, title, page_h):
    """정답 제목이 머리부에서 처음 나오는 단어의 세로 중심 (못 찾으면 None) — 수신 구역의 아래 끝"""
    g = set(bigrams(title))
    hits = [(cy(it), len(set(bigrams(it["text"])) & g)) for it in items if cy(it) < ZONE * page_h]
    best = max((n for _, n in hits), default=0)
    return min((y for y, n in hits if n >= max(2, 0.5 * best)), default=None)


def judge(d, gt, crop, zone="head", title=""):
    """(판정 'in'|'out'|'nogt', 덮음 여부). zone = 정답을 찾는 구역 (head 쪽 위 60%, above 제목 줄 위, bottom 쪽 아래 40%)"""
    g = set(bigrams(gt))
    items = [it for it in d["items"] if it["text"].strip()]
    _, page_h = layout.page_size(items) if items else (0, 0)
    lo, hi = (ZONE * page_h, float("inf")) if zone == "bottom" else (0, ZONE * page_h)
    if zone == "above":
        hi = title_y(items, title, page_h) or hi
    page = {b for it in items if lo <= cy(it) < hi for b in bigrams(it["text"])} & g  # 구역 OCR에서 찾을 수 있는 정답 글자 쌍
    if len(page) < 2:
        return "nogt", False
    if crop is None:
        return "out", False
    got = {b for it in items if center_in(it["box"], crop) for b in bigrams(it["text"])} & g
    return ("in" if len(got) >= IN * len(page) else "out"), len(got) >= COVER * len(page)


def pct(a, b):
    return f"{a}/{b} ({a / b:.1%})" if b else "0/0"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gt", default=str(GT))
    ap.add_argument("--ocr", default=str(OCR))
    ap.add_argument("--detail", action="store_true", help="틀린 문서 번호를 화면에 (커밋·PR에 넣지 말 것)")
    ap.add_argument("--save", help="잘린 이미지를 쓸 로컬 폴더 (눈으로 확인용, 저장소 밖)")
    a = ap.parse_args()
    rows = [r for r in csv.DictReader(Path(a.gt).open(encoding="utf-8-sig")) if r["is_start"] == "Y"]
    stat = {k: {"in": 0, "out": 0, "nogt": 0, "cover": 0, "found": 0} for k in ("recipient", "title", "list")}
    srcs = {"head": 0, "list": 0}
    bad = []
    for r in rows:
        p = r["page"]
        vol = p.rsplit("-", 1)[0]
        d = json.loads((Path(a.ocr) / vol / f"{p}.json").read_text(encoding="utf-8"))
        is_list = layout.is_list_ref(layout.rule_recipient(d)) or bool(re.match(r"처\s*참\s*조", layout.rule_recipient(d)))
        for kind in layout.LINE_KINDS:
            box, src, _ = layout.line_box(d, kind)
            zone = "head" if kind == "title" else "bottom" if is_list else "above"
            res, cover = judge(d, r[kind], box, zone, r["title"])
            groups = [kind] + (["list"] if kind == "recipient" and is_list else [])
            for k in groups:
                stat[k][res] += 1
                stat[k]["cover"] += cover
                stat[k]["found"] += box is not None
            if box is not None and kind == "recipient":
                srcs[src] += 1
            if res == "out":
                bad.append((p, kind, src))
            if a.save and box is not None:
                import cv2  # noqa: PLC0415 — 눈으로 볼 때만

                from app import ocr  # noqa: PLC0415
                img = ocr.read_image(IMAGES / vol.split("-")[0] / vol.split("-")[1] / f"{p}.jpg")
                if img is not None:
                    H, W = img.shape[:2]
                    x0, y0, x1, y1 = max(box[0], 0), max(box[1], 0), min(box[2], W), min(box[3], H)
                    out = Path(a.save) / f"{p}_{kind}{'_x' if res == 'out' else ''}.png"
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(cv2.imencode(".png", img[y0:y1, x0:x1])[1].tobytes())
    print(f"김해 GT 시작 쪽 {len(rows)}건 (수신처 참조 {stat['list']['in'] + stat['list']['out'] + stat['list']['nogt']}건)")
    print(f"수신 자른 상자 출처: 머리부 {srcs['head']}, 아래 목록 {srcs['list']}")
    for k, name in (("recipient", "수신"), ("title", "제목"), ("list", "수신처 참조")):
        s = stat[k]
        judged = s["in"] + s["out"]
        print(f"{name:8s} 정답 줄이 듦 {pct(s['in'], judged)} | 엄격(판정 불가 포함) {pct(s['in'], judged + s['nogt'])} | "
              f"판정 불가 {s['nogt']} | 상자 찾음 {s['found']} | 여러 줄 다 덮음 {pct(s['cover'], judged)}")
    if a.detail:
        for p, kind, src in bad:
            print(f"  x {p} {kind} {src}")


if __name__ == "__main__":
    main()
