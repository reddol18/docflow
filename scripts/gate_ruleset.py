"""main ruleset에 병합 게이트 commit status 규칙을 건다 (https://github.com/reddol18/docflow/issues/85). LLM 호출 없음(토큰 0).
보통은 `scripts/protect_main.ps1`로 부른다. 저장소 설정을 바꾸므로 사용자가 승인한 뒤에만 --dry-run 없이 돌린다.

classic branch protection이 아니라 이미 있는 ruleset("Protect Main": PR 필수·삭제 금지·force push 금지, 우회 없음)에
`required_status_checks` 규칙 하나를 더해 한 곳에서 관리한다. 다른 규칙·조건·다른 우회 주체는 그대로 둔다.
몇 번 돌려도 같은 결과(멱등). 바뀔 것이 없으면 PUT 하지 않는다.

  필수 status   docflow/local-check, docflow/review-gate (올린 곳(앱)은 묻지 않는다 — gh 토큰으로 올리는 commit status)
  strict        false (기본). true(--strict)면 main이 바뀔 때마다 PR을 main 위로 다시 올리고 로컬 검사를 다시 돌려야 한다
  관리자 우회   ruleset의 bypass_actors가 비어 있으면 저장소 주인도 우회 못 한다 (classic의 enforce_admins와 같음).
                기본은 이 스크립트가 넣는 관리자 우회(RepositoryRole admin)를 뺀다

비상 해제 (status 올리기가 고장 나 병합이 다 막혔을 때 — ruleset 변경은 관리자가 언제든 할 수 있다)
  --allow-admin-bypass   관리자 우회를 넣는다 (규칙은 그대로, 주인만 웹 화면에서 우회 가능)
  --remove-status-rule   status 규칙을 뺀다 (다른 규칙은 그대로)
고친 뒤 인자 없이 다시 돌리면 원래대로 돌아간다.
비교는 GitHub 응답의 모양 차이(빈 parameters, status 규칙의 기본값·순서·빈 integration_id, actor_name)를 줄여서 한다.
종료 코드: 0 성공(바뀐 것 없음 포함. PUT 뒤 다시 읽기만 실패하면 경고하고 0),
           2 ruleset을 찾지 못함·gh 오류·PUT 뒤 다시 읽은 내용이 바꿀 것과 다름.
"""
import argparse
import copy
import difflib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location("merge_gate", Path(__file__).with_name("merge_gate.py"))
mg = importlib.util.module_from_spec(_spec)  # python -I로 돌리므로 import 경로 대신 파일로 불러온다
_spec.loader.exec_module(mg)

REPO = mg.REPO
CONTEXTS = (mg.CONTEXT_CHECK, mg.CONTEXT_REVIEW)
ADMIN_BYPASS = {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}  # 5 = 저장소 admin 역할
PUT_KEYS = ("name", "target", "enforcement", "conditions", "rules", "bypass_actors")


def gh(*args, stdin=None):
    r = subprocess.run(("gh", *args), input=stdin, capture_output=True, text=True, encoding="utf-8")  # noqa: S603 — 고정된 gh 인자
    if r.returncode:
        raise SystemExit(f"gh {' '.join(args[:3])} 실패: {r.stderr.strip()[:500]}")
    return r.stdout


def status_rule(strict=False):
    return {"type": "required_status_checks", "parameters": {
        "strict_required_status_checks_policy": strict, "do_not_enforce_on_create": False,
        "required_status_checks": [{"context": c} for c in CONTEXTS]}}


def _is_admin_bypass(a):
    return a.get("actor_type") == ADMIN_BYPASS["actor_type"] and a.get("actor_id") == ADMIN_BYPASS["actor_id"]


def desired(ruleset, strict=False, remove_status_rule=False, allow_admin_bypass=False):
    """지금 ruleset → PUT할 본문. 네트워크 없이 계산만 한다"""
    body = {k: copy.deepcopy(ruleset.get(k)) for k in PUT_KEYS}
    rules = [r for r in body["rules"] or [] if r.get("type") != "required_status_checks"]
    if not remove_status_rule:
        rules.append(status_rule(strict))
    body["rules"] = rules
    bypass = [a for a in body["bypass_actors"] or [] if not _is_admin_bypass(a)]
    if allow_admin_bypass:
        bypass.append(dict(ADMIN_BYPASS))
    body["bypass_actors"] = bypass
    return body


