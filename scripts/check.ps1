<#
로컬 검사 (CI 0층을 이 PC에서). LLM·외부 API 호출 없음(토큰 0).
https://github.com/reddol18/docflow/issues/24 , https://github.com/reddol18/docflow/issues/25

  .\scripts\check.ps1            # 전부
  .\scripts\check.ps1 -Quick     # semgrep·trivy·osv 빼고 (커밋 전 빠르게)
  .\scripts\check.ps1 -Skip frontend,pg

검사                                   막음(실패 시 종료 코드 1)
  frontend   next typegen → tsc → eslint      예
  ruff       보안 규칙(S) 포함                 예
  lock       requirements.txt 판이 lock과 같은지·해시 있는지  예
  pytest     실제 Postgres(PGlite) 시험 포함  예
  gitleaks   커밋 이력의 비밀값                예
  osv        의존성 취약점 (osv-scanner.toml)  예
  semgrep    정적 분석 (OWASP·Python·TS)       예
  trivy      k8s·Dockerfile 설정 (HIGH 이상)   예 (issues/29부터)

필요한 것: node(npx), uv(uvx). gitleaks·osv-scanner·trivy는 처음에 받아 %LOCALAPPDATA%\docflow-tools에 두고
scripts/tool_pins.txt에 고정한 SHA256으로 확인한다 — 받을 때 자산, 부를 때마다 실행 파일. 다르면 실행하지 않고 그 검사 실패
(https://github.com/reddol18/docflow/issues/46).
끝에 PR 본문 '테스트' 칸에 붙일 요약을 출력한다.

commit status (https://github.com/reddol18/docflow/issues/85): 끝에서 `scripts/gate_status.py`가 조건을 보고 HEAD에
docflow/local-check(success)를 올린다 — 막는 검사 모두 통과, 건너뜀 없음(코드 없는 PR만 osv·semgrep·trivy 허용),
시작·끝 모두 커밋 안 된 변경 없음, HEAD가 그대로이고 push돼 있을 때만. 실패·건너뜀·변경 있음이면 올리지 않는다.
main ruleset이 이 status를 병합 필수 조건으로 건다. 올리지 않으려면 -NoStatus.
종료 코드는 검사 결과만 따른다 (status를 올렸든 못 올렸든 같다).
#>
param(
    [switch]$Quick,
    [string[]]$Skip = @(),
    [switch]$NoStatus
)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$tools = Join-Path $env:LOCALAPPDATA "docflow-tools"
$V = @{ semgrep = "1.180.0"; ruff = "0.14.3"  # gitleaks·osv·trivy는 scripts/tool_pins.txt (버전·해시)
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

# 버전·해시는 scripts/tool_pins.txt 한 곳에 고정한다 (https://github.com/reddol18/docflow/issues/46).
# 릴리스의 체크섬 파일은 받지 않는다(자산과 같은 출처). 받을 때는 자산 해시, 받아 둔 실행 파일은 부를 때마다 실행 파일 해시를 본다.
# 무결성 실패(해시 불일치·고정 파일 형식)는 '!! 검사 도구 무결성 실패'로 시작해 일반 실패(받기 실패 등)와 구별한다.
function Get-Pin([string]$name, [string]$platform = "windows-x64") {
    $pins = Join-Path $PSScriptRoot "tool_pins.txt"
    $bad = "!! 검사 도구 무결성 실패: tool_pins.txt $name $platform"
    $rows = @(Get-Content -Encoding UTF8 $pins | Where-Object { $_ -and $_ -notmatch '^\s*#' } | ForEach-Object {
            $f = $_.Trim() -split '\s+'
            [pscustomobject]@{ name = $f[0]; platform = $f[2]; n = $f.Count; ver = $f[1]; repo = $f[3]; asset = $f[4]
                               assetSha = $f[5]; exe = $f[6]; exeSha = $f[7] }
        } | Where-Object { $_.name -eq $name -and $_.platform -eq $platform })
    if ($rows.Count -ne 1 -or $rows[0].n -ne 8) { throw "$bad 줄이 하나(8칸)여야 합니다 ($($rows.Count)개)" }
    $p = $rows[0]
    # 칸 형식: 경로·URL에 들어가는 칸은 영숫자로 시작하는 [A-Za-z0-9._-]만 (옵션·경로 이탈 막기), 해시는 소문자 64자리
    $nameRe = '^[A-Za-z0-9][A-Za-z0-9._-]*$'
    foreach ($v in $p.ver, $p.asset, $p.exe) { if ($v -cnotmatch $nameRe) { throw "$bad 칸 형식이 틀림: $v" } }
    if ($p.repo -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$') { throw "$bad 저장소 형식이 틀림: $($p.repo)" }
    foreach ($h in $p.assetSha, $p.exeSha) { if ($h -cnotmatch '^[0-9a-f]{64}$') { throw "$bad 해시 형식이 틀림" } }
    $p.assetSha = $p.assetSha.ToUpper(); $p.exeSha = $p.exeSha.ToUpper()  # Get-FileHash는 대문자
    $p
}

function Get-Tool([string]$name) {
    $p = Get-Pin $name
    $toolsDir = $tools
    if ($env:DOCFLOW_TOOLS_DIR -and $env:DOCFLOW_TOOLS_DIR -ne $tools) {  # 시험용으로 바꿀 수 있다 — 바꿨으면 늘 보인다
        $toolsDir = $env:DOCFLOW_TOOLS_DIR
        Write-Host "경고: 검사 도구를 기본값이 아닌 곳에서 씀 (DOCFLOW_TOOLS_DIR=$toolsDir)" -ForegroundColor Yellow
    }
    $dir = Join-Path $toolsDir "$name-$($p.ver)"
    $path = Join-Path $dir $p.exe
    if (-not (Test-Path $path)) {
        # 임시 폴더에 받아 풀고, 실행 파일 해시까지 맞으면 제자리로 옮긴다 (중간에 끊기면 제자리에 없으니 다음에 다시 받는다)
        $new = Join-Path $toolsDir ".$name-new-$([IO.Path]::GetRandomFileName())"
        New-Item -ItemType Directory -Force $new | Out-Null
        try {
            $a = Join-Path $new $p.asset
            Invoke-WebRequest "https://github.com/$($p.repo)/releases/download/v$($p.ver)/$($p.asset)" -OutFile $a -UseBasicParsing
            $got = (Get-FileHash $a -Algorithm SHA256).Hash
            if ($got -ne $p.assetSha) { throw "!! 검사 도구 무결성 실패: $($p.asset) 해시가 tool_pins.txt와 다름 (받은 $got) — 받지 않고 멈춤" }
            $x = Join-Path $new "x"
            if ($p.asset -like "*.zip") { Expand-Archive -Force $a $x } else { New-Item -ItemType Directory $x | Out-Null; Move-Item $a (Join-Path $x $p.exe) }
            $e = Join-Path $x $p.exe
            if (-not (Test-Path $e)) { throw "$($p.asset) 안에 $($p.exe)가 없음" }
            $got = (Get-FileHash $e -Algorithm SHA256).Hash
            if ($got -ne $p.exeSha) { throw "!! 검사 도구 무결성 실패: $($p.asset) 안 $($p.exe) 해시가 tool_pins.txt와 다름 ($got) — 받지 않고 멈춤" }
            New-Item -ItemType Directory -Force $dir | Out-Null
            # 같은 드라이브 안 이름 바꾸기. 실패해도 그 사이 다른 실행이 제자리에 놓았으면 아래 해시 확인으로 간다
            try { Move-Item $e $path -ErrorAction Stop } catch { if (-not (Test-Path $path)) { throw } }
        } finally { Remove-Item -Recurse -Force $new -ErrorAction SilentlyContinue }
    }
    # 받아 둔 실행 파일도 매번 (바뀌었으면 지우지 않고 멈춘다 — 무엇이 바뀌었는지 볼 수 있게)
    $got = (Get-FileHash $path -Algorithm SHA256).Hash
    if ($got -ne $p.exeSha) { throw "!! 검사 도구 무결성 실패: $path 해시가 tool_pins.txt와 다름 ($got) — 실행하지 않음. 확인 뒤 $dir 를 지우면 다시 받는다" }
    return $path
}

Push-Location $root
try {
    $fullSha = (git rev-parse HEAD).Trim()
    $sha = $fullSha.Substring(0, 7)
    $porcelain = git status --porcelain
    $dirty = [bool]$porcelain -or [bool]$LASTEXITCODE  # git status가 실패하면 변경 있음으로 본다 (status 안 올림)

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

    # 이미지는 requirements.lock으로 설치한다 — requirements.txt만 바꾸고 lock을 안 만들면 여기서 막는다 (https://github.com/reddol18/docflow/issues/33)
    Step "lock" $true { uv run --quiet --no-project python -I scripts/lock_hashes.py --check requirements.txt requirements.lock }

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
        $gl = Get-Tool gitleaks
        & $gl git . --no-banner --redact
    }

    Step "osv" $true {
        $osv = Get-Tool osv
        # --no-resolve: requirements.lock은 이미지에 깔리는 전체 목록(해시 포함)이라 다시 풀지 않는다 (풀면 setuptools 거짓 경보)
        # requirements.txt도 함께 본다 (lock 단계가 둘의 판이 같은지 막지만, 예전에 PyJWT 2.10.1이 lock에 없어 지나간 적이 있다)
        & $osv scan source --no-resolve --config osv-scanner.toml --lockfile requirements.txt:requirements.lock `
            --lockfile requirements.txt:requirements.txt --lockfile requirements.txt:requirements-test.txt `
            --lockfile frontend/package-lock.json --lockfile eval/promptfoo/package-lock.json   # 평가 하네스(개발 PC 전용) 의존성
    }

    Step "semgrep" $true {
        $env:SEMGREP_SEND_METRICS = "off"
        # k8s 매니페스트도 본다 (권한 상승·root 실행 규칙, https://github.com/reddol18/docflow/issues/29)
        uvx --quiet "semgrep@$($V.semgrep)" scan --metrics=off --error --quiet --config p/python --config p/typescript `
            --config p/owasp-top-ten --exclude sim --severity ERROR --severity WARNING
    }

    Step "trivy" $true {
        $tv = Get-Tool trivy
        # 빌드 산출물은 건너뛴다 (next dev가 .next 안 파일을 잠가 두면 스캔이 멈춘다)
        & $tv config --quiet --severity HIGH,CRITICAL --exit-code 1 --skip-dirs frontend/node_modules --skip-dirs frontend/.next .
    }
} finally {
    Pop-Location
}

$failed = @($results | Where-Object { $_.blocking -and $_.ok -eq $false })
# 막지 않는 Step은 없다(trivy는 issues/29부터 막음) — 생기면 merge_gate.py 판정에도 그 결과를 받는 규칙을 함께 넣는다
$mark = { param($r) if ($null -eq $r.ok) { "건너뜀" } elseif ($r.ok) { "통과" } else { "**실패**" } }
Write-Host "`n---- PR '테스트' 칸에 붙일 요약 ----"
$at = "{0:yyyy-MM-dd HH:mm}" -f (Get-Date)
Write-Host ("로컬 검사 ``scripts/check.ps1`` — 커밋 ``{0}``{1}, {2}" -f $sha, $(if ($dirty) { " + 커밋 안 된 변경" } else { "" }), $at)
Write-Host "| 검사 | 결과 | 초 |"
Write-Host "|---|---|---|"
foreach ($r in $results) { Write-Host ("| {0} | {1} | {2} |" -f $r.name, (& $mark $r), $r.sec) }
if ($failed.Count) { Write-Host "`n실패: $($failed.name -join ', ')" -ForegroundColor Red }
else { Write-Host "`n막는 검사 모두 통과" -ForegroundColor Green }

# commit status docflow/local-check — 올릴지는 gate_status.py가 정한다 (실패여도 넘겨서 '올리지 않음' 이유를 보인다)
if ($NoStatus) {
    Write-Host "(-NoStatus: commit status 올리지 않음)"
} else {
    $stArgs = @("--start-sha", $fullSha, "--at", $at)
    if ($dirty) { $stArgs += "--start-dirty" }
    foreach ($r in $results) {
        $state = if ($null -eq $r.ok) { "skip" } elseif ($r.ok) { "pass" } else { "fail" }
        $stArgs += "--result", ("{0}={1}" -f $r.name, $state)  # 막는 검사인지는 gate_status가 merge_gate.CHECKS로 정한다
    }
    foreach ($s in $Skip) { if (@($results.name) -notcontains $s) { $stArgs += "--skip-opt", $s } }  # 예: pg
    $prevEnc = [Console]::OutputEncoding
    $prevEap = $ErrorActionPreference
    $stCode = -1
    try {
        # stderr 한 줄·uv 오류가 검사 결과(종료 코드)를 바꾸지 않게 감싼다
        $ErrorActionPreference = "Continue"
        [Console]::OutputEncoding = [Text.Encoding]::UTF8
        Push-Location $root
        try {
            uv run --quiet --no-project python -I scripts/gate_status.py local-check @stArgs | ForEach-Object { Write-Host $_ }
            $stCode = $LASTEXITCODE
        } finally { Pop-Location }
    } catch {
        Write-Host "gate_status 실행 오류: $_" -ForegroundColor Yellow
    } finally {
        [Console]::OutputEncoding = $prevEnc
        $ErrorActionPreference = $prevEap
    }
    switch ($stCode) {
        0 { Write-Host "commit status: 올림 (docflow/local-check = success)" -ForegroundColor Green }
        1 { Write-Host "commit status: 올리지 않음 (이유는 바로 위 줄)" -ForegroundColor Yellow }
        default { Write-Host "commit status: 올리기 실패 (종료 코드 $stCode) — 검사 결과는 그대로" -ForegroundColor Yellow }
    }
}
if ($failed.Count) { exit 1 }
exit 0
