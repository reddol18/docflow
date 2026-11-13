# run.ps1이 점 소싱한다. package-lock.json(overrides 포함)이 바뀌었으면 node_modules를 npm ci로 다시 깐다.
# 전에는 promptfoo 진입 파일만 있으면 건너뛰어, 이미 깐 PC에 lock 갱신(보안 판 올림)이 닿지 않았다 (https://github.com/reddol18/docflow/issues/123).
# 표식: node_modules/.lock-sha256 = 마지막으로 성공한 npm ci 때의 lock SHA256. 표식이 없거나 다르면 다시 깐다.
function Sync-NodeModules {
    param(
        [Parameter(Mandatory)][string]$Dir,
        [scriptblock]$Install = { npm ci --no-audit --no-fund }   # 시험에서 바꿔 끼운다
    )
    $lock = Join-Path $Dir 'package-lock.json'
    $stamp = Join-Path $Dir 'node_modules\.lock-sha256'
    $entry = Join-Path $Dir 'node_modules\promptfoo\dist\src\entrypoint.js'
    $want = (Get-FileHash $lock -Algorithm SHA256).Hash
    if ((Test-Path $entry) -and (Test-Path $stamp) -and ((Get-Content $stamp -Raw).Trim() -eq $want)) { return $false }
    Push-Location $Dir
    try {
        $global:LASTEXITCODE = 0
        & $Install | Out-Host
        if ($LASTEXITCODE) { throw "npm ci 실패 (종료 코드 $LASTEXITCODE)" }
    } finally { Pop-Location }
    if (-not (Test-Path $entry)) { throw "npm ci 뒤에도 promptfoo가 없음: $entry" }
    Set-Content -Path $stamp -Value $want -Encoding ascii   # npm ci가 node_modules를 지우고 새로 까므로 성공한 뒤에만 쓴다
    return $true
}
