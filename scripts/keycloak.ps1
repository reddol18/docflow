<#
데모 Keycloak을 맥 k3s(keycloak 네임스페이스)에 올린다 (https://github.com/reddol18/docflow/issues/39).

  $env:DOCFLOW_MAC = "user@host"
  .\scripts\keycloak.ps1            # 복사 → 맥 scripts/mac/keycloak.sh (디스크 확인 → Secret → realm ConfigMap → apply → 준비 대기)
  .\scripts\keycloak.ps1 -Verify    # docflow 파드에서 discovery 확인 — issuer·주소 출처가 다르면 실패(throw)

순서 (처음): ① 이 스크립트 (docs/oidc Secret도 맞춘다 — 바뀌었고 docflow가 이미 마운트하고 있으면 docflow 재시작) ② .\scripts\deploy.ps1
(docflow가 아직 옛 매니페스트면. Secret oidc가 없어도 배포는 되고 OIDC만 꺼진다) ③ .\scripts\keycloak.ps1 -Verify ④ 브라우저 확인 (README '데모 Keycloak').
비밀값(관리자·client secret·시험 사용자 비밀번호)은 맥에서 무작위로 만들고 화면·저장소에 남기지 않는다.
#>
param(
    [string]$Mac = $env:DOCFLOW_MAC,
    [switch]$Verify
)
$ErrorActionPreference = "Stop"
if (-not $Mac) { throw "맥 주소가 없습니다: -Mac user@host 또는 `$env:DOCFLOW_MAC" }
$root = Split-Path $PSScriptRoot -Parent
# ssh·scp 같은 옵션 (scripts/lock_refresh.ps1과 같은 꼴) — 맥이 답하지 않으면 오래 기다리지 않는다
$SshOpts = @("-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3")

if ($Verify) {
    ssh @SshOpts $Mac "sh ~/doc_platform/scripts/keycloak.sh verify"
    if ($LASTEXITCODE) { throw "discovery 확인 실패 — issuer나 주소 출처가 다르거나 Keycloak에 닿지 못함 (위 메시지)" }
    return
}

ssh @SshOpts $Mac "mkdir -p ~/doc_platform/scripts ~/doc_platform/k8s/keycloak"
if ($LASTEXITCODE) { throw "맥 접속 실패" }
scp -q @SshOpts (Join-Path $root "scripts/mac/keycloak.sh") (Join-Path $root "scripts/mac/kube_put.sh") (Join-Path $root "scripts/mac/disk_check.sh") "${Mac}:doc_platform/scripts/"
if ($LASTEXITCODE) { throw "복사 실패 (scripts)" }
scp -q @SshOpts (Join-Path $root "k8s/keycloak.yaml") "${Mac}:doc_platform/k8s/"
if ($LASTEXITCODE) { throw "복사 실패 (keycloak.yaml)" }
scp -q @SshOpts (Join-Path $root "k8s/keycloak/docflow-realm.json") "${Mac}:doc_platform/k8s/keycloak/"
if ($LASTEXITCODE) { throw "복사 실패 (realm JSON)" }
ssh @SshOpts $Mac "sh ~/doc_platform/scripts/keycloak.sh"
if ($LASTEXITCODE) { throw "Keycloak 올리기 실패 (위 메시지)" }
