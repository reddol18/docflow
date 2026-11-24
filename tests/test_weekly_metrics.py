"""scripts/weekly_metrics.py의 판정 함수 (gh·git 호출 없는 부분)."""
import importlib.util
from pathlib import Path

_p = Path(__file__).parent.parent / "scripts" / "weekly_metrics.py"
_spec = importlib.util.spec_from_file_location("weekly_metrics", _p)
wm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wm)


def test_template_lines_are_not_evidence():
    body = "\n".join([
        "- [x] 버그 리뷰 `/code-review` — 지적과 처리:",  # 체크만 함
        "- `security-reviewer` 판정 (미충족·판단 불가만):",  # 템플릿 그대로
        "- [ ] 계약 리뷰 `/spec-review` — 결과 요약:",
        "<!-- code-review -->",
    ])
    text = wm.evidence(body)
    assert not any(rx.search(text) for rx in wm.MARKS.values())
    filled = wm.evidence("- [x] 버그 리뷰 `/code-review` — 지적과 처리: 3건 고침")
    assert wm.MARKS["버그 리뷰"].search(filled)


def test_pct_excludes_not_applicable_and_counts_late():
    assert wm.pct([True, False, "규칙 전", "코드 없음", None, "늦게 #42"]) == "1/3 (늦게 1)"
    assert wm.pct([True, True]) == "2/2"
    assert wm.pct(["규칙 전", None]) == "–"


def test_non_code_paths():
    for f in ("docs/process/2026-W45.md", ".gitattributes", "frontend/package-lock.json", "other.lock"):
        assert wm.NON_CODE.search(f), f
    # 이미지가 그대로 설치하는 의존성 목록은 코드 (https://github.com/reddol18/docflow/issues/33)
    for f in ("requirements.lock", "requirements.txt", "requirements-test.txt",
              "app/auth.py", "scripts/check.ps1", "scripts/mac/deploy.sh", "k8s/docs.yaml", "security/asvs.yaml",
              ".github/pull_request_template.md", ".claude/agents/spec-reviewer.md"):  # 절차를 바꾸는 .md는 코드
        assert not wm.NON_CODE.search(f), f


def test_no_code_and_issue_refs_shared_with_merge_gate():
    assert wm.is_no_code(["docs/a.md", "README.md"])
    assert not wm.is_no_code(["docs/a.md", "app/main.py"])
    assert not wm.is_no_code([])
    assert not wm.is_no_code([f"docs/{i}.md" for i in range(wm.FILES_CAP)])  # 잘렸을 수 있으면 코드로
    body = ("Refs #13, #14\n<!-- Refs #99 -->\n본문의 #50 언급은 아님\n예: #76은 `Refs #22`로 (글 속 인용)\nCloses #7\n"
            "Refs: https://github.com/reddol18/docflow/issues/15\nRefs https://github.com/other/repo/issues/16\n")
    assert wm.body_refs(body) == {7, 13, 14, 15}  # 다른 저장소 URL·주석·글 속 인용은 뺌
    assert wm.issue_refs("기능 (#12)", body, [7]) == {7, 12, 13, 14, 15}
    assert wm.linked_issues("기능 (#12)", [7]) == {7, 12}  # 리드타임·'닫는 이슈' 칸은 Refs를 넣지 않는다


def test_review_fixes_counts_table_rows():
    text = "\n".join([
        "| 지적 | 반영 |", "|---|---|",
        "| 토큰이 명령줄에 | 헤더 파일로 |",
        "| 해시 고정 | 후속 https://github.com/o/r/issues/46 |",
        "| 경로 백틱 | https://github.com/o/r/issues/40 |",
        "| -Mac 형식 | 그대로 (신뢰 경계 안) |",
        "| useMe 401 | 아님: noRedirect |",
        "", "| 검사 | 결과 |", "|---|---|", "| ruff | 통과 |",  # 다른 표는 세지 않는다
    ])
    assert wm.review_fixes(text) == (1, 2, 2)


