<#
맥 원격 명령은 여기 함수로만 한다 (W46 회고 규칙 B, https://github.com/reddol18/docflow/issues/117).
스크립트에서 ssh·scp를 직접 부르지 않는다 — tests/test_remote_helper.py가 막는다.

  . (Join-Path $PSScriptRoot "lib/remote.ps1")
  Invoke-Mac -Mac $Mac -Script "sh ~/doc_platform/scripts/deploy.sh" -Stream -TimeoutSec 3600 -FailMessage "배포 실패"
  $rows = Invoke-Mac -Mac $Mac -Script "kubectl get pods -n docs </dev/null"          # 출력 줄을 돌려받음
  Invoke-Mac -Mac $Mac -Script "sh ~/doc_platform/scripts/tls_secrets.sh" -Stdin $text  # 표준 입력 (BOM 없음)
  Copy-ToMac -Mac $Mac -Path a.sh, b.sh -Destination "doc_platform/scripts/"
  Copy-FromMac -Mac $Mac -Path "doc_platform/requirements.freeze" -Destination $local

왜 (2026-W46):
- PowerShell 5.1은 네이티브 명령 인자 안의 큰따옴표를 지운다 → 맥 zsh가 'command not found'를 냈고 맥 고장으로 오해했다.
  그래서 스크립트는 base64로 넘긴다. 맥에 가는 명령줄은 늘 아래 한 줄이고 큰따옴표가 없다:
    sh -c 'exec 4<&0; echo <base64> | base64 -D | sh /dev/fd/3 3<&0 0<&4 4<&-'
  (안쪽 sh는 스크립트를 fd 3에서 읽고, 표준 입력은 ssh 표준 입력 그대로 — 스크립트 안 명령이 스크립트 나머지를 먹지 않는다.
   바깥 sh -c: 맥 로그인 셸 zsh는 MULTIOS 때문에 파이프와 0<&4를 이어 붙여 스크립트가 비었다 — 2026-11-14 맥에서 확인)
- 맥이 먹통일 때 시간 제한이 없어 하염없이 기다렸다 → ssh 옵션(접속 5초, 살아 있음 10초×3) + 전체 시간 제한(넘으면 ssh를 끊고 throw).
- 5.1은 네이티브 표준 입력 앞에 [Console]::InputEncoding의 BOM을 붙인다 (Process.Start가 만드는 StreamWriter가 머리말을 쓴다).
  Process를 띄우는 동안만 그 인코딩을 머리말 없는 것으로 바꾸고 바이트를 그대로 쓴다. 콘솔 없는 호스트에서도 예외가 나지 않는다
  (SetConsoleCP를 부르는 공개 setter 대신 캐시 필드를 바꾼다).
- 출력: -Stream이면 줄마다 바로 화면(Write-Host)에 — 배포처럼 긴 일은 사용자가 보고 있다. 아니면 표준 출력 줄을 돌려준다.
  표준 오류는 늘 바로 화면에. 어느 쪽이든 리디렉션 없이 콘솔에 보이므로 check.ps1·deploy.ps1 출력을 리디렉션하지 않는 관행과 맞다.
- 종료 코드가 0이 아니면 throw (-FailMessage가 있으면 그 문구). -AllowFail이면 throw하지 않고 $LASTEXITCODE만 남긴다.
#>

# 시험만 바꾼다 (tests/test_remote_helper.py가 가짜 ssh로 바꿔 끼운다). 첫 칸이 실행 파일, 나머지는 앞에 붙는 인자
$RemoteSshExe = @("ssh.exe")
$RemoteScpExe = @("scp.exe")
$RemoteSshOpts = @("-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3")
$RemotePathLine = 'export PATH=/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH'

function ConvertTo-RemoteArg([string]$Arg) {
    # Windows 명령줄 규칙(MSVCRT)대로 한 인자를 감싼다 — 공백·큰따옴표가 있어도 그대로 전달
    if ($Arg -and $Arg -notmatch '[\s"]') { return $Arg }
    $s = [regex]::Replace($Arg, '(\\*)"', { param($m) ($m.Groups[1].Value * 2) + '\"' })
    $s = [regex]::Replace($s, '(\\+)$', { param($m) $m.Groups[1].Value * 2 })
    return '"' + $s + '"'
}

function ConvertTo-MacCommand([string]$Script) {
    # 맥 쪽 명령줄 한 줄 (큰따옴표 없음). 스크립트는 CR을 빼고 PATH 줄을 앞에 붙여 UTF-8(BOM 없음) base64로
    $body = $RemotePathLine + "`n" + $Script.Replace("`r", "")
    $b64 = [Convert]::ToBase64String([Text.UTF8Encoding]::new($false).GetBytes($body))
    return "sh -c 'exec 4<&0; echo $b64 | base64 -D | sh /dev/fd/3 3<&0 0<&4 4<&-'"
}

function Invoke-RemoteProcess {
    # ssh·scp 한 번 실행: 시간 제한, BOM 없는 표준 입력, 줄 단위 출력. 반환: @{ Code; Out }
    param(
        [Parameter(Mandatory)][string[]]$Exe,
        [Parameter(Mandatory)][string[]]$Arguments,
        [byte[]]$StdinBytes,
        [int]$TimeoutSec,
        [switch]$Stream,
        [string]$Label
    )
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Exe[0]
    $psi.Arguments = (@($Exe | Select-Object -Skip 1) + $Arguments | ForEach-Object { ConvertTo-RemoteArg $_ }) -join " "
    # 상대 경로(scp 대상)는 PowerShell 위치 기준 — .NET 현재 폴더는 Push-Location을 따르지 않는다
    $psi.WorkingDirectory = (Get-Location -PSProvider FileSystem).ProviderPath
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $utf8 = [Text.UTF8Encoding]::new($false)
    $psi.StandardOutputEncoding = $utf8
    $psi.StandardErrorEncoding = $utf8

    # BOM 막기: Process.Start는 [Console]::InputEncoding으로 표준 입력 StreamWriter를 만들고 머리말(BOM)을 바로 쓴다.
    # 그 동안만 머리말 없는 인코딩으로 바꾼다. 공개 setter는 SetConsoleCP를 불러 콘솔 없는 호스트에서 예외가 나므로 캐시 필드를 쓴다.
    $field = $null; $saved = $null
    if ($psi.GetType().GetProperty("StandardInputEncoding")) {
        $psi.GetType().GetProperty("StandardInputEncoding").SetValue($psi, $utf8)  # PowerShell 7(.NET Core)은 정식 속성이 있다
    } else {
        $enc = $null
        try { $enc = [Console]::InputEncoding } catch { $enc = $null }
        if ($enc -and $enc.GetPreamble().Length) {
            $field = [Console].GetField("_inputEncoding", [Reflection.BindingFlags]"NonPublic,Static")
            if (-not $field) { throw "표준 입력을 BOM 없이 넘길 방법이 없습니다 (Console._inputEncoding 없음, 인코딩 $($enc.WebName))" }
            $saved = $field.GetValue($null)
            $field.SetValue($null, $utf8)
        }
    }
    try { $p = [Diagnostics.Process]::Start($psi) }
    finally { if ($field) { $field.SetValue($null, $saved) } }

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSec)
    $out = [Collections.Generic.List[string]]::new()
    $stdin = $p.StandardInput.BaseStream
    $writeTask = $null
    if ($StdinBytes -and $StdinBytes.Length) { $writeTask = $stdin.WriteAsync($StdinBytes, 0, $StdinBytes.Length) }
    else { $stdin.Close(); $stdin = $null }
    $outTask = $p.StandardOutput.ReadLineAsync()
    $errTask = $p.StandardError.ReadLineAsync()
    try {
        while ($outTask -or $errTask -or -not $p.HasExited) {
            if ([DateTime]::UtcNow -gt $deadline) {
                try { $p.Kill() } catch { }
                [void]$p.WaitForExit(5000)
                throw "맥 명령이 ${TimeoutSec}초 안에 끝나지 않아 ssh를 끊었습니다 (시간 제한): $Label — 맥이 답하는지 먼저 확인 (colima list, kubectl get pods -n docs)"
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
                else { Write-Host $line; $errTask = $p.StandardError.ReadLineAsync() }
            }
        }
        $p.WaitForExit()
        return @{ Code = $p.ExitCode; Out = $out.ToArray() }
    } finally {
        if (-not $p.HasExited) { try { $p.Kill() } catch { } }
        $p.Dispose()
    }
}

