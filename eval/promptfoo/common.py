"""하네스 공통: 데이터 위치와 읽기. 실제 문서(OCR 글자·정답·플랫폼 결과)는 저장소 밖 work/ 폴더에만 둔다.

  DOCFLOW_EVAL_WORK  기본 records_poc/work — platform/<vol>.json(저장된 철 결과), ocr_platform/<vol>/<쪽>.json(docflow OCR),
                     gt/review.csv(확정 정답)
  DOCFLOW_EVAL_VOLS  쉼표로 철 고르기 (비우면 platform/ 전부)
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_ROOT = HERE.parents[1]  # doc_platform
WORK = Path(os.environ.get("DOCFLOW_EVAL_WORK") or HERE.parents[2] / "work")  # records_poc/work
for p in (APP_ROOT, APP_ROOT / "eval", WORK.parent / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

SCHEMA_FILE = APP_ROOT / "app" / "field_schemas" / "gimhae.yaml"


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


def user_text(vol, doc_id):
    """app/llm.extract와 같은 입력 — 문서 첫 쪽 OCR 줄 앞 80줄"""
    return "\n".join(ocr(vol, doc_id)["lines"][:80])
