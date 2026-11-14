"""맥 원격 명령은 scripts/lib/remote.ps1 하나로 (W46 회고 규칙 B, https://github.com/reddol18/docflow/issues/117).

1. 정적 시험: *.ps1에서 ssh·scp를 직접 부르는 곳이 0건 (remote.ps1만 예외). PowerShell AST로 찾는다
   (tests/fixtures/remote_calls.ps1 — 문자열 안 '#', 한 줄 안 <# #>를 파서가 가른다). 꼭 필요하면 그 줄에 '# remote-ok: <이유>'.
2. 단위 시험 (맥 없이, 가짜 ssh tests/fixtures/fake_ssh.py를 python -I로): 따옴표·한글 왕복, 표준 입력에 BOM 없음(콘솔 없는 호스트 포함),
   시간 제한(맥 쪽 멈추기 호출), 풀기 실패(97), 종료 코드, scp 인자·문구, 맥 주소 확인, ssh.exe 전체 경로.
   Windows PowerShell(없으면 pwsh)로 돌린다 — 없으면 건너뛴다.
실제 맥 왕복은 PR 본문에 붙인다 (배포 환경이라 시험에서 부르지 않는다).
"""
import base64
import json
import os
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
SCANNER = ROOT / "tests" / "fixtures" / "remote_calls.ps1"
PS = shutil.which("powershell") or shutil.which("pwsh")
pytestmark = pytest.mark.skipif(PS is None, reason="PowerShell 없음")
IS_51 = PS is not None and Path(PS).name.lower().startswith("powershell")

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3"]
PATH_LINE = "export PATH=/usr/local/bin:/opt/homebrew/bin:$HOME/.local/bin:$PATH"
CMD = re.compile(
    r"^sh -c 'b=\$\(echo ([A-Za-z0-9+/=]+) \| /usr/bin/base64 -D\) && \[ -n \"\$b\" \] && "
    r"exec /bin/sh -c \"\$b\" invoke-mac ([A-Za-z0-9+/=]+) (/tmp/invoke-mac\.[0-9a-f]{32}\.pid); "
    r"echo invoke-mac: decode-failed >&2; exit 97'$"
)
SKIP_DIRS = {"node_modules", ".venv", ".git", ".next", "__pycache__"}


