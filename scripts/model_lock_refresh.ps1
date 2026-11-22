<#
OCR 모델 lock(app/models.lock, 목록·sha256)을 다시 만든다 — 모델 판을 올릴 때만 (https://github.com/reddol18/docflow/issues/102).
paddleocr·paddlex 판(requirements.txt → .\scripts\lock_refresh.ps1)이나 app/ocr.py의 모델 설정을 바꿨을 때 쓴다.

  .\scripts\model_lock_refresh.ps1
  .\scripts\model_lock_refresh.ps1 -Distro docflow81

순서: 배포판 디스크(vhdx)가 있는 이 PC 드라이브 여유 확인(-NeedHostGB, 모자라면 중단) → 이 PC의 WSL 배포판(docker + buildx, 기본 docflow81 — 폐쇄망 묶음을 만드는 곳) 안 root로 scripts/model_lock_refresh.sh
      → 디스크 / 여유 확인(모자라면 중단) → Dockerfile model-lock-out 단계만 빌드(모델 받기 → 목록·sha256, 이미지 태그 없음)
      → app/models.lock을 쓰고 바뀐 줄 수를 보여 준다. 맥·배포는 건드리지 않는다 (실험·받기는 이 PC에서).
끝나면 바뀐 파일을 확인하고 판 변경과 함께 커밋한다(PR). 이미지 빌드(맥 배포·폐쇄망 묶음)는 그 lock과 다르면 멈춘다.
#>
param(
    [string]$Distro = $(if ($env:DOCFLOW_WSL) { $env:DOCFLOW_WSL } else { "docflow81" }),
    # 이 PC 쪽 여유: 배포판 디스크(ext4.vhdx)는 빌드 층·모델만큼 커진다. 배포판 안 / 여유는 가상 크기라 이것을 따로 본다
    [int]$NeedHostGB = 30
)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Push-Location $root
try {
    # 반드시 먼저: 배포판 디스크가 있는 이 PC 드라이브 여유 (records_poc/CLAUDE.md '디스크 여유 확인'). 모자라면 WSL을 띄우지도 않는다
    $lxss = Get-ChildItem HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss -ErrorAction SilentlyContinue |
        ForEach-Object { Get-ItemProperty $_.PSPath } | Where-Object { $_.DistributionName -eq $Distro } | Select-Object -First 1
    if (-not $lxss -or -not $lxss.BasePath) { throw "WSL 배포판 '$Distro'의 위치(BasePath)를 찾지 못함 — -Distro를 확인" }
    $base = $lxss.BasePath -replace '^\\\\\?\\', ''
    $drive = Get-PSDrive -Name $base.Substring(0, 1)
    $freeGB = [math]::Floor($drive.Free / 1GB)
    Write-Host "== 디스크 $($drive.Name): ($Distro 디스크 $base) 여유 ${freeGB}GB (필요 ${NeedHostGB}GB)"
    if ($freeGB -lt $NeedHostGB) { throw "디스크 모자람: $($drive.Name): 여유 ${freeGB}GB < ${NeedHostGB}GB — 멈춤 (정리 뒤 다시)" }

    # E:\a\b → /mnt/e/a/b (WSL 기본 자동 마운트)
    $toWsl = { param($p) "/mnt/" + $p.Substring(0, 1).ToLower() + ($p.Substring(2) -replace '\\', '/') }
    $repo = & $toWsl $root
    Write-Host "== $Distro 안에서 모델 받기 → 목록·sha256"
    wsl -d $Distro -u root -- bash "$repo/scripts/model_lock_refresh.sh" "$repo" "$repo/app/models.lock"
    if ($LASTEXITCODE) { throw "모델 lock 만들기 실패 (위 로그)" }
    uv run --quiet --no-project python -I -c "import sys; sys.path.insert(0, '.'); from app import model_lock as m; from pathlib import Path; print('파일', len(m.parse(Path('app/models.lock').read_text(encoding='utf-8'))), '개')"
    if ($LASTEXITCODE) { throw "app/models.lock 형식 확인 실패" }
    $diff = git diff --numstat -- app/models.lock
    if (-not (git ls-files -- app/models.lock)) { Write-Host "== 새로 만듦 (아직 커밋 안 된 파일) — 확인 뒤 커밋" }
    elseif (-not $diff) { Write-Host "== 바뀐 것 없음 (지금 배포처의 모델 = 고정된 목록)" }
    else { Write-Host "== 바뀜 (더한 줄·뺀 줄): $diff — 확인 뒤 판 변경과 함께 커밋"; git diff --stat -- app/models.lock }
} finally {
    Pop-Location
}
