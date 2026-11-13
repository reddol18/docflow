"""맥 원격 명령은 scripts/lib/remote.ps1 하나로 (W46 회고 규칙 B, https://github.com/reddol18/docflow/issues/117).

1. 정적 시험: *.ps1에서 ssh·scp를 직접 부르는 줄이 0건 (remote.ps1만 예외). 주석 줄과 '# remote-ok: <이유>' 표식 줄은 뺀다.
2. 단위 시험 (맥 없이, 가짜 ssh tests/fixtures/fake_ssh.py): 따옴표·한글 왕복, 표준 입력에 BOM 없음(콘솔 없는 호스트 포함),
   시간 제한, 종료 코드, scp 인자. Windows PowerShell(없으면 pwsh)로 돌린다 — 없으면 건너뛴다.
실제 맥 왕복은 PR 본문에 붙인다 (배포 환경이라 시험에서 부르지 않는다).
"""
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "lib" / "remote.ps1"
FAKE = ROOT / "tests" / "fixtures" / "fake_ssh.py"
PS = shutil.which("powershell") or shutil.which("pwsh")
needs_ps = pytest.mark.skipif(PS is None, reason="PowerShell 없음")

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3"]
PATH_LINE = "export PATH=/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
CMD = re.compile(r"^sh -c 'exec 4<&0; echo ([A-Za-z0-9+/=]+) \| base64 -D \| sh /dev/fd/3 3<&0 0<&4 4<&-'$")

# ---- 1. 정적 시험 ----

# 명령 이름 자리의 ssh·scp (ssh.exe·& ssh·"ssh" 포함). 'colima ssh --'(맥 쪽 sh)는 *.ps1 대상이 아니다
DIRECT = re.compile(r"""(?<![\w.$-])(?<!colima )['"]?(ssh|scp)(\.exe)?['"]?(?=\s|$)""", re.I)
MARK = re.compile(r"#\s*remote-ok:\s*(\S.*)?$")
SKIP_DIRS = {"node_modules", ".venv", ".git", ".next", "__pycache__"}


def direct_calls(text: str) -> list[tuple[int, str]]:
    """주석(줄 주석·<# #> 블록)이 아닌 줄에서 ssh·scp 직접 호출 후보. 표식 줄은 이유가 있어야 빠진다"""
    bad, block = [], False
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if block:
            block = "#>" not in s
            continue
        if s.startswith("<#"):
            block = "#>" not in s[2:]
            continue
        if s.startswith("#"):
            continue
        m = MARK.search(line)
        if m and m.group(1):
            continue
        if DIRECT.search(line.split(" #", 1)[0] if not m else line):
            bad.append((n, s))
    return bad


def ps1_files() -> list[Path]:
    return [p for p in ROOT.rglob("*.ps1") if not SKIP_DIRS & set(p.relative_to(ROOT).parts)]


def test_no_direct_ssh_or_scp_outside_helper():
    files = ps1_files()
    assert ROOT / "scripts" / "deploy.ps1" in files and HELPER in files
    found = {str(p.relative_to(ROOT)): direct_calls(p.read_text(encoding="utf-8-sig")) for p in files if p != HELPER}
    assert {k: v for k, v in found.items() if v} == {}  # 측정: ssh·scp 직접 호출 0건


def test_remote_ok_markers_have_reasons():
    for p in ps1_files():
        for line in p.read_text(encoding="utf-8-sig").splitlines():
            m = MARK.search(line)
            if m:
                assert m.group(1) and len(m.group(1).strip()) >= 5, (p, line)


def test_scanner_catches_direct_calls():
    bad = [
        'ssh -o BatchMode=yes $Mac "ls"',
        '$x = ssh $Mac uname',
        '"a" | ssh.exe $Mac cat',
        'scp -q a "${Mac}:b/"',
        '& ssh $Mac uname',
        '& "scp" a b',
        'if ($x) { ssh $Mac uname }',
        'ssh $Mac uname  # remote-ok:',  # 이유 없는 표식은 통과 못 함
    ]
    for line in bad:
        assert direct_calls(line), line
    ok = [
        "# 먼저 터널: ssh -L 18000:127.0.0.1:18000 user@mac",
        "<#\n  ssh user@mac\n#>",
        'Write-Warning "직접: ssh $Mac x"  # remote-ok: 사용자에게 보이는 안내 문구',
        'Invoke-Mac -Mac $Mac -Script "colima ssh -- true"',
        '$sshHost = 1; $DOCFLOW_ssh = 2',
        'Copy-ToMac -Mac $Mac -Path a -Destination b  # scp 대신',
    ]
    for text in ok:
        assert not direct_calls(text), text


def test_callers_dot_source_helper():
    for name in ("deploy", "tls_secrets", "keycloak", "zap_scan", "lock_refresh"):
        t = (ROOT / "scripts" / f"{name}.ps1").read_text(encoding="utf-8-sig")
        assert '. (Join-Path $PSScriptRoot "lib/remote.ps1")' in t, name
        assert "Invoke-Mac" in t, name
        assert "BatchMode" not in t and "$SshOpts" not in t, name  # ssh 옵션은 remote.ps1 한 곳
    for f in ("scripts/zap_scan.ps1", "scripts/mac/tls_secrets.sh"):  # BOM 우회 지움 (https://github.com/reddol18/docflow/issues/117)
        t = (ROOT / f).read_text(encoding="utf-8-sig")
        assert "xEF" not in t and "perl -pe" not in t, f


