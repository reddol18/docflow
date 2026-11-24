"""주간 개발 과정 지표 (https://github.com/reddol18/docflow/issues/24). gh + git만 쓴다 — LLM 호출 없음(토큰 0).

  uv run --no-project python -I scripts/weekly_metrics.py            # 이번 주 (월~일, KST)
  uv run --no-project python -I scripts/weekly_metrics.py --week 2026-W45
  ... --out docs/process                                              # `docs/process/2026-W45.md`에 저장
  ... --milestone --dry-run                                           # 마일스톤에 할 일만 보임 (GitHub에 안 씀)
  ... --milestone                                                     # 마일스톤 `YYYY-Www` 만들기·PR·이슈 붙이기·설명 갱신

--milestone (https://github.com/reddol18/docflow/issues/55): 마일스톤 `YYYY-Www`(마감 그 주 일요일 23:59 KST)가 없으면 만들고,
  그 주에 병합한 PR·닫은 이슈 중 마일스톤이 없는 것을 붙인다(다른 마일스톤에 있는 것은 옮기지 않고 보이기만 — 계획한 주를
  지우지 않게). 설명에는 `<!-- weekly_metrics -->` 표시 사이에 지표 요약과 `docs/process/YYYY-Www.md` 링크를 쓴다
  (표시 밖의 손으로 쓴 설명·회고 줄은 그대로).

무엇을 재나
- 흐름: 병합한 PR 수, PR 크기, 열고 병합하기까지 걸린 시간, 배포 태그 수
- 절차 지킴: 병합한 PR마다 PR 본문·댓글에 아래 흔적이 있는지
    로컬 검사   `scripts/check.ps1`가 출력하는 요약 줄 "로컬 검사 `scripts/check.ps1` — 커밋 ..."
    계약 리뷰   "spec-reviewer" 또는 "spec-review"
    버그 리뷰   "code-review"
    보안 리뷰   "security-reviewer" — 닫거나 가리키는 이슈(닫는 이슈·제목의 #N·본문 줄 머리 Refs #N/이 저장소 이슈 URL)의
                '보안 영향' 칸에 체크가 있을 때만 필요. 'PR별 절차'의 닫는 이슈 칸·리드타임에는 Refs를 넣지 않는다.
                그 이슈나 PR의 글쓴이가 저장소 주인·멤버·협업자가 아니면 칸과 상관없이 필요 (merge_gate와 같음, issues/106)
  병합 전 검사 `scripts/merge_gate.py`가 이 파일의 함수를 함께 쓴다(evidence·check_blocks·block_failed·_tables·
  issue_refs·is_no_code·needs_security·MARKS). merge_gate가 더 엄격하다: 로컬 검사는 PR head 커밋 요약이고 막는 검사 모두
  '통과'(건너뜀 거부, 코드 없는 PR만 osv·semgrep·trivy 건너뜀 허용), 리뷰는 본문이 아닌 댓글·리뷰 한 글 안에 '지적·반영' 표
    배포        병합 뒤 deploy-* 태그가 이 PR의 병합 커밋을 포함하는지
  흔적이 없을 때 구분:
    늦게 #M     나중 PR의 **제목**에 "PR #N"이 있고(예: "PR #38 버그 리뷰 반영") 그 PR에 그 리뷰 흔적이 있음.
                분모에 넣고 제때 한 것과 따로 센다. 늦은 리뷰를 하면 제목을 이렇게 단다
    규칙 전     절차(템플릿·`scripts/check.ps1`)가 생긴 PR #30 병합 전에 열고, 그 뒤 하루 안에 병합한 PR (분모에서 뺌)
    코드 없음   `docs/`·README·잠금 파일(requirements 제외)만 바꾼 PR → 계약·버그 리뷰는 볼 것이 없다 (분모에서 뺌)
- 속도·품질·리뷰·계약 (표의 '어떻게 셌나' 칸): 이슈 시작→병합→배포 시간, 변경 실패율, 재작업률, 다시 열린 이슈,
  리뷰 지적 처리, 리뷰 뒤 버그, 완료 조건 충족률, 범위 변경 비율, 첫 로컬 검사 통과율. AI 비용은 집계 수단이 없어 –
  글에서 읽는 지표는 PR 템플릿·리뷰 댓글 형식('완료 조건별 결과' 표, '지적·반영' 표)을 따를 때만 잡힌다
  완료 조건 충족률은 병합 시점 본문(GraphQL userContentEdits)으로, 첫 로컬 검사 통과율은 `scripts/check.ps1` 실행 기록
  `logs/check_runs.jsonl`(이 PC에만 있음, gitignore)의 PR 브랜치 첫 실행으로 센다 (https://github.com/reddol18/docflow/issues/55)
- 이슈: 연 것·닫은 것, 우선순위별 열린 수, 위험 수용(라벨 risk-accepted) 열린 수
- 보안: `security/asvs.yaml`의 미충족·일부·미판정 항목 수 (지금 작업 트리 기준)
흔적은 글자로 찾는다: PR 본문(템플릿 줄을 뺀 나머지)·댓글·리뷰 본문·줄 댓글. 제목은 흔적이 아니다.
회고 칸은 그 주 작업이 끝난 뒤(일요일) 다시 만들어 채운다.
템플릿 줄을 체크만 하고 결과를 안 적었으면 흔적이 아니다. 리뷰를 돌렸으면 결과를 PR 댓글로 남긴다.
한계: 글자로 찾으므로 "code-review는 다음에" 같은 언급도 잡힌다. 절차를 잊었는지 보는 용도이지 증명이 아니다.
병합 커밋이 이 PC에 없으면 멈춘다 (먼저 `git fetch --tags`).
"""
import argparse
import datetime as dt
import json
import re
import statistics
import subprocess
import sys
from pathlib import Path

