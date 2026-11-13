"""eval/promptfoo/node_deps.ps1 Sync-NodeModules: lock이 바뀌면 다시 깐다 (https://github.com/reddol18/docflow/issues/123).

npm·네트워크 없이 가짜 설치(진입 파일을 만들고 횟수를 남김)로 판단만 본다. Windows PowerShell이 없으면 건너뛴다.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "eval" / "promptfoo" / "node_deps.ps1"
PS = shutil.which("powershell") or shutil.which("pwsh")
pytestmark = pytest.mark.skipif(PS is None, reason="PowerShell 없음")

FAKE_OK = (
    "{ New-Item -ItemType Directory -Force node_modules\\promptfoo\\dist\\src | Out-Null;"
    " Set-Content node_modules\\promptfoo\\dist\\src\\entrypoint.js '';"
    " Add-Content ..\\installs.txt x }"
)
FAKE_FAIL = "{ Add-Content ..\\installs.txt x; cmd /c exit 3 }"


def sync(d: Path, install: str = FAKE_OK) -> subprocess.CompletedProcess:
    cmd = f". '{HELPER}'; $r = Sync-NodeModules -Dir '{d}' -Install {install}; Write-Output \"RESULT=$r\""
    return subprocess.run([PS, "-NoProfile", "-NonInteractive", "-Command", cmd],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)


def installs(d: Path) -> int:
    f = d.parent / "installs.txt"
    return len(f.read_text().split()) if f.exists() else 0


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    d = tmp_path / "pf"
    d.mkdir()
    (d / "package-lock.json").write_text('{"v": 1}\n')
    return d


def test_installs_once_then_skips_until_lock_changes(proj):
    assert "RESULT=True" in sync(proj).stdout and installs(proj) == 1
    assert "RESULT=False" in sync(proj).stdout and installs(proj) == 1  # 같은 lock → 건너뜀
    (proj / "package-lock.json").write_text('{"v": 2}\n')  # overrides 갱신 등
    assert "RESULT=True" in sync(proj).stdout and installs(proj) == 2


def test_reinstalls_existing_tree_without_stamp(proj):
    # 표식 전(이 수정 전 run.ps1)에 깐 PC: 진입 파일은 있지만 표식이 없다 → 다시 깐다
    entry = proj / "node_modules" / "promptfoo" / "dist" / "src"
    entry.mkdir(parents=True)
    (entry / "entrypoint.js").write_text("")
    assert "RESULT=True" in sync(proj).stdout and installs(proj) == 1
    assert (proj / "node_modules" / ".lock-sha256").exists()


def test_failed_install_leaves_no_stamp(proj):
    r = sync(proj, FAKE_FAIL)
    assert r.returncode != 0 and "RESULT=" not in r.stdout
    assert not (proj / "node_modules" / ".lock-sha256").exists()
    assert installs(proj) == 1
