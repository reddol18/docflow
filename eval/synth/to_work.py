"""합성 철(eval/synth/bundle.py 출력) → 모델 비교 하네스(eval/promptfoo/run.ps1 -Work)가 읽는 work 폴더 (https://github.com/reddol18/docflow/issues/80).

  <work>/ocr_platform/<철>/<쪽>.json   쪽 OCR — 운영과 같은 app/ocr.py(PaddleOCR 한국어, CPU, 이 PC 로컬). 이미 있으면 건너뛴다
  <work>/platform/<철>.json            저장된 철 결과 모양. 문서 나누기는 운영 split(layout.is_start), 손글씨 칸 위치는 운영 locate
                                       (layout.value_box, 머리부 비율 넓히기까지). LLM 값은 비워 둔다 — 하네스가 후보 응답으로 채운다.
                                       손글씨 칸 판독(GPT 비전, hw)은 하지 않는다: 모든 후보가 같은 빈 판독으로 채점된다
                                       (텍스트 모델 비교용. 비전 판독 비교는 #80 완료 조건 4에서 따로)
  <work>/gt/review.csv                 철마다 gt.csv를 이어 붙인 정답 (eval/score.py·rescore.py와 같은 열)
  <work>/synth_manifest.json           합성 철 목록·시드·쪽 수. run.ps1은 외부(OpenAI·원격) 후보를 이 목록의 철에만 보낸다

meta.json의 synthetic이 true인 철만 받는다 — 실제 자료(AI Hub 등)는 이 길로 들어오지 않는다.
실행 (records_poc 폴더의 PaddleOCR 가상환경, doc_platform 폴더에서):
  ..\\.venv\\Scripts\\python.exe -I -X utf8 eval/synth/to_work.py <합성 철 폴더> <work 폴더>
"""
import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]
if str(APP_ROOT) not in sys.path:  # python -I는 스크립트 폴더도 현재 폴더도 path에 넣지 않는다
    sys.path.insert(0, str(APP_ROOT))

from app import fields, layout  # noqa: E402

HEAD_RATIOS = [0.40, 0.48, 0.56]  # app/graph.py HEAD_RATIOS와 같다 (graph는 langgraph·DB를 불러와 여기서 쓰지 않는다)
CELLS = ("docno", "date")  # app/graph.py CELLS (김해 스키마)
SCHEMA = fields.load(fields.DIR / "gimhae.yaml")
GT_COLS = ["page", "확인", "is_start", "docno", "date", "recipient", "title", "check", "note", "saved"]


def bundles(src):
    """src 아래 합성 철 폴더 (meta.json synthetic=true). 합성이 아닌 폴더가 섞이면 멈춘다"""
    out = []
    for d in sorted(p for p in Path(src).iterdir() if p.is_dir()):
        meta_p = d / "meta.json"
        if not meta_p.exists():
            continue
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        if meta.get("synthetic") is not True or meta.get("vol") != d.name:
            raise SystemExit(f"합성 철이 아니다 (meta.json synthetic·vol 확인): {d}")
        out.append((d, meta))
    if not out:
        raise SystemExit(f"합성 철이 없다: {src}")
    return out


def locate(items):
    """app/graph.py locate·widen과 같다: 못 찾은 칸이 있으면 머리부 비율을 넓혀 다시 찾는다 → (비율, 상자, 사유)"""
    for i, ratio in enumerate(HEAD_RATIOS):
        boxes, fail = {}, {}
        for k in CELLS:
            boxes[k], fail[k] = layout.value_box(items, k, ratio)
        if all(boxes.values()) or i + 1 == len(HEAD_RATIOS):
            return ratio, boxes, fail
    raise AssertionError  # 닿지 않는다


def split(vol, ocrs):
    """app/graph.py split과 같다 → 문서 목록 (첫 시작 쪽 앞 쪽은 버린다 = 운영 orphans)"""
    segs = []
    for p, d in ocrs.items():
        if layout.is_start(d)[0]:
            segs.append({"doc_id": p, "pages": [p]})
        elif segs:
            segs[-1]["pages"].append(p)
    return segs


