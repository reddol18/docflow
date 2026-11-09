"""검토 단계 설정 — N단계 검수·반려·재검수, 작업자 ≠ 검수자 (https://github.com/reddol18/docflow/issues/86).

색인 칸 스키마처럼 검토 흐름도 설정 파일(`app/workflows/*.yaml`)로 정한다. 쓰는 파일은 환경변수 WORKFLOW(파일 경로),
없으면 `default.yaml` — 지금까지의 입력(reviewer) → 표본 검사(inspector) 2단계와 같다.
시작할 때 읽어 검사하고, 틀리면 WorkflowError로 앱이 멈춘다.

  name: 도로대장 3단계
  distinct_actors: true      # 같은 사람이 같은 문서의 두 단계를 맡지 못한다 (작업자 ≠ 검수자). 기본 true
  count_unconfirmed: true    # 출력 전 검증에서 마지막 단계 승인 전 문서를 '미확정'(unconfirmed)으로 센다. 기본 true
  stages:
    - name: entry            # 첫 단계는 언제나 입력: 색인 결과를 사람이 고치고 넘긴다
      label: 작업자
      role: reviewer
      submit: manual         # manual: 문서마다 승인해 넘김 / on_index: 철 색인이 끝나면 모든 문서가 다음 단계로 (지금 동작)
    - name: qc
      label: 품질관리자
      role: inspector
      select: all            # all: 전수 / sample: 표본 검사 목록(/volumes/{vol}/inspection — 표시 문서 전부 + 비율 표본)
      reject: true           # 반려 가능 (사유 필수)
      reject_to: entry       # 반려하면 돌아갈 앞 단계 (기본 바로 앞 단계)
    - name: supervisor
      label: 감독관
      role: admin
      reject: true

규칙
  - 단계 이름은 영문 소문자로 시작하는 [a-z0-9_] 32자까지, 겹치지 않는다. 맡는 역할은 reviewer·inspector·admin
    (역할은 auth.ROLES 순서대로 넓다 — 위 역할도 맡을 수 있다)
  - sample 단계는 하나까지, 마지막 단계여야 하고 반려를 지원하지 않는다 (표본 밖 문서는 검사 없이 확정)
  - reject_to는 앞 단계만 (sample 단계로는 못 돌린다)
문서 위치(어느 단계에 있나)의 판단은 여기 순수 함수로 하고, 저장은 app/store.py(doc_stage·stage_log), 경로는 app/main.py.
"""
import os
import re
from pathlib import Path

import yaml

DIR = Path(__file__).parent / "workflows"
DEFAULT = DIR / "default.yaml"
CONFIRMED = "_confirmed"  # 마지막 단계까지 승인된 문서의 위치 (단계 이름 규칙에 맞지 않아 겹치지 않는다)
NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
STAGE_ROLES = ("reviewer", "inspector", "admin")  # 보기(viewer)는 단계를 맡을 수 없다
SELECTS = ("all", "sample")
SUBMITS = ("manual", "on_index")
TOP_KEYS = {"name", "note", "distinct_actors", "count_unconfirmed", "stages"}
ENTRY_KEYS = {"name", "label", "role", "submit", "note"}
STAGE_KEYS = {"name", "label", "role", "select", "rate", "reject", "reject_to", "note"}


class WorkflowError(ValueError):
    pass


def _bool(d, key, default, where):
    v = d.get(key, default)
    if not isinstance(v, bool):
        raise WorkflowError(f"{where}{key}는 true/false")
    return v


