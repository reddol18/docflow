<#
내부 데모 CA (https://github.com/reddol18/docflow/issues/48). 이 PC에서만 돈다. 키·인증서 내용은 출력하지 않는다 (경로·지문·만료만).

  .\scripts\ca.ps1 init            CA 만들기 (한 번). 이미 있으면 멈춤
  .\scripts\ca.ps1 issue           서버 인증서 발급·갱신: web(docflow.localhost)·postgres·qdrant(클러스터 안 전체 이름) — 397일
  .\scripts\ca.ps1 show            지문·만료 보기
  .\scripts\ca.ps1 trust-command   브라우저가 CA를 믿게 하는 명령과 지우는 명령을 출력만 한다 (실행하지 않음)

다음: .\scripts\tls_secrets.ps1 (k8s Secret으로 넣기) → .\scripts\deploy.ps1

저장 위치: %LOCALAPPDATA%\docflow-ca (저장소 밖). 폴더 ACL은 지금 사용자만.
  ca.key   CA 키 — 암호화(PKCS8). 암호는 ca.pass에 DPAPI(이 Windows 사용자에게 묶임)로 보관 → 다른 계정·PC에서는 열 수 없다
  ca.crt   CA 인증서 (공개 값 — k8s ConfigMap docflow-ca, Windows 신뢰 루트)
  issued\<web|postgres|qdrant>\tls.crt·tls.key   서버 인증서·키 (키는 k8s Secret으로만 보낸다)
CA 인증서에는 이름 제약이 있다 (scripts/ca.py PERMITTED): docflow.localhost, postgres.docs.svc.cluster.local, qdrant.docs.svc.cluster.local만.
IP는 막고 메일·URI는 쓸 수 없는 이름(.invalid)만. CA의 확장 키 용도는 serverAuth만.
CA를 새로 만들려면: 신뢰 루트에서 지우고(trust-command의 제거 명령) 폴더를 지운 뒤 init → issue → tls_secrets.
#>
param(
    [Parameter(Position = 0)][ValidateSet("init", "issue", "show", "trust-command")][string]$Command = "show"
)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$dir = Join-Path $env:LOCALAPPDATA "docflow-ca"
$passFile = Join-Path $dir "ca.pass"
$caCrt = Join-Path $dir "ca.crt"

function Invoke-Ca([string]$cmd, [string]$pass) {
    Push-Location $root
    $lock = Join-Path $root "uv.lock"
    $hadLock = Test-Path $lock  # 실행 전에 없던 uv.lock만 지운다 (uv run이 만들었을 때)
    $prevEnc = [Console]::OutputEncoding
    [Console]::OutputEncoding = [Text.Encoding]::UTF8  # ca.py 출력(한글)
    try {
        # 암호는 표준 입력으로만 (명령줄·환경 변수에 남기지 않는다)
        if ($pass) { $pass | uv run --quiet --no-project --with-requirements requirements-test.txt python -I scripts/ca.py $cmd $dir }
        else { uv run --quiet --no-project --with-requirements requirements-test.txt python -I scripts/ca.py $cmd $dir }
        if ($LASTEXITCODE) { throw "ca.py $cmd 실패 ($LASTEXITCODE)" }
    } finally {
        [Console]::OutputEncoding = $prevEnc
        Pop-Location
        if (-not $hadLock -and (Test-Path $lock)) { Remove-Item $lock }
    }
}

function Read-Pass {
    if (-not (Test-Path $passFile)) { throw "CA 암호 파일이 없음: $passFile — 먼저 .\scripts\ca.ps1 init" }
    $sec = (Get-Content $passFile -Raw).Trim() | ConvertTo-SecureString  # DPAPI(지금 사용자)
    $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
}

switch ($Command) {
    "init" {
        if ((Test-Path $caCrt) -or (Test-Path (Join-Path $dir "ca.key"))) { throw "이미 CA가 있음: $dir (덮어쓰지 않는다)" }
        New-Item -ItemType Directory -Force $dir | Out-Null
        # 폴더 ACL: 상속을 끊고 지금 사용자만 (SYSTEM·Administrators 상속 제거)
        icacls $dir /inheritance:r /grant:r "$($env:USERDOMAIN)\$($env:USERNAME):(OI)(CI)F" /remove:g "*S-1-5-18" "*S-1-5-32-544" | Out-Null  # SYSTEM·Administrators도 뺀다
        if ($LASTEXITCODE) { throw "폴더 권한 설정 실패: $dir" }
        $bytes = [byte[]]::new(32); [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        $pass = [Convert]::ToBase64String($bytes)
        ConvertTo-SecureString $pass -AsPlainText -Force | ConvertFrom-SecureString | Set-Content -Encoding ASCII $passFile
        Invoke-Ca "init" $pass
        $pass = $null
        Write-Host "다음: .\scripts\ca.ps1 issue → .\scripts\ca.ps1 trust-command(명령 확인 뒤 직접 실행)"
    }
    "issue" {
        Invoke-Ca "issue" (Read-Pass)
        Write-Host "다음: .\scripts\tls_secrets.ps1 (k8s Secret으로) → 배포. 이미 떠 있는 파드는 다시 띄워야 새 인증서를 읽는다"
    }
    "show" { Invoke-Ca "show" $null }
    "trust-command" {
        if (-not (Test-Path $caCrt)) { throw "CA가 없음: $caCrt — 먼저 init" }
        $c = [Security.Cryptography.X509Certificates.X509Certificate2]::new($caCrt)
        Write-Host "CA: $($c.Subject), 만료 $($c.NotAfter.ToString('yyyy-MM-dd')), 지문(SHA-1, Windows 저장소 표시) $($c.Thumbprint)"
        Write-Host ""
        Write-Host "# 신뢰 추가 — '현재 사용자' 루트 저장소에만 (관리자 권한 불필요). Windows 보안 경고 창에서 지문을 위와 맞춰 보고 '예'"
        Write-Host "Import-Certificate -FilePath `"$caCrt`" -CertStoreLocation Cert:\CurrentUser\Root"
        Write-Host ""
        Write-Host "# 확인"
        Write-Host "Get-ChildItem Cert:\CurrentUser\Root | Where-Object Thumbprint -eq '$($c.Thumbprint)'"
        Write-Host ""
        Write-Host "# 제거 (데모를 끝내거나 CA를 새로 만들 때)"
        Write-Host "Get-ChildItem Cert:\CurrentUser\Root | Where-Object Thumbprint -eq '$($c.Thumbprint)' | Remove-Item"
        Write-Host ""
        Write-Host "# Chrome·Edge는 Windows 저장소를 쓴다. Firefox는 about:config의 security.enterprise_roots.enabled = true일 때만"
    }
}
