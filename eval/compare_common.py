"""비전·임베딩 모델 비교(eval/hw_compare.py·eval/embed_compare.py)가 같이 쓰는 것 (https://github.com/reddol18/docflow/issues/80 조건 4·5).

- 합성 철만: 외부(OpenAI·원격 GPU)로 보내는 글자·이미지는 eval/synth 합성 철뿐이다
  (2026-11-20 https://github.com/reddol18/docflow/issues/80 결정). work 폴더의
  synth_manifest.json에 있고 이름이 SYNTH로 시작하는 철만 고르고, 합성 철 폴더(bundle.py 출력)는 meta.json synthetic=true여야 한다.
  PoC work(records_poc/work, AI Hub 결과)는 거부한다. 모델 비교 하네스 `eval/promptfoo/common.py --require-synthetic`과 같은 수준.
- 서버 주소: OpenAI(--openai) 또는 OpenAI 호환 주소(--base-url) — https, 또는 http면 127.0.0.1·localhost(SSH 터널·이 PC Ollama)만,
  주소 안 사용자·비밀번호는 거부 (`eval/promptfoo/run.ps1 -Remote`와 같은 규칙).
- 키: OpenAI는 OPENAI_API_KEY(프로세스 → Windows 사용자 환경변수), 원격은 DOCFLOW_REMOTE_API_KEY(없으면 'none').
  키 값은 찍지 않는다. 원격 호출에 OpenAI 키를 쓰지 않는다.
- 비용: OpenAI는 app/llm.py PRICE로 실제 토큰 비용을 세고 --max-usd를 넘을 호출은 부르지 않는다. 원격·이 PC는 토큰 단가 0
  (GPU 시간 비용은 실행 시간 × 시간당 가격으로 따로).
"""
import csv
import json
import os
import shutil
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:  # python -I는 스크립트 폴더도 현재 폴더도 path에 넣지 않는다
    sys.path.insert(0, str(APP_ROOT))

SYNTH_PREFIX = "SYNTH"
POC_WORK = APP_ROOT.parent / "work"  # PoC 확정 정답·AI Hub 결과 — 외부 비교에 쓰지 않는다
OPENAI_BASE = "https://api.openai.com/v1"
LOOPBACK = ("127.0.0.1", "localhost")


class NotSynthetic(SystemExit):
    """합성 철이 아닌 입력 — 아무것도 보내기 전에 멈춘다"""


REAL_DATA_DIRS = ("aihub_extracted",)  # AI Hub 원본 (폴더 이름만 — 실제 위치는 적지 않는다)


def reject_real_data(path, what="경로"):
    """실제 자료 경로면 멈춘다: 경로 안 어디든 aihub_extracted 폴더, 또는 records_poc/work 자체와 그 아래
    (PoC 확정 정답·AI Hub 결과). 이름이 다른 형제 폴더(records_poc/work_synth 등 합성 work)는 받는다. → 정리한 경로"""
    p = Path(path).resolve()
    parts = [x.lower() for x in p.parts]
    under_poc = p.is_relative_to(POC_WORK.resolve()) or any(
        a == "records_poc" and b == "work" for a, b in zip(parts, parts[1:]))
    if under_poc or any(x in REAL_DATA_DIRS for x in parts):
        raise NotSynthetic(f"{what}에 실제 자료 폴더(aihub_extracted, records_poc/work)를 주지 않는다 — 합성 자료만: {path}")
    return p


def check_base_url(url):
    """원격 OpenAI 호환 주소 검사 → 정리한 주소. https, 또는 http면 루프백(터널·이 PC)만. 사용자·비밀번호 거부"""
    try:
        u = urlsplit(url)
        _ = u.port
    except ValueError as e:
        raise SystemExit(f"--base-url을 읽을 수 없음: {e}") from None
    if u.username or u.password:
        raise SystemExit("--base-url에 사용자·비밀번호(u:p@)를 넣지 않는다 — 서버 키는 DOCFLOW_REMOTE_API_KEY로")
    if not (u.scheme == "https" or (u.scheme == "http" and (u.hostname or "") in LOOPBACK)):
        raise SystemExit("--base-url은 https://… 또는 http://127.0.0.1:<포트>/v1(SSH 터널·이 PC Ollama)만 받는다")
    return url.rstrip("/")


def manifest_vols(work):
    """work의 synth_manifest.json에 있는 합성 철 이름 (SYNTH로 시작하는 것만)"""
    p = Path(work) / "synth_manifest.json"
    if not p.exists():
        return set()
    return {v["vol"] for v in json.loads(p.read_text(encoding="utf-8")).get("vols", [])
            if str(v.get("vol", "")).startswith(SYNTH_PREFIX)}


def synthetic_vols(work, want=None, bundles=None):
    """고를 철 목록 — 모두 합성이어야 한다. want(이름 목록)가 없으면 platform/의 철 전부.
    bundles를 주면 그 철 폴더의 meta.json도 synthetic=true·vol 이름이 맞아야 한다. 아니면 NotSynthetic"""
    work = Path(work)
    if work.resolve() == POC_WORK.resolve():
        raise NotSynthetic(f"PoC work 폴더(실제 자료)는 외부 비교에 쓰지 않는다: {work}")
    have = sorted(p.stem for p in (work / "platform").glob("*.json"))
    vols = list(want) if want else have
    missing = sorted(set(vols) - set(have))
    if missing:
        raise SystemExit(f"work에 없는 철: {missing}")
    known = manifest_vols(work)
    bad = sorted(v for v in vols if v not in known or not v.startswith(SYNTH_PREFIX))
    if bad or not vols:
        raise NotSynthetic(f"합성 철이 아닌 철 {len(bad)}개 (고른 철 {len(vols)}개) — 외부 후보는 합성 철에만 보낸다")
    if bundles is not None:
        for v in vols:
            meta_p = Path(bundles) / v / "meta.json"
            meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.exists() else {}
            if meta.get("synthetic") is not True or meta.get("vol") != v:
                raise NotSynthetic(f"합성 철 폴더가 아니다 (meta.json synthetic·vol): {Path(bundles) / v}")
    return vols