def test_criteria_met_skips_template_row():
    body = ("## 완료 조건별 결과\n| 완료 조건 | 결과 | 근거 |\n|---|---|---|\n"
            "| a | 충족 | x |\n| b | 일부 | y |\n| | 충족 / 일부 / 미충족 | |\n")
    assert wm.criteria_met(body) == (1, 2)


def test_extra_changes_and_scope_change():
    assert wm.extra_changes("## 범위 밖 변경\n<!-- 설명 -->\n없음\n\n## 테스트\n") is False
    assert wm.extra_changes("## 범위 밖 변경\n- PR 템플릿 주석 한 줄\n## 테스트\n") is True
    assert wm.extra_changes("## 범위 밖 변경\n<!-- 설명 -->\n\n## 테스트\n") is None  # 안 채움
    assert wm.extra_changes("본문에 그 절이 없음") is None
    assert wm.needs_security("## 보안 영향\r\n- [x] 인증·세션 변경\r\n\r\n## 관련\r\n")  # CRLF 이슈 본문
    assert not wm.needs_security("## 보안 영향\n- [ ] 인증·세션 변경\n")
    assert wm.scope_change("- **범위 변경:** PR 대신 파일로") is True
    assert wm.scope_change("## 범위 밖 변경\n- 주석 한 줄") is False  # 다른 개념


def test_bold_cells_and_bug_fields():
    body = "| 완료 조건 | 결과 |\n|---|---|\n| a | **충족** |\n| b | 해당 없음 |\n"
    assert wm.criteria_met(body) == (1, 1)
    assert wm.review_fixes("| 지적 | 반영 |\n|---|---|\n| x | **그대로** (이유) |\n") == (0, 0, 1)
    bug = "### 재현\n1\n\n### 발견 단계\n\n운영\n\n### 원인 PR\n\nhttps://github.com/o/r/pull/38\n"
    assert wm.bug_fields(bug) == ("운영", 38)
    assert wm.bug_fields("형식 없음") == (None, None)


def test_first_check_stops_at_table_end():
    s = ("로컬 검사 `scripts/check.ps1` — 커밋 `a`\n| 검사 | 결과 |\n|---|---|\n| ruff | 통과 |\n\n"
         "막는 검사 모두 통과\n- 수동: 로그인 **실패** 401 확인")
    assert wm.first_check_passed([s]) is True
    s2 = "로컬 검사 `scripts/check.ps1` — 커밋 `a`\n| 검사 | 결과 |\n|---|---|\n| ruff | **실패** |"
    assert wm.first_check_passed([s2]) is False


def test_first_check_passed_uses_first_summary():
    ok = "로컬 검사 `scripts/check.ps1` — 커밋 `abc`\n| ruff | 통과 |"
    bad = "로컬 검사 `scripts/check.ps1` — 커밋 `abc`\n| ruff | **실패** |"
    assert wm.first_check_passed(["본문", bad, ok]) is False
    assert wm.first_check_passed([ok, bad]) is True
    assert wm.first_check_passed(["요약 없음"]) is None


# ---- 실행 기록·병합 시점 본문·마일스톤 (https://github.com/reddol18/docflow/issues/55)

LOG3 = [  # check.ps1이 쓰는 꼴 (가짜 값)
    '{"at":"2026-11-24T10:00:00+09:00","branch":"feat/a","commit":"' + "a" * 40 + '","dirty":true,"quick":false,'
    '"skip":[],"results":{"ruff":"fail","pytest":"pass","osv":"skip"},"ok":false}',
    '{"at":"2026-11-24T10:05:00+09:00","branch":"feat/a","commit":"' + "b" * 40 + '","dirty":false,"quick":false,'
    '"skip":[],"results":{"ruff":"pass","pytest":"pass","osv":"skip"},"ok":true}',
    '{"at":"2026-11-24T11:00:00+09:00","branch":"feat/b","commit":"' + "c" * 40 + '","dirty":false,"quick":false,'
    '"skip":["pg"],"results":{"ruff":"pass","pytest":"pass"},"ok":true}',
]


