"""OCR 모델 파일을 목록·sha256으로 고정한다 (https://github.com/reddol18/docflow/issues/102).

이미지 빌드는 `python -m app.warm`으로 PaddleOCR 모델을 받는다(PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True, 받는 쪽은 해시를 안 본다).
그래서 받은 뒤 모델 폴더(PADDLE_PDX_CACHE_HOME) 아래 파일 전부(받기 도구의 `.cache` 기록 폴더는 빼고, scan 참고)를 app/models.lock과 견준다 — 빠진 파일·남는 파일·해시가 다른
파일이 하나라도 있으면 0이 아닌 값으로 끝나 빌드가 멈춘다.

  python -m app.model_lock verify <모델 폴더> <lock>   # Dockerfile 모델 층 (보통 빌드)
  python -m app.model_lock write  <모델 폴더> <lock>   # Dockerfile model-lock 단계 (scripts/model_lock_refresh.ps1, 모델 판을 올릴 때만)

lock 형식은 sha256sum과 같다(`<sha256>  <모델 폴더 기준 경로>`, 경로 순). #으로 시작하는 줄은 머리말.
표준 라이브러리만 쓴다 (시험: tests/test_model_lock.py).
"""
import hashlib
import os
import re
import sys
from pathlib import Path

HEADER = (
    "# 자동 생성 — 손으로 고치지 않는다. OCR 모델 판을 올릴 때만 다시 만든다: .\\scripts\\model_lock_refresh.ps1\n"
    "# 이미지 빌드(Dockerfile 모델 층)가 받은 모델 폴더(PADDLE_PDX_CACHE_HOME) 아래 파일 전부와 견준다 — 다르면 빌드 실패\n"
    "# (https://github.com/reddol18/docflow/issues/102). 폐쇄망 묶음(scripts/offline/build_bundle.sh)도 같은 빌드라 같은 목록이다.\n"
)
SKIP_DIRS = frozenset({".cache"})
_LINE = re.compile(r"^([0-9a-f]{64}) {2}(\S(?:.*\S)?)$")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def scan(root):
    """모델 폴더 아래 일반 파일 전부 → {상대 경로(/ 구분): sha256}. 심볼릭 링크·특수 파일이 있으면 멈춘다.

    `.cache` 폴더(SKIP_DIRS)는 뺀다: 모델을 받은 도구(huggingface_hub)의 내려받기 기록(`download/*.metadata`에 받은 시각,
    빈 `*.lock`)이라 빌드마다 바뀐다. 모델이 읽는 파일이 아니다 (2026-11-23 실제 빌드에서 확인).
    """
    root = Path(root)
    if not root.is_dir():
        raise SystemExit(f"모델 폴더 없음: {root}")
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in dirnames:
            if (Path(dirpath) / name).is_symlink():
                raise SystemExit(f"모델 폴더에 심볼릭 링크(폴더): {Path(dirpath) / name}")
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if p.is_symlink() or not p.is_file():
                raise SystemExit(f"모델 폴더에 일반 파일이 아닌 것: {p}")
            out[p.relative_to(root).as_posix()] = sha256_file(p)
    return out


def parse(text):
    """lock 글 → {경로: sha256}. 형식이 틀리거나 경로가 겹치면 ValueError."""
    out = {}
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            raise ValueError(f"lock {n}번째 줄 형식이 틀림 (<sha256 64자리 소문자>  <경로>): {line!r}")
        digest, path = m.groups()
        if path.startswith("/") or ".." in path.split("/"):
            raise ValueError(f"lock {n}번째 줄 경로는 모델 폴더 안 상대 경로여야 함: {path!r}")
        if path in out:
            raise ValueError(f"lock {n}번째 줄 경로가 겹침: {path!r}")
        out[path] = digest
    if not out:
        raise ValueError("lock에 파일이 하나도 없음")
    return out


def render(entries):
    return HEADER + "".join(f"{entries[p]}  {p}\n" for p in sorted(entries))


def compare(want, have):
    """(빠진 것, 남는 것, 해시 다른 것) — 경로 목록, 각각 정렬."""
    missing = sorted(set(want) - set(have))
    extra = sorted(set(have) - set(want))
    changed = sorted(p for p in set(want) & set(have) if want[p] != have[p])
    return missing, extra, changed


def verify(root, lock_path):
    """맞으면 0, 다르면 1 (무엇이 다른지 표준 오류로)."""
    want = parse(Path(lock_path).read_text(encoding="utf-8"))
    have = scan(root)
    missing, extra, changed = compare(want, have)
    if missing or extra or changed:
        for label, paths in (("빠짐", missing), ("남음(lock에 없음)", extra), ("해시 다름", changed)):
            for p in paths:
                detail = f" (lock {want[p]}, 받은 것 {have[p]})" if label == "해시 다름" else ""
                print(f"!! 모델 {label}: {p}{detail}", file=sys.stderr)
        print(
            f"!! 모델 불일치: 빠짐 {len(missing)} · 남음 {len(extra)} · 해시 다름 {len(changed)} — {lock_path}와 다름. "
            "모델 판을 일부러 올린 것이면 .\\scripts\\model_lock_refresh.ps1로 lock을 다시 만들어 확인 뒤 커밋",
            file=sys.stderr,
        )
        return 1
    print(f"모델 일치: OCR 모델 파일 {len(want)}개 = {lock_path}")
    return 0


def write(root, lock_path):
    have = scan(root)
    if not have:
        raise SystemExit(f"모델 폴더가 비어 있음: {root}")
    Path(lock_path).write_bytes(render(have).encode("utf-8"))
    print(f"모델 lock 씀: 파일 {len(have)}개 → {lock_path}")
    return 0


def main(argv):
    if len(argv) != 3 or argv[0] not in ("verify", "write"):
        print(__doc__, file=sys.stderr)
        return 2
    cmd, root, lock_path = argv
    return verify(root, lock_path) if cmd == "verify" else write(root, lock_path)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
