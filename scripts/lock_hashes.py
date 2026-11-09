"""고정 목록(pip freeze --all)에 PyPI 해시를 붙여 requirements.lock을 만든다 (https://github.com/reddol18/docflow/issues/33).

  python -I scripts/lock_hashes.py <freeze 파일> requirements.lock
  python -I scripts/lock_hashes.py --check requirements.txt requirements.lock   # scripts/check.ps1의 lock 단계

이미지는 `pip install --require-hashes --no-deps -r requirements.lock`로 설치한다.
해시는 그 판의 PyPI 배포 파일 전부(모든 플랫폼 휠 + sdist)의 sha256 — pip-compile --generate-hashes와 같은 방식이라
플랫폼과 무관하고, 어느 파일을 고르든 pip가 대조한다. 판 해석(어떤 판이 깔리는가)은 이 스크립트가 하지 않는다:
그건 이미지 플랫폼(linux/amd64)의 컨테이너 안에서 requirements.txt로 설치해 얻은 freeze가 정한다 (scripts/lock_refresh.ps1).
표준 라이브러리만 쓴다 (새 도구 없음).
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request

HEADER = """\
# 자동 생성 — 손으로 고치지 않는다. requirements.txt를 바꿨을 때만 다시 만든다: .\\scripts\\lock_refresh.ps1
# 이미지 설치: pip install --require-hashes --no-deps -r requirements.lock (Dockerfile). 없는 판·해시면 빌드 실패.
# 판은 linux/amd64 컨테이너에서 requirements.txt를 풀어 pip freeze --all, 해시는 PyPI의 그 판 배포 파일 전부.
"""

PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)")


def read_pins(path: str) -> list[tuple[str, str]]:
    pins = []
    with open(path, encoding="utf-8-sig") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            m = PIN.match(line)
            if not m:
                # freeze가 URL·편집 설치(-e, @ file://)를 쓰면 해시 잠금으로 재현할 수 없다 — 멈춘다
                raise SystemExit(f"name==version 형식이 아닌 줄: {line!r}")
            pins.append((m.group(1), m.group(2)))
    return pins


def hashes(name: str, version: str) -> tuple[list[str], bool]:
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    with urllib.request.urlopen(url, timeout=60) as r:  # noqa: S310 — 고정된 https PyPI 주소
        data = json.load(r)
    files = data.get("urls") or []
    if not files:
        raise SystemExit(f"{name}=={version}: PyPI에 배포 파일이 없다")
    has_wheel = any(f["packagetype"] == "bdist_wheel" for f in files)
    return sorted({f["digests"]["sha256"] for f in files}), has_wheel


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def check(req_path: str, lock_path: str) -> None:
    """requirements.txt의 고정 판이 lock에 그대로 있는지, lock 줄마다 해시가 있는지 본다 (scripts/check.ps1)."""
    lock: dict[str, str] = {}
    bad = []
    with open(lock_path, encoding="utf-8-sig") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            m = PIN.match(line)
            if not m or "--hash=sha256:" not in line:
                bad.append(f"lock 줄 형식(name==version --hash=sha256:…): {line[:80]}")
                continue
            lock[norm(m.group(1))] = m.group(2)
    req = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?==([^\s;#]+)")
    with open(req_path, encoding="utf-8-sig") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            m = req.match(line)
            if not m:
                bad.append(f"requirements.txt는 name==version만: {line}")
                continue
            have = lock.get(norm(m.group(1)))
            if have != m.group(3):
                bad.append(f"{m.group(1)}=={m.group(3)} → lock에는 {have or '없음'}")
    if bad:
        print("\n".join(bad))
        raise SystemExit("requirements.lock이 requirements.txt와 맞지 않는다 → .\\scripts\\lock_refresh.ps1로 다시 만든다")
    print(f"requirements.lock: {len(lock)}개, requirements.txt 고정 판 모두 일치")


def main() -> None:
    if len(sys.argv) == 4 and sys.argv[1] == "--check":
        check(sys.argv[2], sys.argv[3])
        return
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    src, dst = sys.argv[1], sys.argv[2]
    pins = sorted(read_pins(src), key=lambda p: p[0].lower())
    out = [HEADER]
    for name, version in pins:
        hs, has_wheel = hashes(name, version)
        if not has_wheel:
            print(f"주의: {name}=={version} 휠 없음 (sdist 빌드)", file=sys.stderr)
        out.append(f"{name}=={version} " + " ".join(f"--hash=sha256:{h}" for h in hs) + "\n")
    with open(dst, "w", encoding="utf-8", newline="\n") as f:
        f.writelines(out)
    print(f"{dst}: {len(pins)}개")


if __name__ == "__main__":
    main()
