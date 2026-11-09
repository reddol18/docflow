"""이미 처리한 철에 수신·제목 줄 잘라낸 이미지를 채운다 (https://github.com/reddol18/docflow/issues/35).

OCR을 다시 하지 않고 LLM·임베딩도 부르지 않는다. 읽는 것: 저장된 문서 목록(Postgres의 LangGraph 체크포인트 — 화면
/volumes/{vol}/documents가 보여 주는 것과 같은 docs, 문서 키 (철, doc_id)), 저장된 OCR 결과(/data/ocr), 쪽 이미지(/data/pages).
쓰는 것: /data/crops 뿐 (DB는 읽기만). 수신 이미지는 문서에 저장된 수신 보정 규칙·값(info.recipient_fix, 수신 칸)과
같은 줄을 자른다 — 흐름과 같은 판단.

실행 (docflow 파드 안):  python -m app.backfill_crops [--vol 철 …] [--force] [--dry-run]
  기본은 이미 있는 파일을 건너뛴다 (여러 번 돌려도 같은 결과). --force는 다시 자른다. --dry-run은 세기만 한다.
  문서마다 실패해도 이어 가고, 실패 수·사유(분류)를 끝에 센다.
"""
import argparse
import collections
import re
import sys

from app import fields, line_crops, store

# 철 이름 규칙 (app/main.py VOL_RX와 같다 — 시험이 둘을 맞춘다)
VOL_RX = re.compile(r"(?!\.+\Z)[\w.-]{1,80}")


def stored_docs(vol):
    """체크포인트에 저장된 그 철의 문서 목록 (흐름이 만든 docs). 처리한 적 없으면 []"""
    from langgraph.checkpoint.postgres import PostgresSaver  # 파드 안에서만 필요 (시험은 가짜 목록을 넘긴다)

    tup = PostgresSaver(store.pool).get_tuple({"configurable": {"thread_id": vol}})
    return list(((tup.checkpoint.get("channel_values") or {}).get("docs") or [])) if tup else []


def vols():
    root = store.DATA / "ocr"
    return sorted(p.name for p in root.iterdir() if p.is_dir() and VOL_RX.fullmatch(p.name)) if root.is_dir() else []


def recipient_of(doc, schema):
    """문서의 (수신 보정 규칙, 최종 수신 값) — 수신 rule 칸이 없는 스키마는 (None, None) (머리부 OCR로 판단)"""
    f = fields.by_rule(schema, "recipient")
    if not f:
        return None, None
    return (doc.get("info") or {}).get("recipient_fix") or "", (doc.get("fields") or {}).get(f["name"]) or ""


def run(names, kinds, force=False, dry=False, out=print, docs_of=stored_docs, schema=None):
    schema = schema or fields.SCHEMA
    count = collections.Counter()
    for vol in names:
        docs = docs_of(vol)
        count["vols"] += 1
        count["docs"] += len(docs)
        for doc in docs:
            doc_id = doc["doc_id"]
            if dry:
                for k in kinds:
                    count["would_skip" if store.crop_path(vol, doc_id, k).exists() and not force else "would_crop"] += 1
                continue
            fix, value = recipient_of(doc, schema)
            try:
                res = line_crops.save(vol, doc_id, kinds, force=force, fix=fix, value=value)
            except Exception as e:  # noqa: BLE001 — 한 문서가 깨져도 나머지는 채운다
                count["failed"] += 1
                count[f"failed:{type(e).__name__}"] += 1
                continue
            for k, r in res.items():
                count[r if r in ("saved", "exists") else f"not_found:{k}:{r}"] += 1
        out(f"{vol}: 문서 {len(docs)}건")
    return count


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vol", action="append", help="이 철만 (여러 번 줄 수 있다). 없으면 /data/ocr의 철 전부")
    ap.add_argument("--force", action="store_true", help="이미 있는 파일도 다시 자른다")
    ap.add_argument("--dry-run", action="store_true", help="쓰지 않고 셈만")
    a = ap.parse_args(argv)
    bad = [v for v in a.vol or () if not VOL_RX.fullmatch(v)]
    if bad:
        ap.error(f"철 이름 형식이 아님: {bad}")
    kinds = line_crops.kinds(fields.SCHEMA)
    if not kinds:
        print("스키마에 crop: 칸이 없다 — 할 일 없음")
        return 0
    store.pool.open(wait=True)  # 체크포인트 읽기만
    try:
        count = run(a.vol or vols(), kinds, a.force, a.dry_run)
    finally:
        store.pool.close()
    print(f"종류 {kinds} | " + ", ".join(f"{k}={v}" for k, v in sorted(count.items())))
    return 1 if count["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