MIN_FREE_GB = 2  # eval/synth/to_work.py와 같다 — 결과 JSON은 수 MB지만 같은 드라이브의 다른 실행 몫까지


def out_dir(work, sub, min_free_gb=MIN_FREE_GB):
    """결과 폴더 <work>/<sub> — 저장소 밖이어야 하고(문서 글자가 든다), 그 드라이브 여유가 min_free_gb 이상이어야 한다
    (실험 실행 전 디스크 여유 확인 — records_poc/CLAUDE.md)"""
    d = Path(work) / sub
    if d.resolve().is_relative_to(APP_ROOT):
        raise SystemExit("결과는 저장소 밖 work 폴더에 쓴다")
    probe = d.resolve()
    while not probe.exists():
        if probe.parent == probe:  # 없는 드라이브(Z:\ 등) — 뿌리의 부모는 자기 자신이라 더 올라가지 않는다
            raise SystemExit(f"결과 폴더 드라이브가 없다: {probe}")
        probe = probe.parent
    free = shutil.disk_usage(probe).free / 1024**3
    print(f"디스크 여유: {probe.anchor} {free:.1f}GB (필요 {min_free_gb}GB)", flush=True)
    if free < min_free_gb:
        raise SystemExit("결과 폴더 드라이브의 디스크 여유가 모자라 멈춘다")
    d.mkdir(parents=True, exist_ok=True)
    return d


def vols_arg(s):
    """--vols 'A, B' → ['A', 'B'] (앞뒤 공백·빈 값 뺌)"""
    return [v.strip() for v in (s or "").split(",") if v.strip()]


def load_gt(work):
    """합성 정답 gt/review.csv → {시작 쪽: 줄}"""
    with (Path(work) / "gt" / "review.csv").open(encoding="utf-8-sig", newline="") as f:
        return {r["page"]: r for r in csv.DictReader(f) if r["is_start"] == "Y"}


def stored_docs(work, vol):
    return json.loads((Path(work) / "platform" / f"{vol}.json").read_text(encoding="utf-8"))


def page_ocr(work, vol, page):
    return json.loads((Path(work) / "ocr_platform" / vol / f"{page}.json").read_text(encoding="utf-8"))


def _user_env(name):
    """Windows 사용자 환경변수 (이 셸이 시작된 뒤 넣은 값도). 다른 OS·없으면 None"""
    try:
        import winreg  # noqa: PLC0415
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return winreg.QueryValueEx(k, name)[0] or None
    except (ImportError, OSError):
        return None


def make_client(openai_flag, base_url, timeout=180):
    """(OpenAI SDK 클라이언트, 가격을 셀지). OpenAI면 OpenAI 키, 아니면 원격 키(없으면 'none') — 키는 찍지 않는다"""
    from openai import OpenAI  # noqa: PLC0415
    if openai_flag:
        key = os.environ.get("OPENAI_API_KEY") or _user_env("OPENAI_API_KEY")
        if not key:
            raise SystemExit("OPENAI_API_KEY가 없다 (Windows 사용자 환경변수)")
        return OpenAI(api_key=key, base_url=OPENAI_BASE, timeout=60, max_retries=4), True
    key = os.environ.get("DOCFLOW_REMOTE_API_KEY") or _user_env("DOCFLOW_REMOTE_API_KEY") or "none"
    return OpenAI(api_key=key, base_url=check_base_url(base_url), timeout=timeout, max_retries=2), False


class Budget:
    """OpenAI 지출 상한: 부르기 전에 예상 상한을 잡고(spent + held + est ≤ cap일 때만), 끝나면 실제 비용으로 바꾼다"""

    def __init__(self, cap):
        self.cap, self.spent, self.held = cap, 0.0, 0.0
        self.lock = threading.Lock()

    def reserve(self, est):
        with self.lock:
            if self.spent + self.held + est > self.cap:
                return False
            self.held += est
            return True

    def settle(self, est, usd):
        with self.lock:
            self.held -= est
            self.spent += usd


def add_args(ap):
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--openai", action="store_true", help="OpenAI API (지금 운영 제공자)")
    g.add_argument("--base-url", help="OpenAI 호환 주소: https://… 또는 http://127.0.0.1:<포트>/v1")
    ap.add_argument("--model", required=True)
    ap.add_argument("--label", required=True, help="결과 파일 이름 (영문·숫자·_-)")
    ap.add_argument("--work", required=True, help="합성 work 폴더 (eval/synth/to_work.py 출력)")
    ap.add_argument("--vols", default="", help="쉼표로 철 고르기 (비우면 work의 철 전부)")
    ap.add_argument("--max-usd", type=float, default=1.0, help="OpenAI 지출 상한 (이번 실행)")


def label_ok(label):
    import re  # noqa: PLC0415
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", label):
        raise SystemExit("--label은 영문·숫자·_.- 64자까지")
    return label


def p95(xs):
    """nearest-rank p95: 정렬해 ⌈0.95·n⌉번째 값 (n=49 → 47번째, n=30 → 29번째)"""
    xs = sorted(xs)
    return xs[max(0, -(-95 * len(xs) // 100) - 1)] if xs else 0.0  # 정수 올림 (부동소수 0.95 오차 없이)
