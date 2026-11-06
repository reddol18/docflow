<#
백엔드(docflow API)를 맥 k3s에 배포하고 배포 태그를 남긴다 (https://github.com/reddol18/docflow/issues/24).

  $env:DOCFLOW_MAC = "user@host"       # 맥 ssh 주소 (저장소에 적지 않는다)
  .\scripts\deploy.ps1
  .\scripts\deploy.ps1 -Mac user@host
  .\scripts\deploy.ps1 -NoTag          # 시험 배포 (태그 없음)
  .\scripts\deploy.ps1 -NoCheck        # 로컬 검사 건너뜀 (방금 돌렸을 때만)

순서: 커밋 안 된 변경 확인 → 로컬 검사(scripts/check.ps1) → 맥 디스크 여유 확인(scripts/mac/disk_check.sh, 모자라면 중단) → 맥으로 복사 → scripts/mac/deploy.sh(빌드·재시작·준비 대기)
      → 태그 deploy-YYYYMMDD-HHMM push
(맥에는 git 저장소가 없어서 태그는 이 스크립트가 남긴다)
프런트엔드는 k3s가 아니라 이 PC에서 next로 돈다(배포 대상 아님).
#>
param(
    [string]$Mac = $env:DOCFLOW_MAC,
    [switch]$NoTag,
    [switch]$NoCheck
)
$ErrorActionPreference = "Stop"
if (-not $Mac) { throw "맥 주소가 없습니다: -Mac user@host 또는 `$env:DOCFLOW_MAC" }
$root = Split-Path $PSScriptRoot -Parent
Push-Location $root
try {
    if (git status --porcelain) { throw "커밋 안 된 변경이 있습니다. 배포는 커밋된 코드로만 합니다 (태그가 가리키는 코드와 같아야 함)." }
    $sha = (git rev-parse --short HEAD).Trim()
    $branch = (git rev-parse --abbrev-ref HEAD).Trim()
    if ($branch -ne "main" -and -not $NoTag) { throw "main이 아닌 브랜치($branch)입니다. 시험 배포는 -NoTag" }

    if (-not $NoCheck) {
        & (Join-Path $PSScriptRoot "check.ps1")
        if ($LASTEXITCODE) { throw "로컬 검사 실패. 배포하지 않습니다." }
    }

    Write-Host "== 디스크 여유 확인"
    # 반드시: 모자란 채로 빌드하면 kubelet이 운영 파드를 내쫓는다 (2026-11-07). 맥 쪽 deploy.sh도 빌드 전에 한 번 더 본다
    ssh -o BatchMode=yes $Mac "mkdir -p ~/doc_platform/scripts ~/doc_platform/k8s"
    if ($LASTEXITCODE) { throw "맥 접속 실패" }
    scp -q scripts/mac/disk_check.sh "${Mac}:doc_platform/scripts/"
    ssh -o BatchMode=yes $Mac "sh ~/doc_platform/scripts/disk_check.sh"
    if ($LASTEXITCODE) { throw "디스크 여유가 모자랍니다. 정리한 뒤 배포하세요 (위 메시지)." }

    Write-Host "== 복사 ($sha → $Mac)"
    scp -q -r Dockerfile requirements.txt app "${Mac}:doc_platform/"
    if ($LASTEXITCODE) { throw "복사 실패 (app)" }
    scp -q k8s/docs.yaml "${Mac}:doc_platform/k8s/"
    if ($LASTEXITCODE) { throw "복사 실패 (k8s)" }
    scp -q (Get-ChildItem scripts/mac/*.sh).FullName "${Mac}:doc_platform/scripts/"
    if ($LASTEXITCODE) { throw "복사 실패 (scripts)" }
    # Windows scp가 새로 만든 폴더는 700 — 이미지 안 app 사용자가 못 읽는다 (Dockerfile chmod와 이중으로)
    ssh -o BatchMode=yes $Mac "chmod -R a+rX ~/doc_platform/app"
    if ($LASTEXITCODE) { throw "권한 고치기 실패" }

    Write-Host "== 빌드·재시작"
    ssh -o BatchMode=yes $Mac "sh ~/doc_platform/scripts/deploy.sh"
    if ($LASTEXITCODE) { throw "배포 실패 (위 로그). 태그를 남기지 않습니다." }

    # 이미지 안에서 pip freeze한 목록을 가져와 바뀌었는지 본다
    scp -q "${Mac}:doc_platform/requirements.lock" requirements.lock
    if (git status --porcelain requirements.lock) { Write-Warning "requirements.lock이 바뀌었습니다. 확인 후 커밋하세요." }

    if (-not $NoTag) {
        $tag = "deploy-" + (Get-Date -Format "yyyyMMdd-HHmm")
        git tag -a $tag -m "deploy $sha to mac k3s"
        git push origin $tag
        Write-Host "== 태그 $tag ($sha)"
    }
} finally {
    Pop-Location
}