REPO = "reddol18/docflow"
ROOT = Path(__file__).resolve().parent.parent
KST = dt.timezone(dt.timedelta(hours=9))
MARKS = {
    "로컬 검사": re.compile(r"로컬 검사 `scripts/check\.ps1` — 커밋"),
    "계약 리뷰": re.compile(r"spec-review"),
    "버그 리뷰": re.compile(r"code-review"),
    "보안 리뷰": re.compile(r"security-reviewer"),
}
RULES_SINCE = dt.datetime(2026, 11, 3, 23, 34, 20, tzinfo=dt.timezone.utc)  # PR #30 병합: 템플릿·`check.ps1`·리뷰 절차 시작
RULES_GRACE = dt.timedelta(days=1)  # 규칙 전에 연 PR이라도 이보다 늦게 병합하면 규칙을 따른다
REVIEWS = ("계약 리뷰", "버그 리뷰")  # 코드가 없으면 볼 것이 없는 리뷰
# 절차를 바꾸는 .md(템플릿·에이전트·스킬)는 코드로 본다 → docs/와 README만
# requirements*.txt·requirements.lock은 코드로 본다: 이미지가 그대로 설치한다 (https://github.com/reddol18/docflow/issues/33)
NON_CODE = re.compile(r"^docs/|(^|/)README\.md$|(^|/)(?!requirements\.lock$)[^/]*\.lock$|(^|/)package-lock\.json$|^\.gitattributes$")
REWORK_DAYS = dt.timedelta(days=7)  # 재작업: 병합 뒤 이 안에 같은 코드 파일을 다른 PR이 고침
FILES_CAP = 100  # gh pr view --json files 상한: 이만큼이면 잘렸을 수 있으니 코드 있음으로 본다


def sh(*args):
    r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8")  # noqa: S603 — 고정된 gh·git 인자
    if r.returncode:
        raise SystemExit(f"{' '.join(args[:3])} 실패: {r.stderr.strip()[:500]}")
    return r.stdout


def gh_json(*args):
    return json.loads(sh("gh", *args, "--repo", REPO) or "[]")


