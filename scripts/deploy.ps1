<#
백엔드(docflow API)·프런트(web, 운영 빌드)를 맥 k3s에 배포하고 배포 태그를 남긴다 (https://github.com/reddol18/docflow/issues/24).

  $env:DOCFLOW_MAC = "user@host"       # 맥 ssh 주소 (저장소에 적지 않는다)
  .\scripts\deploy.ps1
  .\scripts\deploy.ps1 -Mac user@host
  .\scripts\deploy.ps1 -NoTag          # 시험 배포 (태그 없음)
  .\scripts\deploy.ps1 -NoCheck        # 로컬 검사 건너뜀 (방금 돌렸을 때만)

이미지는 requirements.lock(해시 고정)으로 설치한다. requirements.txt를 바꿨으면 배포 전에 .\scripts\lock_refresh.ps1로 lock을 다시 만들어 커밋한다.

순서: 커밋 안 된 변경 확인 → 로컬 검사(scripts/check.ps1) → 맥 디스크 확인(scripts/mac/disk_check.sh — 여유 GB와 이미지 디스크 사용률·kubelet GC 문턱, 걸리면 중단) → 맥으로 복사 → scripts/mac/deploy.sh(빌드·재시작·준비 대기)
      → 태그 deploy-YYYYMMDD-HHMM push
(맥에는 git 저장소가 없어서 태그는 이 스크립트가 남긴다)
프런트엔드 (https://github.com/reddol18/docflow/issues/48): k3s web 파드 = 운영 빌드. 맥 build_image.sh가 frontend/Dockerfile로
next build 하고 파드는 next start + TLS 종단(https://docflow.localhost:8443, ssh 터널). frontend는 HEAD 커밋 그대로(git archive) 복사한다.
이 PC의 localhost:3000 개발 서버(npm run dev, DOCFLOW_URL)는 화면 확인용으로 그대로 — 배포 대상 아님.
처음 한 번(그리고 인증서 갱신 때): .\scripts\ca.ps1 init·issue → .\scripts\tls_secrets.ps1 (없으면 맥 deploy.sh가 apply 전에 멈춘다)
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
        # 배포 검사는 늘 기본 도구 폴더로 — 시험용 경로 변수는 검사 동안만 지우고 끝나면 되돌린다 (https://github.com/reddol18/docflow/issues/46)
        $savedToolsDir = $env:DOCFLOW_TOOLS_DIR
        Remove-Item Env:DOCFLOW_TOOLS_DIR -ErrorAction SilentlyContinue
        try {
            & (Join-Path $PSScriptRoot "check.ps1") -NoStatus  # 배포 검사는 main에서 돈다 — PR 병합 게이트 status는 올리지 않는다 (https://github.com/reddol18/docflow/issues/85)
            $checkCode = $LASTEXITCODE
        } finally { if ($null -ne $savedToolsDir) { $env:DOCFLOW_TOOLS_DIR = $savedToolsDir } }
        if ($checkCode) { throw "로컬 검사 실패. 배포하지 않습니다." }
    }

    Write-Host "== 디스크 여유 확인"
    # 반드시: 모자란 채로 빌드하면 kubelet이 운영 파드를 내쫓는다 (2026-11-07). 맥 쪽 deploy.sh도 빌드 전에 한 번 더 본다
    ssh -o BatchMode=yes $Mac "mkdir -p ~/doc_platform/scripts ~/doc_platform/k8s"
    if ($LASTEXITCODE) { throw "맥 접속 실패" }
    scp -q scripts/mac/disk_check.sh "${Mac}:doc_platform/scripts/"
    ssh -o BatchMode=yes $Mac "sh ~/doc_platform/scripts/disk_check.sh"
    if ($LASTEXITCODE) { throw "디스크 여유가 모자랍니다. 정리한 뒤 배포하세요 (위 메시지)." }

    Write-Host "== 복사 ($sha → $Mac)"
    scp -q -r Dockerfile requirements.txt requirements.lock app "${Mac}:doc_platform/"
    if ($LASTEXITCODE) { throw "복사 실패 (app)" }
    scp -q k8s/docs.yaml "${Mac}:doc_platform/k8s/"
    if ($LASTEXITCODE) { throw "복사 실패 (k8s)" }
    # 프런트 소스: 커밋된 HEAD 그대로 (node_modules·.next·.env 없이). 파이프로 넘기면 PowerShell 5.1이 바이너리를 깨므로 파일로
    $webTar = Join-Path ([IO.Path]::GetTempPath()) "docflow-frontend.tar"
    try {
        git archive --format=tar -o $webTar HEAD frontend
        if ($LASTEXITCODE) { throw "git archive 실패 (frontend)" }
        scp -q $webTar "${Mac}:doc_platform/frontend.tar"
        if ($LASTEXITCODE) { throw "복사 실패 (frontend)" }
    } finally { Remove-Item $webTar -ErrorAction SilentlyContinue }
    ssh -o BatchMode=yes $Mac "cd ~/doc_platform && rm -rf frontend && tar -xf frontend.tar && rm -f frontend.tar && chmod -R a+rX frontend"
    if ($LASTEXITCODE) { throw "풀기 실패 (frontend)" }
    # tool_pins.txt: scan_image.sh가 읽는 trivy 버전·고정 해시 (https://github.com/reddol18/docflow/issues/46)
    scp -q (Get-ChildItem scripts/mac/*.sh).FullName scripts/tool_pins.txt "${Mac}:doc_platform/scripts/"
    if ($LASTEXITCODE) { throw "복사 실패 (scripts)" }
    # Windows scp가 새로 만든 폴더는 700 — 이미지 안 app 사용자가 못 읽는다 (Dockerfile chmod와 이중으로)
    ssh -o BatchMode=yes $Mac "chmod -R a+rX ~/doc_platform/app"
    if ($LASTEXITCODE) { throw "권한 고치기 실패" }

    Write-Host "== 빌드·재시작"
    ssh -o BatchMode=yes $Mac "sh ~/doc_platform/scripts/deploy.sh"
    if ($LASTEXITCODE) { throw "배포 실패 (위 로그). 태그를 남기지 않습니다." }

    # 이미지 안 설치 결과(pip freeze --all)를 저장소의 requirements.lock 판과 맞대 본다 (https://github.com/reddol18/docflow/issues/33).
    # 이미지는 lock으로 설치하고 빌드 안에서 이미 diff하므로(빌드 출력의 'lock 일치' 줄) 차이는 없어야 한다 — 있으면 실제 어긋남.
    # 이미 롤아웃이 끝난 뒤라 여기서 실패해도 경고만 하고 태그는 남긴다 (배포된 코드와 태그가 어긋나지 않게)
    $freeze = Join-Path ([IO.Path]::GetTempPath()) "docflow-requirements.freeze"
    Remove-Item $freeze -ErrorAction SilentlyContinue
    scp -q "${Mac}:doc_platform/requirements.freeze" $freeze
    if ($LASTEXITCODE -or -not (Test-Path $freeze) -or -not (Get-Item $freeze).Length) {
        Write-Warning "이미지 패키지 목록(requirements.freeze)을 받지 못해 lock 비교를 건너뜁니다. 빌드 출력의 'lock 일치' 줄을 확인하세요."
    } else {
        $pins = { param($p) Get-Content $p | Where-Object { $_ -and $_ -notmatch '^\s*#' } | ForEach-Object { ($_.Trim() -split '\s+')[0] } }
        $want = @(& $pins requirements.lock)
        $got = @(& $pins $freeze)
        $onlyLock = @($want | Where-Object { $got -notcontains $_ })
        $onlyImage = @($got | Where-Object { $want -notcontains $_ })
        if ($onlyLock.Count -or $onlyImage.Count) {
            $onlyLock | ForEach-Object { Write-Host "  lock   $_" }
            $onlyImage | ForEach-Object { Write-Host "  이미지 $_" }
            Write-Warning "이미지 안 패키지가 requirements.lock과 다릅니다 (위 목록). lock을 고치지 말고 원인을 확인하세요."
        } else { Write-Host "== 이미지 패키지 = requirements.lock ($($want.Count)개)" }
    }
    Remove-Item $freeze -ErrorAction SilentlyContinue

    if (-not $NoTag) {
        $tag = "deploy-" + (Get-Date -Format "yyyyMMdd-HHmm")
        git tag -a $tag -m "deploy $sha to mac k3s"
        git push origin $tag
        Write-Host "== 태그 $tag ($sha)"
    }
} finally {
    Pop-Location
}