function Complete-Remote($r, [string]$FailMessage, [string]$Label, [switch]$AllowFail, [switch]$Stream) {
    $global:LASTEXITCODE = $r.Code
    if ($r.Code -and -not $AllowFail) {
        if ($FailMessage) { throw $FailMessage }
        throw "맥 명령 실패 (종료 코드 $($r.Code)): $Label"
    }
    if (-not $Stream) { $r.Out }
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
    $bytes = $null
    if ($Stdin -is [byte[]]) { $bytes = $Stdin }
    elseif ($null -ne $Stdin) { $bytes = [Text.UTF8Encoding]::new($false).GetBytes([string]$Stdin) }
    $label = ($Script.Trim() -split "`n")[0].Trim()
    $r = Invoke-RemoteProcess -Exe $RemoteSshExe -Arguments ($RemoteSshOpts + @($Mac, (ConvertTo-MacCommand $Script))) `
        -StdinBytes $bytes -TimeoutSec $TimeoutSec -Stream:$Stream -Label $label
    Complete-Remote $r $FailMessage $label -AllowFail:$AllowFail -Stream:$Stream
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
    $args_ = @("-q") + $RemoteSshOpts + $(if ($Recurse) { @("-r") } else { @() }) + $Path + @("${Mac}:$Destination")
    $label = "scp → ${Mac}:$Destination"
    $r = Invoke-RemoteProcess -Exe $RemoteScpExe -Arguments $args_ -TimeoutSec $TimeoutSec -Stream -Label $label
    Complete-Remote $r $FailMessage $label -AllowFail:$AllowFail -Stream
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
    $label = "scp ${Mac}:$Path →"
    $r = Invoke-RemoteProcess -Exe $RemoteScpExe -Arguments (@("-q") + $RemoteSshOpts + @("${Mac}:$Path", $Destination)) `
        -TimeoutSec $TimeoutSec -Stream -Label $label
    Complete-Remote $r $FailMessage $label -AllowFail:$AllowFail -Stream
}
