"""이미 처리한 철에 수신·제목 줄 잘라낸 이미지를 채운다 (https://github.com/reddol18/docflow/issues/35).

OCR을 다시 하지 않고 LLM·임베딩도 부르지 않는다 — 저장된 OCR 결과(/data/ocr)와 쪽 이미지(/data/pages)만 읽고
/data/crops에 쓴다. DB·그래프 상태는 건드리지 않는다.
문서 경계는 흐름의 split과 같은 규칙(app/layout.py is_start)으로 OCR 결과에서 다시 구한다 — 문서 id = 첫 쪽 이름.

실행 (docflow 파드 안):  python -m app.backfill_crops [--vol 철 …] [--force] [--dry-run]
  기본은 이미 있는 파일을 건너뛴다 (여러 번 돌려도 같은 결과). --force는 다시 자른다. --dry-run은 세기만 한다.
"""
import argparse
import collections
import sys

from app import fields, layout, line_crops, store


def docs_of(vol):
    """철의 문서 첫 쪽 이름들 (app/graph.py split과 같은 규칙)"""
    pages = sorted(p.stem for p in store.ocr_dir(vol).glob("*.json"))
    return [p for p in pages if layout.is_start(store.load_ocr(vol, p))[0]]


def vols():
    root = store.DATA / "ocr"
    return sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []


def run(names, kinds, force=False, dry=False, out=print):
    count = collections.Counter()
    for vol in names:
        docs = docs_of(vol)
        count["vols"] += 1
        count["docs"] += len(docs)
        for doc_id in docs:
            if dry:
                for k in kinds:
                    count["would_skip" if store.crop_path(vol, doc_id, k).exists() and not force else "would_crop"] += 1
                continue
            for k, res in line_crops.save(vol, doc_id, kinds, force=force).items():
                count[res if res in ("saved", "exists") else f"not_found:{k}:{res}"] += 1
        out(f"{vol}: 문서 {len(docs)}건")
    return count


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vol", action="append", help="이 철만 (여러 번 줄 수 있다). 없으면 /data/ocr의 철 전부")
    ap.add_argument("--force", action="store_true", help="이미 있는 파일도 다시 자른다")
    ap.add_argument("--dry-run", action="store_true", help="쓰지 않고 셈만")
    a = ap.parse_args(argv)
    kinds = line_crops.kinds(fields.SCHEMA)
    if not kinds:
        print("스키마에 crop: 칸이 없다 — 할 일 없음")
        return 0
    names = a.vol or vols()
    count = run(names, kinds, a.force, a.dry_run)
    print(f"종류 {kinds} | " + ", ".join(f"{k}={v}" for k, v in sorted(count.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
