"""워커 최대 메모리 재기 (https://github.com/reddol18/docflow/issues/5 쿼터 A안 확인, 이 PC): app.ocr.ocr_page를 큰 쪽 이미지
N장에 차례로 — 운영 워커의 쪽 작업과 같은 호출. paddleocr·paddlepaddle이 lock과 같은 판인 가상 환경에서 돌린다.
사용: <paddle 가상 환경 python> -I -X utf8 scripts/measure_worker_memory.py <doc_platform 경로> <쪽 이미지 폴더> <장 수>
2026-11-13 결과 (records_poc/.venv, OCR_THREADS=4, 큰 쪽 8장): 최대 2399MiB"""
import os
import sys
import threading
import time
from pathlib import Path

import psutil

root, img_dir, n = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3])
sys.path.insert(0, root)
os.environ.setdefault("OCR_THREADS", "4")
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
proc = psutil.Process()
peak = {"rss": 0}
stop = threading.Event()


def watch():
    while not stop.wait(0.05):
        peak["rss"] = max(peak["rss"], proc.memory_info().rss)


threading.Thread(target=watch, daemon=True).start()
base = proc.memory_info().rss
from app import ocr  # noqa: E402

imgs = sorted(img_dir.rglob("*.jpg"), key=lambda p: p.stat().st_size, reverse=True)[:n]
t0 = time.time()
for i, p in enumerate(imgs, 1):
    t = time.time()
    d = ocr.ocr_page(p)
    print(f"page {i}: {time.time() - t:.1f}s boxes={len(d['items'])} rss={proc.memory_info().rss / 2**20:.0f}MiB", flush=True)
stop.set()
mi = proc.memory_info()
print(f"pages={len(imgs)} total={time.time() - t0:.0f}s base={base / 2**20:.0f}MiB "
      f"peak_rss={max(peak['rss'], mi.rss) / 2**20:.0f}MiB peak_wset={getattr(mi, 'peak_wset', 0) / 2**20:.0f}MiB")
