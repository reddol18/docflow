<#
main 브랜치 보호 — 병합 게이트 commit status를 필수로 건다 (https://github.com/reddol18/docflow/issues/85). LLM 호출 없음(토큰 0).
저장소 설정을 바꾸는 스크립트다. 사용자가 승인한 뒤에만 -DryRun 없이 돌린다. 몇 번 돌려도 같은 설정이 된다(PUT).

  .\scripts\protect_main.ps1 -DryRun                    # 지금 설정과 걸 설정(JSON)만 출력
  .\scripts\protect_main.ps1                            # 건다
  .\scripts\protect_main.ps1 -EnforceAdmins $false      # 관리자(저장소 주인)는 웹 화면에서 우회할 수 있게 (비상용)

거는 것
  필수 status   docflow/local-check (`scripts/check.ps1` → `scripts/gate_status.py`)
                docflow/review-gate (`scripts/merge.ps1` → `scripts/merge_gate.py --status`)
                올린 곳(앱)은 묻지 않는다 — gh 토큰으로 올리는 commit status라서
  strict        false (기본). true면 main이 바뀔 때마다 PR을 main 위로 다시 올리고(rebase/merge) 로컬 검사를 처음부터
                다시 돌려야 한다. 검사가 로컬에서만 돌고 병합은 한 사람이 차례로 하므로 기본은 끈다. -Strict로 켠다
  enforce_admins  true (기본) — 저장소 주인도 status 없이는 병합·push 못 한다. 끄지 않으면 주인은 웹 화면에서
                "Bypass"로 병합할 수 있어 보호가 반쪽이 된다
  리뷰 승인 요구 없음 (한 사람 저장소), push 제한 없음, force push 금지, 브랜치 삭제 금지

비상 복구 (status 올리기가 고장 나 병합이 다 막혔을 때 — 보호 설정 변경은 enforce_admins와 상관없이 관리자가 할 수 있다)
  .\scripts\protect_main.ps1 -EnforceAdmins $false      # 주인만 우회 허용 (필수 status는 그대로)
  gh api -X DELETE repos/reddol18/docflow/branches/main/protection/enforce_admins   # 같은 것, 이 파일 없이
  gh api -X DELETE repos/reddol18/docflow/branches/main/protection                  # 보호 전체 해제
고친 뒤 다시 `.\scripts\protect_main.ps1`로 건다.
#>
param(
    [switch]$DryRun,
    [bool]$EnforceAdmins = $true,
    [switch]$Strict,
    [string]$Repo = "reddol18/docflow",
    [string]$Branch = "main"
)
$ErrorActionPreference = "Stop"
$contexts = @("docflow/local-check", "docflow/review-gate")
$want = [ordered]@{
    required_status_checks        = [ordered]@{ strict = [bool]$Strict; checks = @($contexts | ForEach-Object { [ordered]@{ context = $_ } }) }
    enforce_admins                = $EnforceAdmins
    required_pull_request_reviews = $null
    restrictions                  = $null
    allow_force_pushes            = $false
    allow_deletions               = $false
}
$json = $want | ConvertTo-Json -Depth 6
$api = "repos/$Repo/branches/$Branch/protection"
$prevEnc = [Console]::OutputEncoding
[Console]::OutputEncoding = [Text.Encoding]::UTF8

function Show-Current {
    $ErrorActionPreference = "Continue"  # 보호가 없으면 gh가 404를 stderr로 낸다
    $cur = gh api $api --jq '{strict: .required_status_checks.strict, checks: [.required_status_checks.checks[]?.context], enforce_admins: .enforce_admins.enabled, reviews: (.required_pull_request_reviews != null), force_pushes: .allow_force_pushes.enabled, deletions: .allow_deletions.enabled}' 2>$null
    if ($LASTEXITCODE) { Write-Host "  (보호 없음 — $Branch)" } else { Write-Host "  $cur" }
}

try {
    Write-Host "지금 $Repo $Branch 보호:"
    Show-Current
    Write-Host "걸 설정 (PUT $api):"
    Write-Host $json
    if ($DryRun) { Write-Host "(-DryRun: 바꾸지 않음)"; exit 0 }
    $tmp = Join-Path $env:TEMP "docflow_protect_$PID.json"
    try {
        [IO.File]::WriteAllText($tmp, $json, (New-Object Text.UTF8Encoding $false))
        gh api -X PUT $api -H "Accept: application/vnd.github+json" --input $tmp | Out-Null
        if ($LASTEXITCODE) { Write-Host "보호를 걸지 못했습니다 (위 gh 오류)." -ForegroundColor Red; exit 2 }
    } finally { Remove-Item $tmp -ErrorAction SilentlyContinue }
    Write-Host "걸었습니다. 지금:" -ForegroundColor Green
    Show-Current
    exit 0
} finally {
    [Console]::OutputEncoding = $prevEnc
}
