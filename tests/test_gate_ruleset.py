"""scripts/gate_ruleset.py — main ruleset에 status 규칙 더하기 (https://github.com/reddol18/docflow/issues/85).
ruleset은 지금 저장소의 "Protect Main"을 본뜬 것, 네트워크·gh 없음."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

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


# ---- 병합 게이트 보강 (https://github.com/reddol18/docflow/issues/106)

def github_shape(body):
    """GitHub이 돌려주는 모양: 빈 parameters, status 규칙 기본값 빠짐·순서 다름·integration_id null, actor_name"""
    b = copy.deepcopy(body)
    for r in b["rules"]:
        if r["type"] == "deletion":
            r["parameters"] = {}
        if r["type"] == "required_status_checks":
            p = r["parameters"]
            p.pop("do_not_enforce_on_create")
            p["required_status_checks"] = [{"context": c["context"], "integration_id": None}
                                           for c in reversed(p["required_status_checks"])]
    b["rules"] = list(reversed(b["rules"]))
    b["bypass_actors"] = [{**a, "actor_name": "admin"} for a in b["bypass_actors"]]
    return b


class FakeGh:
    """gh api 대역 — 실제 ruleset을 바꾸지 않는다"""

    def __init__(self, current, after=None, reread_fails=False):
        self.current, self.after, self.reread_fails, self.calls, self.reads = current, after, reread_fails, [], 0

    def __call__(self, *args, stdin=None):
        self.calls.append(args)
        if args[1:3] == ("-X", "PUT"):
            self.current = self.after if self.after is not None else {**self.current, **json.loads(stdin)}
            return "{}"
        if args[1].endswith("/rulesets"):
            return json.dumps([{"id": self.current["id"], "name": self.current["name"], "target": "branch"}])
        self.reads += 1
        if self.reread_fails is True and self.reads > 1:
            raise SystemExit("gh api repos/x/rulesets/1 실패: HTTP 502")
        if isinstance(self.reread_fails, str) and self.reads > 1:  # gh는 됐는데 응답이 이상함
            return self.reread_fails
        return json.dumps(self.current)

    @property
    def puts(self):
        return [c for c in self.calls if c[1:3] == ("-X", "PUT")]


def test_github_response_shape_does_not_reput(monkeypatch, capsys):
    """7: 이미 적용된 ruleset을 GitHub 모양으로 읽어도 바뀔 것 없음 — 다시 PUT하지 않는다"""
    applied = {**CURRENT, **gr.desired({**CURRENT, "bypass_actors": [gr.ADMIN_BYPASS]}, allow_admin_bypass=True)}
    shaped = github_shape(applied)
    assert gr.diff({k: shaped.get(k) for k in gr.PUT_KEYS}, gr.desired(shaped, allow_admin_bypass=True)) == []
    fake = FakeGh(shaped)
    monkeypatch.setattr(gr, "gh", fake)
    assert gr.main(["--allow-admin-bypass"]) == 0 and fake.puts == []
    assert "바뀔 것 없음" in capsys.readouterr().out


def test_real_change_still_detected_after_norm():
    shaped = github_shape({**CURRENT, **gr.desired(CURRENT)})
    assert gr.diff({k: shaped.get(k) for k in gr.PUT_KEYS}, gr.desired(shaped, strict=True)) != []


def test_reread_failure_after_put_is_not_failure(monkeypatch, capsys):
    """7: PUT은 성공, 다시 읽기만 실패 → 경고하고 0"""
    fake = FakeGh(copy.deepcopy(CURRENT), reread_fails=True)
    monkeypatch.setattr(gr, "gh", fake)
    assert gr.main([]) == 0 and len(fake.puts) == 1
    out = capsys.readouterr().out
    assert "PUT 성공" in out and not any(ln.startswith("실패:") for ln in out.splitlines())


def test_reread_after_put_differs_is_failure(monkeypatch, capsys):
    fake = FakeGh(copy.deepcopy(CURRENT), after=copy.deepcopy(CURRENT))  # PUT이 먹지 않음
    monkeypatch.setattr(gr, "gh", fake)
    assert gr.main([]) == 2 and len(fake.puts) == 1
    assert "다시 읽은 ruleset이 바꿀 것과 다름" in capsys.readouterr().out


def test_put_then_reread_ok(monkeypatch, capsys):
    fake = FakeGh(copy.deepcopy(CURRENT))
    monkeypatch.setattr(gr, "gh", fake)
    assert gr.main([]) == 0 and len(fake.puts) == 1
    assert "바꿈. 지금:" in capsys.readouterr().out


def test_put_failure_is_failure(monkeypatch, capsys):
    fake = FakeGh(copy.deepcopy(CURRENT))

    def gh(*args, stdin=None):
        if args[1:3] == ("-X", "PUT"):
            raise SystemExit("gh api -X PUT 실패: HTTP 403")
        return fake(*args, stdin=stdin)
    monkeypatch.setattr(gr, "gh", gh)
    assert gr.main([]) == 2
    assert "실패:" in capsys.readouterr().out


@pytest.mark.parametrize("reply", ["not json", "{}", '{"name": "Protect Main"}', "[]"])
def test_reread_bad_response_after_put_is_warning(monkeypatch, capsys, reply):
    """C3: PUT 뒤 다시 읽은 응답이 JSON이 아니거나 모양이 달라도(ValueError·KeyError·…) 경고하고 0"""
    fake = FakeGh(copy.deepcopy(CURRENT), reread_fails=reply)
    monkeypatch.setattr(gr, "gh", fake)
    assert gr.main([]) == 0 and len(fake.puts) == 1
    assert "PUT 성공" in capsys.readouterr().out
