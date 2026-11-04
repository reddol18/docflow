<#
로컬 검사 (CI 0층을 이 PC에서). LLM·외부 API 호출 없음(토큰 0).
https://github.com/reddol18/docflow/issues/24 , https://github.com/reddol18/docflow/issues/25

  .\scripts\check.ps1            # 전부
  .\scripts\check.ps1 -Quick     # semgrep·trivy·osv 빼고 (커밋 전 빠르게)
  .\scripts\check.ps1 -Skip frontend,pg

검사                                   막음(실패 시 종료 코드 1)
  frontend   next typegen → tsc → eslint      예
  ruff       보안 규칙(S) 포함                 예
  pytest     실제 Postgres(PGlite) 시험 포함  예
  gitleaks   커밋 이력의 비밀값                예
  osv        의존성 취약점 (osv-scanner.toml)  예
  semgrep    정적 분석 (OWASP·Python·TS)       예
  trivy      k8s·Dockerfile 설정               아니오 (issues/29 고치기 전까지 보고만)

필요한 것: node(npx), uv(uvx). gitleaks·osv-scanner·trivy는 처음에 받아 %LOCALAPPDATA%\docflow-tools에 두고 체크섬을 확인한다.
끝에 PR 본문 '테스트' 칸에 붙일 요약을 출력한다.
#>
param(
    [switch]$Quick,
    [string[]]$Skip = @()
)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$tools = Join-Path $env:LOCALAPPDATA "docflow-tools"
$V = @{ gitleaks = "8.30.1"; osv = "2.7.0"; trivy = "0.75.0"; semgrep = "1.180.0"; ruff = "0.14.3"
        pglite = "0.5.14"; pgliteSocket = "0.2.17" }
$Skip = @($Skip | ForEach-Object { $_ -split "," } | ForEach-Object { $_.Trim() } | Where-Object { $_ })  # -File로 부르면 "a,b"가 한 문자열로 온다
if ($Quick) { $Skip += "osv", "semgrep", "trivy" }
$env:PYTHONUTF8 = "1"
$results = [System.Collections.Generic.List[object]]::new()

function Step([string]$name, [bool]$blocking, [scriptblock]$body) {
    if ($Skip -contains $name) { $results.Add([pscustomobject]@{ name = $name; ok = $null; blocking = $blocking; sec = 0 }); return }
    Write-Host "`n== $name" -ForegroundColor Cyan
    $t = [Diagnostics.Stopwatch]::StartNew()
    $ok = $true
    try { & $body; if ($LASTEXITCODE) { $ok = $false } } catch { Write-Host $_ -ForegroundColor Red; $ok = $false }
    $results.Add([pscustomobject]@{ name = $name; ok = $ok; blocking = $blocking; sec = [int]$t.Elapsed.TotalSeconds })
}

function Get-Tool([string]$name, [string]$ver, [string]$asset, [string]$sums, [string]$repo, [string]$exe) {
    $dir = Join-Path $tools "$name-$ver"
    $path = Join-Path $dir $exe
    if (Test-Path $path) { return $path }
    New-Item -ItemType Directory -Force $dir | Out-Null
    $base = "https://github.com/$repo/releases/download/v$ver"
    $a = Join-Path $dir $asset
    Invoke-WebRequest "$base/$asset" -OutFile $a -UseBasicParsing
    $s = Join-Path $dir $sums
    Invoke-WebRequest "$base/$sums" -OutFile $s -UseBasicParsing  # 내용으로 받으면 octet-stream이라 바이트 배열이 된다
    $want = Get-Content $s | Where-Object { $_ -match "\s\*?$([regex]::Escape($asset))\s*$" } |
        ForEach-Object { ($_ -split "\s+")[0] } | Select-Object -First 1
    $got = (Get-FileHash $a -Algorithm SHA256).Hash
    if (-not $want -or $got -ne $want.ToUpper()) { Remove-Item -Recurse -Force $dir; throw "$asset 체크섬 불일치" }
    if ($asset -like "*.zip") { Expand-Archive -Force $a $dir } elseif ($asset -ne $exe) { Move-Item $a $path }
    return $path
}

