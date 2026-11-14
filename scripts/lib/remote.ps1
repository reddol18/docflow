<#
맥 원격 명령은 여기 함수로만 한다 (W46 회고 규칙 B, https://github.com/reddol18/docflow/issues/117).
스크립트에서 ssh·scp를 직접 부르지 않는다 — tests/test_remote_helper.py가 PowerShell AST로 막는다.

  . (Join-Path $PSScriptRoot "lib/remote.ps1")
  Invoke-Mac -Mac $Mac -Script "sh ~/doc_platform/scripts/deploy.sh" -Stream -TimeoutSec 3600 -FailMessage "배포 실패"
  $rows = Invoke-Mac -Mac $Mac -Script "kubectl get pods -n docs"                   # 출력 줄을 돌려받음
  Invoke-Mac -Mac $Mac -Script "sh ~/doc_platform/scripts/tls_secrets.sh" -Stdin $text  # 표준 입력 (BOM 없음)
  Copy-ToMac -Mac $Mac -Path a.sh, b.sh -Destination "doc_platform/scripts/"
  Copy-FromMac -Mac $Mac -Path "doc_platform/requirements.freeze" -Destination $local

왜 (2026-W46):
- PowerShell 5.1은 네이티브 명령 인자 안의 큰따옴표를 지운다 → 맥 zsh가 'command not found'를 냈고 맥 고장으로 오해했다.
  여기서는 ssh를 PowerShell 네이티브 호출이 아니라 Process로 띄우고 인자를 Windows 규칙대로 직접 감싼다. 스크립트 본문은 base64로 넘긴다.
  맥에 가는 명령줄은 늘 아래 꼴이다 (바깥 sh -c: 맥 로그인 셸 zsh의 MULTIOS 같은 차이를 피한다):
    sh -c 'b=$(echo <부트스트랩 b64> | /usr/bin/base64 -D) && [ -n "$b" ] && exec /bin/sh -c "$b" invoke-mac <스크립트 b64> <pidfile>; ...; exit 97'
  부트스트랩(아래 $RemoteBootstrap): 스크립트를 풀고(실패하거나 비면 종료 코드 97), 새 프로세스 그룹에서 돌리고,
  그 그룹 번호를 pidfile(/tmp/invoke-mac.<무작위>.pid, 600, O_EXCL)에 남긴다. 스크립트의 표준 입력은 ssh 표준 입력 그대로.
- 맥이 먹통일 때 시간 제한이 없어 하염없이 기다렸다 → ssh 옵션(접속 5초, 살아 있음 10초×3) + 전체 시간 제한.
  넘으면 ssh를 끊고, 짧은 두 번째 호출로 맥 쪽 프로세스 그룹에 TERM(5초 뒤 남으면 KILL)을 보낸 뒤 결과를 담아 throw한다
  (ssh만 끊으면 tty가 없어 맥 쪽 작업은 계속 돈다).
- 5.1은 네이티브 표준 입력 앞에 [Console]::InputEncoding의 BOM을 붙인다 (Process.Start가 만드는 StreamWriter가 머리말을 쓴다).
  -Stdin이 있을 때만: 띄우는 동안 그 인코딩을 머리말 없는 UTF-8로 바꾼다 — 먼저 공개 setter, 콘솔 없는 호스트에서 setter가
  예외를 내면 캐시 필드(Console._inputEncoding)로. -Stdin이 없으면 표준 입력을 리디렉션하지 않고 ssh -n.
- 출력: -Stream이면 줄마다 바로 화면(Write-Host)에 — 배포처럼 긴 일은 사용자가 보고 있다. 아니면 표준 출력 줄을 돌려준다.
  표준 오류는 늘 바로 화면에. 어느 쪽이든 리디렉션 없이 콘솔에 보이므로 check.ps1·deploy.ps1 출력을 리디렉션하지 않는 관행과 맞다.
- 종료 코드가 0이 아니면 throw (-FailMessage가 있으면 그 문구, '{code}'는 종료 코드로). -AllowFail이면 $LASTEXITCODE만 남긴다.
- PATH: 맥 ssh 비대화 셸의 PATH는 /usr/bin:/bin:/usr/sbin:/sbin뿐(kubectl·colima 없음, 2026-11-14 확인).
  맥 스크립트 대부분과 같은 순서로 앞에 붙인다 — kubectl은 /usr/local/bin(1.15)이 ~/.local/bin(1.37)보다 먼저. sh·base64·perl은 시스템 것 그대로.
#>

# ssh.exe·scp.exe는 PATH에서 한 번 찾아 전체 경로로 — Process.Start는 현재 폴더를 먼저 뒤지므로 이름만 주지 않는다.
# 시험만 바꾼다 (tests/test_remote_helper.py가 가짜 ssh로 바꿔 끼운다). 첫 칸이 실행 파일, 나머지는 앞에 붙는 인자
$RemoteSshExe = @(Get-Command ssh.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1 | ForEach-Object Source)
$RemoteScpExe = @(Get-Command scp.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1 | ForEach-Object Source)
$RemoteSshOpts = @("-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3")
$RemotePathLine = 'export PATH=/usr/local/bin:/opt/homebrew/bin:$HOME/.local/bin:$PATH'
$RemoteDecodeFailed = "invoke-mac: decode-failed"
$RemoteBootstrap = @'
# invoke-mac 부트스트랩 ($1 = 스크립트 base64, $2 = pidfile). scripts/lib/remote.ps1이 만든다
p=$(printf '%s' "$1" | /usr/bin/base64 -D 2>/dev/null) || p=
if [ -z "$p" ]; then echo 'invoke-mac: decode-failed (스크립트 base64 -D 실패 또는 빈 스크립트)' >&2; exit 97; fi
# 새 프로세스 그룹(번호 = 이 perl의 pid)에서 스크립트를 돌리고 그 번호를 pidfile에 — 시간 제한 때 그룹째 멈춘다
/usr/bin/perl -e 'use Fcntl; setpgrp(0, 0) or die "setpgrp: $!\n"; my $pf = shift;
  sysopen(my $f, $pf, O_WRONLY | O_CREAT | O_EXCL, 0600) or die "invoke-mac: pidfile $pf: $!\n"; print $f "$$\n"; close $f;
  exec @ARGV or die "exec: $!\n"' "$2" /bin/sh -c "$p" invoke-mac
rc=$?
rm -f "$2"
exit $rc
'@

function Assert-MacHost([string]$Mac) {
    # ssh·scp 옵션으로 읽히지 않게 (예: -oProxyCommand=…)
    if (-not $Mac -or $Mac.StartsWith("-") -or $Mac -match '\s') { throw "맥 주소가 이상합니다: '$Mac' (user@host 꼴, '-'로 시작하거나 공백이 있으면 안 됨)" }
}

function ConvertTo-RemoteArg([string]$Arg) {
    # Windows 명령줄 규칙(MSVCRT)대로 한 인자를 감싼다 — 공백·큰따옴표가 있어도 그대로 전달
    if ($Arg -and $Arg -notmatch '[\s"]') { return $Arg }
    $s = [regex]::Replace($Arg, '(\\*)"', { param($m) ($m.Groups[1].Value * 2) + '\"' })
    $s = [regex]::Replace($s, '(\\+)$', { param($m) $m.Groups[1].Value * 2 })
    return '"' + $s + '"'
}

function ConvertTo-B64([string]$Text) { [Convert]::ToBase64String([Text.UTF8Encoding]::new($false).GetBytes($Text.Replace("`r", ""))) }

$RemoteBootstrapB64 = ConvertTo-B64 $RemoteBootstrap  # 고정 글이라 dot-source 때 한 번

function ConvertTo-MacCommand([string]$Script, [string]$PidFile) {
    # 맥 쪽 명령줄 한 줄. 스크립트는 PATH 줄을 앞에 붙여 base64로, 부트스트랩도 base64로 (부트스트랩이 안 풀리면 97)
    $body = ConvertTo-B64 ($RemotePathLine + "`n" + $Script)
    return "sh -c 'b=`$(echo $RemoteBootstrapB64 | /usr/bin/base64 -D) && [ -n `"`$b`" ] && exec /bin/sh -c `"`$b`" invoke-mac $body $PidFile; echo $RemoteDecodeFailed >&2; exit 97'"
}

function New-MacPidFile { "/tmp/invoke-mac." + [guid]::NewGuid().ToString("N") + ".pid" }

function Invoke-RemoteProcess {
    # ssh·scp 한 번 실행: 시간 제한, BOM 없는 표준 입력, 줄 단위 출력.
    # 반환: @{ Code; Out; Marked(표준 오류에 -Marker로 시작하는 줄이 있었나); TimedOut }
    param(
        [string[]]$Exe,
        [Parameter(Mandatory)][string[]]$Arguments,
        [byte[]]$StdinBytes,
        [int]$TimeoutSec,
        [switch]$Stream,
        [string]$Marker
    )
    if (-not $Exe -or -not $Exe[0]) { throw "ssh.exe·scp.exe를 찾지 못했습니다 (Windows OpenSSH 클라이언트가 PATH에 있어야 함)" }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Exe[0]
    $psi.Arguments = (@($Exe | Select-Object -Skip 1) + $Arguments | ForEach-Object { ConvertTo-RemoteArg $_ }) -join " "
    # 상대 경로(scp 대상)는 PowerShell 위치 기준 — .NET 현재 폴더는 Push-Location을 따르지 않는다
    $psi.WorkingDirectory = (Get-Location -PSProvider FileSystem).ProviderPath
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $hasStdin = $null -ne $StdinBytes -and $StdinBytes.Length -gt 0  # 배열에 -and를 걸면 [byte]0 한 개가 거짓이 된다
    $psi.RedirectStandardInput = $hasStdin
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $utf8 = [Text.UTF8Encoding]::new($false)
    $psi.StandardOutputEncoding = $utf8
    $psi.StandardErrorEncoding = $utf8

    # BOM 막기 (-Stdin이 있을 때만): Process.Start는 [Console]::InputEncoding으로 표준 입력 StreamWriter를 만들고 머리말을 바로 쓴다
    $restore = $null
    if ($hasStdin) {
        if ($psi.GetType().GetProperty("StandardInputEncoding")) {
            $psi.GetType().GetProperty("StandardInputEncoding").SetValue($psi, $utf8)  # PowerShell 7(.NET Core)은 정식 속성이 있다
        } else {
            $enc = $null
            try { $enc = [Console]::InputEncoding } catch { $enc = $null }
            if ($enc -and $enc.GetPreamble().Length) {
                try {
                    [Console]::InputEncoding = $utf8  # 공개 setter 먼저 (콘솔이 있으면 된다)
                    $restore = { try { [Console]::InputEncoding = $enc } catch { } }.GetNewClosure()
                } catch {
                    # 콘솔 없는 호스트: setter가 SetConsoleCP 실패로 예외 → 캐시 필드만 바꾼다
                    $field = [Console].GetField("_inputEncoding", [Reflection.BindingFlags]"NonPublic,Static")
                    if (-not $field) { throw "표준 입력을 BOM 없이 넘길 방법이 없습니다 (Console._inputEncoding 없음, 인코딩 $($enc.WebName))" }
                    $saved = $field.GetValue($null)
                    $field.SetValue($null, $utf8)
                    $restore = { $field.SetValue($null, $saved) }.GetNewClosure()
                }
            }
        }
    }
    try { $p = [Diagnostics.Process]::Start($psi) }
    finally { if ($restore) { & $restore } }

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSec)
    $out = [Collections.Generic.List[string]]::new()
    $marked = $false
    $stdin = $null; $writeTask = $null
    if ($hasStdin) { $stdin = $p.StandardInput.BaseStream; $writeTask = $stdin.WriteAsync($StdinBytes, 0, $StdinBytes.Length) }
    $outTask = $p.StandardOutput.ReadLineAsync()
    $errTask = $p.StandardError.ReadLineAsync()
    try {
        while ($outTask -or $errTask -or -not $p.HasExited) {
            if ([DateTime]::UtcNow -gt $deadline) {
                try { $p.Kill() } catch { }
                [void]$p.WaitForExit(5000)
                return @{ Code = $null; Out = $out.ToArray(); Marked = $marked; TimedOut = $true }
            }
            if ($stdin -and $writeTask.IsCompleted) {
                if ($writeTask.IsFaulted) { Write-Host "표준 입력 쓰기 실패: $($writeTask.Exception.InnerException.Message)" }
                $stdin.Close(); $stdin = $null
            }
            $pending = @($outTask, $errTask) | Where-Object { $_ }
            if ($pending) { [void][Threading.Tasks.Task]::WaitAny([Threading.Tasks.Task[]]$pending, 200) }
            else { [void]$p.WaitForExit(200) }
            if ($outTask -and $outTask.IsCompleted) {
                $line = $outTask.Result
                if ($null -eq $line) { $outTask = $null }
                else {
                    if ($Stream) { Write-Host $line } else { $out.Add($line) }
                    $outTask = $p.StandardOutput.ReadLineAsync()
                }
            }
            if ($errTask -and $errTask.IsCompleted) {
                $line = $errTask.Result
                if ($null -eq $line) { $errTask = $null }
                else {
                    Write-Host $line
                    if ($Marker -and $line.StartsWith($Marker)) { $marked = $true }
                    $errTask = $p.StandardError.ReadLineAsync()
                }
            }
        }
        $p.WaitForExit()
        return @{ Code = $p.ExitCode; Out = $out.ToArray(); Marked = $marked; TimedOut = $false }
    } finally {
        if (-not $p.HasExited) { try { $p.Kill() } catch { } }
        $p.Dispose()
    }
}