def ps(args: list[str], timeout: int = 60, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([PS, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", *args],
                          capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout, **kw)


# ---- 1. 정적 시험 (AST) ----

def scan(files: list[Path], tmp_path: Path) -> list[dict]:
    out = tmp_path / "hits.json"
    r = ps(["-File", str(SCANNER), "-Out", str(out), *map(str, files)], timeout=120)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    return json.loads(out.read_text(encoding="utf-8"))


def ps1_files() -> list[Path]:
    return sorted(p for p in ROOT.rglob("*.ps1") if not SKIP_DIRS & set(p.relative_to(ROOT).parts))


def test_no_direct_ssh_or_scp_outside_helper(tmp_path):
    files = ps1_files()
    assert ROOT / "scripts" / "deploy.ps1" in files and HELPER in files
    hits = scan([p for p in files if p != HELPER and p != SCANNER], tmp_path)
    assert [h for h in hits if not h["allowed"]] == []  # 측정: ssh·scp 직접 호출 0건
    assert [h for h in hits if h["allowed"]] == []  # 지금은 예외 표식도 필요 없다 (생기면 이유와 함께 여기서 본다)
    assert scan([HELPER], tmp_path) == []  # remote.ps1도 이름이 아니라 Process로 띄운다


BAD = [
    'ssh -o BatchMode=yes $Mac "ls"',
    '$x = ssh $Mac uname',
    '"a" | ssh.exe $Mac cat',
    'scp -q a "${Mac}:b/"',
    '& ssh $Mac uname',
    "& 'scp' a b",
    '& "C:\\Windows\\System32\\OpenSSH\\ssh.exe" $Mac uname',
    'if ($x) { ssh $Mac uname }',
    'Write-Host "a # b"; ssh $Mac uname',  # 문자열 안 '#' 뒤 (줄 정규식은 주석으로 잘랐다)
    '<# c #> ssh $Mac uname',  # 한 줄 안 블록 주석 뒤 (줄 정규식은 블록 주석 줄로 건너뛰었다)
    'Start-Process ssh.exe -ArgumentList $Mac',
    'ssh $Mac uname  # remote-ok:',  # 이유 없는 표식
    'ssh $Mac uname  # remote-ok: ab',  # 너무 짧은 이유
    # 실행기 안의 문자열 (리뷰 2차 예시): 별칭은 풀어서 본다
    'cmd /c "ssh $Mac uname"',
    'cmd.exe /c ssh $Mac uname',
    'Invoke-Expression "ssh $Mac uname"',
    "iex 'scp a b:c'",
    'start ssh.exe -ArgumentList $Mac',
    "saps -FilePath 'scp' -ArgumentList 'a', 'b:c'",
    '[Diagnostics.Process]::Start("ssh.exe", "$Mac uname")',
    "[System.Diagnostics.Process]::Start('scp', 'a b:c')",
    '& "$env:WINDIR\\System32\\OpenSSH\\ssh.exe" $Mac uname',
    '. "$dir\\scp.exe" a b',
]
OK = [
    "# 먼저 터널: ssh -L 18000:127.0.0.1:18000 user@mac",
    "<#\n  ssh user@mac\n#>",
    'Write-Warning "직접: ssh $Mac x"',
    'Invoke-Mac -Mac $Mac -Script "colima ssh -- true"',
    '$sshHost = 1; $DOCFLOW_ssh = 2; $h = @{ ssh = 1 }',
    'Copy-ToMac -Mac $Mac -Path a -Destination b  # scp 대신',
    "$s = 'x # y'; Write-Host $s <# ssh $Mac #>",
    'Write-Host "ssh 터널을 여세요: ssh -L 8000:127.0.0.1:8000 user@mac"',  # 실행기가 아닌 안내 문구
    "Start-Process notepad.exe -ArgumentList 'readme.txt'",
    "[Diagnostics.Process]::Start($psi)",
    "cmd /c echo hello; iex $code",
    "Get-Command ssh.exe -CommandType Application",
    'ssh $Mac uname  # remote-ok: 사용자 안내용 예시 실행',
]


def test_scanner_catches_direct_calls(tmp_path):
    files = []
    for i, text in enumerate(BAD + OK):
        f = tmp_path / f"s{i:02}.ps1"
        f.write_text(text + "\n", encoding="utf-8-sig")
        files.append(f)
    hits = scan(files, tmp_path)
    bad = {Path(h["file"]).name for h in hits if not h["allowed"]}
    assert bad == {f"s{i:02}.ps1" for i in range(len(BAD))}, [h for h in hits if not h["allowed"]]
    allowed = [h for h in hits if h["allowed"]]
    assert [Path(h["file"]).name for h in allowed] == [f"s{len(BAD) + len(OK) - 1:02}.ps1"]


def test_callers_dot_source_helper():
    for name in ("deploy", "tls_secrets", "keycloak", "zap_scan", "lock_refresh"):
        t = (ROOT / "scripts" / f"{name}.ps1").read_text(encoding="utf-8-sig")
        assert '. (Join-Path $PSScriptRoot "lib/remote.ps1")' in t, name
        assert "Invoke-Mac" in t, name
        assert "BatchMode" not in t and "$SshOpts" not in t, name  # ssh 옵션은 remote.ps1 한 곳
    for f in ("scripts/zap_scan.ps1", "scripts/mac/tls_secrets.sh"):  # BOM 우회 지움 (https://github.com/reddol18/docflow/issues/117)
        t = (ROOT / f).read_text(encoding="utf-8-sig")
        assert "xEF" not in t and "perl -pe" not in t, f
    deploy = (ROOT / "scripts" / "deploy.ps1").read_text(encoding="utf-8-sig")
    line = next(x for x in deploy.splitlines() if "Copy-ToMac" in x and "disk_check.sh" in x)
    assert "-AllowFail" not in line and "-FailMessage" in line  # 디스크 확인은 필수 → 복사 실패면 멈춘다
    # freeze가 없어도 경고만 하고 태그는 남긴다 (Get-Item이 없는 파일에 throw하지 않게 Test-Path가 먼저)
    line = next(x for x in deploy.splitlines() if "-not $copied" in x)
    assert line.index("Test-Path $freeze") < line.index("Get-Item $freeze"), line


def test_vm_build_stopped_on_timeout():
    # 호스트 프로세스 그룹을 멈춰도 colima VM 안 빌드는 계속 돈다 → build_image.sh 트랩이 우리 빌드만 멈춘다
    build = (ROOT / "scripts/mac/build_image.sh").read_text(encoding="utf-8")
    helper = HELPER.read_text(encoding="utf-8-sig")
    pat = re.search(r"^VM_BUILD='(.+)'$", build, re.M).group(1)
    assert f"$RemoteVmBuildPattern = '{pat}'" in helper  # 두 곳이 같은 무늬
    assert pat.startswith("([n]erdctl|[b]uildctl)") and "docflow(-web)?:dev" in pat  # 자기 셸을 잡지 않고, 우리 태그만
    assert re.search(r"^trap stop_vm_build TERM INT HUP$", build, re.M)
    assert 'colima ssh -- sudo pkill -TERM -f -- "$VM_BUILD"' in build
    assert build.index("trap stop_vm_build") < build.index("nerdctl -- --namespace k8s.io build")
    deploy = (ROOT / "scripts/mac/deploy.sh").read_text(encoding="utf-8")
    assert re.search(r"^trap '.*exit 143' TERM INT HUP$", deploy, re.M)
    assert deploy.index("trap '") < deploy.index("build_image.sh all deploy")


def test_secret_tempdirs_trap_pipe():
    # 비밀값 임시 폴더를 쓰는 맥 스크립트: ssh가 끊겨 출력이 막혀도(PIPE) EXIT 트랩으로 지운다
    for f in ("scripts/mac/tls_secrets.sh", "scripts/mac/keycloak.sh"):
        t = (ROOT / f).read_text(encoding="utf-8")
        assert re.search(r"^trap 'exit 1' HUP INT TERM PIPE", t, re.M), f


# ---- 2. 단위 시험 (가짜 ssh) ----

def run_ps(tmp_path: Path, body: str, env: dict | None = None, timeout: int = 60, fake: bool = True) -> subprocess.CompletedProcess:
    """ASCII 드라이버 .ps1로 돌린다. 한글·따옴표 데이터는 UTF-8 파일로만 오간다 (명령줄 따옴표 문제를 시험 쪽에서 피함)"""
    drv = tmp_path / "drv.ps1"
    pre = (
        "$ErrorActionPreference = 'Stop'\n"
        f". '{HELPER}'\n"
        + (f"$RemoteSshExe = @('{sys.executable}', '-I', '{FAKE}')\n$RemoteScpExe = $RemoteSshExe\n" if fake else "")
        + f"$T = '{tmp_path}'\n"
        "$U8 = [Text.UTF8Encoding]::new($false)\n"
        "function Save($name, $lines) { [IO.File]::WriteAllLines((Join-Path $T $name), [string[]]@($lines), $U8) }\n"
        "function Load($name) { [IO.File]::ReadAllText((Join-Path $T $name), $U8) }\n"
    )
    drv.write_text(pre + body, encoding="ascii")
    e = {**os.environ, "FAKE_SSH_DIR": str(tmp_path), **(env or {})}
    return ps(["-File", str(drv)], timeout=timeout, env=e)


def read(tmp_path: Path, name: str) -> str:
    return (tmp_path / name).read_text(encoding="utf-8")


def calls(tmp_path: Path) -> list[dict]:
    return [json.loads(x) for x in read(tmp_path, "calls.jsonl").splitlines()]


QUOTED = "printf '%s\\n' \"a'b\" 'c\"d' \"한글 \\$HOME\" | grep -v x (y) $(z)\r\necho \"끝\" `w`\n"


def test_quoting_round_trip(tmp_path):
    (tmp_path / "in.txt").write_bytes(QUOTED.encode("utf-8"))
    r = run_ps(tmp_path, "$o = Invoke-Mac -Mac 'u@h' -Script (Load 'in.txt'); Save 'out.txt' $o; Save 'code.txt' $LASTEXITCODE\n")
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    argv = json.loads(read(tmp_path, "argv.json"))
    assert argv[:-2] == SSH_OPTS + ["-n"] and argv[-2] == "u@h"  # -Stdin 없음 → ssh -n
    m = CMD.match(argv[-1])  # 큰따옴표가 든 맥 명령줄도 Windows 인자 규칙대로 그대로 도착
    assert m, argv[-1]
    assert (tmp_path / "script.txt").read_bytes() == (PATH_LINE + "\n" + QUOTED.replace("\r", "")).encode("utf-8")
    boot = read(tmp_path, "boot.txt")
    assert "/usr/bin/base64 -D" in boot and "exit 97" in boot and "setpgrp(0, 0)" in boot and "O_EXCL, 0600" in boot
    assert read(tmp_path, "out.txt").splitlines() == ["출력 한글 줄", "둘째 줄"]  # 표준 출력 줄을 돌려받음 (UTF-8)
    assert read(tmp_path, "code.txt").strip() == "0"


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


@pytest.mark.parametrize("console", [True, False], ids=["console", "no-console"])
def test_stdin_has_no_bom(tmp_path, console):
    (tmp_path / "in.txt").write_bytes("pw-한글\nline2\n".encode("utf-8"))
    body = ("" if console else FREE_CONSOLE) + FORCE_BOM_INPUT + (
        "Invoke-Mac -Mac 'u@h' -Script 'cat' -Stdin (Load 'in.txt') | Out-Null\n"
        "Copy-Item (Join-Path $T 'stdin.bin') (Join-Path $T 'stdin_str.bin')\n"
        "Invoke-Mac -Mac 'u@h' -Script 'cat' -Stdin ([byte[]](0x61, 0x0d, 0x0a, 0xff)) | Out-Null\n"
        "Save 'after.txt' ([Console]::InputEncoding.GetPreamble().Length)\n"
        "Save 'done.txt' 'ok'\n"
    )
    r = run_ps(tmp_path, body)
    assert r.returncode == 0 and read(tmp_path, "done.txt").strip() == "ok", r.stderr.decode("utf-8", "replace")
    if IS_51:
        assert read(tmp_path, "preamble.txt").strip() == "3"  # 5.1에서 실제로 BOM 조건을 만들었는지
        assert read(tmp_path, "after.txt").strip() == "3"  # 끝나면 원래 인코딩으로 되돌린다
        if not console:
            assert read(tmp_path, "setter.txt").strip() == "threw"  # 정말 콘솔이 없는 상태였는지 (→ 캐시 필드로)
    assert "-n" not in json.loads(read(tmp_path, "argv.json"))
    assert (tmp_path / "stdin_str.bin").read_bytes() == "pw-한글\nline2\n".encode("utf-8")  # 앞에 EF BB BF 없음
    assert (tmp_path / "stdin.bin").read_bytes() == b"a\r\n\xff"  # byte[]는 그대로


def test_timeout_stops_remote_job_and_throws(tmp_path):
    body = (
        "$sw = [Diagnostics.Stopwatch]::StartNew()\n"
        "try { Invoke-Mac -Mac 'u@h' -Script 'sleep 100' -TimeoutSec 2 -Stream; Save 'msg.txt' 'NO_THROW' }\n"
        "catch { Save 'msg.txt' $_.Exception.Message }\n"
        "Save 'sec.txt' ([int]$sw.Elapsed.TotalSeconds)\n"
        "Save 'code.txt' $LASTEXITCODE\n"
    )
    t0 = time.monotonic()
    r = run_ps(tmp_path, body, env={"FAKE_SSH_MODE": "sleep"})
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    msg = read(tmp_path, "msg.txt").strip()
    assert msg == ("시간 제한(2초)으로 끊음: sleep 100 — 맥 쪽 작업을 멈추려 했다"
                   "(결과: 프로세스 그룹 4242 멈춤(TERM) / colima VM 안 docflow 빌드 없음). "
                   "배포 상태를 확인한 뒤 다시"), msg
    c = calls(tmp_path)
    assert len(c) == 2 and c[0]["script"].endswith("sleep 100")
    assert read(tmp_path, "kill.txt") == c[0]["pidfile"]  # 두 번째 호출이 첫 호출의 pidfile 그룹을 멈춘다
    kill = c[1]["script"]
    assert 'kill -TERM -- "-$g"' in kill and 'kill -KILL -- "-$g"' in kill and "$i -lt 5" in kill
    assert read(tmp_path, "code.txt").strip() == "124"
    assert int(read(tmp_path, "sec.txt")) < 10 and time.monotonic() - t0 < 25  # 가짜 ssh의 30초를 기다리지 않음


def test_decode_failure_is_not_success(tmp_path):
    body = (
        "try { Invoke-Mac -Mac 'u@h' -Script 'true' -AllowFail | Out-Null; Save 'a.txt' 'NO_THROW' }\n"
        "catch { Save 'a.txt' $_.Exception.Message }\n"
        "try { Invoke-Mac -Mac 'u@h' -Script 'true' -FailMessage 'copy failed' | Out-Null; Save 'b.txt' 'NO_THROW' }\n"
        "catch { Save 'b.txt' $_.Exception.Message }\n"
        "Save 'code.txt' $LASTEXITCODE\n"
    )
    r = run_ps(tmp_path, body, env={"FAKE_SSH_MODE": "decodefail"})
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    why = "맥에서 스크립트를 풀지 못함 (base64 -D 실패 또는 빈 스크립트, 종료 코드 97): true"
    assert read(tmp_path, "a.txt").strip() == why  # -AllowFail이어도 성공으로 넘기지 않는다
    assert read(tmp_path, "b.txt").strip() == "copy failed — " + why
    assert read(tmp_path, "code.txt").strip() == "97"


def test_bootstrap_rejects_empty_or_bad_script(tmp_path):
    # 부트스트랩 자체(맥 /bin/sh에서 도는 글)를 확인: 풀린 스크립트가 비면 97, 그룹·pidfile·정리
    run_ps(tmp_path, "Invoke-Mac -Mac 'u@h' -Script 'true' | Out-Null\n")
    m = CMD.match(json.loads(read(tmp_path, "argv.json"))[-1])
    boot = base64.b64decode(m.group(1)).decode("utf-8")
    assert "|| p=" in boot and 'if [ -z "$p" ]' in boot and "exit 97" in boot
    assert '"$2" /bin/sh -c "$p" invoke-mac' in boot and 'rm -f "$2"' in boot and "exit $rc" in boot
    assert "\r" not in boot


def test_nonzero_exit(tmp_path):
    body = (
        "try { Invoke-Mac -Mac 'u@h' -Script 'false' -FailMessage 'deploy failed ({code})' | Out-Null; Save 'a.txt' 'NO_THROW' }\n"
        "catch { Save 'a.txt' $_.Exception.Message }\n"
        "try { Invoke-Mac -Mac 'u@h' -Script 'false' | Out-Null; Save 'b.txt' 'NO_THROW' } catch { Save 'b.txt' $_.Exception.Message }\n"
        "$o = Invoke-Mac -Mac 'u@h' -Script 'false' -AllowFail; Save 'c.txt' (@($LASTEXITCODE) + $o)\n"
        "Invoke-Mac -Mac 'u@h' -Script 'false' -Stream 6>$null\n"
    )
    r = run_ps(tmp_path, body, env={"FAKE_SSH_EXIT": "3"})
    assert read(tmp_path, "a.txt").strip() == "deploy failed (3)"
    assert read(tmp_path, "b.txt").strip() == "맥 명령 실패 (종료 코드 3): false"
    assert read(tmp_path, "c.txt").splitlines() == ["3", "출력 한글 줄", "둘째 줄"]
    assert r.returncode != 0  # 마지막 줄: 스트림이어도 실패면 throw


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


def test_copy_args_and_messages(tmp_path):
    body = (
        "Copy-ToMac -Mac 'u@h' -Path 'a b.txt', 'c' -Destination 'doc_platform/x/' -Recurse 6>$null\n"
        "Copy-FromMac -Mac 'u@h' -Path 'doc_platform/f' -Destination 'C:\\tmp dir\\f' 6>$null\n"
        "$env:FAKE_SSH_EXIT = '1'\n"
        "try { Copy-ToMac -Mac 'u@h' -Path 'k.sh' -Destination 'd/' 6>$null } catch { Save 'fail.txt' $_.Exception.Message }\n"
        "$env:FAKE_SSH_EXIT = '0'; $RemoteScpExe = @('" + sys.executable + "', '-I', '-c', 'import time; time.sleep(30)')\n"
        "try { Copy-FromMac -Mac 'u@h' -Path 'big' -Destination 'x' -TimeoutSec 2 6>$null }\n"
        "catch { Save 'slow.txt' $_.Exception.Message }\n"
    )
    r = run_ps(tmp_path, body)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    c = calls(tmp_path)
    assert c[0]["argv"] == ["-q", *SSH_OPTS, "-r", "a b.txt", "c", "u@h:doc_platform/x/"]
    assert c[1]["argv"] == ["-q", *SSH_OPTS, "u@h:doc_platform/f", "C:\\tmp dir\\f"]
    assert read(tmp_path, "fail.txt").strip() == "scp 전송 실패 (종료 코드 1): k.sh → u@h:d/"  # '맥 명령'이 아니라 전송
    slow = read(tmp_path, "slow.txt").strip()
    assert slow.startswith("scp 전송이 2초 안에 끝나지 않아 끊었습니다 (시간 제한): u@h:big → x"), slow


def test_rejects_option_like_host(tmp_path):
    body = "".join(
        f"try {{ {call} | Out-Null; Save 'r{i}.txt' 'NO_THROW' }} catch {{ Save 'r{i}.txt' $_.Exception.Message }}\n"
        for i, call in enumerate([
            "Invoke-Mac -Mac '-oProxyCommand=calc' -Script 'true'",
            "Copy-ToMac -Mac '-oProxyCommand=calc' -Path a -Destination b 6>$null",
            "Copy-FromMac -Mac 'u@h x' -Path a -Destination b 6>$null",
        ])
    )
    r = run_ps(tmp_path, body)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    for i in range(3):
        assert read(tmp_path, f"r{i}.txt").startswith("맥 주소가 이상합니다"), i
    assert not (tmp_path / "calls.jsonl").exists()  # ssh·scp를 띄우지도 않음


@pytest.mark.skipif(shutil.which("ssh.exe") is None, reason="Windows OpenSSH 없음")
def test_ssh_resolved_to_full_path(tmp_path):
    r = run_ps(tmp_path, "Save 'exe.txt' @($RemoteSshExe[0], $RemoteScpExe[0], $RemoteSshExe.Count)\n", fake=False)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    ssh, scp, n = read(tmp_path, "exe.txt").splitlines()
    assert Path(ssh).is_absolute() and Path(ssh).name.lower() == "ssh.exe" and n == "1"
    assert Path(scp).is_absolute() and Path(scp).name.lower() == "scp.exe"


def test_stop_script_handles_pidfile_race(tmp_path):
    # 시간 제한이 pidfile보다 먼저면: 잠깐 기다리고, 그래도 없으면 그 이름을 먼저 만들어(set -C) 부트스트랩 O_EXCL이 실패하게
    r = run_ps(tmp_path, "Invoke-Mac -Mac 'u@h' -Script 'sleep 100' -TimeoutSec 1 6>$null\n", env={"FAKE_SSH_MODE": "sleep"})
    assert r.returncode != 0
    kill = calls(tmp_path)[1]["script"]
    assert 'while [ ! -s "$pf" ] && [ $i -lt 10 ]; do sleep 0.5' in kill
    assert '( set -C; echo cancelled > "$pf" )' in kill and "이제는 시작하지 못함" in kill
    assert "cancelled) echo" in kill  # 선점된 이름을 다시 만나도 그룹으로 오인하지 않음
    assert "''|*[!0-9]*)" in kill  # 숫자만 kill
    assert "pgrep -f -- '([n]erdctl|[b]uildctl).*docflow(-web)?:dev'" in kill  # VM 안 빌드가 남았는지 정직하게
    boot = read(tmp_path, "boot.txt")
    assert "O_WRONLY | O_CREAT | O_EXCL, 0600" in boot  # 선점된 이름이면 perl이 죽고 스크립트는 돌지 않는다


def test_interrupted_call_stops_remote_job(tmp_path):
    # Ctrl+C·파이프라인 중단·예외 대신: -Stream 출력(Write-Host)이 멈춤 예외를 내게 해 호출을 중간에 끊는다
    body = (
        "$InformationPreference = 'Stop'\n"
        "try { Invoke-Mac -Mac 'u@h' -Script 'long job' -Stream; Save 'msg.txt' 'NO_THROW' } catch { Save 'msg.txt' 'THREW' }\n"
    )
    r = run_ps(tmp_path, body)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    assert read(tmp_path, "msg.txt").strip() == "THREW"
    c = calls(tmp_path)
    assert len(c) == 2 and c[0]["script"].endswith("long job") and "kill -TERM" in c[1]["script"]
    assert read(tmp_path, "kill.txt") == c[0]["pidfile"]  # finally가 같은 pidfile로 멈추기를 불렀다


def test_single_zero_byte_stdin(tmp_path):
    # [byte[]]@(0)에 -and를 걸면 거짓 → 표준 입력이 빠지고 -n이 붙던 문제
    r = run_ps(tmp_path, "Invoke-Mac -Mac 'u@h' -Script 'cat' -Stdin ([byte[]]@(0)) | Out-Null\n")
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    assert (tmp_path / "stdin.bin").read_bytes() == b"\x00"
    assert "-n" not in json.loads(read(tmp_path, "argv.json"))


def test_bootstrap_b64_precomputed(tmp_path):
    body = (
        "Save 'b64.txt' $RemoteBootstrapB64\n"
        "Invoke-Mac -Mac 'u@h' -Script 'true' | Out-Null\n"
        f"$src = Get-Content -Raw -Encoding UTF8 '{HELPER}'\n"
        "Save 'decl.txt' ($src -match '\\$RemoteBootstrapB64 = ConvertTo-B64 \\$RemoteBootstrap')\n"
    )
    r = run_ps(tmp_path, body)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    m = CMD.match(json.loads(read(tmp_path, "argv.json"))[-1])
    assert m.group(1) == read(tmp_path, "b64.txt").strip()  # 호출마다 다시 만들지 않고 dot-source 때 만든 값
    assert read(tmp_path, "decl.txt").strip() == "True"
