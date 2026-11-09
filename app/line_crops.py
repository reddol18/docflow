"""수신·제목 줄 잘라 두기 (https://github.com/reddol18/docflow/issues/35). LLM·OCR을 부르지 않는다.

저장된 OCR 상자(문서 첫 쪽, /data/ocr)로 줄을 찾고(app/layout.py line_box) 쪽 이미지에서 잘라
/data/crops/{철}/{doc_id}_{종류}.png로 둔다 — 손글씨 칸 잘라낸 이미지와 같은 자리·같은 경로(/crops)로 보여 준다.
어느 칸에 붙일지는 스키마 crop:(app/fields.py line_crops). 흐름(app/graph.py read)과 보충 스크립트(app/backfill_crops.py)가 같이 쓴다.
"""
import cv2

from app import fields, layout, ocr, store


def kinds(schema):
    """이 스키마로 잘라 둘 줄 종류 (layout.LINE_KINDS 순서)"""
    return list(fields.line_crops(schema).values())


def save(vol, doc_id, todo, force=True):
    """문서 doc_id(첫 쪽)의 줄 종류 todo를 잘라 저장한다. {종류: 'saved'|'exists'|못 찾은 사유}.
    못 찾으면 예전 파일을 지운다 (다시 돌린 철에 옛 이미지가 남지 않게) — 화면은 이미지 자리를 숨긴다.
    force=False면 이미 있는 파일은 건너뛴다 (보충 스크립트를 다시 돌려도 같은 결과)"""
    out, img = {}, None
    d = None
    for k in todo:
        p = store.crop_path(vol, doc_id, k)
        if not force and p.exists():
            out[k] = "exists"
            continue
        if d is None:
            d = store.load_ocr(vol, doc_id)
        box, _src, why = layout.line_box(d, k)
        if box is None:
            p.unlink(missing_ok=True)
            out[k] = why
            continue
        if img is None:
            img = ocr.read_image(store.page_path(vol, doc_id))
            if img is None:
                return {**out, **{x: "쪽 이미지 없음" for x in todo if x not in out}}
        H, W = img.shape[:2]
        x0, y0, x1, y1 = max(box[0], 0), max(box[1], 0), min(box[2], W), min(box[3], H)
        if x1 <= x0 or y1 <= y0:
            p.unlink(missing_ok=True)
            out[k] = "상자가 쪽 밖"
            continue
        ok, buf = cv2.imencode(".png", img[y0:y1, x0:x1])
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes(buf.tobytes())
        tmp.replace(p)  # 화면이 반쯤 쓴 파일을 읽지 않게
        out[k] = "saved"
    return out
