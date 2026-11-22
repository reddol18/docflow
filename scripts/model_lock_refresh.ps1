<#
OCR 모델 lock(app/models.lock, 목록·sha256)을 다시 만든다 — 모델 판을 올릴 때만 (https://github.com/reddol18/docflow/issues/102).
paddleocr·paddlex 판(requirements.txt → .\scripts\lock_refresh.ps1)이나 app/ocr.py의 모델 설정을 바꿨을 때 쓴다.

  .\scripts\model_lock_refresh.ps1
  .\scripts\model_lock_refresh.ps1 -Distro docflow81

순서: 이 PC의 WSL 배포판(docker + buildx, 기본 docflow81 — 폐쇄망 묶음을 만드는 곳) 안 root로 scripts/model_lock_refresh.sh
      → 디스크 / 여유 확인(모자라면 중단) → Dockerfile model-lock-out 단계만 빌드(모델 받기 → 목록·sha256, 이미지 태그 없음)
      → app/models.lock을 쓰고 바뀐 줄 수를 보여 준다. 맥·배포는 건드리지 않는다 (실험·받기는 이 PC에서).
끝나면 바뀐 파일을 확인하고 판 변경과 함께 커밋한다(PR). 이미지 빌드(맥 배포·폐쇄망 묶음)는 그 lock과 다르면 멈춘다.
#>
param(
    [string]$Distro = $(if ($env:DOCFLOW_WSL) { $env:DOCFLOW_WSL } else { "docflow81" })
)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Push-Location $root
try {
    # E:\a\b → /mnt/e/a/b (WSL 기본 자동 마운트)
    $toWsl = { param($p) "/mnt/" + $p.Substring(0, 1).ToLower() + ($p.Substring(2) -replace '\\', '/') }
    $repo = & $toWsl $root
    Write-Host "== $Distro 안에서 모델 받기 → 목록·sha256"
    wsl -d $Distro -u root -- bash "$repo/scripts/model_lock_refresh.sh" "$repo" "$repo/app/models.lock"
    if ($LASTEXITCODE) { throw "모델 lock 만들기 실패 (위 로그)" }
    uv run --quiet --no-project python -I -c "import sys; sys.path.insert(0, '.'); from app import model_lock as m; from pathlib import Path; print('파일', len(m.parse(Path('app/models.lock').read_text(encoding='utf-8'))), '개')"
    if ($LASTEXITCODE) { throw "app/models.lock 형식 확인 실패" }
    $diff = git diff --numstat -- app/models.lock
    if (-not $diff) { Write-Host "== 바뀐 것 없음 (지금 배포처의 모델 = 고정된 목록)" }
    else { Write-Host "== 바뀜 (더한 줄·뺀 줄): $diff — 확인 뒤 판 변경과 함께 커밋"; git diff --stat -- app/models.lock }
} finally {
    Pop-Location
}
