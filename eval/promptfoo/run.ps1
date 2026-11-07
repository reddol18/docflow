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
  .\eval\promptfoo\run.ps1 -Local -LocalModel qwen2.5:7b -BaselineFrom ..\work\promptfoo\results_full_20261107.json
                                                로컬 모델만 돌리고 기준(gpt-4.1-mini·current)은 지난 결과에서 가져온다 — OpenAI 호출 0, 키 불필요
                                                (https://github.com/reddol18/docflow/issues/13). 기본 후보 '^local-ollama$', 판 '^current$'
  .\eval\promptfoo\run.ps1 -Replay              LLM 호출 없이 저장 결과로 채점 연결만 확인 (eval/score.py와 같아야 함)
  .\eval\promptfoo\run.ps1 -View                마지막 결과를 promptfoo 비교 화면으로 (http://127.0.0.1:15500)

종료 코드는 gate.py 그대로: 0 통과, 1 기준을 어긴 후보 있음, 2 입력 문제.
OpenAI 키는 Windows 사용자 환경변수 OPENAI_API_KEY에서 읽어 promptfoo를 부르는 동안만 이 프로세스 환경에 둔다
(promptfoo와 promptfoo가 띄우는 Python 지시문·채점 함수만 물려받음. Ollama는 키를 빼고 켜고, gate.py도 키 없이 돈다.
끝나면 원래 값으로 되돌린다). 화면·파일에 남기지 않는다. 키는 월 한도를 건 이 하네스 전용 OpenAI 프로젝트 키를 권한다.
로컬 Ollama는 OLLAMA_HOST=127.0.0.1:11434로 켠다 (Machine 환경변수 0.0.0.0이 물려지면 LAN 전체에 인증 없이 열린다).
이미 켜져 있던 Ollama가 모든 인터페이스에서 듣고 있으면 경고만 한다 (끄지 않는다).
실제 문서 글자가 담긴 결과는 저장소 밖 work/promptfoo/ 에만 쓴다. promptfoo 원격 기능(공유·원격 생성·사용 통계·업데이트 확인)은 끈다.
단, promptfoo 0.125.0은 사용 통계를 꺼도 'telemetry disabled' 이벤트 한 번을 r.promptfoo.app으로 보낸다
(문서 글자·결과는 없음, promptfoo는 고치지 않는다).
-View 화면은 localhost_only.cjs로 127.0.0.1에만 묶고, Host·Origin이 127.0.0.1/localhost:15500이 아닌 요청은 403으로 막는다.
#>
param(
    [string[]]$Vols = @(),
    [switch]$Local,
    [switch]$Replay,
    [switch]$Cache,       # 같은 응답을 다시 쓰기 (비용 0, 대신 지연 측정 없음)
    [switch]$View,
    [string]$Prompts = '',
    [string]$Providers = '',
    [string]$LocalModel = 'llama3.1:8b',
    [string]$BaselineFrom = ''   # 기준 후보 줄을 가져올 지난 results.json (기준 모델을 다시 부르지 않는다)
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

if ($View) {   # 127.0.0.1에서만 열고 다른 Host·Origin은 403 (localhost_only.cjs). 화면엔 키가 필요 없다 → 빼고 연다
    $prevKey = $env:OPENAI_API_KEY
    Remove-Item Env:OPENAI_API_KEY -ErrorAction SilentlyContinue
    try { node --require (Join-Path $here 'localhost_only.cjs') $pfjs view --yes -p 15500; $vcode = $LASTEXITCODE }
    finally { if ($prevKey) { $env:OPENAI_API_KEY = $prevKey } }
    exit $vcode
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

# OpenAI 키: 사용자 환경변수는 새 프로세스마다 물려지므로 이 프로세스 환경에서 먼저 뺀다 → Ollama·gate.py가 물려받지 않게.
# promptfoo를 부르는 동안만 넣고 finally에서 원래 값(있었으면)으로 되돌린다. 키 값은 찍지 않는다.
$prevKey = $env:OPENAI_API_KEY
$key = $null
Remove-Item Env:OPENAI_API_KEY -ErrorAction SilentlyContinue
if ($Replay) {
    $providers = '^replay$'
    if (-not $Prompts) { $Prompts = '^current$' }
} else {
    if ($BaselineFrom) {   # 기준은 지난 결과에서 → 이번엔 로컬 모델만 (기준을 거르지 않으면 gate.py가 종료 코드 2)
        if (-not (Test-Path $BaselineFrom)) { if ($prevKey) { $env:OPENAI_API_KEY = $prevKey }; throw "기준 결과가 없다: $BaselineFrom" }
        $BaselineFrom = (Resolve-Path $BaselineFrom).Path
        if (-not $Providers) { $Providers = '^local-ollama$' }
        if (-not $Prompts) { $Prompts = '^current$' }
    }
    $providers = if ($Providers) { $Providers } elseif ($Local) { '^(gpt-4\.1-mini|gpt-4o-mini|local-ollama)$' } else { '^(gpt-4\.1-mini|gpt-4o-mini)$' }
    # 키는 OpenAI 후보가 걸러질 때만 (로컬 모델만이면 키를 읽지도 넣지도 않는다)
    if (@('gpt-4.1-mini', 'gpt-4o-mini') | Where-Object { $_ -match $providers }) {
        $key = if ($prevKey) { $prevKey } else { [Environment]::GetEnvironmentVariable('OPENAI_API_KEY', 'User') }
        if (-not $key) { if ($prevKey) { $env:OPENAI_API_KEY = $prevKey }; throw 'OPENAI_API_KEY가 없다 — Windows 사용자 환경변수에 넣는다' }
    }
}

function Test-Ollama {
    try { Invoke-WebRequest 'http://127.0.0.1:11434/api/tags' -UseBasicParsing -TimeoutSec 3 | Out-Null; $true } catch { $false }
}
function Wait-Ollama {
    foreach ($i in 1..30) { if (Test-Ollama) { return } ; Start-Sleep 2 }
    throw 'Ollama가 127.0.0.1:11434에서 응답하지 않음'
}
function Get-OllamaListen {   # 11434를 듣는 주소들 (127.0.0.1 / 0.0.0.0 / :: …)
    @(Get-NetTCPConnection -LocalPort 11434 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { $_.LocalAddress } | Sort-Object -Unique)
}
function Test-AllInterfaces($addrs) { [bool]($addrs | Where-Object { $_ -notin @('127.0.0.1', '::1') }) }

$served = $null
$servedAt = $null
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
            # Machine 환경변수 OLLAMA_HOST=0.0.0.0:11434가 물려지면 LAN 전체에 인증 없이 열린다 → 이 serve만 127.0.0.1로.
            # 키는 위에서 이미 이 프로세스 환경에서 뺐다 → serve(와 그 자식 runner)는 키를 물려받지 않는다.
            $prevHost = $env:OLLAMA_HOST
            $env:OLLAMA_HOST = '127.0.0.1:11434'
            try {
                $servedAt = (Get-Date).AddSeconds(-2)
                $served = Start-Process $exe -ArgumentList 'serve' -PassThru -WindowStyle Hidden   # 이미 켜져 있던 Ollama는 건드리지 않는다
            } finally {
                if ($null -eq $prevHost) { Remove-Item Env:OLLAMA_HOST -ErrorAction SilentlyContinue } else { $env:OLLAMA_HOST = $prevHost }
            }
            Wait-Ollama
            $listen = Get-OllamaListen
            Write-Output ("Ollama: 이 스크립트가 켬, 듣는 주소 {0}" -f ($listen -join ', '))
            if (Test-AllInterfaces $listen) { throw "켠 Ollama가 127.0.0.1 밖($($listen -join ', '))에서 듣는다 — 멈춤" }
        } else {
            $listen = Get-OllamaListen
            if (Test-AllInterfaces $listen) {
                Write-Warning ("이미 켜져 있던 Ollama가 모든 인터페이스({0})에서 듣는다 — LAN에서 인증 없이 보인다. 끄지 않고 그대로 쓴다. " +
                               "OLLAMA_HOST=127.0.0.1:11434로 다시 켜는 것을 권한다" -f ($listen -join ', '))
            }
        }
        $tags = (Invoke-WebRequest 'http://127.0.0.1:11434/api/tags' -UseBasicParsing).Content | ConvertFrom-Json
        if ($LocalModel -notin $tags.models.name) { throw "Ollama에 모델 $LocalModel 이 없다 — 먼저 ollama pull (디스크 여유 확인 뒤)" }
    }
    $res = Join-Path $out 'results.json'
    if ($BaselineFrom -and $BaselineFrom -eq [IO.Path]::GetFullPath($res)) { throw '기준 결과로 results.json을 쓰면 이번 실행이 지운다 — 다른 이름으로 복사해 넘긴다' }
    Remove-Item $res -ErrorAction SilentlyContinue   # 실패한 실행이 지난 결과로 판정되지 않게
    $args_ = @('eval', '-c', (Join-Path $here 'promptfooconfig.yaml'), '-o', $res, '--filter-providers', $providers,
               '--no-share', '--no-table', '--no-progress-bar')
    if ($Prompts) { $args_ += @('--filter-prompts', $Prompts) }
    if (-not $Cache) { $args_ += '--no-cache' }
    if ($Local) { $args_ += @('-j', '1') }   # GPU·CPU 한 대에서 도는 로컬 모델은 하나씩 (OpenAI 후보도 같이 느려진다)
    Push-Location $here
    if ($key) { $env:OPENAI_API_KEY = $key }   # promptfoo를 부르는 동안만
    try {
        Invoke-Promptfoo @args_
        $pcode = $LASTEXITCODE
    } finally {
        Remove-Item Env:OPENAI_API_KEY -ErrorAction SilentlyContinue
        Pop-Location
    }
    # promptfoo는 단언 실패가 있으면 종료 코드 100 — 판정은 gate.py가 한다. 그 밖의 코드는 실행 자체가 실패
    if (($pcode -ne 0 -and $pcode -ne 100) -or -not (Test-Path $res)) { Write-Output "promptfoo 실패 (종료 코드 $pcode)"; exit 2 }
    $gate = @((Join-Path $here 'gate.py'), $res, '--out', $out)
    if ($Replay) { $gate += '--expect-stored' }
    if ($BaselineFrom -and -not $Replay) { $gate += @('--baseline-results', $BaselineFrom) }
    Push-Location $app
    & $py -I -X utf8 @gate
    $code = $LASTEXITCODE
    Pop-Location
    Write-Output "결과: $out\gate.md · promptfoo 화면: .\eval\promptfoo\run.ps1 -View"
    exit $code
} finally {
    # 키를 원래대로 (이 스크립트를 부른 셸에 있던 값이면 되살린다)
    if ($prevKey) { $env:OPENAI_API_KEY = $prevKey } else { Remove-Item Env:OPENAI_API_KEY -ErrorAction SilentlyContinue }
    if ($served) {   # 이 스크립트가 켠 Ollama만 끈다 (serve가 띄운 모델 실행 프로세스 = 자식 프로세스도 함께)
        # serve가 죽어도 자식의 ParentProcessId는 남는다 → 남은 것이 없을 때까지 (최대 15초) 다시 찾아 끈다.
        # PID는 다시 쓰일 수 있으므로 이름(ollama*)과 시작 시각(켠 뒤)까지 맞아야 끈다.
        $flt = "ProcessId=$($served.Id) OR ParentProcessId=$($served.Id)"
        $mine = { @(Get-CimInstance Win32_Process -Filter $flt -ErrorAction SilentlyContinue |
                    Where-Object { $_.Name -like 'ollama*' -and $_.CreationDate -ge $servedAt }) }
        foreach ($i in 1..15) {
            $ids = @(& $mine | ForEach-Object { $_.ProcessId })
            if (-not $ids) { break }
            Stop-Process -Id $ids -Force -ErrorAction SilentlyContinue
            Start-Sleep 1
        }
        Write-Output ("Ollama: 이 스크립트가 켠 것을 껐다 (남은 프로세스 {0}개)" -f @(& $mine).Count)
    }
}