def parse(t):
    """설정 dict → 검사한 흐름. 모르는 키·역할·앞 단계가 아닌 반려 대상 등은 거부"""
    if not isinstance(t, dict):
        raise WorkflowError("흐름 설정은 사전(dict)이어야 함")
    if set(t) - TOP_KEYS:
        raise WorkflowError(f"모르는 키: {sorted(set(t) - TOP_KEYS)}")
    raw = t.get("stages")
    if not isinstance(raw, list) or not raw:
        raise WorkflowError("stages 목록 필요 (첫 단계는 입력)")
    stages = []
    for i, s in enumerate(raw):
        if not isinstance(s, dict):
            raise WorkflowError(f"stages[{i}]: 사전이어야 함")
        name = s.get("name")
        where = f"stages[{i}]({name}): "
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise WorkflowError(f"{where}name은 [a-z][a-z0-9_] 32자까지")
        if any(x["name"] == name for x in stages):
            raise WorkflowError(f"{where}단계 이름 중복")
        keys = ENTRY_KEYS if i == 0 else STAGE_KEYS
        if set(s) - keys:
            raise WorkflowError(f"{where}모르는 키 {sorted(set(s) - keys)}")
        if s.get("role") not in STAGE_ROLES:
            raise WorkflowError(f"{where}role은 {', '.join(STAGE_ROLES)} 중 하나")
        label = s.get("label", name)
        if not isinstance(label, str) or not label.strip() or len(label) > 40:
            raise WorkflowError(f"{where}label은 40자까지 글자")
        st = {"name": name, "label": label.strip(), "role": s["role"], "index": i}
        if i == 0:
            if s.get("submit", "manual") not in SUBMITS:
                raise WorkflowError(f"{where}submit은 {' | '.join(SUBMITS)}")
            st.update(kind="entry", submit=s.get("submit", "manual"), select="all", reject=False, reject_to=None, rate=None)
        else:
            sel = s.get("select", "all")
            if sel not in SELECTS:
                raise WorkflowError(f"{where}select는 {' | '.join(SELECTS)}")
            rate = s.get("rate")
            if rate is not None:
                if sel != "sample":
                    raise WorkflowError(f"{where}rate는 sample 단계만")
                if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not 0 < rate <= 1:
                    raise WorkflowError(f"{where}rate는 0~1 사이 소수")
            reject = _bool(s, "reject", False, where)
            to = s.get("reject_to")
            if to is not None and not reject:
                raise WorkflowError(f"{where}reject_to는 reject: true일 때만")
            if reject:
                to = to if to is not None else stages[-1]["name"]
                prev = {x["name"]: x for x in stages}
                if to not in prev:
                    raise WorkflowError(f"{where}reject_to는 앞 단계 이름이어야 함 ({to!r})")
                if prev[to]["select"] == "sample":
                    raise WorkflowError(f"{where}sample 단계로는 반려할 수 없음")
            st.update(kind="review", select=sel, rate=float(rate) if rate is not None else None, reject=reject,
                      reject_to=to if reject else None)
        stages.append(st)
    if len(stages) == 1 and stages[0]["submit"] == "on_index":
        raise WorkflowError("입력 단계 하나뿐인 흐름은 submit: on_index일 수 없음 (색인만으로 확정되어 검토가 없다)")
    samples = [s for s in stages if s["select"] == "sample"]
    if len(samples) > 1:
        raise WorkflowError("sample 단계는 하나까지")
    if samples:
        if samples[0] is not stages[-1]:
            raise WorkflowError("sample 단계는 마지막 단계여야 함 (표본 밖 문서는 검사 없이 확정되므로)")
        if samples[0]["reject"]:
            raise WorkflowError("sample 단계는 반려를 지원하지 않음")
    name = t.get("name", "")
    if not isinstance(name, str) or len(name) > 80:
        raise WorkflowError("name은 80자까지 글자")
    return {"name": name, "distinct_actors": _bool(t, "distinct_actors", True, ""),
            "count_unconfirmed": _bool(t, "count_unconfirmed", True, ""), "stages": stages}


def load(path=None):
    path = Path(path or os.environ.get("WORKFLOW") or DEFAULT)
    try:
        t = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise WorkflowError(f"흐름 설정을 읽을 수 없음: {path.name} ({type(e).__name__})") from None
    return parse(t)


# ---------- 위치 (순수 함수) ----------

def stage(wf, name):
    """이름으로 단계. 없으면 None (설정을 바꿔 사라진 단계 이름이 저장돼 있을 때)"""
    return next((s for s in wf["stages"] if s["name"] == name), None)


def entry(wf):
    return wf["stages"][0]


def sample_stage(wf):
    return next((s for s in wf["stages"] if s["select"] == "sample"), None)


def initial(wf):
    """색인이 끝난 철의 문서가 처음 놓이는 단계. 입력이 on_index면 입력은 이미 끝난 것으로 본다 (지금 동작)"""
    st = wf["stages"]
    return st[1]["name"] if st[0]["submit"] == "on_index" else st[0]["name"]  # 입력 하나뿐 + on_index는 parse가 거부


def next_stage(wf, name):
    s = stage(wf, name)
    if s is None:
        raise WorkflowError(f"모르는 단계 {name!r}")
    st = wf["stages"]
    return st[s["index"] + 1]["name"] if s["index"] + 1 < len(st) else CONFIRMED


def editable_positions(wf):
    """입력 경로(PATCH /documents)로 고칠 수 있는 위치: 입력 단계. 예외로 입력이 on_index이고 처음 놓이는 단계가
    표본(sample) 단계면 그 단계도 (기본 흐름 — 검사 전·검사 목록 밖 문서를 지금처럼 고친다).
    전수(all) 검수 단계에 있는 문서는 입력 경로로 못 고친다 — 검수자가 승인하며 고치거나 반려로 돌아와야 고친다"""
    st = wf["stages"]
    out = {st[0]["name"]}
    if st[0]["submit"] == "on_index" and st[1]["select"] == "sample":
        out.add(st[1]["name"])
    return out


def actionable(wf, name):
    """/workflow 경로로 승인·반려하는 단계인가 (sample 단계는 /inspection 경로)"""
    s = stage(wf, name)
    return s is not None and s["select"] != "sample"


def public(wf):
    """화면용 흐름 — 단계 목록과 단계마다 할 수 있는 일"""
    return {"name": wf["name"], "distinct_actors": wf["distinct_actors"], "count_unconfirmed": wf["count_unconfirmed"],
            "initial": initial(wf),
            "stages": [{k: s[k] for k in ("name", "label", "role", "kind", "select", "rate", "reject", "reject_to")}
                       | {"submit": s.get("submit"), "actions": s["select"] != "sample"} for s in wf["stages"]]}
