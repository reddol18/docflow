<#
PR 병합은 이 스크립트로만 한다 (https://github.com/reddol18/docflow/issues/83). LLM 호출 없음(토큰 0).
병합 전에 PR 본문·댓글에서 절차 흔적을 확인하고, 하나라도 빠지면 병합하지 않고 빠진 것을 출력한다.
판정은 `scripts/merge_gate.py`(규칙은 `scripts/weekly_metrics.py`와 공유), 이 파일은 감싸기만 한다.

  .\scripts\merge.ps1 84 -DryRun     # 확인만 (병합 안 함)
  .\scripts\merge.ps1 84             # 확인 → 묻고 → squash 병합 (제목 끝 "(#84)")
  .\scripts\merge.ps1 84 -Yes        # 묻지 않음 — 사용자가 이 PR 병합을 승인한 뒤에만

확인하는 흔적: 로컬 검사 요약(PR head 커밋, 실패 없음) · 계약 리뷰 · 버그 리뷰 결과 댓글('| 지적 | 반영 |' 표,
코드 없는 PR은 면제) · 관련 이슈 보안 영향 칸에 체크가 있으면 보안 리뷰 결과 댓글.
병합은 확인한 head 커밋 그대로일 때만(--match-head-commit). 그 사이 push가 있으면 gh가 거절한다.
종료 코드: 0 통과(병합함 / -DryRun이면 확인만), 1 빠진 흔적·취소, 2 병합할 수 없는 상태(이미 병합·닫힘·초안).
#>
param(
    [Parameter(Mandatory = $true)][int]$PR,
    [switch]$DryRun,
    [switch]$Yes
)
$ErrorActionPreference = "Stop"
$py = Join-Path $PSScriptRoot "merge_gate.py"
$prevEnc = [Console]::OutputEncoding
[Console]::OutputEncoding = [Text.Encoding]::UTF8  # 파이썬 출력(UTF-8)이 깨지지 않게
$env:PYTHONUTF8 = "1"
$code = 1
try {
    uv run --quiet --no-project python -I $py $PR
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        Write-Host "병합하지 않습니다." -ForegroundColor Red
    } elseif ($DryRun) {
        Write-Host "(-DryRun: 병합하지 않음)"
    } else {
        $go = $Yes
        if (-not $go) { $go = (Read-Host "PR #$PR 을 squash 병합할까요? (y/N)") -eq "y" }
        if ($go) {
            uv run --quiet --no-project python -I $py $PR --merge  # 한 번 더 확인하고 그 head 커밋으로 병합
            $code = $LASTEXITCODE
        } else {
            Write-Host "취소했습니다."
            $code = 1
        }
    }
} finally {
    [Console]::OutputEncoding = $prevEnc
}
exit $code
