"""합성 철(eval/synth/bundle.py 출력) → 모델 비교 하네스(eval/promptfoo/run.ps1 -Work)가 읽는 work 폴더 (https://github.com/reddol18/docflow/issues/80).

  <work>/ocr_platform/<철>/<쪽>.json   쪽 OCR — 운영과 같은 app/ocr.py(PaddleOCR 한국어, CPU, 이 PC 로컬). 이미지 해시(image_sha256)가
                                       같으면 건너뛰고, 다르면(다시 만든 철) 다시 읽는다
  <work>/platform/<철>.json            저장된 철 결과 모양. 문서 나누기는 운영 split(layout.is_start), 손글씨 칸 위치는 운영 locate
                                       (layout.value_box, 머리부 비율 넓히기까지). LLM 값은 비워 둔다 — 하네스가 후보 응답으로 채운다.
                                       손글씨 칸 판독(GPT 비전, hw)은 하지 않는다: 모든 후보가 같은 빈 판독으로 채점된다
                                       (텍스트 모델 비교용. 비전 판독 비교는 #80 완료 조건 4에서 따로)
  <work>/gt/review.csv                 철마다 gt.csv를 이어 붙인 정답 (eval/score.py·scoring.load_gt와 같은 열)
  <work>/synth_manifest.json           합성 철 목록·시드·쪽 수. run.ps1은 외부(OpenAI·원격) 후보를 이 목록의 철에만 보낸다

meta.json의 synthetic이 true이고 이름이 SYNTH로 시작하는 철만 받는다 — 실제 자료(AI Hub 등)는 이 길로 들어오지 않는다.
work는 빈 폴더이거나 이미 합성 work(synth_manifest.json, platform/의 철이 모두 목록에 있음)여야 한다 — PoC work(records_poc/work)나
목록 없이 정답이 있는 폴더는 멈춘다. 같은 work에 다른 묶음을 더 넣으면 manifest·정답을 합친다 (같은 철 이름은 바꾼다).
실행 (records_poc 폴더의 PaddleOCR 가상환경, doc_platform 폴더에서):
  ..\\.venv\\Scripts\\python.exe -I -X utf8 eval/synth/to_work.py <합성 철 폴더> <work 폴더>
"""
import argparse
import csv
import functools
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]
for _p in (APP_ROOT, Path(__file__).resolve().parent):  # python -I는 스크립트 폴더도 현재 폴더도 path에 넣지 않는다
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import forms  # noqa: E402

from app import fields, layout  # noqa: E402

HEAD_RATIOS = [0.40, 0.48, 0.56]  # app/graph.py HEAD_RATIOS와 같다 (graph는 langgraph·DB를 불러와 여기서 쓰지 않는다)
CELLS = ("docno", "date")  # app/graph.py CELLS (김해 스키마)


@functools.cache
def schema_of(form):
    """철 양식(meta.json form, 없으면 gimhae) → 칸 정의 (forms.SCHEMA_FILE). 새 양식 철을 김해 칸 정의로 만들지 않게"""
    return fields.load(fields.DIR / forms.SCHEMA_FILE[form])


SCHEMA = schema_of(forms.DEFAULT_FORM)  # 김해 (옛 철·form 없는 meta)
GT_COLS = ["page", "확인", "is_start", "docno", "date", "recipient", "title", "check", "note", "saved"]
SYNTH_PREFIX = "SYNTH"  # bundle.py 철 이름 (SYNTH<seed>-<연도>-<번호>)
POC_WORK = APP_ROOT.parent / "work"  # PoC 확정 정답·AI Hub 결과가 든 work — 여기엔 쓰지 않는다
MIN_FREE_GB = 2  # 쪽 OCR JSON은 철 10개에 수 MB지만, 같은 드라이브의 다른 실행 몫까지 보고 넉넉히


