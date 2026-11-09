"""scripts/gate_ruleset.py — main ruleset에 status 규칙 더하기 (https://github.com/reddol18/docflow/issues/85).
ruleset은 지금 저장소의 "Protect Main"을 본뜬 것, 네트워크·gh 없음."""
import copy
import importlib.util
from pathlib import Path

_p = Path(__file__).parent.parent / "scripts" / "gate_ruleset.py"
_spec = importlib.util.spec_from_file_location("gate_ruleset", _p)
gr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gr)

PR_RULE = {"type": "pull_request", "parameters": {"required_approving_review_count": 0,
                                                  "required_review_thread_resolution": True,
                                                  "allowed_merge_methods": ["merge", "squash", "rebase"]}}
CURRENT = {"id": 26504223, "name": "Protect Main", "target": "branch", "enforcement": "active",
           "conditions": {"ref_name": {"exclude": [], "include": ["~DEFAULT_BRANCH"]}},
           "rules": [{"type": "deletion"}, {"type": "non_fast_forward"}, PR_RULE],
           "bypass_actors": [], "node_id": "x", "_links": {}}


def types(body):
    return [r["type"] for r in body["rules"]]


def test_adds_status_rule_and_keeps_others():
    new = gr.desired(CURRENT)
    assert types(new) == ["deletion", "non_fast_forward", "pull_request", "required_status_checks"]
    assert new["rules"][2] == PR_RULE and new["conditions"] == CURRENT["conditions"]
    p = new["rules"][3]["parameters"]
    assert [c["context"] for c in p["required_status_checks"]] == ["docflow/local-check", "docflow/review-gate"]
    assert p["strict_required_status_checks_policy"] is False
    assert new["bypass_actors"] == []  # 우회 없음 = enforce_admins
    assert set(new) == set(gr.PUT_KEYS)  # id·링크 같은 읽기 전용 값은 보내지 않는다
    assert CURRENT["rules"][-1] is PR_RULE and len(CURRENT["rules"]) == 3  # 원본은 그대로


def test_idempotent():
    once = gr.desired(CURRENT)
    applied = {**CURRENT, **copy.deepcopy(once)}
    assert gr.desired(applied) == once
    assert gr.diff({k: applied.get(k) for k in gr.PUT_KEYS}, gr.desired(applied)) == []
    assert gr.diff({k: CURRENT.get(k) for k in gr.PUT_KEYS}, once) != []


def test_strict_replaces_existing_status_rule():
    applied = {**CURRENT, **gr.desired(CURRENT)}
    new = gr.desired(applied, strict=True)
    assert types(new).count("required_status_checks") == 1
    assert new["rules"][-1]["parameters"]["strict_required_status_checks_policy"] is True


def test_emergency_switches_and_restore():
    applied = {**CURRENT, **gr.desired(CURRENT)}
    bypass = gr.desired(applied, allow_admin_bypass=True)
    assert bypass["bypass_actors"] == [gr.ADMIN_BYPASS] and "required_status_checks" in types(bypass)
    removed = gr.desired(applied, remove_status_rule=True)
    assert types(removed) == ["deletion", "non_fast_forward", "pull_request"]
    # 인자 없이 다시 돌리면 원래대로 — 다른 우회 주체는 남긴다
    other = {"actor_id": 9, "actor_type": "Team", "bypass_mode": "pull_request"}
    back = gr.desired({**applied, "bypass_actors": [gr.ADMIN_BYPASS, other], "rules": removed["rules"]})
    assert back["bypass_actors"] == [other] and "required_status_checks" in types(back)
