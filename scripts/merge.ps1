<#
PR 병합은 이 스크립트로만 한다 (https://github.com/reddol18/docflow/issues/83). LLM 호출 없음(토큰 0).
병합 전에 PR 본문·댓글에서 절차 흔적을 확인하고, 하나라도 빠지면 병합하지 않고 빠진 것을 출력한다.
판정은 모두 `scripts/merge_gate.py`(규칙·공유 함수는 그 머리말), 이 파일은 감싸기만 한다.

  .\scripts\merge.ps1 84 -DryRun     # 확인만 (병합 안 함)
  .\scripts\merge.ps1 84             # 확인 → 묻고 → 확인한 head 그대로 squash 병합 (제목 끝 "(#84)")
  .\scripts\merge.ps1 84 -Yes        # 묻지 않고 한 번에 확인·병합 — 사용자가 이 PR 병합을 승인한 뒤에만
  .\scripts\merge.ps1 84 -NoStatus   # 판정 통과여도 commit status를 올리지 않음

판정 통과(열린 PR)면 -DryRun이어도 head 커밋에 docflow/review-gate status(success)를 올린다 (https://github.com/reddol18/docflow/issues/85).
main 브랜치 보호(`scripts/protect_main.ps1`)가 이것과 docflow/local-check(check.ps1이 올림)를 요구하므로,
웹 화면·`gh pr merge`로 병합하려 해도 이 판정이 먼저 있어야 한다. 거부면 아무것도 올리지 않는다.

확인하는 흔적: 로컬 검사 요약(PR head 커밋, 막는 검사 모두 통과, 건너뜀 거부 — 코드 없는 PR만 osv·semgrep·trivy 건너뜀 허용) ·
계약 리뷰 · 버그 리뷰 결과 댓글(댓글·리뷰 한 글 안에 이름과 '| 지적 | 반영 |' 표, 코드 없는 PR은 면제) ·
관련 이슈 보안 영향 칸에 체크가 있으면 보안 리뷰 결과 댓글.
묻고 병합할 때는 처음 판정한 head를 --expect-head로 넘긴다: 그 사이 push가 있으면 병합하지 않는다.
종료 코드: 0 통과(병합함 / -DryRun이면 확인만), 1 빠진 흔적·취소, 2 병합할 수 없음(이미 병합·닫힘·초안, head 바뀜, 없는 이슈 등).
#>
param(
    [Parameter(Mandatory = $true)][int]$PR,
    [switch]$DryRun,
    [switch]$Yes,
    [switch]$NoStatus
)
$ErrorActionPreference = "Stop"
$py = Join-Path $PSScriptRoot "merge_gate.py"
$prevEnc = [Console]::OutputEncoding
[Console]::OutputEncoding = [Text.Encoding]::UTF8  # 파이썬 출력(UTF-8)이 깨지지 않게
$env:PYTHONUTF8 = "1"
$st = @(if (-not $NoStatus) { "--status" })  # 판정 통과면 head에 docflow/review-gate status (https://github.com/reddol18/docflow/issues/85)
$code = 1
try {
    if ($Yes -and -not $DryRun) {
        uv run --quiet --no-project python -I $py $PR --merge @st  # 판정 한 번, 판정한 head 그대로 병합
        $code = $LASTEXITCODE
    } else {
        $out = @(uv run --quiet --no-project python -I $py $PR @st)
        $code = $LASTEXITCODE
        $out | ForEach-Object { Write-Host $_ }
        $head = ($out | Where-Object { $_ -match "^head ([0-9a-f]{40})," } | Select-Object -First 1) -replace "^head ([0-9a-f]{40}),.*", '$1'
        if ($code -ne 0) {
            # 빠진 것·상태는 merge_gate.py가 출력했다
        } elseif ($DryRun) {
            Write-Host "(-DryRun: 병합하지 않음)"
        } elseif (-not $head) {
            Write-Host "판정 출력에서 head 커밋을 찾지 못했습니다. 병합하지 않습니다." -ForegroundColor Red
            $code = 2
        } elseif ((Read-Host "PR #$PR (head $($head.Substring(0, 7)))을 squash 병합할까요? (y/N)") -eq "y") {
            uv run --quiet --no-project python -I $py $PR --merge --expect-head $head @st
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