function Complete-Remote($r, [string]$FailMessage, [string]$Default, [switch]$AllowFail, [switch]$Stream) {
    $global:LASTEXITCODE = $r.Code
    if ($r.Code -and -not $AllowFail) {
        if ($FailMessage) { throw $FailMessage.Replace("{code}", "$($r.Code)") }
        throw $Default
    }
    if (-not $Stream) { $r.Out }
}

# 맥 VM 안 docflow 빌드 (colima nerdctl build·buildctl). '[n]erdctl'은 pgrep·pkill이 자기를 감싼 셸 명령줄을 잡지 않게
# (scripts/mac/build_image.sh 트랩과 같은 무늬 — tests/test_remote_helper.py가 둘이 같은지 본다)
$RemoteVmBuildPattern = '([n]erdctl|[b]uildctl).*docflow(-web)?:dev'

function Stop-MacJob([string]$Mac, [string]$PidFile, [int]$TimeoutSec = 30) {
    # 시간 제한·중단 뒤: pidfile의 프로세스 그룹에 TERM, 5초 뒤 남으면 KILL. 결과 한 줄을 돌려준다 (실패해도 throw하지 않음).
    # pidfile이 아직 없으면(시간 제한이 시작보다 빨랐음) 5초 기다리고, 그래도 없으면 그 이름을 먼저 만들어(set -C) 부트스트랩의
    # O_EXCL이 실패하게 한다 → 이제 시작하지 못한다. 끝에 colima VM 안 docflow 빌드가 남았는지 정직하게 덧붙인다
    # (호스트 프로세스 그룹을 멈춰도 VM 안 빌드는 build_image.sh 트랩이 멈추려 할 뿐이다)
    $kill = @"
pf=$PidFile
vm() {
  command -v colima >/dev/null 2>&1 || { echo ' / VM 확인 못 함(colima 없음)'; return; }
  if colima ssh -- pgrep -f -- '$RemoteVmBuildPattern' </dev/null >/dev/null 2>&1; then
    echo ' / 주의: colima VM 안 docflow 빌드가 아직 돈다(build_image.sh 트랩이 멈추려 했다) — colima ssh -- pgrep -fl nerdctl 로 확인'
  else echo ' / colima VM 안 docflow 빌드 없음'; fi
}
i=0
while [ ! -s "`$pf" ] && [ `$i -lt 10 ]; do sleep 0.5; i=`$((i + 1)); done
if [ ! -e "`$pf" ] && ( set -C; echo cancelled > "`$pf" ) 2>/dev/null; then
  echo "실행 중인 작업 없음(이미 끝났거나 아직 시작 전 — pidfile 이름을 선점해 이제는 시작하지 못함)`$(vm)"; exit 0
fi
[ -s "`$pf" ] || sleep 1
g=`$(cat "`$pf" 2>/dev/null)
case "`$g" in
  cancelled) echo "실행 중인 작업 없음(이미 선점됨)`$(vm)"; exit 0 ;;
  ''|*[!0-9]*) echo "pidfile 내용이 이상함('`$g') — 맥에서 직접 확인 필요: ps -axo pid,pgid,command`$(vm)"; exit 1 ;;
