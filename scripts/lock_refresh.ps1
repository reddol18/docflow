<#
requirements.lock(해시 고정)을 다시 만든다 — requirements.txt를 바꿨을 때만 (https://github.com/reddol18/docflow/issues/33).

  $env:DOCFLOW_MAC = "user@host"
  .\scripts\lock_refresh.ps1
  .\scripts\lock_refresh.ps1 -Mac user@host

순서: 맥 디스크 여유 확인(scripts/mac/disk_check.sh, 모자라면 중단) → Dockerfile·requirements.txt 복사
      → scripts/mac/lock_refresh.sh (Dockerfile의 lock-out 단계만 --platform linux/amd64로 빌드해 freeze 파일만 꺼냄, 이미지 태그 없음)
      → freeze를 받아 scripts/lock_hashes.py가 PyPI 해시를 붙여 requirements.lock을 쓴다 → 바뀐 판을 보여 준다.
판 해석은 이미지 플랫폼에서 해야 해서(이 PC는 Windows) 맥 컨테이너 빌드로 한다. 배포·재시작은 하지 않는다.
끝나면 바뀐 판을 확인하고 requirements.txt와 함께 커밋한다(PR). 배포(scripts/deploy.ps1)는 그 lock 그대로 설치한다.
#>
param(
    [string]$Mac = $env:DOCFLOW_MAC
)
$ErrorActionPreference = "Stop"
# 접속이 걸려 멈추지 않게: 5초 안에 못 붙으면 실패, 10초마다 살아 있음 확인 3번 실패면 끊는다
$SshOpts = @("-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3")
if (-not $Mac) { throw "맥 주소가 없습니다: -Mac user@host 또는 `$env:DOCFLOW_MAC" }
$root = Split-Path $PSScriptRoot -Parent
Push-Location $root
try {
    Write-Host "== 디스크 여유 확인"
    # 반드시: 모자란 채로 빌드하면 kubelet이 운영 파드를 내쫓는다 (2026-11-07). 맥 쪽 lock_refresh.sh도 빌드 전에 한 번 더 본다
    ssh @SshOpts $Mac "mkdir -p ~/doc_platform/scripts"
    if ($LASTEXITCODE) { throw "맥 접속 실패" }
    scp -q @SshOpts scripts/mac/disk_check.sh scripts/mac/lock_refresh.sh "${Mac}:doc_platform/scripts/"
    if ($LASTEXITCODE) { throw "복사 실패 (scripts)" }
    ssh @SshOpts $Mac "sh ~/doc_platform/scripts/disk_check.sh 4"
    if ($LASTEXITCODE) { throw "디스크 여유가 모자랍니다. 정리한 뒤 다시 하세요 (위 메시지)." }

    Write-Host "== 복사 → linux/amd64에서 requirements.txt 풀기"
    scp -q @SshOpts Dockerfile requirements.txt "${Mac}:doc_platform/"
    if ($LASTEXITCODE) { throw "복사 실패" }
    ssh @SshOpts $Mac "sh ~/doc_platform/scripts/lock_refresh.sh"
    if ($LASTEXITCODE) { throw "lock 풀기 실패 (위 로그)" }

    Write-Host "== 해시 붙이기 (PyPI)"
    $freeze = Join-Path ([IO.Path]::GetTempPath()) "docflow-lock-refresh.freeze"
    scp -q @SshOpts "${Mac}:doc_platform/requirements.freeze" $freeze
    if ($LASTEXITCODE) { throw "freeze 받기 실패" }
    uv run --quiet --no-project python -I scripts/lock_hashes.py $freeze requirements.lock
    if ($LASTEXITCODE) { throw "해시 붙이기 실패" }
    Remove-Item $freeze
    uv run --quiet --no-project python -I scripts/lock_hashes.py --check requirements.txt requirements.lock
    if ($LASTEXITCODE) { throw "lock 확인 실패" }

    # 해시 줄은 길어서 판만 보여 준다
    $pins = { param($lines) $lines | Where-Object { $_ -match '^[^#\-+@ ]' } | ForEach-Object { ($_ -split '\s+')[0] } }
    $diff = git diff -U0 requirements.lock
    $removed = @(& $pins ($diff | Where-Object { $_ -match '^-[^-]' } | ForEach-Object { $_.Substring(1) }))
    $added = @(& $pins ($diff | Where-Object { $_ -match '^\+[^+]' } | ForEach-Object { $_.Substring(1) }))
    $gone = @($removed | Where-Object { $added -notcontains $_ })
    $new = @($added | Where-Object { $removed -notcontains $_ })
    if (-not $gone.Count -and -not $new.Count) { Write-Host "== 바뀐 판 없음 (해시·머리말만 바뀌었으면 그대로 커밋해도 된다)" }
    else {
        Write-Host "== 바뀐 판 (확인 후 requirements.txt와 함께 커밋)"
        $gone | ForEach-Object { Write-Host "  - $_" }
        $new | ForEach-Object { Write-Host "  + $_" }
    }
} finally {
    Pop-Location
}
