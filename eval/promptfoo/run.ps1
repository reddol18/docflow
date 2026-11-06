<#
모델·지시문 비교 하네스 (https://github.com/reddol18/docflow/issues/22). 명령 한 번으로 promptfoo 행렬 → 통과 기준 판정.

  .\eval\promptfoo\run.ps1                      OpenAI 두 후보(gpt-4.1-mini 기준, gpt-4o-mini) × 지시문 두 판 × 7개 철
  .\eval\promptfoo\run.ps1 -Local               + 로컬 모델(이 PC의 Ollama, 기본 llama3.1:8b). 꺼져 있으면 켜고 끝나면 끈다
  .\eval\promptfoo\run.ps1 -Local -LocalModel qwen2.5:7b                모델 고르기 (ollama pull로 미리 받아 둔다)
  .\eval\promptfoo\run.ps1 -Local -Providers '^(gpt-4\.1-mini|local-ollama)$'   후보 고르기 (기준은 꼭 넣는다)
  .\eval\promptfoo\run.ps1 -Vols 5350109-1997-0002,5350109-1999-0002   철 고르기 (로컬 모델은 느려서 작은 철로)
실험 실행은 이 Windows PC 자원만 쓴다. 맥(colima k3s)은 클라우드 배포를 로컬에서 시험하는 배포 환경이라 실험에 쓰지 않는다
(2026-11-07 맥에서 Ollama를 띄웠다가 디스크 부족으로 운영 파드가 축출된 뒤 정함). 로컬 모델 채택 측정은 https://github.com/reddol18/docflow/issues/13.
로컬 모델을 띄우기 전에 모델 폴더 드라이브의 디스크 여유(10GB)를 먼저 확인하고, 모자라면 멈춘다.
  .\eval\promptfoo\run.ps1 -Replay              LLM 호출 없이 저장 결과로 채점 연결만 확인 (eval/score.py와 같아야 함)
  .\eval\promptfoo\run.ps1 -View                마지막 결과를 promptfoo 비교 화면으로 (http://localhost:15500)

종료 코드는 gate.py 그대로: 0 통과, 1 기준을 어긴 후보 있음, 2 입력 문제.
OpenAI 키는 Windows 사용자 환경변수 OPENAI_API_KEY에서 promptfoo 프로세스로만 넘긴다 (화면·파일에 남기지 않음).
실제 문서 글자가 담긴 결과는 저장소 밖 work/promptfoo/ 에만 쓴다. promptfoo 원격 기능(공유·원격 생성·사용 통계)은 끈다.
#>
param(
    [string[]]$Vols = @(),
    [switch]$Local,
    [switch]$Replay,
    [switch]$Cache,       # 같은 응답을 다시 쓰기 (비용 0, 대신 지연 측정 없음)
    [switch]$View,
    [string]$Prompts = '',
    [string]$Providers = '',
    [string]$LocalModel = 'llama3.1:8b'
)
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$app = (Resolve-Path "$here\..\..").Path
$work = if ($env:DOCFLOW_EVAL_WORK) { $env:DOCFLOW_EVAL_WORK } else { (Resolve-Path "$app\..\work").Path }
$out = Join-Path $work 'promptfoo'
New-Item -ItemType Directory -Force $out | Out-Null

$env:PROMPTFOO_DISABLE_TELEMETRY = '1'
$env:PROMPTFOO_DISABLE_UPDATE = '1'
$env:PROMPTFOO_DISABLE_SHARING = '1'
$env:PROMPTFOO_DISABLE_REMOTE_GENERATION = 'true'
$env:PROMPTFOO_CONFIG_DIR = Join-Path $out '.promptfoo'   # 결과 DB(실제 문서 글자)도 work/ 아래
$env:PYTHONUTF8 = '1'
$env:DOCFLOW_EVAL_WORK = $work
$env:DOCFLOW_EVAL_VOLS = ($Vols -join ',')
# .cmd 껍데기를 거치면 cmd.exe가 정규식의 '|'를 파이프로 읽는다 → node로 바로 부른다
$pfjs = Join-Path $here 'node_modules\promptfoo\dist\src\entrypoint.js'
if (-not (Test-Path $pfjs)) { Push-Location $here; npm ci --no-audit --no-fund; Pop-Location }
function Invoke-Promptfoo { node $pfjs @args }

if ($View) {   # 127.0.0.1에서만 연다 (localhost_only.cjs)
    node --require (Join-Path $here 'localhost_only.cjs') $pfjs view --yes -p 15500; exit $LASTEXITCODE
}

# promptfoo·gate.py가 부르는 Python = 시험 의존성(requirements-test.txt)을 깐 고정 가상환경 (work/promptfoo/.venv)
$venv = Join-Path $out '.venv'
$py = Join-Path $venv 'Scripts\python.exe'
$req = Join-Path $app 'requirements-test.txt'
$stamp = Join-Path $venv 'requirements-test.sha256'
$want = (Get-FileHash $req -Algorithm SHA256).Hash
if (-not (Test-Path $py) -or -not (Test-Path $stamp) -or (Get-Content $stamp) -ne $want) {
    uv venv -q --allow-existing $venv
    uv pip install -q --python $py -r $req
    if ($LASTEXITCODE -ne 0) { throw '평가 가상환경을 만들지 못함' }
    Set-Content -Path $stamp -Value $want
}
$env:PROMPTFOO_PYTHON = $py

# promptfoo는 거르기 전에 모든 제공자를 읽는다 → 로컬 모델 이름은 늘 채워 둔다
$env:DOCFLOW_LOCAL_MODEL = $LocalModel

if ($Replay) {
    $providers = '^replay$'
    if (-not $Prompts) { $Prompts = '^current$' }
} else {
    $providers = if ($Providers) { $Providers } elseif ($Local) { '^(gpt-4\.1-mini|gpt-4o-mini|local-ollama)$' } else { '^(gpt-4\.1-mini|gpt-4o-mini)$' }
    if (-not $env:OPENAI_API_KEY) {
        $env:OPENAI_API_KEY = [Environment]::GetEnvironmentVariable('OPENAI_API_KEY', 'User')
    }
    if (-not $env:OPENAI_API_KEY) { throw 'OPENAI_API_KEY가 없다 — Windows 사용자 환경변수에 넣는다' }
}

function Test-Ollama {
    try { Invoke-WebRequest 'http://127.0.0.1:11434/api/tags' -UseBasicParsing -TimeoutSec 3 | Out-Null; $true } catch { $false }
}
function Wait-Ollama {
    foreach ($i in 1..30) { if (Test-Ollama) { return } ; Start-Sleep 2 }
    throw 'Ollama가 127.0.0.1:11434에서 응답하지 않음'
}

$served = $null
try {
    if ($Local -and -not $Replay) {
        # 이 PC의 Ollama. 모델 폴더(OLLAMA_MODELS) 드라이브 여유를 먼저 본다 — 모델을 새로 받거나 캐시를 쓸 수 있다
        foreach ($scope in 'User', 'Machine') {   # 이 셸이 시작된 뒤 넣은 값도 읽는다
            if (-not $env:OLLAMA_MODELS) { $env:OLLAMA_MODELS = [Environment]::GetEnvironmentVariable('OLLAMA_MODELS', $scope) }
        }
        $mdir = if ($env:OLLAMA_MODELS) { $env:OLLAMA_MODELS } else { Join-Path $HOME '.ollama\models' }
        $free = (Get-PSDrive ([IO.Path]::GetPathRoot($mdir).Substring(0, 1))).Free / 1GB
        Write-Output ("디스크 여유: {0} {1:N1}GB (필요 10GB)" -f $mdir, $free)
        if ($free -lt 10) { throw '모델 폴더 드라이브의 디스크 여유가 10GB보다 작아 로컬 모델을 띄우지 않음' }
        if (-not (Test-Ollama)) {
            $exe = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
            if (-not (Test-Path $exe)) { throw "Ollama가 없다 ($exe)" }
            $served = Start-Process $exe -ArgumentList 'serve' -PassThru -WindowStyle Hidden   # 이미 켜져 있던 Ollama는 건드리지 않는다
            Wait-Ollama
        }
        $tags = (Invoke-WebRequest 'http://127.0.0.1:11434/api/tags' -UseBasicParsing).Content | ConvertFrom-Json
        if ($LocalModel -notin $tags.models.name) { throw "Ollama에 모델 $LocalModel 이 없다 — 먼저 ollama pull (디스크 여유 확인 뒤)" }
    }
    $res = Join-Path $out 'results.json'
    Remove-Item $res -ErrorAction SilentlyContinue   # 실패한 실행이 지난 결과로 판정되지 않게
    $args_ = @('eval', '-c', (Join-Path $here 'promptfooconfig.yaml'), '-o', $res, '--filter-providers', $providers,
               '--no-share', '--no-table', '--no-progress-bar')
    if ($Prompts) { $args_ += @('--filter-prompts', $Prompts) }
    if (-not $Cache) { $args_ += '--no-cache' }
    if ($Local) { $args_ += @('-j', '1') }   # GPU·CPU 한 대에서 도는 로컬 모델은 하나씩 (OpenAI 후보도 같이 느려진다)
    Push-Location $here
    Invoke-Promptfoo @args_
    $pcode = $LASTEXITCODE
    Pop-Location
    # promptfoo는 단언 실패가 있으면 종료 코드 100 — 판정은 gate.py가 한다. 그 밖의 코드는 실행 자체가 실패
    if (($pcode -ne 0 -and $pcode -ne 100) -or -not (Test-Path $res)) { Write-Output "promptfoo 실패 (종료 코드 $pcode)"; exit 2 }
    $gate = @((Join-Path $here 'gate.py'), $res, '--out', $out)
    if ($Replay) { $gate += '--expect-stored' }
    Push-Location $app
    & $py -I -X utf8 @gate
    $code = $LASTEXITCODE
    Pop-Location
    Write-Output "결과: $out\gate.md · promptfoo 화면: .\eval\promptfoo\run.ps1 -View"
    exit $code
} finally {
    if ($served) {   # 이 스크립트가 켠 Ollama만 끈다 (serve가 띄운 모델 실행 프로세스 = 자식 프로세스도 함께)
        # serve가 죽어도 자식의 ParentProcessId는 남는다 → 남은 것이 없을 때까지 (최대 15초) 다시 찾아 끈다
        $flt = "ProcessId=$($served.Id) OR ParentProcessId=$($served.Id)"
        foreach ($i in 1..15) {
            $ids = @(Get-CimInstance Win32_Process -Filter $flt -ErrorAction SilentlyContinue | ForEach-Object { $_.ProcessId })
            if (-not $ids) { break }
            Stop-Process -Id $ids -Force -ErrorAction SilentlyContinue
            Start-Sleep 1
        }
        Write-Output ("Ollama: 이 스크립트가 켠 것을 껐다 (남은 프로세스 {0}개)" -f @(Get-CimInstance Win32_Process -Filter $flt).Count)
    }
}