esac
if ! kill -TERM -- "-`$g" 2>/dev/null; then rm -f "`$pf"; echo "작업 없음(그룹 `$g 이미 끝남)`$(vm)"; exit 0; fi
i=0; how=TERM
while kill -0 -- "-`$g" 2>/dev/null && [ `$i -lt 5 ]; do sleep 1; i=`$((i + 1)); done
if kill -0 -- "-`$g" 2>/dev/null; then kill -KILL -- "-`$g" 2>/dev/null; how='TERM 뒤 KILL'; sleep 1; fi
if kill -0 -- "-`$g" 2>/dev/null; then echo "프로세스 그룹 `$g 가 아직 남아 있음 — 맥에서 직접 확인 필요`$(vm)"; exit 1; fi
rm -f "`$pf"
echo "프로세스 그룹 `$g 멈춤(`$how)`$(vm)"
"@
    try {
        $r = Invoke-RemoteProcess -Exe $RemoteSshExe -Arguments ($RemoteSshOpts + @("-n", $Mac, (ConvertTo-MacCommand $kill (New-MacPidFile)))) -TimeoutSec $TimeoutSec
        if ($r.TimedOut) { return "멈추기 호출도 시간 제한(${TimeoutSec}초) — 맥 쪽 작업이 아직 돌 수 있다, 맥에서 직접 확인 필요" }
        $msg = ($r.Out | Where-Object { $_ }) -join " "
        if ($r.Code) { return "실패 (종료 코드 $($r.Code)): $msg" }
        return $msg
    } catch { return "실패: $($_.Exception.Message) — 맥 쪽 작업이 아직 돌 수 있다, 맥에서 직접 확인 필요" }
}

