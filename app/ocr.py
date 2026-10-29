"""PaddleOCR(한국어, CPU)로 쪽 이미지를 단어 상자와 줄 텍스트로 바꾼다. 설정은 PoC scripts/run_ocr.py와 같다."""
import functools
import os

import cv2
import numpy as np


@functools.cache
def engine():
    from paddleocr import PaddleOCR

    return PaddleOCR(
        lang="korean",
        device="cpu",
        cpu_threads=int(os.environ.get("OCR_THREADS", "4")),
        # 원본(2464x3504) 그대로는 쪽당 ~25초. 긴 변 1920으로 줄이면 ~10초, 라벨 대비 글자 재현율은 같거나 약간 높음
        text_det_limit_side_len=1920,
        text_det_limit_type="max",
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
    )


def group_lines(items):
    """단어 상자를 세로 중심 기준으로 줄로 묶고, 줄 안에서는 왼쪽부터 정렬한다."""
    items = sorted(items, key=lambda it: (it["box"][1] + it["box"][3]) / 2)
    lines = []
    for it in items:
        x0, y0, x1, y1 = it["box"]
        cy, h = (y0 + y1) / 2, y1 - y0
        if lines:
            last = lines[-1]
            if abs(cy - last["cy"]) < 0.5 * max(h, last["h"]):
                last["items"].append(it)
                n = len(last["items"])
                last["cy"] = (last["cy"] * (n - 1) + cy) / n
                last["h"] = max(last["h"], h)
                continue
        lines.append({"cy": cy, "h": h, "items": [it]})
    out = []
    for i, ln in enumerate(lines):
        ln["items"].sort(key=lambda it: it["box"][0])
        for it in ln["items"]:
            it["line"] = i
        out.append(" ".join(it["text"] for it in ln["items"]))
    return out


def read_image(path):
    return cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)


def ocr_page(path):
    res = engine().predict(read_image(path))[0]
    items = [
        {"text": t, "score": round(float(s), 4), "box": [int(v) for v in b]}
        for t, s, b in zip(res["rec_texts"], res["rec_scores"], res["rec_boxes"])
    ]
    lines = group_lines(items)
    return {"lines": lines, "items": items}