Push-Location $root
try {
    $sha = (git rev-parse --short HEAD).Trim()
    $dirty = [bool](git status --porcelain)

    Step "frontend" $true {
        Push-Location frontend
        try {
            if (-not (Test-Path node_modules)) { npm ci; if ($LASTEXITCODE) { return } }
            npx next typegen | Out-Null; if ($LASTEXITCODE) { return }
            npx tsc --noEmit; if ($LASTEXITCODE) { return }
            npx eslint src
        } finally { Pop-Location }
    }

    Step "ruff" $true { uvx --quiet "ruff@$($V.ruff)" check . --output-format concise }

    Step "pytest" $true {
        $pg = $null
        if ($Skip -notcontains "pg") {
            # 실제 Postgres 엔진(PGlite, WASM)을 잠깐 띄워 리포트 SQL 시험을 돌린다
            $port = 55432
            $pg = Start-Process -PassThru -WindowStyle Hidden npx.cmd -ArgumentList @(
                "-y", "-p", "@electric-sql/pglite@$($V.pglite)", "-p", "@electric-sql/pglite-socket@$($V.pgliteSocket)",
                "pglite-server", "--port=$port", "--max-connections=4")
            $ready = $false
            for ($i = 0; $i -lt 60 -and -not $ready; $i++) {
                try { $c = [Net.Sockets.TcpClient]::new("127.0.0.1", $port); $c.Close(); $ready = $true } catch { Start-Sleep -Milliseconds 500 }
            }
            if (-not $ready) { throw "PGlite가 뜨지 않음" }
            $env:TEST_PG_URL = "postgresql://postgres@127.0.0.1:$port/postgres?sslmode=disable"
            $env:TEST_PG_NO_PREPARE = "1"  # PGlite 소켓 서버는 prepared statement 재사용에 약하다
        }
        $code = 1
        try {
            # 버전은 requirements-test.txt (앱을 불러와 경로 보호를 시험할 만큼, OCR 제외)
            uv run --quiet --no-project --with-requirements requirements-test.txt python -m pytest -q
            $code = $LASTEXITCODE
            if ($code -eq 5) { Write-Host "(모은 시험 없음 — 통과로 본다)"; $code = 0 }
        } finally {
            Remove-Item Env:TEST_PG_URL, Env:TEST_PG_NO_PREPARE -ErrorAction SilentlyContinue
            if ($pg) { taskkill /T /F /PID $pg.Id 2>$null | Out-Null }
            $global:LASTEXITCODE = $code  # taskkill의 종료 코드가 시험 결과를 덮지 않게
        }
    }

    Step "gitleaks" $true {
        $gl = Get-Tool gitleaks $V.gitleaks "gitleaks_$($V.gitleaks)_windows_x64.zip" "gitleaks_$($V.gitleaks)_checksums.txt" `
            "gitleaks/gitleaks" "gitleaks.exe"
        & $gl git . --no-banner --redact
    }

    Step "osv" $true {
        $osv = Get-Tool osv $V.osv "osv-scanner_windows_amd64.exe" "osv-scanner_SHA256SUMS" "google/osv-scanner" "osv-scanner_windows_amd64.exe"
        # --no-resolve: requirements.lock은 pip freeze 전체 목록이라 다시 풀지 않는다 (풀면 setuptools 거짓 경보)
        # requirements.txt도 본다: 새로 고정한 패키지는 배포해서 잠금 파일을 받아 오기 전까지 lock에 없다 (PyJWT 2.10.1이 그렇게 지나갔다)
        & $osv scan source --no-resolve --config osv-scanner.toml --lockfile requirements.txt:requirements.lock `
            --lockfile requirements.txt:requirements.txt --lockfile frontend/package-lock.json
    }

    Step "semgrep" $true {
        $env:SEMGREP_SEND_METRICS = "off"
        # k8s 매니페스트는 trivy가 맡는다
        uvx --quiet "semgrep@$($V.semgrep)" scan --metrics=off --error --quiet --config p/python --config p/typescript `
            --config p/owasp-top-ten --exclude k8s --exclude sim --severity ERROR --severity WARNING
    }

    Step "trivy" $false {
        $tv = Get-Tool trivy $V.trivy "trivy_$($V.trivy)_windows-64bit.zip" "trivy_$($V.trivy)_checksums.txt" "aquasecurity/trivy" "trivy.exe"
        # 빌드 산출물은 건너뛴다 (next dev가 .next 안 파일을 잠가 두면 스캔이 멈춘다)
        & $tv config --quiet --severity HIGH,CRITICAL --exit-code 0 --skip-dirs frontend/node_modules --skip-dirs frontend/.next .
    }
} finally {
    Pop-Location
}

$failed = @($results | Where-Object { $_.blocking -and $_.ok -eq $false })
$mark = { param($r) if ($null -eq $r.ok) { "건너뜀" } elseif (-not $r.blocking) { "보고만 (출력 확인)" } elseif ($r.ok) { "통과" } else { "**실패**" } }
Write-Host "`n---- PR '테스트' 칸에 붙일 요약 ----"
Write-Host ("로컬 검사 ``scripts/check.ps1`` — 커밋 ``{0}``{1}, {2:yyyy-MM-dd HH:mm}" -f $sha, $(if ($dirty) { " + 커밋 안 된 변경" } else { "" }), (Get-Date))
Write-Host "| 검사 | 결과 | 초 |"
Write-Host "|---|---|---|"
foreach ($r in $results) { Write-Host ("| {0} | {1} | {2} |" -f $r.name, (& $mark $r), $r.sec) }
if ($failed.Count) { Write-Host "`n실패: $($failed.name -join ', ')" -ForegroundColor Red; exit 1 }
Write-Host "`n막는 검사 모두 통과" -ForegroundColor Green
exit 0