def bundles(src):
    """src 아래 합성 철 폴더 (meta.json synthetic=true). 합성이 아닌 폴더가 섞이면 멈춘다"""
    out = []
    for d in sorted(p for p in Path(src).iterdir() if p.is_dir()):
        meta_p = d / "meta.json"
        if not meta_p.exists():
            continue
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        if meta.get("synthetic") is not True or meta.get("vol") != d.name or not d.name.startswith(SYNTH_PREFIX):
            raise SystemExit(f"합성 철이 아니다 (meta.json synthetic·vol, 이름 {SYNTH_PREFIX}… 확인): {d}")
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


def stored_doc(vol, seg, first_ocr, schema=None):
    """schema: 그 철 양식의 칸 정의 (schema_of(form)). 없으면 김해"""
    schema = schema or SCHEMA
    ratio, _, fail = locate(first_ocr["items"])
    b = fields.parse_extract(schema, "{}")  # 빈 추출 — 하네스가 후보 응답으로 바꾼다
    return {"doc_id": seg["doc_id"], "vol": vol, "pages": seg["pages"],
            "fields": {f["name"]: "" for f in schema["fields"]}, "b": dict(b),
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
    imgs = sorted(d.glob(f"{vol}-*.jpg"))
    for old in out_dir.glob("*.json"):  # 다시 만든 철에 없는 쪽의 옛 OCR은 지운다
        if old.stem not in {i.stem for i in imgs}:
            old.unlink()
    for img in imgs:
        out = out_dir / f"{img.stem}.json"
        h = sha256(img)
        prev = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
        if not prev or prev.get("image_sha256") != h:  # 같은 이름이라도 이미지가 바뀌었으면(다른 level 등) 다시 읽는다
            t0 = time.time()
            r = ocr_page(img)
            prev = {"page": img.stem, "image_sha256": h, "seconds": round(time.time() - t0, 2), **r}
            tmp = out.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(prev, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, out)
            print(f"ocr {img.stem} {time.time() - t0:.1f}s", flush=True)
        res[img.stem] = prev
    return res


def sha256(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _gt_only_of(gt_p, allow):
    """정답 파일의 줄이 모두 이번에 넣을 합성 철(allow, SYNTH…)의 것인가 — 빈 파일은 참"""
    with gt_p.open(encoding="utf-8-sig", newline="") as f:
        vols = {r.get("page", "").rsplit("-", 1)[0] for r in csv.DictReader(f)}
    return all(v in set(allow) and v.startswith(SYNTH_PREFIX) for v in vols)


def check_work(work, allow=()):
    """합성 work로 쓸 수 있는 폴더인지 — PoC 확정 정답(records_poc/work) 등 실제 자료 work를 덮어쓰지 않게.
    빈 폴더이거나, synth_manifest.json이 있고 platform/의 철이 모두 그 목록에 있어야 한다. 반환: 지금 manifest (없으면 None)"""
    work = Path(work)
    if work.resolve() == POC_WORK.resolve():
        raise SystemExit(f"PoC work 폴더에는 쓰지 않는다: {work}")
    man_p = work / "synth_manifest.json"
    manifest = json.loads(man_p.read_text(encoding="utf-8")) if man_p.exists() else None
    known = {v["vol"] for v in (manifest or {}).get("vols", [])}
    gt_p = work / "gt" / "review.csv"
    if manifest is None and gt_p.exists() and not _gt_only_of(gt_p, allow):  # 이번 철 정답뿐이면 = 죽은 앞 실행이 남긴 것
        raise SystemExit(f"합성 목록(synth_manifest.json) 없이 정답 파일이 있다 — 실제 자료 work일 수 있어 멈춘다: {work}")
    # allow = 이번에 넣을 합성 철 — 앞 실행이 manifest를 쓰기 전에 죽어 남은 그 철 파일은 다시 만들 것이라 막지 않는다
    others = sorted(p.stem for p in (work / "platform").glob("*.json")
                    if p.stem not in known and not (p.stem in set(allow) and p.stem.startswith(SYNTH_PREFIX)))
    if others:
        raise SystemExit(f"합성 목록에 없는 철 {len(others)}개가 platform/에 있다 — 합성 work가 아니면 쓰지 않는다: {work}")
    return manifest


def run(src, work, ocr_page=None):
    """src의 합성 철을 work에 더한다. 같은 이름의 철은 바꾸고, 앞서 넣은 다른 철(manifest·정답)은 그대로 둔다"""
    work = Path(work)
    found = bundles(src)  # 합성 아닌 철이 섞였으면 아무것도 쓰기 전에 멈춘다
    manifest = check_work(work, allow={m["vol"] for _, m in found})
    if ocr_page is None:
        from app.ocr import ocr_page  # noqa: PLC0415 — PaddleOCR는 무겁다 (시험은 가짜 OCR을 넘긴다)
    (work / "platform").mkdir(parents=True, exist_ok=True)
    (work / "gt").mkdir(parents=True, exist_ok=True)
    entries = {v["vol"]: v for v in (manifest or {}).get("vols", [])}
    gt_p = work / "gt" / "review.csv"
    rows = {}  # 철 → 정답 줄
    if manifest is not None and gt_p.exists():
        with gt_p.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):  # manifest에 있는 철의 정답만 잇는다 (죽은 실행이 남긴 줄은 버림)
                v = r["page"].rsplit("-", 1)[0]
                if v in entries:
                    rows.setdefault(v, []).append(r)
    for d, meta in found:
        vol, form = meta["vol"], forms.form_of(meta)
        ocrs = ocr_vol(d, vol, work, ocr_page)
        docs = [stored_doc(vol, s, ocrs[s["doc_id"]], schema_of(form)) for s in split(vol, ocrs)]
        (work / "platform" / f"{vol}.json").write_text(json.dumps(docs, ensure_ascii=False, indent=1), encoding="utf-8")
        with (d / "gt.csv").open(encoding="utf-8-sig", newline="") as f:
            rows[vol] = list(csv.DictReader(f))
        starts = sum(r["is_start"] == "Y" for r in rows[vol])
        # form: 하네스(eval/promptfoo/common.schema_of)가 철마다 이 양식의 칸 정의·지시문으로 추출·채점한다
        entries[vol] = {"vol": vol, "seed": meta["seed"], "level": meta["level"], "generator": meta["generator"],
                        "form": form, "pages": len(ocrs), "docs_gt": starts, "docs_split": len(docs)}
        print(f"{vol}: 쪽 {len(ocrs)}, 정답 문서 {starts}, 나눈 문서 {len(docs)}", flush=True)
    # 정답을 먼저 쓰고 manifest를 마지막에 — 중간에 죽으면 manifest가 옛것이라 새 철은 '합성 아님'으로 막힌다
    with gt_p.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=GT_COLS, extrasaction="ignore")
        w.writeheader()
        for vol in sorted(rows):
            w.writerows(rows[vol])
    manifest = {"source": "eval/synth/bundle.py", "vols": [entries[v] for v in sorted(entries)]}
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
    probe = Path(a.work).resolve()
    while not probe.exists():  # 아직 없는 폴더면 있는 조상의 드라이브를 본다
        probe = probe.parent
    free = shutil.disk_usage(probe).free / 1024**3
    print(f"디스크 여유: {probe.anchor} {free:.1f}GB (필요 {MIN_FREE_GB}GB)")
    if free < MIN_FREE_GB:
        print("work 폴더 드라이브의 디스크 여유가 모자라 멈춘다", file=sys.stderr)
        return 2
    # PaddleX 모델 출처 접속 확인 끔 (모델은 받아 둔 것). 이름은 Dockerfile과 같다 — paddleocr를 불러오기 전에 넣는다
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    m = run(a.src, a.work)
    print(f"철 {len(m['vols'])}, 쪽 {sum(v['pages'] for v in m['vols'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