def _norm_rule(r):
    """규칙 하나의 비교용 모양: 빈 parameters를 없애고, status 규칙은 기본값을 채우고 순서·빈 integration_id를 없앤다"""
    r = {k: v for k, v in r.items() if not (k == "parameters" and not v)}
    if r.get("type") == "required_status_checks":
        p = dict(r.get("parameters") or {})
        p.setdefault("strict_required_status_checks_policy", False)
        p.setdefault("do_not_enforce_on_create", False)
        checks = [{k: v for k, v in c.items() if not (k == "integration_id" and v is None)}
                  for c in p.get("required_status_checks") or []]
        p["required_status_checks"] = sorted(checks, key=lambda c: json.dumps(c, sort_keys=True))
        r["parameters"] = p
    return r


def _norm(body):
    """비교용: GitHub이 돌려주는 기본값·순서·모양 차이를 줄인다 — 같은 내용을 다시 PUT하지 않게 (issues/106)"""
    b = copy.deepcopy(body)
    actors = [{k: v for k, v in a.items() if k != "actor_name"} for a in b.get("bypass_actors") or []]
    b["rules"] = sorted((_norm_rule(r) for r in b.get("rules") or []),
                        key=lambda r: (r.get("type", ""), json.dumps(r, sort_keys=True)))
    b["bypass_actors"] = sorted(actors, key=lambda a: json.dumps(a, sort_keys=True))
    return b


def diff(cur, new):
    a = json.dumps(_norm(cur), indent=2, ensure_ascii=False, sort_keys=True).splitlines()
    b = json.dumps(_norm(new), indent=2, ensure_ascii=False, sort_keys=True).splitlines()
    return list(difflib.unified_diff(a, b, "지금", "바꿀 것", lineterm=""))


def find(name):
    rs = json.loads(gh("api", f"repos/{REPO}/rulesets"))
    hit = [r for r in rs if r.get("name") == name and r.get("target") == "branch"]
    if len(hit) != 1:
        raise SystemExit(f"branch ruleset '{name}'이 {len(hit)}개 (하나여야 함)")
    return json.loads(gh("api", f"repos/{REPO}/rulesets/{hit[0]['id']}"))


def summary(rs):
    st = next((r for r in rs.get("rules") or [] if r.get("type") == "required_status_checks"), None)
    ctx = [c["context"] for c in st["parameters"]["required_status_checks"]] if st else []
    strict = st["parameters"].get("strict_required_status_checks_policy") if st else None
    return (f"ruleset '{rs['name']}' (id {rs['id']}, {rs['enforcement']}) 규칙: "
            f"{', '.join(r['type'] for r in rs.get('rules') or [])} | 필수 status: {', '.join(ctx) or '없음'}"
            f"{f' (strict {strict})' if st else ''} | 우회: {len(rs.get('bypass_actors') or [])}명"
            f"{' (관리자 우회 있음)' if any(_is_admin_bypass(a) for a in rs.get('bypass_actors') or []) else ''}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="Protect Main")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--remove-status-rule", action="store_true")
    ap.add_argument("--allow-admin-bypass", action="store_true")
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        cur = find(a.name)
        print("지금: " + summary(cur))
        new = desired(cur, a.strict, a.remove_status_rule, a.allow_admin_bypass)
        d = diff({k: cur.get(k) for k in PUT_KEYS}, new)
        if not d:
            print("바뀔 것 없음 (이미 그 설정).")
            return 0
        print("\n".join(d))
        if a.dry_run:
            print("(--dry-run: 바꾸지 않음)")
            return 0
        gh("api", "-X", "PUT", f"repos/{REPO}/rulesets/{cur['id']}", "--input", "-", stdin=json.dumps(new))
    except SystemExit as e:
        print(f"실패: {e}")
        return 2
    try:  # PUT은 성공했다 — 다시 읽기 실패는 '실패'가 아니다 (issues/106)
        got = find(a.name)
    except SystemExit as e:
        print(f"바꿈 (PUT 성공). 다시 읽어 확인하지 못함: {e} — `--dry-run`으로 다시 확인한다")
        return 0
    if diff({k: got.get(k) for k in PUT_KEYS}, new):
        print("PUT은 성공했지만 다시 읽은 ruleset이 바꿀 것과 다름: " + summary(got))
        return 2
    print("바꿈. 지금: " + summary(got))
    return 0


if __name__ == "__main__":
    sys.exit(main())