def parse_ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def week_range(label):
    if label:
        y, w = label.split("-W")
        start = dt.datetime.fromisocalendar(int(y), int(w), 1).replace(tzinfo=KST)
    else:
        today = dt.datetime.now(KST)
        start = (today - dt.timedelta(days=today.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    y, w, _ = start.isocalendar()
    return f"{y}-W{w:02d}", start, start + dt.timedelta(days=7)


def _norm(line):
    return re.sub(r"\[[xX ]\]", "[ ]", line.strip())


TEMPLATE = {_norm(ln) for ln in (ROOT / ".github" / "pull_request_template.md").read_text(encoding="utf-8").splitlines()
            if ln.strip()}


def evidence(body):
    """PR 본문에서 템플릿이 원래 갖고 있는 글자는 뺀다: HTML 주석, 체크 안 한 칸, 템플릿 줄 그대로인 줄(체크만 한 것 포함).
    (템플릿에 '/code-review'·'security-reviewer'가 적혀 있으므로 그대로 두면 리뷰를 안 해도 흔적으로 잡힌다)"""
    body = re.sub(r"<!--.*?-->", "", body or "", flags=re.S)
    return "\n".join(ln for ln in body.splitlines()
                     if not re.match(r"\s*- \[ \]", ln) and _norm(ln) not in TEMPLATE)


def evidence_texts(body, comments, reviews):
    """흔적을 찾는 글들: PR 본문(템플릿 줄 뺌)·댓글·리뷰 본문·줄 댓글. `scripts/merge_gate.py`도 쓴다"""
    return [evidence(body)] + list(comments) + list(reviews)


def needs_security(issue_body):
    s = section(issue_body, "보안 영향")
    return bool(s and re.search(r"- \[[xX]\]", s))


TRUSTED = {"OWNER", "MEMBER", "COLLABORATOR"}  # 글을 믿는 글쓴이 (author_association). merge_gate도 쓴다


def associations():
    """이슈·PR 번호 → 글쓴이 author_association (REST 이슈 목록은 PR도 담는다)"""
    out = sh("gh", "api", "--paginate", f"repos/{REPO}/issues?state=all&per_page=100",
             "--jq", '.[] | "\\(.number) \\(.author_association)"')
    return {int(n): a for n, a in (ln.split() for ln in out.splitlines() if ln.strip())}


def security_needed(pr, closes, by_no, assoc):
    """보안 리뷰가 필요한가 (merge_gate.judge와 같은 규칙): PR이나 가리키는 이슈의 글쓴이가 저장소 사람이 아니거나
    (모르면 아닌 것으로), 가리키는 이슈의 '보안 영향' 칸에 체크"""
    if assoc.get(pr["number"]) not in TRUSTED:
        return True
    refs = [n for n in issue_refs(pr["title"], pr["body"], closes) if n in by_no]
    return any(assoc.get(n) not in TRUSTED or needs_security(by_no[n].get("body")) for n in refs)


_REF = r"(?:#\d+|https://github\.com/[\w.-]+/[\w.-]+/issues/\d+)"
# 줄 머리의 'Refs #N'·'Refs: #N'·'Closes https://github.com/<이 저장소>/issues/N' (글 속 인용은 아님)
REFS = re.compile(rf"^[ \t]*(?:Refs?|Closes|Fixes|Resolves)[ \t]*:?[ \t]+((?:{_REF}[ \t,]*)+)", re.I | re.M)
THIS_REPO_ISSUE = re.compile(rf"https://github\.com/{re.escape(REPO)}/issues/(\d+)$", re.I)


def linked_issues(title, closing):
    """닫는 이슈 + 제목의 #N (지표의 '닫는 이슈' 칸·리드타임 시작). 이슈인지(PR 번호가 아닌지)는 부르는 쪽이 거른다"""
    return set(closing) | {int(n) for n in re.findall(r"#(\d+)", title or "")}


def body_refs(body):
    """본문 줄 머리의 Refs/Closes/Fixes/Resolves가 가리키는 번호. 다른 저장소 URL은 뺀다"""
    out = set()
    for m in REFS.finditer(re.sub(r"<!--.*?-->", "", body or "", flags=re.S)):
        for tok in re.findall(_REF, m.group(1)):
            if tok.startswith("#"):
                out.add(int(tok[1:]))
            elif u := THIS_REPO_ISSUE.match(tok):
                out.add(int(u.group(1)))
    return out


def issue_refs(title, body, closing):
    """보안 영향을 볼 이슈 후보: linked_issues + body_refs"""
    return linked_issues(title, closing) | body_refs(body)


def is_no_code(files):
    """`docs/`·README·잠금 파일(requirements 제외 — 이미지 입력)만 바꾼 PR (계약·버그 리뷰는 볼 것이 없다).
    목록이 상한이면 잘렸을 수 있어 코드로 본다"""
    return 0 < len(files) < FILES_CAP and all(NON_CODE.search(f) for f in files)


def deploy_tags():
    out = sh("git", "for-each-ref", "refs/tags/deploy-*", "--format=%(refname:short)\t%(creatordate:iso-strict)")
    return [(n, parse_ts(t)) for n, t in (ln.split("\t") for ln in out.splitlines() if ln.strip())]


def deployed(merge_sha):
    """이 커밋을 처음 포함한 배포 태그. 커밋이 이 PC에 없으면 '배포 안 됨'으로 잘못 세지 않게 멈춘다."""
    if subprocess.run(["git", "cat-file", "-e", f"{merge_sha}^{{commit}}"], capture_output=True).returncode:  # noqa: S603, S607
        raise SystemExit(f"병합 커밋 {merge_sha[:8]}이 이 PC에 없습니다. git fetch --tags 후 다시")
    names = sh("git", "tag", "--contains", merge_sha, "--list", "deploy-*", "--sort=creatordate").split()
    return names[0] if names else None


def pr_extra(n):
    """커밋 수, 바꾼 파일, 댓글이 아니라 리뷰로 남긴 글 (리뷰 본문·줄 댓글)"""
    v = json.loads(sh("gh", "pr", "view", str(n), "--repo", REPO, "--json", "commits,reviews,files"))
    inline = sh("gh", "api", "--paginate", f"repos/{REPO}/pulls/{n}/comments", "--jq", ".[].body")
    return len(v["commits"]), [f["path"] for f in v["files"]], [r["body"] for r in v["reviews"]] + [inline]


def pr_edits(numbers):
    """PR 번호 → 본문 편집 이력(GraphQL userContentEdits, 최근 100판). 20개씩 한 쿼리로"""
    owner, name = REPO.split("/")
    out = {}
    for i in range(0, len(numbers), 20):
        chunk = numbers[i:i + 20]
        q = ("query($o: String!, $r: String!) { repository(owner: $o, name: $r) { "
             + " ".join(f"p{n}: pullRequest(number: {int(n)}) {{ userContentEdits(first: 100) "
                        "{ totalCount nodes { editedAt diff } } }" for n in chunk) + " } }")
        d = json.loads(sh("gh", "api", "graphql", "-f", f"query={q}", "-f", f"o={owner}", "-f", f"r={name}"))
        for n in chunk:
            out[n] = d["data"]["repository"][f"p{n}"]["userContentEdits"]
    return out


def prior_merge(branch, n, merged):
    """같은 이름 브랜치에서 PR n보다 먼저 병합한 PR의 마지막 병합 시각 (없으면 None)"""
    got = gh_json("pr", "list", "--state", "merged", "--head", branch, "--limit", "100", "--json", "number,mergedAt")
    return max((t for q in got if q["number"] != n and (t := parse_ts(q["mergedAt"])) and t < merged), default=None)


def check_log_path():
    """실행 기록 파일: 본 저장소(워크트리면 git common dir의 부모) 아래 logs/check_runs.jsonl"""
    common = sh("git", "-C", str(ROOT), "rev-parse", "--git-common-dir").strip()
    return (ROOT / common).resolve().parent / CHECK_LOG


def read_check_log(path):
    if not path.exists():
        return [], 0
    return parse_check_log(path.read_text(encoding="utf-8").splitlines())


def pr_marks(p):
    commits, files, reviews = pr_extra(p["number"])
    text = "\n\n".join(evidence_texts(p["body"], [c["body"] for c in p["comments"]], reviews))  # 빈 줄로 이어 표끼리 붙지 않게
    return commits, files, text, {k: bool(rx.search(text)) for k, rx in MARKS.items()}


# ---------- 글에서 읽는 지표 (순수 함수 — 시험은 tests/test_weekly_metrics.py) ----------

def section(body, name):
    """'## name' 절의 내용 (HTML 주석 뺌). 절이 없으면 None"""
    body = re.sub(r"<!--.*?-->", "", (body or "").replace("\r\n", "\n"), flags=re.S)  # 이슈 본문은 CRLF일 수 있다
    m = re.search(rf"#+[ \t]*{re.escape(name)}[ \t]*\n(.*?)(?=\n#+\s|\Z)", body, re.S)  # \s*는 빈 줄을 삼켜 다음 절까지 읽는다
    return m.group(1) if m else None


def _plain(cell):
    return re.sub(r"[*_`]", "", cell).strip()  # **충족**·_그대로_도 같게


def _tables(text, *need):
    """머리 줄에 need 낱말이 모두 있는 마크다운 표의 데이터 행들 (칸 목록, 강조 표시 뺌)"""
    rows, head = [], None
    for ln in (text or "").splitlines():
        s = ln.strip()
        if not s.startswith("|"):
            head = None
            continue
        cells = [_plain(c) for c in s.strip("|").split("|")]
        if head is None:
            head = cells if all(any(w in c for c in cells) for w in need) else []
            continue
        if not head or set(s) <= set("|-: "):
            continue
        rows.append(dict(zip(head, cells)))
    return rows


DECLINED = re.compile(r"^(그대로|아님|반영 없음|없음)")
DEFERRED = re.compile(r"후속|^https://github\.com/\S+/issues/\d+\s*$|^#\d+")


def review_fixes(text):
    """리뷰 결과 댓글의 '| 지적 | 반영 |' 표 (PR 템플릿이 정한 형식) → (고침, 후속으로 넘김, 그대로 둠)"""
    fixed = deferred = declined = 0
    for r in _tables(text, "지적", "반영"):
        v = r.get("반영", "")
        if DECLINED.search(v):
            declined += 1
        elif DEFERRED.search(v):
            deferred += 1
        elif v:
            fixed += 1
    return fixed, deferred, declined


def criteria_met(body):
    """PR 본문 '완료 조건별 결과' 표 → (충족, 대상). 빈 행과 '해당 없음'은 뺀다. 작성자가 적은 값이다"""
    rows = [r for r in _tables(body, "완료 조건", "결과") if r.get("완료 조건")]
    rows = [r for r in rows if not r.get("결과", "").startswith("해당 없음")]
    return sum(r.get("결과", "").startswith("충족") for r in rows), len(rows)


def extra_changes(body):
    """'범위 밖 변경' 절에 무엇이 적혀 있나: True / '없음'이면 False / 절이 없거나 비었으면 None(안 채움)"""
    s = section(body, "범위 밖 변경")
    if s is None:
        return None
    s = s.strip().lstrip("-* ").strip()
    if not s:
        return None
    return not s.startswith("없음")


SCOPE_CHANGE = re.compile(r"범위 변경")  # '범위 밖 변경'과 다르다: 완료 조건을 줄이거나 바꾼 기록


def scope_change(body):
    return bool(SCOPE_CHANGE.search(re.sub(r"<!--.*?-->", "", body or "", flags=re.S)))


CHECK_HEAD = re.compile(r"로컬 검사 `scripts/check\.ps1` — 커밋 `([0-9a-fA-F]+)`(.*)")


def check_blocks(text):
    """글 안의 로컬 검사 요약들 → [(요약 첫 줄의 match, 요약 덩어리)]. 덩어리는 요약 표가 끝나는 곳까지
    (뒤따르는 '실패: …' 한 줄은 넣는다). `scripts/merge_gate.py`도 쓴다"""
    out, lines = [], (text or "").splitlines()
    for i, ln in enumerate(lines):
        if not MARKS["로컬 검사"].search(ln):
            continue
        block, started = [ln], False
        for nxt in lines[i + 1:]:
            if nxt.strip().startswith("|"):
                started = True
            elif started or len(block) > 3:
                if nxt.strip().startswith("실패:"):
                    block.append(nxt)
                break
            block.append(nxt)
        out.append((CHECK_HEAD.search(ln), "\n".join(block)))
    return out


def block_failed(block):
    return "**실패**" in block or "실패:" in block


def first_check_passed(texts):
    """시간 순 글들에서 처음 나온 로컬 검사 요약 표가 통과였나 (없으면 None). 표가 끝나는 곳까지만 본다"""
    for t in texts:
        for _, block in check_blocks(t):
            return not block_failed(block)
    return None


# ---------- 로컬 검사 실행 기록·병합 시점 본문·마일스톤 (순수 함수, https://github.com/reddol18/docflow/issues/55) ----------

CHECK_LOG = Path("logs") / "check_runs.jsonl"  # `scripts/check.ps1`가 본 저장소(git common dir의 부모) 아래에 쓴다


def parse_check_log(lines):
    """실행 기록 줄들 → (실행 목록, 깨진 줄 수). 실행은 {"at": datetime, "branch", "results": {검사: pass/fail/skip}, ...}"""
    runs, bad = [], 0
    for ln in lines:
        if not ln.strip():
            continue
        try:
            r = json.loads(ln)
            at = parse_ts(r["at"])
            if at is None or at.tzinfo is None or not isinstance(r.get("branch"), str) or not isinstance(r.get("results"), dict):
                raise ValueError
        except (ValueError, KeyError, TypeError, AttributeError):
            bad += 1
            continue
        runs.append({**r, "at": at})
    return runs, bad


def run_passed(run):
    """막는 검사 중 fail이 하나도 없음 (건너뜀은 실패로 보지 않는다 — check.ps1의 '막는 검사 모두 통과'와 같다)"""
    return "fail" not in run["results"].values()


def first_run_passed(runs, branch, since=None, until=None):
    """그 브랜치의 첫 `scripts/check.ps1` 실행이 통과였나. 기록이 없으면 None.
    since(그 시각 뒤)·until(그 시각까지)로 자른다: 같은 이름 브랜치를 앞 PR이 이미 병합했으면 그 뒤 실행만, 병합 뒤 실행은 뺀다"""
    xs = [r for r in runs if r["branch"] == branch
          and (since is None or r["at"] > since) and (until is None or r["at"] <= until)]
    return run_passed(min(xs, key=lambda r: r["at"])) if xs else None


def body_at(body, edits, at):
    """시각 at(병합)의 PR 본문. edits는 GraphQL userContentEdits {totalCount, nodes: [{editedAt, diff}]} — diff가 그 판의
    본문 전체이고 가장 오래된 판이 처음 본문이다(2026-11-25 PR 57로 확인). 고친 적 없으면 지금 본문.
    받은 판 중 at 이전 판이 없거나(판이 너무 많아 잘림) 그 판이 지워졌으면(diff null) None"""
    nodes = [e for e in ((edits or {}).get("nodes") or []) if e and e.get("editedAt")]
    if not nodes:
        return body
    before = [e for e in nodes if parse_ts(e["editedAt"]) <= at]
    if not before:
        return None
    return max(before, key=lambda e: parse_ts(e["editedAt"])).get("diff")


MS_BEGIN, MS_END = "<!-- weekly_metrics -->", "<!-- /weekly_metrics -->"


def milestone_due(start):
    """그 주(월요일 00:00 KST 시작)의 마감 = 일요일 23:59:59 KST, GitHub API 형식(UTC)"""
    due = start + dt.timedelta(days=6, hours=23, minutes=59, seconds=59)
    return due.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_attach(prs, issues, start, end, title):
    """마일스톤 title에 붙일 것 → (붙일 것, 다른 마일스톤에 있어 두는 것). 각 항목 (종류, 번호, 지금 마일스톤).
    대상: start~end에 병합한 PR(mergedAt)·닫은 이슈(closedAt, 지금 닫힘). 이미 title에 있으면 뺀다.
    다른 마일스톤에 있는 것은 옮기지 않는다 (계획한 주의 기록을 지우지 않게 — 사람이 정한다)"""
    attach, other = [], []
    items = ([("PR", p["number"], p.get("mergedAt"), p.get("milestone")) for p in prs]
             + [("이슈", i["number"], i.get("closedAt"), i.get("milestone")) for i in issues
                if i.get("state", "CLOSED") != "OPEN"])
    for kind, n, ts, ms in items:
        t = parse_ts(ts)
        if t is None or not (start <= t < end):
            continue
        cur = (ms or {}).get("title")
        if cur == title:
            continue
        (other if cur else attach).append((kind, n, cur))
    return sorted(attach), sorted(other)


def milestone_description(old, summary):
    """설명에서 표시 사이만 summary로 바꾼다. 표시가 없으면 끝에 붙인다.
    표시 밖(손으로 쓴 설명·회고 줄)은 줄바꿈까지 그대로 둔다. 블록은 설명의 줄바꿈(CRLF/LF)을 따른다.
    표시는 줄 전체가 표시 문자열일 때만 센다 (글 속에 적은 표시 문자열은 표시가 아님).
    표시가 깨졌으면(하나만 있음·여러 쌍·순서 뒤바뀜) 손으로 쓴 줄을 지우지 않게 ValueError"""
    old = old or ""
    nl = "\r\n" if "\r\n" in old else "\n"
    body = nl.join(summary.strip().replace("\r\n", "\n").split("\n"))
    block = f"{MS_BEGIN}{nl}{body}{nl}{MS_END}"
    begins = [m.start() for m in re.finditer(rf"(?m)^{re.escape(MS_BEGIN)}(?=\r?$)", old)]
    ends = [m.end() for m in re.finditer(rf"(?m)^{re.escape(MS_END)}(?=\r?$)", old)]
    if not begins and not ends:
        return f"{old.rstrip()}{nl}{nl}{block}" if old.strip() else block
    if len(begins) != 1 or len(ends) != 1 or ends[0] < begins[0]:
        raise ValueError(f"마일스톤 설명의 지표 표시가 깨짐 (시작 {len(begins)}개, 끝 {len(ends)}개"
                         + (", 끝이 시작보다 앞" if len(begins) == len(ends) == 1 else "")
                         + "): 손으로 고친 뒤 다시 실행")
    return old[:begins[0]] + block + old[ends[0]:]


def bug_fields(body):
    """버그 템플릿의 '발견 단계'·'원인 PR' 칸"""
    stage = (section(body, "발견 단계") or "").strip().splitlines()
    cause = re.search(r"/pull/(\d+)", section(body, "원인 PR") or "")
    return (stage[0].strip() if stage else None), (int(cause.group(1)) if cause else None)


ESCAPED = {"배포 후 확인", "운영"}  # 리뷰·로컬 검사를 지나 나온 버그

def asvs_counts():
    p = ROOT / "security" / "asvs.yaml"
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    return {s: len(re.findall(rf"status:\s*{s}\b", text)) for s in ("미충족", "일부", "미판정")}


def collect(start, end, log_path=None):
    # commits는 목록에 넣으면 GraphQL 노드 상한을 넘는다 → 이 주 PR만 따로 센다
    fields = ("number,title,createdAt,mergedAt,additions,deletions,body,comments,closingIssuesReferences,mergeCommit,"
              "headRefName,milestone")
    # 병합 날짜로 찾는다 (최근 N개를 받아 거르면 오래 열려 있던 PR·지난 주가 빠진다)
    span = f"merged:{start.isoformat(timespec='seconds')}..{(end - dt.timedelta(seconds=1)).isoformat(timespec='seconds')}"
    prs = gh_json("pr", "list", "--state", "merged", "--search", span, "--limit", "200", "--json", fields)
    if len(prs) >= 200:
        raise SystemExit("한 주에 병합한 PR이 200개 이상: --limit을 올릴 것")
    prs = [p for p in prs if start <= parse_ts(p["mergedAt"]) < end]
    seen, later = {}, []

    def info(p):
        if p["number"] not in seen:
            seen[p["number"]] = pr_marks(p)
        return seen[p["number"]]

    def later_prs():
        """이 주 시작 뒤 병합한 PR 전부 (늦은 리뷰·뒤따른 수정·재작업을 찾는다). 필요할 때 한 번만 받는다"""
        if not later:
            got = gh_json("pr", "list", "--state", "merged", "--search", f"merged:>={start.isoformat(timespec='seconds')}",
                          "--limit", "1000", "--json", "number,title,mergedAt,body,comments")
            if len(got) >= 1000:
                raise SystemExit("나중 PR이 1000개 이상: --limit을 올릴 것")
            later.extend(sorted(got, key=lambda q: q["mergedAt"]) or [None])
        return [q for q in later if q]

    def follow_ups(n, merged):
        """제목에 'PR #N'이 있는 나중 PR (늦은 리뷰 반영이나 그 PR의 수정)"""
        ref = re.compile(rf"\bPR #{n}\b")
        return [q for q in later_prs() if q["number"] != n and parse_ts(q["mergedAt"]) >= merged and ref.search(q["title"])]

    def late(n, merged, key):
        """뒤따른 PR 중 그 리뷰 흔적이 있는 첫 PR"""
        for q in follow_ups(n, merged):
            if info(q)[3][key]:
                return q["number"]
        return None

    def rework(n, merged, files):
        """병합 뒤 7일 안에 같은 코드 파일을 다시 고친 다른 PR. 코드를 안 바꾼 PR은 None(분모에서 뺌)"""
        code = {f for f in files if not NON_CODE.search(f)}
        if not code:
            return None
        out = []
        for q in later_prs():
            t = parse_ts(q["mergedAt"])
            if q["number"] != n and merged < t <= merged + REWORK_DAYS and code & set(info(q)[1]):
                out.append(q["number"])
        return out
    limit = 1000
    issues = gh_json("issue", "list", "--state", "all", "--limit", str(limit),
                     "--json", "number,title,createdAt,closedAt,labels,state,body,milestone")
    if len(issues) >= limit:
        raise SystemExit(f"이슈가 {limit}개 이상이라 열린 수가 틀린다: limit을 올릴 것")
    by_no = {i["number"]: i for i in issues}
    assoc = associations()
    tags = deploy_tags()
    edits = pr_edits([p["number"] for p in prs])
    log_path = log_path or check_log_path()
    runs, bad_log = read_check_log(log_path)
    rows = []
    for p in sorted(prs, key=lambda p: p["number"]):
        commits, files, _, marks = info(p)
        closes = [c["number"] for c in p["closingIssuesReferences"]]
        # 제목의 (#N)도 관련 이슈로 보되, 이슈인 번호만 ("PR #38"의 38은 PR)
        refs = {n for n in linked_issues(p["title"], closes) if n in by_no}
        # 보안 리뷰 필요 판정만 본문 Refs #N·글쓴이까지 본다 (merge_gate와 같게). 리드타임·'닫는 이슈' 칸은 그대로
        sec = security_needed(p, closes, by_no, assoc)
        marks = dict(marks)
        if not sec:
            marks["보안 리뷰"] = None  # 필요 없으면 세지 않음
        no_code = is_no_code(files)
        merged = parse_ts(p["mergedAt"])
        before_rules = parse_ts(p["createdAt"]) < RULES_SINCE and merged < RULES_SINCE + RULES_GRACE
        for k, v in marks.items():
            if v is not False:
                continue
            if k != "로컬 검사" and (m := late(p["number"], merged, k)):
                marks[k] = f"늦게 #{m}"
            elif before_rules:
                marks[k] = "규칙 전"
            elif no_code and k in REVIEWS:
                marks[k] = "코드 없음"
        sha = (p.get("mergeCommit") or {}).get("oid")
        tag = deployed(sha) if sha else None
        tag_at = dict(tags).get(tag)
        started = [parse_ts(by_no[n]["createdAt"]) for n in refs]
        comments = [c["body"] for c in p["comments"]]
        # 변경 실패: 배포된 뒤에 제목 'PR #N'인 PR이 뒤따름 (배포 안 된 PR은 분모에서 뺌)
        fixes = None if not tag_at else [q["number"] for q in follow_ups(p["number"], merged)
                                         if parse_ts(q["mergedAt"]) > tag_at]
        window_done = dt.datetime.now(dt.timezone.utc) >= merged + REWORK_DAYS
        # 병합 시점 본문 (병합 뒤 고친 것은 빼고). 편집 이력이 잘려 모르면 지금 본문
        merged_body = body_at(p["body"], edits.get(p["number"]), merged)
        if merged_body is None:
            merged_body = p["body"]
        # 같은 이름 브랜치를 앞서(지난 주 포함) 병합한 PR이 있으면 그 병합 뒤 실행만 본다
        since = prior_merge(p["headRefName"], p["number"], merged) if runs else None
        rows.append({
            "n": p["number"], "title": p["title"], "size": p["additions"] + p["deletions"], "commits": commits,
            "hours": (merged - parse_ts(p["createdAt"])).total_seconds() / 3600,
            "closes": sorted(refs), "marks": marks, "deploy": tag,
            "lead_h": (merged - min(started)).total_seconds() / 3600 if started else None,      # 이슈 시작 → 병합
            "deploy_h": (tag_at - merged).total_seconds() / 3600 if tag_at else None,           # 병합 → 배포
            "follow": fixes, "rework": rework(p["number"], merged, files), "rework_done": window_done,
            "review": review_fixes(info(p)[2]),
            "criteria": criteria_met(merged_body), "extra": extra_changes(merged_body), "scope": scope_change(merged_body),
            # 실행 기록의 브랜치 첫 실행 (기록 없으면 None — 이 PC 밖에서 돌렸거나 기록 전 PR)
            "first_check": first_run_passed(runs, p["headRefName"], since, merged),
            # 예전 방식(PR에 처음 붙인 요약) — 기록 없는 PR 참고용. 댓글(시간 순)을 먼저 본다
            "first_check_pr": first_check_passed(comments + [evidence(p["body"])]),
        })
    # 다시 열린 이슈 (PR 제외, 이슈 수). 저장소 전체 이벤트를 넘겨 본다 — 커지면 기간으로 자를 방법을 찾을 것
    ev = sh("gh", "api", "--paginate", f"repos/{REPO}/issues/events",
            "--jq", '.[] | select(.event=="reopened" and .issue.pull_request == null) | "\\(.issue.number) \\(.created_at)"')
    reopened = {int(n) for n, ts in (ln.split() for ln in ev.splitlines() if ln.strip()) if start <= parse_ts(ts) < end}
    bugs = [(i["number"], *bug_fields(i["body"])) for i in issues if start <= parse_ts(i["createdAt"]) < end
            and any(lb["name"] == "bug" for lb in i["labels"])]
    extra = {"reopened": len(reopened), "bugs": bugs, "bad_log": bad_log,
             "prs": [{k: p[k] for k in ("number", "mergedAt", "milestone")} for p in prs]}
    return rows, issues, [t for t in tags if start <= t[1] < end], extra


def pct(xs):
    """제때 한 것/대상. 규칙 전·코드 없음·해당 없음은 대상에서 뺀다. 늦게 한 것은 대상에 넣고 따로 적는다."""
    xs = [x for x in xs if x is True or x is False or (isinstance(x, str) and x.startswith("늦게"))]
    if not xs:
        return "–"
    n_late = sum(isinstance(x, str) for x in xs)
    return f"{sum(x is True for x in xs)}/{len(xs)}" + (f" (늦게 {n_late})" if n_late else "")


def ok(b):
    return "–" if b is None else b if isinstance(b, str) else ("O" if b else "**X**")


def _med(xs, unit="시간"):
    xs = [x for x in xs if x is not None]
    return f"{statistics.median(xs):.1f} {unit} (PR {len(xs)})" if xs else "–"


def _ratio(xs):
    xs = [x for x in xs if x is not None]
    return f"{sum(bool(x) for x in xs)}/{len(xs)}" if xs else "–"


HOW = {  # 표의 '어떻게 셌나' 칸
    "lead": "PR이 가리키는 이슈(닫는 이슈·제목의 #N) 중 가장 이른 생성 시각부터. 오래 열린 묶음 이슈는 길게 나온다",
    "deploy": "병합 커밋을 처음 포함한 deploy 태그",
    "fail": "배포된 PR 중, 배포 뒤 제목에 'PR #N'인 PR(그 PR의 수정·늦은 리뷰 반영)이 뒤따른 것",
    "rework": "코드를 바꾼 PR 중, 병합 뒤 7일 안에 다른 PR이 같은 코드 파일을 고친 것",
    "reopen": "이 주에 reopened된 이슈 수 (PR 제외)",
    "review": "리뷰 결과 댓글의 '지적·반영' 표 (PR 템플릿 형식)",
    "bug": "이 주에 연 bug 이슈 중 발견 단계가 배포 후 확인·운영인 것",
    "criteria": "병합 시점 PR 본문(편집 이력 userContentEdits)의 '완료 조건별 결과' 표 '충족' (작성자가 적은 값, 해당 없음 제외)",
    "scope": "PR 본문에 '범위 변경' (완료 조건을 줄이거나 바꾼 기록)",
    "extra": "'범위 밖 변경' 절이 '없음'이 아님 (안 채운 PR은 뺌)",
    "check": ("실행 기록 `logs/check_runs.jsonl`(이 PC, gitignore)에서 PR 브랜치의 병합 전 첫 `scripts/check.ps1` 실행에 실패가 "
              "없음(-Quick·-Skip으로 건너뛴 검사는 실패로 보지 않음 — check.ps1의 '막는 검사 모두 통과'와 같은 기준). "
              "기록 없는 PR은 분모에서 빼고, 괄호에 PR에 처음 붙인 요약 기준(예전 방식)을 함께 적는다. "
              "이 주 PR이 모두 기록에 없으면 예전 방식 값"),
    "cost": "gh·git에는 없다. 세션 기록(`~/.claude/projects/*.jsonl`)은 이 PC에만 있고 이슈와 잇는 키가 없다",
}


def first_check_value(rows, extra):
    """'첫 로컬 검사 통과' 칸: 실행 기록 기준 비율 + (기록 없는 PR 수, 예전 방식 값, 깨진 기록 줄).
    이 주 PR이 하나도 기록에 없으면(기록 전 주를 다시 만들 때) 예전 방식 값을 앞에 둔다"""
    no_log = sum(r["first_check"] is None for r in rows)
    if rows and no_log == len(rows):
        return f"{_ratio([r.get('first_check_pr') for r in rows])} (실행 기록 없음 — PR에 붙인 요약 기준)"
    notes = []
    if no_log:
        notes.append(f"기록 없는 PR {no_log}")
    notes.append(f"PR에 붙인 요약 기준 {_ratio([r.get('first_check_pr') for r in rows])}")
    if extra.get("bad_log"):
        notes.append(f"깨진 기록 줄 {extra['bad_log']}")
    return f"{_ratio([r['first_check'] for r in rows])} ({', '.join(notes)})"


def milestone_summary(label, rows, extra, now=None):
    """마일스톤 설명에 넣을 지표 요약 (몇 줄)"""
    now = now or dt.datetime.now(KST)
    fx = [sum(r["review"][i] for r in rows) for i in range(3)]
    met, total = sum(r["criteria"][0] for r in rows), sum(r["criteria"][1] for r in rows)
    return "\n".join([
        f"지표 요약 ({now:%Y-%m-%d %H:%M} KST, `scripts/weekly_metrics.py --milestone`):",
        f"- 병합 PR {len(rows)}, 병합→배포 중앙값 {_med([r['deploy_h'] for r in rows])}, "
        f"변경 실패 {_ratio([r['follow'] for r in rows])}, 재작업 {_ratio([r['rework'] for r in rows])}",
        f"- 리뷰 지적 고침 {f'{fx[0]}/{sum(fx)}' if sum(fx) else '–'}, 병합 때 완료 조건 충족 {f'{met}/{total}' if total else '–'}, "
        f"첫 로컬 검사 통과 {first_check_value(rows, extra)}",
        f"- 기록: https://github.com/{REPO}/blob/main/docs/process/{label}.md",
    ])


def sync_milestone(label, start, end, issues, extra, summary, dry_run):
    """마일스톤 label을 만들거나 찾아, 이 주 병합 PR·닫은 이슈를 붙이고 설명의 지표 칸을 갱신한다. dry_run이면 할 일만 출력"""
    got = json.loads(sh("gh", "api", f"repos/{REPO}/milestones?state=all&per_page=100"))
    if len(got) >= 100:
        raise SystemExit("마일스톤이 100개 이상: 페이지를 넘겨 찾도록 고칠 것")
    ms = next((m for m in got if m["title"] == label), None)
    attach, other = to_attach(extra["prs"], issues, start, end, label)
    old = (ms or {}).get("description") or ""  # 설명이 비면 API는 null
    try:  # 쓰기 전에(dry-run에서도) 깨진 표시를 알리고 멈춘다
        desc = milestone_description(old, summary)
    except ValueError as e:
        raise SystemExit(f"마일스톤 {label}: {e}") from None
    tag = "(dry-run) " if dry_run else ""
    print(f"{tag}마일스톤 {label}: " + (f"있음 #{ms['number']}" if ms else f"만듦 (마감 {milestone_due(start)})"))
    print(f"{tag}설명 지표 칸: " + ("바꿀 것 없음" if ms and desc == old else "씀"))
    print(f"{tag}붙일 것 {len(attach)}: " + (", ".join(f"{k} #{n}" for k, n, _ in attach) or "없음"))
    if other:
        print("다른 마일스톤에 있어 그대로 둠: " + ", ".join(f"{k} #{n}({m})" for k, n, m in other))
    if dry_run:
        print("---- 설명 지표 칸 ----\n" + summary)
        return
    if ms is None:
        ms = json.loads(sh("gh", "api", "-X", "POST", f"repos/{REPO}/milestones", "-f", f"title={label}",
                           "-f", f"due_on={milestone_due(start)}", "-f", f"description={desc}"))
    elif desc != old:
        sh("gh", "api", "-X", "PATCH", f"repos/{REPO}/milestones/{int(ms['number'])}", "-f", f"description={desc}")
    for _, n, _ in attach:  # PR도 이슈 API로 붙인다
        sh("gh", "api", "-X", "PATCH", f"repos/{REPO}/issues/{int(n)}", "-F", f"milestone={int(ms['number'])}")
    print(f"마일스톤 {label} #{ms['number']}: {len(attach)}개 붙임")


def quality(rows, extra):
    fx = [sum(r["review"][i] for r in rows) for i in range(3)]
    nfx = sum(fx)
    met, total = sum(r["criteria"][0] for r in rows), sum(r["criteria"][1] for r in rows)
    open_win = sum(r["rework"] is not None and not r["rework_done"] for r in rows)
    escaped = [b for b in extra["bugs"] if b[1] in ESCAPED]
    bugs = ", ".join(f"#{n}({s or '단계 없음'}{f', 원인 #{c}' if c else ''})" for n, s, c in extra["bugs"]) or "없음"
    rework = _ratio([r["rework"] for r in rows]) + (f" (잠정: 7일이 안 지난 PR {open_win})" if open_win else "")
    review = f"{fx[0]}/{nfx} (후속 {fx[1]}, 그대로 {fx[2]})" if nfx else "–"
    check = first_check_value(rows, extra)
    vals = [
        ("이슈 시작 → 병합 중앙값", _med([r["lead_h"] for r in rows]), HOW["lead"]),
        ("병합 → 배포 중앙값", _med([r["deploy_h"] for r in rows]), HOW["deploy"]),
        ("변경 실패율", _ratio([r["follow"] for r in rows]), HOW["fail"]),
        ("재작업률", rework, HOW["rework"]),
        ("다시 열린 이슈", extra["reopened"], HOW["reopen"]),
        ("리뷰 지적 고친 비율", review, HOW["review"]),
        ("리뷰 뒤 나온 버그", f"{len(escaped)} / bug 이슈 {len(extra['bugs'])}", f"{HOW['bug']}. {bugs}"),
        ("병합 때 완료 조건 충족", f"{met}/{total}" if total else "–", HOW["criteria"]),
        ("범위 변경 기록이 있는 PR", _ratio([r["scope"] for r in rows]), HOW["scope"]),
        ("범위 밖 변경이 있는 PR", _ratio([r["extra"] for r in rows]), HOW["extra"]),
        ("첫 로컬 검사 통과", check, HOW["check"]),
        ("AI 비용", "–", HOW["cost"]),
    ]
    return (["## 속도·품질·리뷰·계약", "", "| 지표 | 값 | 어떻게 셌나 |", "|---|---|---|"]
            + [f"| {k} | {v} | {h} |" for k, v, h in vals] + [""])

def render(label, start, end, rows, issues, tags, extra=None):
    opened = [i for i in issues if start <= parse_ts(i["createdAt"]) < end]
    closed = [i for i in issues if i["closedAt"] and start <= parse_ts(i["closedAt"]) < end]
    open_now = [i for i in issues if i["state"] == "OPEN"]
    prio = {p: sum(any(lb["name"] == p for lb in i["labels"]) for i in open_now) for p in ("P0", "P1", "P2", "P3")}
    risk = sum(any(lb["name"] == "risk-accepted" for lb in i["labels"]) for i in open_now)
    last = end - dt.timedelta(days=1)
    out = [f"# 개발 과정 지표 {label}", "",
           f"기간: {start:%Y-%m-%d} ~ {last:%Y-%m-%d} (KST). 만든 시각 {dt.datetime.now(KST):%Y-%m-%d %H:%M}, "
           "`scripts/weekly_metrics.py` (gh + git, 토큰 0)", "",
           "## 요약", "", "| 지표 | 값 |", "|---|---|",
           f"| 병합한 PR | {len(rows)} |",
           f"| PR 크기 중앙값 (추가+삭제 줄) | {statistics.median([r['size'] for r in rows]) if rows else '–'} |",
           f"| 열고 병합까지 중앙값 (시간) | {statistics.median([r['hours'] for r in rows]) if rows else 0:.1f} |",
           f"| 배포 태그 | {len(tags)} |"]
    for k in MARKS:
        out.append(f"| 절차: {k} 흔적 있는 PR | {pct([r['marks'][k] for r in rows])} |")
    out.append(f"| 절차: 배포된 PR | {pct([bool(r['deploy']) for r in rows])} |")
    out += [f"| 이슈 연 것 / 닫은 것 | {len(opened)} / {len(closed)} |",
            f"| 열린 이슈 P0 / P1 / P2 / P3 | {prio['P0']} / {prio['P1']} / {prio['P2']} / {prio['P3']} |",
            f"| 열린 위험 수용 (risk-accepted) | {risk} |",
            "| 보안 체크리스트 미충족 / 일부 / 미판정 (`security/asvs.yaml`) | {미충족} / {일부} / {미판정} |".format(**asvs_counts()),
            ""]
    if extra is not None:
        out += quality(rows, extra)
    out += [
            "## PR별 절차", "", "| PR | 크기 | 커밋 | 시간 | 닫는 이슈 | " + " | ".join(MARKS) + " | 배포 |",
            "|---|---|---|---|---|" + "---|" * len(MARKS) + "---|"]
    for r in rows:
        out.append(f"| #{r['n']} {r['title'][:40]} | {r['size']} | {r['commits']} | {r['hours']:.1f} | "
                   f"{', '.join(f'#{n}' for n in r['closes']) or '–'} | "
                   + " | ".join(ok(r["marks"][k]) for k in MARKS) + f" | {r['deploy'] or '**X**'} |")
    out += ["", "O 흔적 있음, **X** 없음, – 해당 없음 (보안 영향 칸이 비었음), 늦게 #M 나중 PR에서 함, "
            "규칙 전 절차가 생기기 전에 연 PR, 코드 없음 문서·잠금 파일만. 요약의 분모에는 O·X·늦게만 넣는다.",
            "흔적은 PR 본문(템플릿 줄 제외)·댓글·리뷰의 글자로 찾는다. 제목은 늦은 리뷰 연결에만 쓴다.", "",
            "## 회고 (사람이 채움)", "", "- 잘된 것:", "- 빠뜨린 것:", "- 다음 주 바꿀 규칙 1개:", ""]
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", help="예: 2026-W45 (기본: 이번 주)")
    ap.add_argument("--out", help="이 폴더에 YYYY-Www.md로 저장")
    ap.add_argument("--check-log", type=Path, help="실행 기록 파일 (기본: 본 저장소 logs/check_runs.jsonl)")
    ap.add_argument("--milestone", action="store_true", help="마일스톤 YYYY-Www 만들기·PR·이슈 붙이기·설명 지표 칸 갱신")
    ap.add_argument("--dry-run", action="store_true", help="--milestone: GitHub에 쓰지 않고 할 일만 출력")
    a = ap.parse_args(argv)
    if a.dry_run and not a.milestone:
        ap.error("--dry-run은 --milestone과 함께")
    label, start, end = week_range(a.week)
    rows, issues, tags, extra = collect(start, end, a.check_log)
    md = render(label, start, end, rows, issues, tags, extra)
    sys.stdout.reconfigure(encoding="utf-8")
    if a.out:  # 마일스톤 갱신이 멈춰도(깨진 표시) 요약이 링크하는 기록 파일은 남게 먼저 쓴다
        p = Path(a.out) / f"{label}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(md, encoding="utf-8")
        print(f"저장: {p}")
    elif not a.milestone:
        print(md)
    if a.milestone:
        sync_milestone(label, start, end, issues, extra, milestone_summary(label, rows, extra), a.dry_run)


if __name__ == "__main__":
    main()