function Invoke-Mac {
    <# 맥에서 sh 스크립트를 돌린다. -Stdin은 문자열(UTF-8) 또는 byte[] — BOM 없이 그대로 #>
    param(
        [Parameter(Mandatory)][string]$Mac,
        [Parameter(Mandatory)][string]$Script,
        [object]$Stdin,
        [int]$TimeoutSec = 60,
        [switch]$Stream,
        [switch]$AllowFail,
        [string]$FailMessage
    )
    Assert-MacHost $Mac
    $bytes = $null
    if ($Stdin -is [byte[]]) { $bytes = $Stdin }
    elseif ($null -ne $Stdin) { $bytes = [Text.UTF8Encoding]::new($false).GetBytes([string]$Stdin) }
    $label = ($Script.Trim() -split "`n")[0].Trim()
    $pidFile = New-MacPidFile
    # 표준 입력 없음 → ssh -n (리디렉션도 BOM 손질도 하지 않음)
    $n = if ($null -ne $bytes -and $bytes.Length -gt 0) { @() } else { @("-n") }
    $r = $null; $stopped = $false
    try {
        $r = Invoke-RemoteProcess -Exe $RemoteSshExe -Arguments ($RemoteSshOpts + $n + @($Mac, (ConvertTo-MacCommand $Script $pidFile))) `
            -StdinBytes $bytes -TimeoutSec $TimeoutSec -Stream:$Stream -Marker $RemoteDecodeFailed
        if ($r.TimedOut) {
            $res = Stop-MacJob $Mac $pidFile; $stopped = $true
            $global:LASTEXITCODE = 124
            throw "시간 제한(${TimeoutSec}초)으로 끊음: $label — 맥 쪽 작업을 멈추려 했다(결과: $res). 배포 상태를 확인한 뒤 다시"
        }
    } finally {
        # Ctrl+C·파이프라인 중단·예외로 끝나지 못했으면(ssh는 Invoke-RemoteProcess가 이미 끊음) 맥 쪽도 멈춰 본다 — 짧게, 최선만
        if (-not $r -and -not $stopped) {
            $res = Stop-MacJob $Mac $pidFile 15
            try { Write-Host "맥 명령이 중단됨: $label — 맥 쪽 작업을 멈추려 했다(결과: $res)" } catch { }
        }
    }
    if ($r.Code -eq 97 -and $r.Marked) {
        $global:LASTEXITCODE = 97
        $why = "맥에서 스크립트를 풀지 못함 (base64 -D 실패 또는 빈 스크립트, 종료 코드 97): $label"
        throw $(if ($FailMessage) { "$($FailMessage.Replace('{code}', '97')) — $why" } else { $why })
    }
    Complete-Remote $r $FailMessage "맥 명령 실패 (종료 코드 $($r.Code)): $label" -AllowFail:$AllowFail -Stream:$Stream
}
function Invoke-Scp([string[]]$Arguments, [int]$TimeoutSec, [string]$Label, [string]$FailMessage, [switch]$AllowFail) {
    $r = Invoke-RemoteProcess -Exe $RemoteScpExe -Arguments $Arguments -TimeoutSec $TimeoutSec -Stream
    if ($r.TimedOut) {
        $global:LASTEXITCODE = 124
        throw "scp 전송이 ${TimeoutSec}초 안에 끝나지 않아 끊었습니다 (시간 제한): $Label — 맥·네트워크 상태를 확인한 뒤 다시"
    }
    Complete-Remote $r $FailMessage "scp 전송 실패 (종료 코드 $($r.Code)): $Label" -AllowFail:$AllowFail -Stream
}

function Copy-ToMac {
    <# scp 이 PC → 맥. -Destination은 맥 쪽 경로 (홈 기준, 예: doc_platform/scripts/) #>
    param(
        [Parameter(Mandatory)][string]$Mac,
        [Parameter(Mandatory)][string[]]$Path,
        [Parameter(Mandatory)][string]$Destination,
        [switch]$Recurse,
        [int]$TimeoutSec = 600,
        [switch]$AllowFail,
        [string]$FailMessage
    )
    Assert-MacHost $Mac
    $a = @("-q") + $RemoteSshOpts + $(if ($Recurse) { @("-r") } else { @() }) + $Path + @("${Mac}:$Destination")
    Invoke-Scp $a $TimeoutSec "$($Path -join ', ') → ${Mac}:$Destination" $FailMessage -AllowFail:$AllowFail
}

function Copy-FromMac {
    <# scp 맥 → 이 PC. -Path는 맥 쪽 경로 (홈 기준) #>
    param(
        [Parameter(Mandatory)][string]$Mac,
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Destination,
        [int]$TimeoutSec = 600,
        [switch]$AllowFail,
        [string]$FailMessage
    )
    Assert-MacHost $Mac
    Invoke-Scp (@("-q") + $RemoteSshOpts + @("${Mac}:$Path", $Destination)) $TimeoutSec "${Mac}:$Path → $Destination" $FailMessage -AllowFail:$AllowFail
}