def test_first_local_check_from_log_is_one_of_two():
    runs, bad = wm.parse_check_log(LOG3)
    assert bad == 0 and len(runs) == 3
    firsts = [wm.first_run_passed(runs, b) for b in ("feat/a", "feat/b")]
    assert firsts == [False, True]
    assert wm._ratio(firsts) == "1/2"
    assert wm.first_run_passed(runs, "feat/none") is None  # 기록 없음 → 분모에서 뺌


def test_first_run_respects_since_until_and_order():
    runs, _ = wm.parse_check_log(list(reversed(LOG3)) + ["깨진 줄", '{"at":"2026-11-24","branch":"x","results":{}}', ""])
    assert len(runs) == 3  # 시각대 없는 줄·깨진 줄은 뺀다
    assert wm.parse_check_log(["{}", "[1]", '{"at":null}'])[1] == 3
    t = wm.parse_ts
    assert wm.first_run_passed(runs, "feat/a") is False  # 줄 순서가 아니라 시각 순
    assert wm.first_run_passed(runs, "feat/a", since=t("2026-11-24T01:01:00Z")) is True  # 앞 PR 병합 뒤 실행만
    assert wm.first_run_passed(runs, "feat/b", until=t("2026-11-24T01:30:00Z")) is None  # 병합 뒤 실행은 뺌


def test_body_at_merge_ignores_later_edits():
    merged = wm.parse_ts("2026-11-04T13:42:01Z")
    edits = {"totalCount": 3, "nodes": [  # GraphQL은 최근 판이 먼저
        {"editedAt": "2026-11-04T14:12:08Z", "diff": "| 완료 조건 | 결과 |\n|---|---|\n| a | 충족 |\n| b | 충족 |"},
        {"editedAt": "2026-11-04T12:54:52Z", "diff": "| 완료 조건 | 결과 |\n|---|---|\n| a | 충족 |\n| b | 일부 |"},
        {"editedAt": "2026-11-04T12:47:52Z", "diff": "처음 본문"},
    ]}
    now_body = edits["nodes"][0]["diff"]
    assert wm.criteria_met(now_body) == (2, 2)
    assert wm.criteria_met(wm.body_at(now_body, edits, merged)) == (1, 2)  # 병합 시점 값
    assert wm.body_at("지금", None, merged) == "지금"  # 고친 적 없음
    assert wm.body_at("지금", {"totalCount": 0, "nodes": []}, merged) == "지금"
    assert wm.body_at("지금", {"totalCount": 200, "nodes": edits["nodes"][:1]}, merged) is None  # 잘려서 모름
    gone = {"totalCount": 2, "nodes": [{"editedAt": "2026-11-04T12:54:52Z", "diff": None}, edits["nodes"][2]]}
    assert wm.body_at("지금", gone, merged) is None  # 지운 판 → 부르는 쪽이 지금 본문으로


def test_first_check_value_falls_back_when_week_has_no_log():
    rows = [{"first_check": None, "first_check_pr": True}, {"first_check": None, "first_check_pr": False}]
    assert wm.first_check_value(rows, {}) == "1/2 (실행 기록 없음 — PR에 붙인 요약 기준)"
    rows[0]["first_check"] = False
    assert wm.first_check_value(rows, {"bad_log": 1}) == "0/1 (기록 없는 PR 1, PR에 붙인 요약 기준 1/2, 깨진 기록 줄 1)"


def test_milestone_due_is_sunday_2359_kst():
    _, start, _ = wm.week_range("2026-W48")
    assert wm.milestone_due(start) == "2026-11-29T14:59:59Z"  # 11-29(일) 23:59:59 KST


