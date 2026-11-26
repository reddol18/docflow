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
                                                (https://github.com/reddol18/docflow/issues/13). 기본 후보 '^local-ollama$', 판 '^current$'.
                                                -Local 필요, -Replay와 같이 못 씀. 걸러진 후보·판이 기준과 겹치면 부르기 전에 멈춘다(plan.py).
                                                기준 파일은 work 아래 사본을 gate.py에 넘긴다
  .\eval\promptfoo\run.ps1 -Work ..\work_synth\w47 ...   다른 work 폴더 (합성 철: eval/synth/to_work.py가 만든 것, 정답도 그 폴더의 gt/review.csv)
  ssh -N -L 18434:127.0.0.1:11434 root@<파드 IP> -p <SSH 포트>        (다른 창에 SSH 터널 — 기본. 이 PC Ollama 11434와 안 겹치게 18434)
  .\eval\promptfoo\run.ps1 -Work ..\work_synth\w47 -Remote http://127.0.0.1:18434/v1 -RemoteModel gemma3:27b `
                           -BaselineFrom ..\work_synth\w47\promptfoo\results_gpt41mini.json
                                                원격 OpenAI 호환 서버(런파드 GPU의 Ollama·vLLM) (https://github.com/reddol18/docflow/issues/80).
                                                대안: 런파드 프록시 https://<pod>-11434.proxy.runpod.net/v1 — 인증 없는 Ollama가 파드가 켜진 동안
                                                공개되므로 터널을 먼저 쓴다 (위험 수용 https://github.com/reddol18/docflow/issues/144).
                                                후보 label 'remote'. 부르기 전에 <주소>/models에 모델이 있는지 본다. 주소는 https, 또는 http면
                                                127.0.0.1·localhost(터널)만, 주소 안 사용자·비밀번호(u:p@)는 거부. 서버 키(vLLM --api-key)는
                                                환경변수 DOCFLOW_REMOTE_API_KEY에 두면 -Remote 실행에서 promptfoo를 부르는 동안만 넣는다 (화면·파일에 안 남김).
                                                토큰 단가 0은 주소가 127.0.0.1·localhost·*.proxy.runpod.net일 때만 (그 밖은 gate.py가 단가 모름으로 멈춤).
                                                끝나면 파드 정지·볼륨 삭제를 확인한다 (실행 끝에 안내).
                                                -BaselineFrom이면 기본 후보 '^remote$', 아니면 '^(gpt-4\.1-mini|remote)$'. 판 기본 '^current$'.
                                                -Concurrency로 동시 요청 수 (기본: -Local이면 1, 아니면 promptfooconfig.yaml의 4)
외부 후보(OpenAI·remote·Ollama *-cloud 모델)는 합성 철에만 보낸다 (2026-11-20 #80 결정): 고른 철이 모두 work 폴더
synth_manifest.json에 있지(이름 SYNTH…) 않으면 부르기 전에 멈춘다 (-Remote나 OpenAI 후보가 있으면 늘 확인).
-Work·-Vols 등으로 바꾼 DOCFLOW_* 환경변수는 끝나면 시작 값으로 되돌린다. -Work면 그 드라이브 여유(2GB)를 먼저 본다. AI Hub 철은 -Local(이 PC Ollama)과 -BaselineFrom(지난 결과, 호출 없음)으로만 잰다.
  .\eval\promptfoo\run.ps1 -Replay              LLM 호출 없이 저장 결과로 채점 연결만 확인 (eval/score.py와 같아야 함)
  .\eval\promptfoo\run.ps1 -View                마지막 결과를 promptfoo 비교 화면으로 (http://127.0.0.1:15500)
  .\eval\promptfoo\run.ps1 -RedTeam [-Repeat 3]  레드팀 고정 10건 (https://github.com/reddol18/docflow/issues/27 조건 4, redteam27.yaml).
                                                운영 함수 llm.extract·llm.answer + 운영 모델(gpt-4.1-mini·gpt-4o-mini). 가상 값만이라
                                                합성 철 확인은 하지 않는다. 부르기 전에 비용 상한 추정(> $0.25면 멈춤), 실행 중 누적
                                                상한 $0.5(장부). 결과 work/promptfoo/redteam27/ (results.json·redteam27.md·json).
                                                종료 코드 0 조종 0건, 1 조종(또는 오류) 있음, 2 실행 실패.
                                                -RedTeamChatModel gpt-4.1-mini: 답변 모델만 후보로 바꿔 같은 세트를 잰다

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
    [string]$BaselineFrom = '',  # 기준 후보 줄을 가져올 지난 results.json (기준 모델을 다시 부르지 않는다)
    [string]$Work = '',          # work 폴더 (기본 records_poc/work 또는 DOCFLOW_EVAL_WORK)
    [string]$Remote = '',        # 원격 OpenAI 호환 주소 (…/v1)
    [string]$RemoteModel = '',
    [int]$Concurrency = 0,
    [switch]$RedTeam,            # 레드팀 고정 10건 (#27 조건 4)
    [int]$Repeat = 3,
    [string]$RedTeamChatModel = ''   # 레드팀 답변 모델 후보 (비우면 운영 기본값, PRICE에 있어야 함)
)
$ErrorActionPreference = 'Stop'
# 이 스크립트는 부른 셸 안에서 돈다 → 바꾼 DOCFLOW_* 환경변수는 맨 끝 finally에서 시작 값으로 되돌린다
# (안 그러면 -Work 다음 실행이 -Work 없이도 그 work를 잰다). 키 두 개는 따로 아래에서 되돌린다
$envNames = 'DOCFLOW_EVAL_WORK', 'DOCFLOW_EVAL_VOLS', 'DOCFLOW_LOCAL_MODEL', 'DOCFLOW_REMOTE_MODEL', 'DOCFLOW_REMOTE_BASE',
            'DOCFLOW_RT27_LEDGER', 'DOCFLOW_RT27_CHAT_MODEL'
$envSaved = @{}
foreach ($n in $envNames) { $envSaved[$n] = [Environment]::GetEnvironmentVariable($n, 'Process') }
try {   # 끝: 환경변수 되돌리기 (맨 아래 finally)
$here = $PSScriptRoot
$app = (Resolve-Path "$here\..\..").Path
$workArg = $Work   # PowerShell 변수 이름은 대소문자를 가리지 않는다 — 아래 $work 대입이 -Work 값을 덮으므로 먼저 받아 둔다
$work = if ($workArg) { (Resolve-Path $workArg).Path } elseif ($env:DOCFLOW_EVAL_WORK) { $env:DOCFLOW_EVAL_WORK } else { (Resolve-Path "$app\..\work").Path }
$out = Join-Path $work 'promptfoo'
if ($workArg) {   # 다른 work는 평가 가상환경(.venv)을 새로 깔고 결과를 쓴다 → 그 드라이브 여유를 먼저 본다
    $wfree = (Get-PSDrive ([IO.Path]::GetPathRoot($work).Substring(0, 1))).Free / 1GB
    Write-Output ("디스크 여유: {0} {1:N1}GB (필요 2GB)" -f $work, $wfree)
    if ($wfree -lt 2) { throw 'work 폴더 드라이브의 디스크 여유가 2GB보다 작아 멈춤' }
}
New-Item -ItemType Directory -Force $out | Out-Null

$env:PROMPTFOO_DISABLE_TELEMETRY = '1'
$env:PROMPTFOO_DISABLE_UPDATE = '1'
$env:PROMPTFOO_DISABLE_SHARING = '1'
$env:PROMPTFOO_DISABLE_REMOTE_GENERATION = 'true'
$env:PROMPTFOO_DISABLE_REDTEAM_REMOTE_GENERATION = 'true'   # 레드팀 세트는 손으로 쓴 고정 10건 — 외부 생성 안 씀 (#27)
$env:PROMPTFOO_CONFIG_DIR = Join-Path $out '.promptfoo'   # 결과 DB(실제 문서 글자)도 work/ 아래
$env:PYTHONUTF8 = '1'
$env:DOCFLOW_EVAL_WORK = $work
$env:DOCFLOW_EVAL_VOLS = ($Vols -join ',')
# .cmd 껍데기를 거치면 cmd.exe가 정규식의 '|'를 파이프로 읽는다 → node로 바로 부른다
$pfjs = Join-Path $here 'node_modules\promptfoo\dist\src\entrypoint.js'
. (Join-Path $here 'node_deps.ps1')
if (Sync-NodeModules -Dir $here) { Write-Host 'promptfoo 의존성을 lock대로 다시 깔았다 (npm ci)' }   # lock이 바뀌면 다시 깐다 (#123)
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

# promptfoo는 거르기 전에 모든 제공자를 읽는다 → 로컬·원격 모델 이름과 주소는 늘 채워 둔다 (안 쓰면 닿지 않는 자리 값)
$env:DOCFLOW_LOCAL_MODEL = $LocalModel
$env:DOCFLOW_REMOTE_MODEL = if ($RemoteModel) { $RemoteModel } else { 'unused' }
$env:DOCFLOW_REMOTE_BASE = if ($Remote) { $Remote.TrimEnd('/') } else { 'http://127.0.0.1:9/v1' }
# 원격 서버 키: 이 프로세스 환경에서 먼저 빼 두고(Ollama 등 자식이 물려받지 않게) promptfoo를 부르는 동안만 넣는다.
# 비어 있으면 promptfoo가 OPENAI_API_KEY로 떨어져 OpenAI 키가 원격 서버로 갈 수 있다 → 키가 없으면 'none'
$prevRemoteKey = $env:DOCFLOW_REMOTE_API_KEY
$remoteKey = if ($prevRemoteKey) { $prevRemoteKey } else { [Environment]::GetEnvironmentVariable('DOCFLOW_REMOTE_API_KEY', 'User') }
if (-not $remoteKey) { $remoteKey = 'none' }
Remove-Item Env:DOCFLOW_REMOTE_API_KEY -ErrorAction SilentlyContinue

# OpenAI 키: 사용자 환경변수는 새 프로세스마다 물려지므로 이 프로세스 환경에서 먼저 뺀다 → Ollama·gate.py가 물려받지 않게.
# promptfoo를 부르는 동안만 넣고 finally에서 원래 값(있었으면)으로 되돌린다. 키 값은 찍지 않는다.
$prevKey = $env:OPENAI_API_KEY
$key = $null
Remove-Item Env:OPENAI_API_KEY -ErrorAction SilentlyContinue

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
$baseDir = $null
try {   # 입력 검사도 여기 안에서 — 어디서 멈추든 키 복원은 아래 finally 하나로
    if ($RedTeam) {   # 레드팀 고정 10건 (#27 조건 4) — 가상 값만, 운영 함수·운영 모델
        if ($Local -or $Replay -or $Remote -or $BaselineFrom -or $Vols) { throw '-RedTeam은 다른 실행 방식(-Local·-Replay·-Remote·-BaselineFrom·-Vols)과 같이 쓰지 않는다' }
        if ($Repeat -lt 1 -or $Repeat -gt 3) { throw '-Repeat는 1~3 (사용자 결정: 10건 × 최대 3회)' }
        $env:DOCFLOW_RT27_CHAT_MODEL = $RedTeamChatModel   # 빈 값이면 운영 기본 답변 모델
        $rt = Join-Path $out 'redteam27'
        New-Item -ItemType Directory -Force $rt | Out-Null
        $env:DOCFLOW_RT27_LEDGER = Join-Path $rt ('ledger-{0}.jsonl' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
        & $py -I -X utf8 (Join-Path $here 'redteam27.py') estimate --repeat $Repeat
        if ($LASTEXITCODE -ne 0) { throw '비용 상한 추정이 $0.25를 넘거나 추정 실패 — 부르지 않음' }
        $key = if ($prevKey) { $prevKey } else { [Environment]::GetEnvironmentVariable('OPENAI_API_KEY', 'User') }
        if (-not $key) { throw 'OPENAI_API_KEY가 없다 — Windows 사용자 환경변수에 넣는다' }
        $res = Join-Path $rt 'results.json'
        Remove-Item $res -ErrorAction SilentlyContinue
        $args_ = @('eval', '-c', (Join-Path $here 'redteam27.yaml'), '-o', $res, '--repeat', "$Repeat", '-j', '1',
                   '--no-cache', '--no-share', '--no-table', '--no-progress-bar')
        Push-Location $here
        $env:OPENAI_API_KEY = $key   # promptfoo를 부르는 동안만
        try { Invoke-Promptfoo @args_; $pcode = $LASTEXITCODE }
        finally { Remove-Item Env:OPENAI_API_KEY -ErrorAction SilentlyContinue; Pop-Location }
        if (($pcode -ne 0 -and $pcode -ne 100) -or -not (Test-Path $res)) { Write-Output "promptfoo 실패 (종료 코드 $pcode)"; exit 2 }
        & $py -I -X utf8 (Join-Path $here 'redteam27.py') report $res --out $rt --ledger $env:DOCFLOW_RT27_LEDGER
        exit $LASTEXITCODE
    }
    if ($Replay) {
        if ($BaselineFrom) { throw '-BaselineFrom은 -Replay와 같이 쓰지 않는다 (Replay는 저장 결과와의 일치만 본다)' }
        $providers = '^replay$'
        if (-not $Prompts) { $Prompts = '^current$' }
    } else {
        if ($Remote) {   # 원격 OpenAI 호환 서버 (#80): https, 또는 SSH 터널(http://127.0.0.1·localhost)만
            if (-not $RemoteModel) { throw '-Remote에는 -RemoteModel이 필요하다' }
            $u = [Uri]$Remote
            if ($u.UserInfo) { throw '-Remote 주소에 사용자·비밀번호(u:p@)를 넣지 않는다 — 서버 키는 DOCFLOW_REMOTE_API_KEY로' }   # 화면에 찍히지 않게
            if (-not ($u.Scheme -eq 'https' -or ($u.Scheme -eq 'http' -and $u.Host -in @('127.0.0.1', 'localhost')))) {
                throw '-Remote 주소는 https://… 또는 SSH 터널 http://127.0.0.1:<포트>/v1 만 받는다 (평문으로 문서 글자를 보내지 않게)'
            }
        } elseif ($RemoteModel) { throw '-RemoteModel은 -Remote와 같이 쓴다' }
        if ($BaselineFrom) {   # 기준은 지난 결과에서 → 이번엔 로컬·원격 모델만
            if (-not ($Local -or $Remote)) { throw '-BaselineFrom은 -Local이나 -Remote와 같이 쓴다 (기준 재사용은 로컬·원격 모델 비교용)' }
            if (-not (Test-Path $BaselineFrom -PathType Leaf)) { throw "기준 결과가 없다: $BaselineFrom" }
            if (-not $Providers) { $Providers = '^(' + (@(if ($Local) { 'local-ollama' }) + @(if ($Remote) { 'remote' }) -join '|') + ')$' }
            if (-not $Prompts) { $Prompts = '^current$' }
        }
        if ($Remote -and -not $Prompts) { $Prompts = '^current$' }
        $providers = if ($Providers) { $Providers } else {
            $p = @('gpt-4\.1-mini') + @(if (-not $Remote) { 'gpt-4o-mini' }) + @(if ($Local) { 'local-ollama' }) + @(if ($Remote) { 'remote' })
            '^(' + ($p -join '|') + ')$'
        }
        # 걸러질 제공자·판을 promptfoo와 같은 규칙(id·label, 대소문자 구분)으로 미리 셈 → 키가 필요한가, 기준과 겹치는가 (plan.py)
        $planOut = & $py -I -X utf8 (Join-Path $here 'plan.py') "--providers=$providers" "--prompts=$Prompts" "--local-model=$LocalModel" "--remote-model=$($env:DOCFLOW_REMOTE_MODEL)"
        if ($LASTEXITCODE -ne 0) { throw '후보 거르기 계산(plan.py) 실패' }
        $plan = ($planOut -join "`n") | ConvertFrom-Json
        Write-Output ("후보: {0} · 판: {1}" -f ($plan.providers -join ', '), ($plan.prompts -join ', '))
        if (-not $plan.providers -or -not $plan.prompts) { throw '걸러진 후보나 지시문 판이 없다 — -Providers·-Prompts 정규식 확인' }
        if ($BaselineFrom -and $plan.baseline_overlap) {   # 기준을 다시 부르면 돈을 쓰고 결과는 gate.py가 버린다 → 부르기 전에 멈춤
            throw '-BaselineFrom인데 걸러진 후보·판이 기준(criteria.yaml baseline)과 겹친다 — -Providers·-Prompts에서 기준을 뺀다'
        }
        # OpenAI·원격으로 글자를 보내는 후보 → 고른 철이 모두 합성 철이어야 (2026-11-20 #80 결정).
        # plan.py의 거르기 흉내가 promptfoo와 갈려 external이 거짓으로 나와도, -Remote나 OpenAI 키가 필요한 후보가 있으면 늘 확인한다
        if ($plan.external -or $Remote -or $plan.need_key) {
            & $py -I -X utf8 (Join-Path $here 'common.py') --require-synthetic
            if ($LASTEXITCODE -ne 0) { throw '외부 후보(OpenAI·remote)는 합성 철에만 보낸다 — -Work로 합성 work 폴더를 고르거나 -Local만 쓴다' }
        }
        if ($Remote -and ($plan.providers -contains 'remote')) {   # 부르기 전에 서버·모델 확인 (키는 헤더로만, 찍지 않음)
            $hdr = @{ Authorization = "Bearer $remoteKey" }
            try { $models = (Invoke-WebRequest "$($env:DOCFLOW_REMOTE_BASE)/models" -Headers $hdr -UseBasicParsing -TimeoutSec 15).Content | ConvertFrom-Json }
            catch { throw "원격 서버가 응답하지 않는다: $($env:DOCFLOW_REMOTE_BASE)/models ($($_.Exception.Message))" }
            if ($RemoteModel -notin @($models.data | ForEach-Object { $_.id })) { throw "원격 서버에 모델 $RemoteModel 이 없다 (있는 것: $(@($models.data.id) -join ', '))" }
            Write-Output "원격: $($env:DOCFLOW_REMOTE_BASE) 모델 $RemoteModel"
        }
        if ($plan.need_key) {   # 키는 OpenAI 후보가 걸러질 때만 (로컬 모델만이면 키를 읽지도 넣지도 않는다)
            $key = if ($prevKey) { $prevKey } else { [Environment]::GetEnvironmentVariable('OPENAI_API_KEY', 'User') }
            if (-not $key) { throw 'OPENAI_API_KEY가 없다 — Windows 사용자 환경변수에 넣는다' }
        }
        if ($BaselineFrom) {
            # 기준 파일은 사본을 gate.py에 넘긴다 — 원본이 이번 실행의 results.json이든 실행 중 바뀌든 기준이 지워지거나 섞이지 않게
            $baseDir = Join-Path $out (".baseline-{0}" -f $PID)
            New-Item -ItemType Directory -Force $baseDir | Out-Null
            $baseCopy = Join-Path $baseDir (Split-Path $BaselineFrom -Leaf)
            Copy-Item -LiteralPath $BaselineFrom -Destination $baseCopy -Force
        }
    }
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
    Remove-Item $res -ErrorAction SilentlyContinue   # 실패한 실행이 지난 결과로 판정되지 않게
    $args_ = @('eval', '-c', (Join-Path $here 'promptfooconfig.yaml'), '-o', $res, '--filter-providers', $providers,
               '--no-share', '--no-table', '--no-progress-bar')
    if ($Prompts) { $args_ += @('--filter-prompts', $Prompts) }
    if (-not $Cache) { $args_ += '--no-cache' }
    if ($Concurrency -gt 0) { $args_ += @('-j', "$Concurrency") }
    elseif ($Local) { $args_ += @('-j', '1') }   # GPU·CPU 한 대에서 도는 로컬 모델은 하나씩 (OpenAI 후보도 같이 느려진다)
    Push-Location $here
    if ($key) { $env:OPENAI_API_KEY = $key }   # promptfoo를 부르는 동안만
    # 원격 제공자 apiKeyEnvar — -Remote일 때만 실제 키, 아니면 자리 값 'none' (비우면 promptfoo가 OPENAI_API_KEY로 떨어질 수 있다)
    $env:DOCFLOW_REMOTE_API_KEY = if ($Remote) { $remoteKey } else { 'none' }
    $t0 = Get-Date
    try {
        Invoke-Promptfoo @args_
        $pcode = $LASTEXITCODE
    } finally {
        Remove-Item Env:OPENAI_API_KEY -ErrorAction SilentlyContinue
        Remove-Item Env:DOCFLOW_REMOTE_API_KEY -ErrorAction SilentlyContinue
        Write-Output ("promptfoo 실행 시간: {0:N0}초" -f ((Get-Date) - $t0).TotalSeconds)   # 원격 GPU 비용 = 이 시간 × 시간당 가격
        Pop-Location
    }
    # promptfoo는 단언 실패가 있으면 종료 코드 100 — 판정은 gate.py가 한다. 그 밖의 코드는 실행 자체가 실패
    if (($pcode -ne 0 -and $pcode -ne 100) -or -not (Test-Path $res)) { Write-Output "promptfoo 실패 (종료 코드 $pcode)"; exit 2 }
    $gate = @((Join-Path $here 'gate.py'), $res, '--out', $out)
    if ($Replay) { $gate += '--expect-stored' }
    if ($baseDir) { $gate += @('--baseline-results', $baseCopy) }
    Push-Location $app
    & $py -I -X utf8 @gate
    $code = $LASTEXITCODE
    Pop-Location
    Write-Output "결과: $out\gate.md · promptfoo 화면: .\eval\promptfoo\run.ps1 -View"
    if ($Remote) { Write-Output '원격 GPU: 끝났으면 파드 정지(/moni-pod:gpu-stop)와 볼륨 삭제를 확인하고 #80에 기록한다' }
    exit $code
} finally {
    # 키를 원래대로 (이 스크립트를 부른 셸에 있던 값이면 되살린다)
    if ($prevKey) { $env:OPENAI_API_KEY = $prevKey } else { Remove-Item Env:OPENAI_API_KEY -ErrorAction SilentlyContinue }
    if ($prevRemoteKey) { $env:DOCFLOW_REMOTE_API_KEY = $prevRemoteKey } else { Remove-Item Env:DOCFLOW_REMOTE_API_KEY -ErrorAction SilentlyContinue }
    if ($baseDir) { Remove-Item -LiteralPath $baseDir -Recurse -Force -ErrorAction SilentlyContinue }
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
} finally {   # 바꾼 DOCFLOW_* 환경변수를 시작 값으로 (맨 위 try)
    foreach ($n in $envNames) { [Environment]::SetEnvironmentVariable($n, $envSaved[$n], 'Process') }
}
