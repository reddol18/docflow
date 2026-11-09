"""고정 목록(pip freeze --all)에 PyPI 해시를 붙여 requirements.lock을 만든다 (https://github.com/reddol18/docflow/issues/33).

  python -I scripts/lock_hashes.py <freeze 파일> requirements.lock
  python -I scripts/lock_hashes.py --check requirements.txt requirements.lock   # scripts/check.ps1의 lock 단계

이미지는 `pip install --require-hashes --no-deps --only-binary :all: -r requirements.lock`로 설치한다.
해시는 그 판의 PyPI 휠 전부(모든 플랫폼, 철회된 파일 제외)의 sha256 — pip-compile --generate-hashes와 같은 방식이고,
이미지(cp311, linux x86_64)에 맞는 휠이 하나도 없으면 멈춘다. 나중에 같은 판에 휠이 더 올라와도 pip는 허용 해시와 안 맞는
후보를 버리므로 빌드가 깨지지 않는다 (pip 26.3 pip/_internal/index/package_finder.py filter_unallowed_hashes).
판 해석(어떤 판이 깔리는가)은 이 스크립트가 하지 않는다:
그건 이미지 플랫폼(linux/amd64)의 컨테이너 안에서 requirements.txt로 설치해 얻은 freeze가 정한다 (scripts/lock_refresh.ps1).
표준 라이브러리만 쓴다 (새 도구 없음).
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HEADER = """\
# 자동 생성 — 손으로 고치지 않는다. requirements.txt를 바꿨을 때만 다시 만든다: .\\scripts\\lock_refresh.ps1
# 이미지 설치: pip install --require-hashes --no-deps --only-binary :all: -r requirements.lock (Dockerfile). 없는 판·해시면 빌드 실패.
# 판은 linux/amd64 컨테이너에서 requirements.txt를 풀어 pip freeze --all, 해시는 PyPI의 그 판 휠 전부.
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


# 이미지 플랫폼 — Dockerfile의 FROM python:3.11.x-slim-bookworm (glibc 2.36), linux/amd64. 바꾸면 여기도
PY_MINOR = 11
GLIBC_MINOR = 36
_MANYLINUX = re.compile(r"^manylinux_2_(\d+)_x86_64$")
_LEGACY = {"manylinux1_x86_64", "manylinux2010_x86_64", "manylinux2014_x86_64"}


def fits_image(filename: str) -> bool:
    """휠 파일 이름의 태그가 이미지(CPython 3.11, linux x86_64 glibc 2.36)에 깔릴 수 있는가"""
    py_tags, abi_tags, plat_tags = filename[: -len(".whl")].split("-")[-3:]

    def py_abi_ok(p: str, a: str) -> bool:
        if a == f"cp3{PY_MINOR}":
            return p == f"cp3{PY_MINOR}"
        m = re.fullmatch(r"cp3(\d+)", p)
        if a == "abi3":
            return bool(m) and int(m.group(1)) <= PY_MINOR
        if a == "none":
            n = re.fullmatch(r"py3(\d*)", p)
            return p == f"cp3{PY_MINOR}" or (bool(n) and (not n.group(1) or int(n.group(1)) <= PY_MINOR))
        return False

    def plat_ok(t: str) -> bool:
        m = _MANYLINUX.match(t)
        return t == "any" or t in _LEGACY or (bool(m) and int(m.group(1)) <= GLIBC_MINOR)

    return (any(py_abi_ok(p, a) for p in py_tags.split(".") for a in abi_tags.split("."))
            and any(plat_ok(t) for t in plat_tags.split(".")))


def hashes(name: str, version: str) -> list[str]:
    """그 판의 (철회 안 된) 휠 전부의 sha256. 이미지에 맞는 휠이 없거나 판이 철회됐으면 멈춘다.
    이미지는 --only-binary :all:로 설치하므로 sdist는 넣지 않는다 (sdist 빌드 의존성은 해시 확인이 안 된다)."""
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    with urllib.request.urlopen(url, timeout=60) as r:  # noqa: S310 — 고정된 https PyPI 주소
        data = json.load(r)
    info = data.get("info") or {}
    if info.get("version") != version:
        raise SystemExit(f"{name}=={version}: PyPI가 다른 판({info.get('version')})을 돌려줬다")
    if info.get("yanked"):
        raise SystemExit(f"{name}=={version}: PyPI에서 철회된(yanked) 판 — {info.get('yanked_reason') or '사유 없음'}")
    wheels = [f for f in data.get("urls") or [] if f["packagetype"] == "bdist_wheel" and not f.get("yanked")]
    if not any(fits_image(f["filename"]) for f in wheels):
        raise SystemExit(f"{name}=={version}: 이미지(cp3{PY_MINOR}, linux x86_64)에 맞는 휠이 없다 — sdist 빌드는 받지 않는다")
    return sorted({f["digests"]["sha256"] for f in wheels})


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
    with ThreadPoolExecutor(max_workers=8) as pool:  # PyPI 요청 100여 개를 나눠 보낸다 (결과 순서는 pins 그대로)
        got = list(pool.map(lambda p: hashes(*p), pins))
    out = [HEADER]
    for (name, version), hs in zip(pins, got, strict=True):
        out.append(f"{name}=={version} " + " ".join(f"--hash=sha256:{h}" for h in hs) + "\n")
    with open(dst, "w", encoding="utf-8", newline="\n") as f:
        f.writelines(out)
    print(f"{dst}: {len(pins)}개")


if __name__ == "__main__":
    main()