def stored_doc(vol, seg, first_ocr):
    ratio, _, fail = locate(first_ocr["items"])
    b = fields.parse_extract(SCHEMA, "{}")  # 빈 추출 — 하네스가 후보 응답으로 바꾼다
    return {"doc_id": seg["doc_id"], "vol": vol, "pages": seg["pages"],
            "fields": {f["name"]: "" for f in SCHEMA["fields"]}, "b": dict(b),
            "hw": {k: {"text": "", "conf": 0.0} for k in CELLS},
            "info": {"prefix": "", "blank": False, "conf_serial": 0.0, "conf_date": 0.0, "b_serial": "", "b_date": "",
                     "recipient_fix": "", "head_ratio": ratio, "box_fail": fail, "llm_skipped": [], "llm_failed": [],
                     "synthetic": True},
            "flags": {}}


def ocr_vol(d, vol, work, ocr_page):
    """쪽마다 OCR (있으면 건너뜀) → {쪽: OCR}"""
    out_dir = Path(work) / "ocr_platform" / vol
    out_dir.mkdir(parents=True, exist_ok=True)
    res = {}
    for img in sorted(d.glob(f"{vol}-*.jpg")):
        out = out_dir / f"{img.stem}.json"
        if not out.exists():
            t0 = time.time()
            r = ocr_page(img)
            tmp = out.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps({"page": img.stem, "seconds": round(time.time() - t0, 2), **r}, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, out)
            print(f"ocr {img.stem} {time.time() - t0:.1f}s", flush=True)
        res[img.stem] = json.loads(out.read_text(encoding="utf-8"))
    return res


def run(src, work, ocr_page=None):
    if ocr_page is None:
        from app.ocr import ocr_page  # noqa: PLC0415 — PaddleOCR는 무겁다 (시험은 가짜 OCR을 넘긴다)
    work = Path(work)
    (work / "platform").mkdir(parents=True, exist_ok=True)
    (work / "gt").mkdir(parents=True, exist_ok=True)
    rows, manifest = [], {"source": "eval/synth/bundle.py", "vols": []}
    for d, meta in bundles(src):
        vol = meta["vol"]
        ocrs = ocr_vol(d, vol, work, ocr_page)
        docs = [stored_doc(vol, s, ocrs[s["doc_id"]]) for s in split(vol, ocrs)]
        (work / "platform" / f"{vol}.json").write_text(json.dumps(docs, ensure_ascii=False, indent=1), encoding="utf-8")
        with (d / "gt.csv").open(encoding="utf-8-sig", newline="") as f:
            gt = list(csv.DictReader(f))
        rows += gt
        starts = sum(r["is_start"] == "Y" for r in gt)
        manifest["vols"].append({"vol": vol, "seed": meta["seed"], "level": meta["level"], "generator": meta["generator"],
                                 "pages": len(ocrs), "docs_gt": starts, "docs_split": len(docs)})
        print(f"{vol}: 쪽 {len(ocrs)}, 정답 문서 {starts}, 나눈 문서 {len(docs)}", flush=True)
    with (work / "gt" / "review.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=GT_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    (work / "synth_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return manifest


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("src", help="bundle.py 출력 폴더 (철 폴더들이 든 곳)")
    ap.add_argument("work", help="하네스 work 폴더 (저장소 밖)")
    a = ap.parse_args(argv)
    if Path(a.work).resolve().is_relative_to(APP_ROOT):
        print("work 폴더는 저장소 밖에 둔다", file=sys.stderr)
        return 2
    os.environ.setdefault("DISABLE_MODEL_SOURCE_CHECK", "True")  # PaddleX 모델 출처 접속 확인 끔 (모델은 받아 둔 것)
    m = run(a.src, a.work)
    print(f"철 {len(m['vols'])}, 쪽 {sum(v['pages'] for v in m['vols'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
