<#
데모 Keycloak을 맥 k3s(keycloak 네임스페이스)에 올린다 (https://github.com/reddol18/docflow/issues/39).

  $env:DOCFLOW_MAC = "user@host"
  .\scripts\keycloak.ps1            # 복사 → 맥 scripts/mac/keycloak.sh (디스크 확인 → Secret → realm ConfigMap → apply → 준비 대기)
  .\scripts\keycloak.ps1 -Verify    # docflow를 새 k8s/docs.yaml로 다시 띄운 뒤: docflow 파드에서 discovery 문서 받기

순서 (처음): ① 이 스크립트 (docs/oidc Secret도 만든다) ② .\scripts\deploy.ps1 (docflow를 AUTH_MODES=local,oidc로 — Secret oidc가
없으면 맥 deploy.sh가 apply 전에 멈춘다) ③ .\scripts\keycloak.ps1 -Verify ④ 브라우저 확인 (README '데모 Keycloak').
비밀값(관리자·client secret·시험 사용자 비밀번호)은 맥에서 무작위로 만들고 화면·저장소에 남기지 않는다.
#>
param(
    [string]$Mac = $env:DOCFLOW_MAC,
    [switch]$Verify
)
$ErrorActionPreference = "Stop"
if (-not $Mac) { throw "맥 주소가 없습니다: -Mac user@host 또는 `$env:DOCFLOW_MAC" }
$root = Split-Path $PSScriptRoot -Parent
$ssh = @("-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3")

if ($Verify) {
    ssh @ssh $Mac "sh ~/doc_platform/scripts/keycloak.sh verify"
    if ($LASTEXITCODE) { throw "discovery 확인 실패 (위 메시지)" }
    return
}

ssh @ssh $Mac "mkdir -p ~/doc_platform/scripts ~/doc_platform/k8s/keycloak"
if ($LASTEXITCODE) { throw "맥 접속 실패" }
scp -q (Join-Path $root "scripts/mac/keycloak.sh") (Join-Path $root "scripts/mac/disk_check.sh") "${Mac}:doc_platform/scripts/"
if ($LASTEXITCODE) { throw "복사 실패 (scripts)" }
scp -q (Join-Path $root "k8s/keycloak.yaml") "${Mac}:doc_platform/k8s/"
if ($LASTEXITCODE) { throw "복사 실패 (keycloak.yaml)" }
scp -q (Join-Path $root "k8s/keycloak/docflow-realm.json") "${Mac}:doc_platform/k8s/keycloak/"
if ($LASTEXITCODE) { throw "복사 실패 (realm JSON)" }
ssh @ssh $Mac "sh ~/doc_platform/scripts/keycloak.sh"
if ($LASTEXITCODE) { throw "Keycloak 올리기 실패 (위 메시지)" }
