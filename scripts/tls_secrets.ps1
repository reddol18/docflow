<#
TLS 인증서·키를 맥 k3s(docs 네임스페이스) Secret으로 넣는다 (https://github.com/reddol18/docflow/issues/48).

  $env:DOCFLOW_MAC = "user@host"
  .\scripts\tls_secrets.ps1
  .\scripts\tls_secrets.ps1 -Mac user@host

읽는 것: %LOCALAPPDATA%\docflow-ca\ca.crt, issued\<web|postgres|qdrant>\tls.crt·tls.key (.\scripts\ca.ps1 init·issue로 만든다)
넣는 것(맥 scripts/mac/tls_secrets.sh): Secret tls-web·tls-postgres·tls-qdrant, ConfigMap docflow-ca, Secret qdrant-api-key(없을 때만 무작위)
키는 ssh 표준 입력으로만 넘긴다 — 명령줄·맥 디스크(임시 폴더는 끝나면 지움)·화면에 남기지 않는다. 출력은 지문·만료뿐.
처음 배포 전에 한 번, 인증서를 갱신할 때마다(397일) 부른다. 갱신이면 그 뒤 postgres·web 파드를 다시 띄운다(재배포).
#>
param([string]$Mac = $env:DOCFLOW_MAC)
$ErrorActionPreference = "Stop"
if (-not $Mac) { throw "맥 주소가 없습니다: -Mac user@host 또는 `$env:DOCFLOW_MAC" }
$root = Split-Path $PSScriptRoot -Parent
$dir = Join-Path $env:LOCALAPPDATA "docflow-ca"
$files = [ordered]@{ "ca.crt" = Join-Path $dir "ca.crt" }
foreach ($s in "web", "postgres", "qdrant") {
    $files["$s.crt"] = Join-Path $dir "issued\$s\tls.crt"
    $files["$s.key"] = Join-Path $dir "issued\$s\tls.key"
}
foreach ($f in $files.Values) { if (-not (Test-Path $f)) { throw "없음: $f — 먼저 .\scripts\ca.ps1 init / issue" } }

ssh -o BatchMode=yes $Mac "mkdir -p ~/doc_platform/scripts"
if ($LASTEXITCODE) { throw "맥 접속 실패" }
scp -q (Join-Path $root "scripts/mac/tls_secrets.sh") "${Mac}:doc_platform/scripts/"
if ($LASTEXITCODE) { throw "복사 실패 (tls_secrets.sh)" }

$lines = foreach ($k in $files.Keys) { "$k " + [Convert]::ToBase64String([IO.File]::ReadAllBytes($files[$k])) }
$prevEnc = $OutputEncoding
try {
    # base64 글자뿐. 5.1은 이것과 상관없이 [Console]::InputEncoding의 BOM을 표준 입력 앞에 붙일 수 있어 맥 쪽(tls_secrets.sh)이 뗀다
    $OutputEncoding = [Text.Encoding]::ASCII
    ($lines -join "`n") | ssh -o BatchMode=yes $Mac "sh ~/doc_platform/scripts/tls_secrets.sh"
    if ($LASTEXITCODE) { throw "Secret 넣기 실패 (위 메시지)" }
} finally {
    $OutputEncoding = $prevEnc
    $lines = $null
}
