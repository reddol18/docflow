"""OCR을 뺀 API 메모리 재기 (https://github.com/reddol18/docflow/issues/5 쿼터 A안 확인, 이 PC). 앱(lifespan 포함)을 띄우고
큰 쪽 이미지 올리기(디코드 검사)·상태·검토 쪽 이미지·/ask 기다림을 돌려 프로세스 최대 RSS를 본다. 쪽 OCR·그래프는 워커 몫이라
부르지 않는다. 데이터 폴더는 임시(끝나면 지움). 이미지는 읽기만 한다 (저장소에 넣지 않음).
사용 (PGlite를 TEST_PG_URL로 띄운 뒤 — scripts/check.ps1과 같은 방식, prepared statement 끔):
  uv run --no-project --with-requirements requirements-test.txt --with psutil python -I -X utf8 scripts/measure_api_memory.py
      <doc_platform 경로> <쪽 이미지 폴더> <업로드당 장 수> <동시 업로드 수>
2026-11-13 결과: 50장 × 3개 동시 → 최대 821MiB (시작 직후 159MiB)"""
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

import psutil

root, img_dir, n, par = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
sys.path.insert(0, root)
os.environ["PG_URL"] = os.environ["TEST_PG_URL"]
data = Path(tempfile.mkdtemp(prefix="docflow-mem-"))
os.environ["DATA_DIR"] = str(data)
os.environ["ASK_WAIT_SECONDS"] = "1"
proc = psutil.Process()
peak = {"rss": 0}
stop = threading.Event()


def watch():
    while not stop.wait(0.02):
        peak["rss"] = max(peak["rss"], proc.memory_info().rss)


threading.Thread(target=watch, daemon=True).start()
from fastapi.testclient import TestClient  # noqa: E402

from app import auth, store  # noqa: E402

store.pool.kwargs["prepare_threshold"] = None
store.lock_pool.kwargs["prepare_threshold"] = None
store.qdrant = lambda: type("Q", (), {"collection_exists": lambda self, n: True, "scroll": lambda self, *a, **k: ([], None)})()
auth.MODES = frozenset({"local"})
auth.session_user = lambda t: auth.User(1, "a", "a", "admin")
from app import main  # noqa: E402

H = {"authorization": "Bearer a", "x-docflow-csrf": "1"}
imgs = sorted(img_dir.rglob("*.jpg"), key=lambda p: p.stat().st_size, reverse=True)[:n]
try:
    with TestClient(main.app) as c:
        after_start = proc.memory_info().rss
        res = []

        def up(k):
            files = [("files", (f"p{k}-{i:03d}.jpg", p.read_bytes(), "image/jpeg")) for i, p in enumerate(imgs)]
            res.append(c.post(f"/volumes/mem{k}/pages", headers=H, files=files).status_code)
        ts = [threading.Thread(target=up, args=(k,)) for k in range(par)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        after_up = peak["rss"]
        for k in range(par):
            c.get(f"/volumes/mem{k}", headers=H)
            for i in range(min(5, n)):
                c.get(f"/head/mem{k}/p{k}-{i:03d}", headers=H)  # 검토 화면 쪽 위 이미지 (디코드·줄이기)
        c.get("/volumes", headers=H)
        ask = c.get("/ask", params={"q": "가상 질문"}, headers=H).status_code  # 워커 없음 → 1초 기다린 뒤 202
        print(f"uploads={res} ask={ask} pages_per_upload={len(imgs)} concurrent={par}")
        print(f"after_start={after_start / 2**20:.0f}MiB peak_after_upload={after_up / 2**20:.0f}MiB")
finally:
    stop.set()
    shutil.rmtree(data, ignore_errors=True)
mi = proc.memory_info()
print(f"peak_rss={max(peak['rss'], mi.rss) / 2**20:.0f}MiB peak_wset={getattr(mi, 'peak_wset', 0) / 2**20:.0f}MiB")
