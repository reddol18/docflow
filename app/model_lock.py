"""OCR 모델 파일을 목록·sha256으로 고정한다 (https://github.com/reddol18/docflow/issues/102).

이미지 빌드는 `python -m app.warm`으로 PaddleOCR 모델을 받는다(PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True, 받는 쪽은 해시를 안 본다).
그래서 받은 뒤 모델 폴더(PADDLE_PDX_CACHE_HOME) 아래 파일 전부(받기 기록·설명 파일은 빼고, SKIP_DIRS·SKIP_NAMES)를
app/models.lock과 견준다 — 빠진 파일·남는 파일·해시가 다른 파일이 하나라도 있으면 0이 아닌 값으로 끝나 빌드가 멈춘다.
뺀 파일은 prune이 지워 이미지에는 대조한 파일만 남는다.

  python -m app.model_lock verify <모델 폴더> <lock>   # Dockerfile 모델 층 (보통 빌드)
  python -m app.model_lock prune  <모델 폴더> <lock>   # 그 바로 뒤: 뺀 것을 지우고, 남은 파일 전부가 lock 그대로인지 다시 본다
  python -m app.model_lock write  <모델 폴더> <lock>   # Dockerfile model-lock 단계 (scripts/model_lock_refresh.ps1, 모델 판을 올릴 때만)

lock 형식은 sha256sum과 같다(`<sha256>  <모델 폴더 기준 경로>`, 경로 순). #으로 시작하는 줄은 머리말.
표준 라이브러리만 쓴다 (시험: tests/test_model_lock.py).
"""
import hashlib
import os
import re
import shutil
import sys
from pathlib import Path

HEADER = (
    "# 자동 생성 — 손으로 고치지 않는다. OCR 모델 판을 올릴 때만 다시 만든다: .\\scripts\\model_lock_refresh.ps1\n"
    "# 이미지 빌드(Dockerfile 모델 층)가 받은 모델 폴더(PADDLE_PDX_CACHE_HOME) 아래 파일과 견준다 — 다르면 빌드 실패\n"
    "# 받기 부산물(app/model_lock.py SKIP_DIRS·SKIP_NAMES)은 빼고 견준 뒤 이미지에서 지운다.\n"
    "# (https://github.com/reddol18/docflow/issues/102). 폐쇄망 묶음(scripts/offline/build_bundle.sh)도 같은 빌드라 같은 목록이다.\n"
)
# 대조하지 않는 것 (명시한 이름만 — 그 밖의 새 파일은 모두 '남음'으로 빌드를 멈춘다). 모델(PaddleX)이 읽지 않는 받기 부산물이다:
#  .cache          huggingface_hub 내려받기 기록 — download/*.metadata에 받은 시각이 들어 빌드마다 바뀐다 (2026-11-23 실제 빌드)
#  README.md       모델 저장소 설명 — 배포처가 설명만 고쳐도 배포 빌드가 멈추지 않게 (PR #154 리뷰)
#  .gitattributes  모델 저장소의 git LFS 설정
SKIP_DIRS = frozenset({".cache"})
SKIP_NAMES = frozenset({"README.md", ".gitattributes"})
_LINE = re.compile(r"^([0-9a-f]{64}) {2}(\S(?:.*\S)?)$")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def scan(root, skip=True):
    """모델 폴더 아래 일반 파일 → {상대 경로(/ 구분): sha256}. 심볼릭 링크·특수 파일이 있으면 멈춘다.

    skip=True면 SKIP_DIRS 폴더와 SKIP_NAMES 파일을 뺀다. 심볼릭 링크는 빼기 전에 모든 폴더에서 본다.
    """
    root = Path(root)
    if not root.is_dir():
        raise SystemExit(f"모델 폴더 없음: {root}")
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            if (Path(dirpath) / name).is_symlink():
                raise SystemExit(f"모델 폴더에 심볼릭 링크(폴더): {Path(dirpath) / name}")
        dirnames[:] = sorted(d for d in dirnames if not (skip and d in SKIP_DIRS))
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if p.is_symlink() or not p.is_file():
                raise SystemExit(f"모델 폴더에 일반 파일이 아닌 것: {p}")
            if skip and name in SKIP_NAMES:
                continue
            out[p.relative_to(root).as_posix()] = sha256_file(p)
    return out


def remove_skipped(root):
    """SKIP_DIRS 폴더·SKIP_NAMES 파일을 지운다 → 지운 것(상대 경로) 목록."""
    root = Path(root)
    gone = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in sorted(dirnames):
            d = Path(dirpath) / name
            if name in SKIP_DIRS and not d.is_symlink():
                shutil.rmtree(d)
                gone.append(d.relative_to(root).as_posix() + "/")
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if name in SKIP_NAMES and p.is_file() and not p.is_symlink():
                p.unlink()
                gone.append(p.relative_to(root).as_posix())
    return gone


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


def verify(root, lock_path, skip=True):
    """맞으면 0, 다르면 1 (무엇이 다른지 표준 오류로). skip=False면 뺄 것도 하나 없이 lock 그대로여야 한다."""
    want = parse(Path(lock_path).read_text(encoding="utf-8"))
    have = scan(root, skip=skip)
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
    if skip:
        print(f"모델 일치: OCR 모델 파일 {len(want)}개 = {lock_path}")
    return 0


def prune(root, lock_path):
    """대조하지 않은 것(SKIP_*)을 지우고, 남은 파일 전부가 lock 그대로인지 본다 — 이미지에는 대조한 파일만 남는다."""
    gone = remove_skipped(root)
    if verify(root, lock_path, skip=False):
        return 1
    print(f"모델 폴더 정리: 대조 안 한 것 {len(gone)}개 지움 — 남은 파일 = {lock_path} 그대로")
    return 0


def write(root, lock_path):
    have = scan(root)
    if not have:
        raise SystemExit(f"모델 폴더가 비어 있음: {root}")
    Path(lock_path).write_bytes(render(have).encode("utf-8"))
    print(f"모델 lock 씀: 파일 {len(have)}개 → {lock_path}")
    return 0


def main(argv):
    cmds = {"verify": verify, "prune": prune, "write": write}
    if len(argv) != 3 or argv[0] not in cmds:
        print(__doc__, file=sys.stderr)
        return 2
    cmd, root, lock_path = argv
    return cmds[cmd](root, lock_path)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
