<#
백엔드(docflow API)를 맥 k3s에 배포하고 배포 태그를 남긴다 (https://github.com/reddol18/docflow/issues/24).

  $env:DOCFLOW_MAC = "user@host"       # 맥 ssh 주소 (저장소에 적지 않는다)
  .\scripts\deploy.ps1
  .\scripts\deploy.ps1 -Mac user@host
  .\scripts\deploy.ps1 -NoTag          # 시험 배포 (태그 없음)

순서: 커밋 안 된 변경 확인 → 맥으로 복사 → scripts/mac/deploy.sh(빌드·재시작·준비 대기) → 태그 deploy-YYYYMMDD-HHMM push
프런트엔드는 k3s가 아니라 이 PC에서 next로 돈다(배포 대상 아님).
#>
param(
    [string]$Mac = $env:DOCFLOW_MAC,
    [switch]$NoTag
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

    Write-Host "== 복사 ($sha → $Mac)"
    ssh -o BatchMode=yes $Mac "mkdir -p ~/doc_platform/scripts ~/doc_platform/k8s"
    if ($LASTEXITCODE) { throw "맥 접속 실패" }
    scp -q -r Dockerfile requirements.txt app "${Mac}:doc_platform/"
    scp -q k8s/docs.yaml "${Mac}:doc_platform/k8s/"
    scp -q (Get-ChildItem scripts/mac/*.sh).FullName "${Mac}:doc_platform/scripts/"
    if ($LASTEXITCODE) { throw "복사 실패" }

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
