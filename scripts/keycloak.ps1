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
# 맥 명령은 Invoke-Mac·Copy-ToMac로만 — ssh 옵션·시간 제한은 그 한 곳 (https://github.com/reddol18/docflow/issues/117)
. (Join-Path $PSScriptRoot "lib/remote.ps1")

if ($Verify) {
    Invoke-Mac -Mac $Mac -Script "sh ~/doc_platform/scripts/keycloak.sh verify" -Stream -TimeoutSec 120 -FailMessage "discovery 확인 실패 — issuer나 주소 출처가 다르거나 Keycloak에 닿지 못함 (위 메시지)"
    return
}

Invoke-Mac -Mac $Mac -Script "mkdir -p ~/doc_platform/scripts ~/doc_platform/k8s/keycloak" -Stream -FailMessage "맥 접속 실패"
Copy-ToMac -Mac $Mac -Path (Join-Path $root "scripts/mac/keycloak.sh"), (Join-Path $root "scripts/mac/kube_put.sh"), (Join-Path $root "scripts/mac/disk_check.sh") -Destination "doc_platform/scripts/" -FailMessage "복사 실패 (scripts)"
Copy-ToMac -Mac $Mac -Path (Join-Path $root "k8s/keycloak.yaml") -Destination "doc_platform/k8s/" -FailMessage "복사 실패 (keycloak.yaml)"
Copy-ToMac -Mac $Mac -Path (Join-Path $root "k8s/keycloak/docflow-realm.json") -Destination "doc_platform/k8s/keycloak/" -FailMessage "복사 실패 (realm JSON)"
# 디스크 확인·이미지 받기·롤아웃 대기(600초)·docflow 재시작 대기(300초)를 덮는 시간 제한
Invoke-Mac -Mac $Mac -Script "sh ~/doc_platform/scripts/keycloak.sh" -Stream -TimeoutSec 1800 -FailMessage "Keycloak 올리기 실패 (위 메시지)"
