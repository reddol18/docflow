<#
스테이징(맥 k3s)에 ZAP API 스캔 (https://github.com/reddol18/docflow/issues/25). 이 PC에서 ssh 터널(127.0.0.1:18000)로 돈다.
경고가 있어도 실패로 끝나지 않는다. 계정 준비·로그인 확인·정리가 실패하면 종료 코드 1. 결과는 저장소 밖 %TEMP%\docflow-zap\ 에 둔다 (응답 일부가 들어 있을 수 있어 저장소에 넣지 않는다).

  # 먼저 터널: ssh -L 18000:127.0.0.1:18000 user@mac 'kubectl port-forward -n docs svc/docflow 18000:8000'
  .\scripts\zap_scan.ps1 -Mac user@mac [-Vol test-flags-1999-0002] [-Passive]

하는 일
1. ZAP(버전·SHA-256 고정)을 %LOCALAPPDATA%\docflow-tools 에 받는다. 이 PC의 Java 17+로 돈다
2. 앱에서 OpenAPI 문서를 만든다 (배포본은 /openapi.json을 끔)
3. 맥 docflow에 임시 viewer 계정 zap-scan을 만들거나 비밀번호를 바꿔 켠다 (비밀번호는 무작위, 화면·파일에 남기지 않음)
   viewer라 바꾸는 요청은 모두 403 → 능동 스캔이 데이터를 바꾸지 못한다
4. 직접 확인: 보안 헤더, 철 이름 '..' 경로 이탈 (curl --path-as-is)
5. ZAP 자동화: OpenAPI 가져오기 → 수동 검사 → 능동 검사(-Passive면 뺌) → 보고서(HTML·Markdown)
   /ask·/search는 뺀다 (OpenAI 비용·일일 예산)
6. 끝나면(실패해도) 계정을 끈다(세션도 끊긴다). 토큰은 curl에 파일로, ZAP에 환경 변수로만 넘기고 끝나면 지운다
#>
param(
    [Parameter(Mandatory)][string]$Mac,
    [string]$Vol = "test-flags-1999-0002",
    [string]$Target = "http://127.0.0.1:18000",
    [switch]$Passive,
    [int]$MaxMinutes = 20
)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$ZAP = @{ ver = "2.17.0"; sha = "94c8f767b1c2e94f0db66b3ae56514d5e3f5a728ee1b6c798e0c8fe2d61fbff0" }  # GitHub 릴리스 자산 digest
$out = Join-Path $env:TEMP "docflow-zap\$(Get-Date -Format yyyyMMdd-HHmm)"
New-Item -ItemType Directory -Force $out | Out-Null
$user = "zap-scan"
$kubectl = "/usr/local/bin/kubectl exec -i -n docs deploy/docflow -- python -m app.users"
# Windows PowerShell 5.1은 네이티브 명령으로 파이프할 때 앞에 BOM을 붙인다($OutputEncoding을 바꿔도 붙었다) → 맥에서 떼고 넘긴다
# (app/users.py도 이제 BOM을 뗀다. 그 판이 배포된 뒤에는 이 perl 단계를 지워도 된다)
$noBom = "perl -pe 's/^\xEF\xBB\xBF// if `$. == 1' |"

# 1. ZAP
$dir = Join-Path $env:LOCALAPPDATA "docflow-tools\zap-$($ZAP.ver)"
$bat = Join-Path $dir "ZAP_$($ZAP.ver)\zap.bat"
if (-not (Test-Path $bat)) {
    New-Item -ItemType Directory -Force $dir | Out-Null
    $zip = Join-Path $dir "zap.zip"
    Invoke-WebRequest "https://github.com/zaproxy/zaproxy/releases/download/v$($ZAP.ver)/ZAP_$($ZAP.ver)_Crossplatform.zip" -OutFile $zip -UseBasicParsing
    if ((Get-FileHash $zip -Algorithm SHA256).Hash -ne $ZAP.sha.ToUpper()) { Remove-Item -Recurse -Force $dir; throw "ZAP 체크섬 불일치" }
    Expand-Archive -Force $zip $dir
    Remove-Item $zip
}

# 2. OpenAPI (servers를 터널 주소로)
$spec = Join-Path $out "openapi.json"
Push-Location $root
try {
    uv run --quiet --no-project --with-requirements requirements-test.txt python -I -c @"
import json, sys
sys.path.insert(0, '.')
from app.main import app
d = app.openapi()
d['servers'] = [{'url': '$Target'}]
open(r'$spec', 'w', encoding='utf-8').write(json.dumps(d))
"@
    if ($LASTEXITCODE) { throw "OpenAPI 만들기 실패" }
} finally { Pop-Location }

# 3~5는 모두 try 안 → 어디서 멈춰도(끊김·Ctrl+C) finally가 계정을 끈다
$hdr = Join-Path $out "auth.hdr"  # curl에 토큰을 명령줄이 아니라 파일로 넘긴다 (프로세스 목록·감사 로그에 남지 않게). finally에서 지운다
$failed = $false
function Remote([string]$cmd, [string]$stdin) {
    $o = if ($stdin) { $stdin | ssh -o BatchMode=yes $Mac $cmd } else { ssh -o BatchMode=yes $Mac $cmd }
    if ($LASTEXITCODE) { throw "맥 명령 실패 ($LASTEXITCODE): $($cmd.Split('|')[-1].Trim())" }
    $o
}
try {
    # 3. 임시 viewer (있으면 비밀번호를 바꾸고 역할을 viewer로 되돌리고 켠다)
    $bytes = [byte[]]::new(24); [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $pw = [Convert]::ToBase64String($bytes).Replace("+", "-").Replace("/", "_")
    $rows = @(Remote "$kubectl list")
    $exists = $rows | Where-Object { $_ -match "^$([regex]::Escape($user))\s" -and $_ -notmatch "\bsso\b" }
    if ($exists) {
        Remote "$noBom $kubectl passwd $user --password-stdin" $pw | Out-Null
        Remote "$kubectl role $user viewer" | Out-Null   # 누가 역할을 올려 두었어도 스캔은 viewer로만 (바꾸는 요청이 403이어야 한다)
        Remote "$kubectl enable $user" | Out-Null
    } else {
        Remote "$noBom $kubectl add $user --role viewer --display 'ZAP 스캔' --password-stdin" $pw | Out-Null
    }

    # 로그인 → 세션 토큰. Set-Cookie 헤더에서 읽는다 (쿠키 저장소는 Secure 쿠키를 http 주소에 돌려주지 않는다)
    $body = @{ username = $user; password = $pw } | ConvertTo-Json -Compress
    $r = Invoke-WebRequest "$Target/auth/login" -Method Post -Body $body -ContentType "application/json" `
        -Headers @{ "X-Docflow-CSRF" = "1" } -UseBasicParsing
    $pw = $null; $body = $null
    $token = [regex]::Match(($r.Headers["Set-Cookie"] -join ";"), "docflow_session=([^;]+)").Groups[1].Value
    if (-not $token) { throw "로그인은 됐는데 세션 쿠키가 없음 ($($r.StatusCode))" }
    [IO.File]::WriteAllText($hdr, "Authorization: Bearer $token", [Text.UTF8Encoding]::new($false))

    # 4. 직접 확인 (curl이 실패하면 '헤더 없음'으로 적지 않고 멈춘다)
    $manual = [System.Collections.Generic.List[string]]::new()
    $manual.Add("# 직접 확인 ($(Get-Date -Format 'yyyy-MM-dd HH:mm'), $Target, viewer)")
    $manual.Add("")
    $manual.Add("## 응답 헤더")
    foreach ($p in "/volumes", "/volumes/$Vol/documents", "/review/$Vol", "/report") {
        $h = @(curl.exe -s -o NUL -D - -H "@$hdr" "$Target$p")
        if ($LASTEXITCODE -or -not $h -or $h[0] -notmatch "^HTTP/\S+ (\d{3})") { throw "직접 확인 요청 실패: $p (curl $LASTEXITCODE)" }
        $code = $Matches[1]
        $want = "cache-control", "x-content-type-options", "x-frame-options", "content-security-policy"
        $have = $want | ForEach-Object { $n = $_; if ($h | Where-Object { $_ -match "^${n}:" }) { "$n O" } else { "$n X" } }
        $manual.Add("- ``$p`` $code — $($have -join ', ')")
    }
    $manual.Add("")
    $manual.Add("## 철 이름 '..' (경로 이탈, https://github.com/reddol18/docflow/issues/49)")
    foreach ($p in "/volumes/%2E%2E/documents", "/head/%2E%2E/x", "/crops/%2E%2E/x_docno.png", "/volumes/../documents") {
        $code = curl.exe -s -o NUL -w "%{http_code}" --path-as-is -H "@$hdr" "$Target$p"
        if ($LASTEXITCODE) { throw "직접 확인 요청 실패: $p (curl $LASTEXITCODE)" }
        $manual.Add("- ``$p`` → $code")
    }
    $manual | Out-File -Encoding utf8 (Join-Path $out "manual.md")

    # OpenAPI 손질: 비용이 들거나(ask·search) 세션을 끊는(logout) 경로는 문서에서 뺀다
    # (컨텍스트 제외만으로는 openapi 가져오기 단계의 요청을 막지 못할 수 있다)
    $d = Get-Content -Raw -Encoding utf8 $spec | ConvertFrom-Json
    foreach ($name in @($d.paths.PSObject.Properties.Name)) {
        if ($name -match "^/(ask|search|auth/logout)\b") { $d.paths.PSObject.Properties.Remove($name) }
    }
    # 경로 변수에 실제 값 (없으면 ZAP이 'vol' 같은 값을 넣어 404만 본다)
    $docs = @(curl.exe -s -H "@$hdr" "$Target/volumes/$Vol/documents" | ConvertFrom-Json)
    if (-not $docs -or -not $docs[0].doc_id) { throw "철 $Vol 에 문서가 없음 — -Vol로 처리된 철을 지정" }
    $sample = @{ vol = $Vol; doc_id = "$($docs[0].doc_id)"; page = "$(@($docs[0].pages)[0])"; name = "$($docs[0].doc_id)_docno.png" }
    foreach ($path in $d.paths.PSObject.Properties) {
        foreach ($op in $path.Value.PSObject.Properties) {
            foreach ($prm in @($op.Value.parameters)) {
                if ($prm -and $prm.in -eq "path" -and $sample[$prm.name]) {
                    $prm.schema | Add-Member -Force -NotePropertyName default -NotePropertyValue $sample[$prm.name]
                }
            }
        }
    }
    [IO.File]::WriteAllText($spec, ($d | ConvertTo-Json -Depth 50), [Text.UTF8Encoding]::new($false))

    # 5. ZAP 자동화. 앞뒤로 /volumes 200을 확인해 로그인이 끝까지 붙어 있었는지 본다
    $plan = Join-Path $out "plan.yaml"
    $check = @"
  - type: requestor
    requests:
      - {url: "$Target/volumes", method: GET, responseCode: 200}
"@
    $jobs = @"
env:
  contexts:
    - name: docflow
      urls: ["$Target"]
      excludePaths: ["$Target/ask.*", "$Target/search.*", "$Target/auth/logout.*"]
  parameters: {failOnError: false, progressToStdout: true}
jobs:
  - type: replacer
    rules:
      - {description: auth, matchType: req_header, matchString: Authorization, replacementString: "Bearer `${ZAP_TOKEN}"}
$check
  - type: openapi
    parameters: {apiFile: "$($spec.Replace('\','/'))", targetUrl: "$Target", context: docflow}
  - type: passiveScan-wait
    parameters: {maxDuration: 5}
"@
    if (-not $Passive) {
        $jobs += @"

  - type: activeScan
    parameters: {context: docflow, maxScanDurationInMins: $MaxMinutes, threadPerHost: 2}
"@
    }
    $jobs += @"

$check
  - type: report
    parameters: {template: traditional-html, reportDir: "$($out.Replace('\','/'))", reportFile: zap.html}
  - type: report
    parameters: {template: traditional-md, reportDir: "$($out.Replace('\','/'))", reportFile: zap.md}
"@
    $jobs | Out-File -Encoding utf8 $plan
    $env:ZAP_TOKEN = $token
    Push-Location (Split-Path $bat)  # zap.bat은 자기 폴더에서 zap-*.jar을 찾는다
    try { & $bat -cmd -autorun $plan -dir (Join-Path $out "zaphome") | Tee-Object (Join-Path $out "zap.log") } finally { Pop-Location }
    $bad = Select-String -Path (Join-Path $out "zap.log") -Pattern "requestor.*(expected|unexpected|Error)" -Encoding utf8
    if ($bad) { Write-Warning "로그인 확인(/volumes 200) 실패 — 스캔이 로그아웃 상태로 돌았을 수 있다: $($bad[0].Line)"; $failed = $true }
} catch {
    Write-Host $_ -ForegroundColor Red
    $failed = $true
} finally {
    Remove-Item Env:ZAP_TOKEN -ErrorAction SilentlyContinue
    Remove-Item -Force $hdr -ErrorAction SilentlyContinue
    $token = $null
    # 6. 정리: 계정 끄기(세션도 끊긴다). 실패하면 크게 알린다
    ssh -o BatchMode=yes $Mac "$kubectl disable $user" | Out-Null
    if ($LASTEXITCODE) {
        Write-Warning "임시 계정 $user 를 끄지 못함 — 직접: ssh $Mac `"$kubectl disable $user`""
        $failed = $true
    }
    $zh = Join-Path $out "zaphome"
    if (Test-Path $zh) {
        Remove-Item -Recurse -Force $zh -ErrorAction SilentlyContinue
        if (Test-Path $zh) { Write-Warning "$zh 를 지우지 못함 (요청 기록에 Authorization 헤더가 있다. 세션은 이미 끊김) — 직접 지울 것" }
    }
}
Write-Host "결과: $out (manual.md, zap.md, zap.html, zap.log)"
exit $(if ($failed) { 1 } else { 0 })