# ---- 2. 단위 시험 (가짜 ssh) ----

def run_ps(tmp_path: Path, body: str, env: dict | None = None, flags: int = 0, timeout: int = 60) -> subprocess.CompletedProcess:
    """ASCII 드라이버 .ps1로 돌린다. 한글·따옴표 데이터는 UTF-8 파일로만 오간다 (명령줄 따옴표 문제를 시험 쪽에서 피함)"""
    drv = tmp_path / "drv.ps1"
    pre = (
        "$ErrorActionPreference = 'Stop'\n"
        f". '{HELPER}'\n"
        f"$RemoteSshExe = @('{sys.executable}', '{FAKE}')\n"
        "$RemoteScpExe = $RemoteSshExe\n"
        f"$T = '{tmp_path}'\n"
        "$U8 = [Text.UTF8Encoding]::new($false)\n"
        "function Save($name, $lines) { [IO.File]::WriteAllLines((Join-Path $T $name), [string[]]@($lines), $U8) }\n"
        "function Load($name) { [IO.File]::ReadAllText((Join-Path $T $name), $U8) }\n"
    )
    drv.write_text(pre + body, encoding="ascii")
    e = {**__import__("os").environ, "FAKE_SSH_DIR": str(tmp_path), **(env or {})}
    return subprocess.run([PS, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(drv)],
                          capture_output=True, env=e, creationflags=flags, timeout=timeout)


def read(tmp_path: Path, name: str) -> str:
    return (tmp_path / name).read_text(encoding="utf-8")


QUOTED = "printf '%s\\n' \"a'b\" 'c\"d' \"한글 \\$HOME\" | grep -v x (y) $(z)\r\necho \"끝\" `w`\n"


@needs_ps
def test_quoting_round_trip(tmp_path):
    (tmp_path / "in.txt").write_bytes(QUOTED.encode("utf-8"))
    r = run_ps(tmp_path, "$o = Invoke-Mac -Mac 'u@h' -Script (Load 'in.txt'); Save 'out.txt' $o; Save 'code.txt' $LASTEXITCODE\n")
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    argv = json.loads(read(tmp_path, "argv.json"))
    assert argv[:-2] == SSH_OPTS and argv[-2] == "u@h"
    assert '"' not in argv[-1] and CMD.match(argv[-1])  # 맥에 가는 명령줄에 큰따옴표 없음 (5.1이 지우던 것)
    assert (tmp_path / "script.txt").read_bytes() == (PATH_LINE + "\n" + QUOTED.replace("\r", "")).encode("utf-8")
    assert read(tmp_path, "out.txt").splitlines() == ["출력 한글 줄", "둘째 줄"]  # 표준 출력 줄을 돌려받음 (UTF-8)
    assert read(tmp_path, "code.txt").strip() == "0"
    assert (tmp_path / "stdin.bin").read_bytes() == b""  # -Stdin 없으면 바로 EOF (</dev/null과 같음)


# 이 PC 콘솔처럼 [Console]::InputEncoding이 BOM 있는 UTF-8인 상태를 만든다 (5.1에서 BOM이 붙던 조건)
FORCE_BOM_INPUT = (
    "$f = [Console].GetField('_inputEncoding', [Reflection.BindingFlags]'NonPublic,Static')\n"
    "if ($f) { $f.SetValue($null, [Text.UTF8Encoding]::new($true)) }\n"
    "Save 'preamble.txt' ([Console]::InputEncoding.GetPreamble().Length)\n"
)


# 콘솔 없는 호스트: 콘솔에서 떨어진 뒤 공개 setter([Console]::InputEncoding = ...)는 SetConsoleCP 실패로 예외가 난다
# (DETACHED_PROCESS로 띄운 powershell.exe는 아무것도 하지 않고 끝나서 FreeConsole로 만든다)
FREE_CONSOLE = (
    "Add-Type -Namespace DocflowTest -Name K -MemberDefinition '[DllImport(\"kernel32.dll\")] public static extern bool FreeConsole();'\n"
    "[void][DocflowTest.K]::FreeConsole()\n"
    "try { [Console]::InputEncoding = [Text.UTF8Encoding]::new($true); Save 'setter.txt' 'ok' } catch { Save 'setter.txt' 'threw' }\n"
)
IS_51 = PS is not None and Path(PS).name.lower().startswith("powershell")


