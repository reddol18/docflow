"""병합 게이트를 GitHub 쪽에서도 강제하는 commit status (https://github.com/reddol18/docflow/issues/85). LLM 호출 없음(토큰 0).
클라우드 CI가 없으므로 로컬에서 판정하고 결과만 커밋에 status로 올린다. main ruleset(`scripts/protect_main.ps1`)이
두 status를 병합 필수 조건으로 건다 — 웹 화면·`gh pr merge`로 병합해도 둘 다 success가 아니면 막힌다.

  docflow/local-check   `scripts/check.ps1`이 끝에서 이 파일의 `local-check`를 부른다 (따로 부르지 않는다)
  docflow/review-gate   `scripts/merge_gate.py --merge`(= `scripts/merge.ps1`, 사용자 승인 뒤)가 병합 직전에 올린다

docflow/local-check를 올리는 조건 (하나라도 아니면 올리지 않는다 — 실패를 'failure'로 올리지도 않는다):
  - 검사를 시작할 때와 끝날 때 HEAD가 같고, 둘 다 커밋 안 된 변경(추적 안 된 파일 포함)이 없다.
    `git status`가 실패하면 변경이 있는 것으로 본다
  - 막는 검사(`merge_gate.CHECKS` — check.ps1이 넘기는 값이 아니라 이 목록으로 정한다. trivy는 issues/29부터 포함)가
    모두 돌았고 통과. 같은 검사 이름이 두 번 오면 거부
  - 건너뜀 없음. 단 코드 없는 PR(`git diff origin/main...HEAD` 파일이 weekly_metrics.is_no_code)은
    osv·semgrep·trivy 건너뜀(-Quick) 허용 — merge_gate의 판정과 같은 규칙. pytest 안의 `-Skip pg`도 건너뜀으로 본다
  - HEAD가 GitHub에 있다(push함). 없는 커밋이면 GitHub이 거부(422)하므로 그것으로 안다
status는 커밋(sha)에 붙으므로 push로 head가 바뀌면 새 head에는 둘 다 없다 — 다시 검사·판정해야 한다.

  uv run --quiet --no-project python -I scripts/gate_status.py local-check --start-sha <sha> [--start-dirty]
      --at "2026-11-09 21:00" --result frontend=pass --result trivy=skip ... [--skip-opt pg]
종료 코드: 0 올림, 1 조건이 안 맞아 올리지 않음(이유를 출력), 2 올리기 실패(gh 오류).
"""
import argparse
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location("merge_gate", Path(__file__).with_name("merge_gate.py"))
mg = importlib.util.module_from_spec(_spec)  # python -I로 돌리므로 import 경로 대신 파일로 불러온다
_spec.loader.exec_module(mg)
wm = mg.wm

CONTEXT_CHECK = mg.CONTEXT_CHECK
ROOT = Path(__file__).resolve().parent.parent
STATES = ("pass", "fail", "skip")


def git(*args):
    """→ (종료 코드, 출력). 시험에서 바꾼다"""
    r = subprocess.run(("git", *args), capture_output=True, text=True, encoding="utf-8", cwd=ROOT)  # noqa: S603 — 고정된 git 인자
    return r.returncode, r.stdout


def parse_result(s):
    """'frontend=pass' → ('frontend', 'pass')"""
    name, _, state = s.partition("=")
    if not name or state not in STATES:
        raise argparse.ArgumentTypeError(f"결과는 이름=pass|fail|skip: {s}")
    return name, state


def decide(results, *, start_sha, head_sha, start_dirty, end_dirty, no_code, skip_opts=()):
    """→ (올림?, 이유). results: [(이름, 'pass'|'fail'|'skip')]. 네트워크·git 없이 판정만 한다 (push 여부는 올릴 때 안다)"""
    if not head_sha or head_sha != start_sha:
        return False, f"검사 중에 HEAD가 바뀜 ({start_sha[:7]} → {head_sha[:7] or '?'})"
    if start_dirty or end_dirty:
        return False, "커밋 안 된 변경이 있음 (커밋한 뒤 다시 돌린다)"
    names = [n for n, _ in results]
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        return False, f"같은 검사 결과가 두 번: {', '.join(dup)}"
    by = dict(results)
    failed = [c for c in mg.CHECKS if by.get(c) == "fail"]
    if failed:
        return False, f"막는 검사 실패: {', '.join(failed)}"
    missing = [c for c in mg.CHECKS if c not in by]
    if missing:
        return False, f"검사 결과 없음: {', '.join(missing)}"
    allowed = mg.NO_CODE_SKIP if no_code else set()
    skipped = [n for n, s in results if s == "skip" and n not in allowed] + [f"{o}(옵션)" for o in skip_opts]
    if skipped:
        tail = "" if no_code else " — 코드 없는 PR만 osv·semgrep·trivy 건너뜀 허용"
        return False, f"건너뜀: {', '.join(skipped)}{tail}"
    quick = sorted(n for n, s in results if s == "skip")
    return True, "막는 검사 모두 통과" + (f" (코드 없는 PR, 건너뜀 {'·'.join(quick)})" if quick else "")


def changed_files():
    """main에서 갈라진 뒤 바뀐 파일 (로컬 origin/main 기준 — 오래됐으면 파일이 더 많아져 '코드 있음' 쪽으로 엄격해진다).
    못 구하면 빈 목록 → 코드 있음"""
    code, out = git("diff", "--name-only", "origin/main...HEAD")
    return [ln for ln in out.splitlines() if ln.strip()] if code == 0 else []


def local_check(a):
    code, head = git("rev-parse", "HEAD")
    head = head.strip() if code == 0 else ""
    code, st = git("status", "--porcelain")
    end_dirty = code != 0 or bool(st.strip())  # git status가 실패하면 변경 있음으로 (fail-closed)
    ok, why = decide(a.result, start_sha=a.start_sha, head_sha=head, start_dirty=a.start_dirty, end_dirty=end_dirty,
                     no_code=wm.is_no_code(changed_files()), skip_opts=a.skip_opt)
    if not ok:
        print(f"commit status {CONTEXT_CHECK} 올리지 않음: {why}")
        return 1
    desc = f"check.ps1 {why} · {a.at}"
    try:
        mg.post_status(head, CONTEXT_CHECK, desc)
    except SystemExit as e:
        if re.search(r"422|No commit found", str(e)):
            print(f"commit status {CONTEXT_CHECK} 올리지 않음: HEAD {head[:7]}가 GitHub에 없음 (push한 뒤 다시 돌린다)")
            return 1
        print(f"commit status {CONTEXT_CHECK} 올리기 실패: {e}")
        return 2
    print(f"commit status {CONTEXT_CHECK} = success 올림 ({head[:7]}): {desc}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    lc = sub.add_parser("local-check", help="check.ps1 결과로 docflow/local-check를 올린다 (조건이 맞을 때만)")
    lc.add_argument("--start-sha", required=True, help="검사를 시작할 때의 HEAD (전체 sha)")
    lc.add_argument("--start-dirty", action="store_true", help="검사를 시작할 때 커밋 안 된 변경이 있었음(또는 확인 실패)")
    lc.add_argument("--at", required=True, help="요약에 찍은 시각")
    lc.add_argument("--result", type=parse_result, action="append", default=[], help="이름=pass|fail|skip")
    lc.add_argument("--skip-opt", action="append", default=[], help="단계가 아닌 -Skip 옵션 (예: pg)")
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    return local_check(a)


if __name__ == "__main__":
    sys.exit(main())