def test_to_attach_picks_week_items_without_milestone():
    _, start, end = wm.week_range("2026-W48")
    w48 = {"title": "2026-W48"}
    prs = [
        {"number": 160, "mergedAt": "2026-11-23T01:00:00Z", "milestone": None},  # 이 주 → 붙임
        {"number": 161, "mergedAt": "2026-11-24T01:00:00Z", "milestone": w48},  # 이미 붙음
        {"number": 162, "mergedAt": "2026-11-22T14:59:59Z", "milestone": None},  # 11-22 23:59:59 KST = 지난주
        {"number": 163, "mergedAt": "2026-11-29T14:59:59Z", "milestone": None},  # 일요일 23:59:59 KST → 이 주
        {"number": 164, "mergedAt": "2026-11-29T15:00:00Z", "milestone": None},  # 다음 주 월요일 00:00 KST
    ]
    issues = [
        {"number": 55, "closedAt": "2026-11-25T01:00:00Z", "state": "CLOSED", "milestone": None},
        {"number": 56, "closedAt": "2026-11-25T01:00:00Z", "state": "CLOSED", "milestone": {"title": "2026-W49"}},
        {"number": 57, "closedAt": None, "state": "OPEN", "milestone": None},
        {"number": 58, "closedAt": "2026-11-25T01:00:00Z", "state": "OPEN", "milestone": None},  # 다시 열림
        {"number": 59, "closedAt": "2026-11-20T01:00:00Z", "state": "CLOSED", "milestone": None},  # 지난주
    ]
    attach, other = wm.to_attach(prs, issues, start, end, "2026-W48")
    assert attach == [("PR", 160, None), ("PR", 163, None), ("이슈", 55, None)]
    assert other == [("이슈", 56, "2026-W49")]  # 다른 마일스톤은 옮기지 않음


def test_milestone_description_keeps_hand_written_text():
    old = "주간 점검·회고\r\n\r\n회고 (11-14): 바꾼 규칙 B"
    d1 = wm.milestone_description(old, "지표 요약 1")
    assert d1.startswith("주간 점검·회고\n\n회고 (11-14): 바꾼 규칙 B\n\n<!-- weekly_metrics -->\n지표 요약 1\n")
    d2 = wm.milestone_description(d1 + "\n회고 줄 뒤에 추가", "지표 요약 2")
    assert "지표 요약 1" not in d2 and "지표 요약 2" in d2 and d2.endswith("회고 줄 뒤에 추가")
    assert d2.count(wm.MS_BEGIN) == 1
    assert wm.milestone_description("", "x") == "<!-- weekly_metrics -->\nx\n<!-- /weekly_metrics -->"


def test_milestone_summary_links_week_record():
    row = {"deploy_h": 1.0, "follow": [], "rework": None, "review": (2, 1, 0), "criteria": (3, 4),
           "first_check": True, "first_check_pr": True}
    s = wm.milestone_summary("2026-W48", [row], {"bad_log": 0}, now=wm.dt.datetime(2026, 11, 29, 23, 0, tzinfo=wm.KST))
    assert "docs/process/2026-W48.md" in s and "병합 PR 1" in s and "완료 조건 충족 3/4" in s
    assert "첫 로컬 검사 통과 1/1" in s


# ---- 보안 리뷰 필요 판정을 merge_gate와 같게 (https://github.com/reddol18/docflow/issues/106)

SEC_BODY = "## 보안 영향\n- [x] 새 엔드포인트\n"
PLAIN_BODY = "## 보안 영향\n- [ ] 새 엔드포인트\n"


def _pr(n=90, title="기능 (#7)", body=""):
    return {"number": n, "title": title, "body": body}


def test_security_needed_follows_merge_gate_author_rules():
    by_no = {7: {"body": PLAIN_BODY}, 8: {"body": SEC_BODY}}
    owner = {90: "OWNER", 7: "OWNER", 8: "OWNER"}
    assert wm.security_needed(_pr(), [], by_no, owner) is False
    assert wm.security_needed(_pr(title="기능 (#8)"), [], by_no, owner) is True  # 칸 체크
    assert wm.security_needed(_pr(), [], by_no, {**owner, 7: "NONE"}) is True  # 외부인이 연 이슈
    assert wm.security_needed(_pr(title="기능"), [], by_no, {**owner, 90: "CONTRIBUTOR"}) is True  # 외부인 PR
    assert wm.security_needed(_pr(), [], by_no, {7: "OWNER"}) is True  # PR 글쓴이를 모름 → 외부인으로
    assert wm.TRUSTED == {"OWNER", "MEMBER", "COLLABORATOR"}
