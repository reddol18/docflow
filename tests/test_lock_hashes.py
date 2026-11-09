"""scripts/lock_hashes.py — 휠 태그 판정과 lock 확인 (https://github.com/reddol18/docflow/issues/33). PyPI 호출 없음."""
import importlib.util
from pathlib import Path

import pytest

_p = Path(__file__).parent.parent / "scripts" / "lock_hashes.py"
_spec = importlib.util.spec_from_file_location("lock_hashes", _p)
lh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lh)

H = "--hash=sha256:" + "a" * 64


@pytest.mark.parametrize("name", [
    "click-8.5.1-py3-none-any.whl",
    "six-1.17.0-py2.py3-none-any.whl",
    "numpy-2.4.6-cp311-cp311-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl",
    "argon2_cffi_bindings-26.1.0-cp310-abi3-manylinux_2_26_x86_64.manylinux_2_28_x86_64.whl",
    "psycopg_binary-3.3.6-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.whl",
    "crc32c-2.9.post0-1-cp311-cp311-manylinux1_x86_64.whl",  # 빌드 번호 있는 이름
])
def test_fits_image(name):
    assert lh.fits_image(name)


@pytest.mark.parametrize("name", [
    "numpy-2.4.6-cp312-cp312-manylinux_2_28_x86_64.whl",  # 다른 파이썬
    "numpy-2.4.6-cp311-cp311-musllinux_1_2_x86_64.whl",  # musl
    "numpy-2.4.6-cp311-cp311-manylinux_2_28_aarch64.whl",  # arm
    "numpy-2.4.6-cp311-cp311-manylinux_2_39_x86_64.whl",  # 이미지 glibc 2.36보다 새것
    "numpy-2.4.6-cp311-cp311-win_amd64.whl",
    "x-1.0-cp312-abi3-manylinux_2_28_x86_64.whl",  # abi3이지만 3.12부터
    "x-1.0-cp314-cp314t-manylinux_2_28_x86_64.whl",
    "x-1.0-py2-none-any.whl",
])
def test_not_fits_image(name):
    assert not lh.fits_image(name)


def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_check_passes_when_pins_match(tmp_path, capsys):
    req = write(tmp_path, "r.txt", "# 주석\npsycopg[binary]==3.3.6\nPyJWT[crypto]==2.15.1\n")
    lock = write(tmp_path, "l.lock", f"# 머리말\npsycopg==3.3.6 {H}\npyjwt==2.15.1 {H}\ncffi==2.1.1 {H}\n")
    lh.check(req, lock)
    assert "모두 일치" in capsys.readouterr().out


@pytest.mark.parametrize("req, lock_line, why", [
    ("fastapi==0.163.0\n", f"fastapi==0.162.0 {H}\n", "lock에는 0.162.0"),
    ("newpkg==1.0\n", f"fastapi==0.162.0 {H}\n", "lock에는 없음"),
    ("fastapi>=0.1\n", f"fastapi==0.162.0 {H}\n", "name==version만"),
    ("fastapi==0.162.0\n", "fastapi==0.162.0\n", "lock 줄 형식"),  # 해시 없음
])
def test_check_fails(tmp_path, capsys, req, lock_line, why):
    with pytest.raises(SystemExit):
        lh.check(write(tmp_path, "r.txt", req), write(tmp_path, "l.lock", lock_line))
    assert why in capsys.readouterr().out


def test_read_pins_rejects_url_installs(tmp_path):
    with pytest.raises(SystemExit):
        lh.read_pins(write(tmp_path, "f.txt", "pkg @ file:///tmp/pkg.whl\n"))