@needs_ps
@pytest.mark.parametrize("console", [True, False], ids=["console", "no-console"])
def test_stdin_has_no_bom(tmp_path, console):
    (tmp_path / "in.txt").write_bytes("pw-한글\nline2\n".encode("utf-8"))
    body = ("" if console else FREE_CONSOLE) + FORCE_BOM_INPUT + (
        "Invoke-Mac -Mac 'u@h' -Script 'cat' -Stdin (Load 'in.txt') | Out-Null\n"
        "Copy-Item (Join-Path $T 'stdin.bin') (Join-Path $T 'stdin_str.bin')\n"
        "Invoke-Mac -Mac 'u@h' -Script 'cat' -Stdin ([byte[]](0x61, 0x0d, 0x0a, 0xff)) | Out-Null\n"
        "Save 'done.txt' 'ok'\n"
    )
    r = run_ps(tmp_path, body)
    assert r.returncode == 0 and read(tmp_path, "done.txt").strip() == "ok", r.stderr.decode("utf-8", "replace")
    if IS_51:
        assert read(tmp_path, "preamble.txt").strip() == "3"  # 5.1에서 실제로 BOM 조건을 만들었는지
        if not console:
            assert read(tmp_path, "setter.txt").strip() == "threw"  # 정말 콘솔이 없는 상태였는지
    assert (tmp_path / "stdin_str.bin").read_bytes() == "pw-한글\nline2\n".encode("utf-8")  # 앞에 EF BB BF 없음
    assert (tmp_path / "stdin.bin").read_bytes() == b"a\r\n\xff"  # byte[]는 그대로


@needs_ps
def test_timeout_kills_and_throws(tmp_path):
    body = (
        "$sw = [Diagnostics.Stopwatch]::StartNew()\n"
        "try { Invoke-Mac -Mac 'u@h' -Script 'sleep 100' -TimeoutSec 2 -Stream; Save 'msg.txt' 'NO_THROW' }\n"
        "catch { Save 'msg.txt' $_.Exception.Message }\n"
        "Save 'sec.txt' ([int]$sw.Elapsed.TotalSeconds)\n"
    )
    t0 = time.monotonic()
    r = run_ps(tmp_path, body, env={"FAKE_SSH_MODE": "sleep"})
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    msg = read(tmp_path, "msg.txt")
    assert "2초 안에 끝나지 않아" in msg and "시간 제한" in msg and "sleep 100" in msg, msg
    assert int(read(tmp_path, "sec.txt")) < 10 and time.monotonic() - t0 < 25  # 가짜 ssh의 30초를 기다리지 않음


@needs_ps
def test_nonzero_exit(tmp_path):
    body = (
        "try { Invoke-Mac -Mac 'u@h' -Script 'false' -FailMessage 'deploy failed (see above)' | Out-Null; Save 'a.txt' 'NO_THROW' }\n"
        "catch { Save 'a.txt' $_.Exception.Message }\n"
        "try { Invoke-Mac -Mac 'u@h' -Script 'false' | Out-Null; Save 'b.txt' 'NO_THROW' } catch { Save 'b.txt' $_.Exception.Message }\n"
        "$o = Invoke-Mac -Mac 'u@h' -Script 'false' -AllowFail; Save 'c.txt' (@($LASTEXITCODE) + $o)\n"
        "Invoke-Mac -Mac 'u@h' -Script 'false' -Stream 6>$null\n"
    )
    r = run_ps(tmp_path, body, env={"FAKE_SSH_EXIT": "3"})
    assert read(tmp_path, "a.txt").strip() == "deploy failed (see above)"
    assert read(tmp_path, "b.txt").strip() == "맥 명령 실패 (종료 코드 3): false"
    assert read(tmp_path, "c.txt").splitlines() == ["3", "출력 한글 줄", "둘째 줄"]
    assert r.returncode != 0  # 마지막 줄: 스트림이어도 실패면 throw


@needs_ps
def test_stream_writes_to_host_and_returns_nothing(tmp_path):
    body = (
        "$info = Invoke-Mac -Mac 'u@h' -Script 'x' -Stream 6>&1\n"
        "Save 'info.txt' ($info | ForEach-Object { \"$_\" })\n"
        "Save 'ret.txt' (@(Invoke-Mac -Mac 'u@h' -Script 'x' -Stream 6>$null).Count)\n"
    )
    r = run_ps(tmp_path, body)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    assert sorted(read(tmp_path, "info.txt").splitlines()) == sorted(["출력 한글 줄", "둘째 줄", "오류 줄"])
    assert read(tmp_path, "ret.txt").strip() == "0"


@needs_ps
def test_copy_args(tmp_path):
    body = (
        "Copy-ToMac -Mac 'u@h' -Path 'a b.txt', 'c' -Destination 'doc_platform/x/' -Recurse 6>$null\n"
        "Copy-Item (Join-Path $T 'argv.json') (Join-Path $T 'to.json')\n"
        "Copy-FromMac -Mac 'u@h' -Path 'doc_platform/f' -Destination 'C:\\tmp dir\\f' 6>$null\n"
    )
    r = run_ps(tmp_path, body)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    assert json.loads(read(tmp_path, "to.json")) == ["-q", *SSH_OPTS, "-r", "a b.txt", "c", "u@h:doc_platform/x/"]
    assert json.loads(read(tmp_path, "argv.json")) == ["-q", *SSH_OPTS, "u@h:doc_platform/f", "C:\\tmp dir\\f"]
