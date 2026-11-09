<#
main ruleset에 병합 게이트 commit status를 필수로 건다 (https://github.com/reddol18/docflow/issues/85). LLM 호출 없음(토큰 0).
저장소 설정을 바꾸는 스크립트다. 사용자가 승인한 뒤에만 -DryRun 없이 돌린다. 계산·적용은 `scripts/gate_ruleset.py`(설명은 그 머리말).

이미 있는 ruleset "Protect Main"(PR 필수·삭제 금지·force push 금지, 우회 없음)에 required_status_checks 규칙
(`docflow/local-check`, `docflow/review-gate`, strict 끔)을 더한다. 다른 규칙은 그대로, 몇 번 돌려도 같다.
우회 주체가 없으므로 저장소 주인도 status 없이는 병합 못 한다 (classic의 enforce_admins와 같음).

  .\scripts\protect_main.ps1 -DryRun                 # 지금 설정과 바뀔 차이만 출력
  .\scripts\protect_main.ps1                         # 건다 (비상 해제를 되돌릴 때도 이것)
  .\scripts\protect_main.ps1 -Strict                 # main이 바뀌면 PR을 다시 올려야 병합 (기본 끔)

비상 해제 (status 올리기가 고장 나 병합이 다 막혔을 때 — ruleset 변경은 관리자가 언제든 할 수 있다)
  .\scripts\protect_main.ps1 -AllowAdminBypass       # 관리자(주인)만 우회 허용, 규칙은 그대로
  .\scripts\protect_main.ps1 -RemoveStatusRule       # status 규칙만 뺀다 (PR 필수 등은 그대로)
  (스크립트 없이) GitHub 웹 Settings → Rules → Rulesets → Protect Main에서 Bypass list에 Repository admin 추가
고친 뒤 인자 없이 다시 돌린다.
#>
param(
    [switch]$DryRun,
    [switch]$Strict,
    [switch]$AllowAdminBypass,
    [switch]$RemoveStatusRule
)
$ErrorActionPreference = "Stop"
$py = Join-Path $PSScriptRoot "gate_ruleset.py"
$a = @()
if ($DryRun) { $a += "--dry-run" }
if ($Strict) { $a += "--strict" }
if ($AllowAdminBypass) { $a += "--allow-admin-bypass" }
if ($RemoveStatusRule) { $a += "--remove-status-rule" }
$prevEnc = [Console]::OutputEncoding
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"
try {
    uv run --quiet --no-project python -I $py @a
    $code = $LASTEXITCODE
} finally {
    [Console]::OutputEncoding = $prevEnc
}
exit $code
