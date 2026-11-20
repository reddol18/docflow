"""하네스 공통: 데이터 위치와 읽기. 실제 문서(OCR 글자·정답·플랫폼 결과)는 저장소 밖 work/ 폴더에만 둔다.

  DOCFLOW_EVAL_WORK  기본 records_poc/work — platform/<vol>.json(저장된 철 결과), ocr_platform/<vol>/<쪽>.json(docflow OCR)
  DOCFLOW_EVAL_VOLS  쉼표로 철 고르기 (비우면 platform/ 전부)
채점 규칙은 PoC records_poc/scripts/rescore.py (이 레포 밖이라 고치지 않는다). 정답은 load_gt()가 work 폴더의 gt/review.csv에서
읽는다 — 기본 work면 rescore.py가 고정으로 읽는 PoC 확정 정답과 같은 파일, 합성 work(eval/synth/to_work.py)면 합성 정답.
외부 후보 막기: 합성 철이 아닌 철이 하나라도 있으면 `python -I common.py --require-synthetic`이 종료 코드 2 (run.ps1이 부른다).
"""
import csv
import functools
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_ROOT = HERE.parents[1]  # doc_platform
POC_ROOT = HERE.parents[2]  # records_poc (scripts/rescore.py — 정답·채점 규칙)
POC_WORK = POC_ROOT / "work"
WORK = Path(os.environ.get("DOCFLOW_EVAL_WORK") or POC_WORK)
SYNTH_PREFIX = "SYNTH"  # 합성 철 이름 (eval/synth/bundle.py)
for p in (APP_ROOT, APP_ROOT / "eval", POC_ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

SCHEMA_FILE = APP_ROOT / "app" / "field_schemas" / "gimhae.yaml"


def gt_path():
    """정답 파일: work 폴더의 gt/review.csv (기본 work면 rescore.py가 읽는 PoC 확정 정답과 같은 파일,
    합성 work면 eval/synth/to_work.py가 이어 붙인 합성 정답 — https://github.com/reddol18/docflow/issues/80)"""
    return WORK / "gt" / "review.csv"


@functools.cache
def load_gt():
    """rescore.load_gt와 같은 (rows, idx, bnd, src) — 다만 정답 파일은 work 폴더 기준 (gt_path)"""
    p = gt_path()
    if not p.exists():
        if WORK.resolve() != POC_WORK.resolve():  # 다른 work(합성 등)에 정답이 없으면 PoC 정답으로 조용히 채점하지 않는다
            raise SystemExit(f"정답 파일이 없다: {p} (합성 work면 eval/synth/to_work.py를 끝까지 돌린다)")
        from rescore import load_gt as poc  # noqa: PLC0415 — 기본 work에 정답이 없을 때만 PoC 고정 위치
        return poc()
    with p.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    idx = {r["page"]: r for r in rows if r["is_start"] == "Y"}
    bnd = {r["page"]: r["is_start"] == "Y" for r in rows if "경계" in r["확인"]}
    src = {r["page"]: "claude" if "Claude 판독" in r["note"] else "human" for r in rows}
    return rows, idx, bnd, src


def synthetic_vols():
    """eval/synth/to_work.py가 만든 합성 철 이름 (synth_manifest.json, 이름이 SYNTH로 시작하는 것만). 없으면 빈 집합"""
    p = WORK / "synth_manifest.json"
    if not p.exists():
        return set()
    return {v["vol"] for v in json.loads(p.read_text(encoding="utf-8")).get("vols", [])
            if str(v.get("vol", "")).startswith(SYNTH_PREFIX)}


def non_synthetic(selected=None):
    """고른 철 중 합성 철이 아닌 것 — 외부(OpenAI·원격 GPU) 후보는 합성 철에만 보낸다 (2026-11-20 #80 결정)"""
    return sorted(set(selected if selected is not None else vols()) - synthetic_vols())


def vols():
    want = [v.strip() for v in os.environ.get("DOCFLOW_EVAL_VOLS", "").split(",") if v.strip()]
    have = sorted(p.stem for p in (WORK / "platform").glob("*.json"))
    if want:
        missing = sorted(set(want) - set(have))
        if missing:
            raise SystemExit(f"DOCFLOW_EVAL_VOLS에 없는 철: {missing} ({WORK / 'platform'})")
        return want
    return have


def stored_docs(vol):
    """저장된 철 결과 — 손글씨 판독(hw)·머리부 비율 등 LLM 추출 밖의 값은 여기서 그대로 쓴다"""
    return json.loads((WORK / "platform" / f"{vol}.json").read_text(encoding="utf-8"))


def ocr(vol, doc_id):
    return json.loads((WORK / "ocr_platform" / vol / f"{doc_id}.json").read_text(encoding="utf-8"))


def load_vol(vol):
    """(저장된 철 결과, {doc_id: 첫 쪽 OCR}) — gate.py가 철마다 한 번 읽어 후보 모두에 쓴다"""
    docs = stored_docs(vol)
    return docs, {d["doc_id"]: ocr(vol, d["doc_id"]) for d in docs}


def user_text(vol, doc_id):
    """app/llm.extract와 같은 입력 — 문서 첫 쪽 OCR 줄 앞 80줄"""
    return "\n".join(ocr(vol, doc_id)["lines"][:80])


def main(argv=None):
    """`--require-synthetic`: 고른 철이 모두 합성 철이면 0, 아니면 2. 다른 인자·철 고르기 오류도 0이 아닌 값 (닫힌 쪽으로 실패)"""
    import argparse  # noqa: PLC0415

    ap = argparse.ArgumentParser()
    ap.add_argument("--require-synthetic", action="store_true", required=True)
    ap.parse_args(argv)  # 모르는 인자 → 종료 코드 2
    try:
        selected = vols()
        bad = non_synthetic(selected)
    except SystemExit as e:  # 없는 철 등 — 문구는 그대로, 종료 코드는 2
        print(e, file=sys.stderr)
        return 2
    if bad or not selected:  # 철 이름 수만 (문서 글자 없음)
        print(f"합성 철이 아닌 철 {len(bad)}개 (고른 철 {len(selected)}개) — 외부 후보(OpenAI·원격)는 합성 철에만 보낸다",
              file=sys.stderr)
        return 2
    print(f"합성 철 {len(selected)}개")
    return 0


if __name__ == "__main__":
    sys.exit(main())
