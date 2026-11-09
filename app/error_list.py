"""검수 오류목록 — 철 단위 오류목록 엑셀 (https://github.com/reddol18/docflow/issues/88).

새로 저장하는 것은 없다. 이미 있는 기록에서 만든다:
  - 문서의 지금 검토 표시 (documents.flags — 확신도·김해 규칙·기계 검수)
  - 수정 이력 edits (칸·이전 값·새 값·사용자·시각·단계 source) — 지금 실행 세대(stage_gen.since 뒤)만
  - 단계 승인·반려 기록 stage_log — 지금 세대(gen)만 (https://github.com/reddol18/docflow/issues/86)
  - 칸 검증 규칙 (app/field_rules) — 고치기 전 값을 '지금 스키마 규칙'으로 다시 검사해, 고쳐서 사라진 위반도 찾는다

행 하나 = 실제 오류 하나 (사용자 결정 2026-11-09: 표시만 붙고 사람이 고치지 않은 칸은 오류가 아니다).
(문서, 칸)마다 다음 중 하나면 오류 행:
  (가) 고친 칸 — 그 칸에 검토 표시가 있거나 처음 고치기 전 값이 칸 검증 규칙을 어겼을 때 (입력 단계 수정이라도)
  (나) 반려 뒤 다음 단계 기록 전까지 고친 칸. 반려 뒤 고친 칸이 없으면 문서 전체 행('(문서)') 하나
  (다) 검수 단계(표본 검사·설정 흐름 검수)에서 고친 칸
  (라) 지금도 남은 기계 검수 위반 — 고치지 않았으면 조치 '미조치'
오류 내용은 그 칸의 표시 사유·규칙 위반·반려 사유를 ' / '로 잇는다 (이유가 없는 (다)는 '검수 수정 · 단계').
표시(확신도 등)만 있고 수정이 없는 칸, 표시도 규칙 위반도 반려도 없는 입력 단계 수정은 넣지 않는다.

같은 (문서, 칸)의 수정은 한 행으로 묶는다: 수정 전 값 = 첫 수정의 이전 값, 수정 후 값 = 마지막 수정의 새 값,
담당자·조치일 = 마지막 수정. 재검수 판정 = 검수 단계마다(흐름 순서) 그 문서의 마지막 판정:
  합격 / 합격 (검수 수정)(그 단계가 이 칸을 고쳤다 — 지금 세대 수정 이력 기준) / 불합격(반려)
  재검수 전 — 그 단계가 승인한 뒤 뒤 단계의 반려로 그 단계 앞까지 되돌아가 아직 다시 보지 않음
  그 단계의 판정이 여러 번이면 '(N차)' (그 단계 판정 횟수). 판정이 하나도 없으면 '미판정'(표본 검사 목록 밖은 '검사 제외')

이 모듈은 DB를 부르지 않는다 (순수 함수) — 값은 app/main.py가 store에서 읽어 넘긴다.
"""
import hashlib
import hmac
import os
from collections import Counter
from pathlib import Path

import yaml

from app import export, field_rules, fields, workflow
from app.store import KST, stage_of_source

DEFAULT_TEMPLATE = Path(__file__).parent / "export_templates" / "error_list.yaml"
ACTORS = ("masked", "name")  # 담당자 칸: 가린 ID(기본) | 표시 이름
TOP_KEYS = {"name", "actor", "sheet"}
SOURCES = {"vol", "doc.seq", "doc.id", "doc.first_page", "doc.last_page", "field.name", "field.label",
           "error.reason", "error.kind", "value.before", "value.after", "action", "actor", "action.date", "verdict",
           "const"}
CELL_TEXT_MAX = 32767  # 엑셀 칸 하나의 글자 수 상한
DOC_LABEL = "(문서)"
MASK_LEN = 8  # 가린 ID의 16진 자리 수 (32비트 — 한 파일 안 사용자 수에서 겹칠 일 없음)


def _check_source(src):
    if src not in SOURCES:
        raise export.TemplateError(f"sheet: 모르는 source {src!r}")


def parse_template(t):
    """오류목록 양식 dict → 검사한 양식. 모르는 키·원천·형식, 모르는 actor는 거부"""
    if not isinstance(t, dict):
        raise export.TemplateError("오류목록 양식은 사전(dict)이어야 함")
    if set(t) - TOP_KEYS:
        raise export.TemplateError(f"모르는 키: {sorted(set(t) - TOP_KEYS)}")
    actor = t.get("actor", "masked")
    if actor not in ACTORS:
        raise export.TemplateError(f"actor는 {' | '.join(ACTORS)}")
    return {"name": str(t.get("name", "")), "actor": actor, "sheet": export.parse_sheet("sheet", t.get("sheet"), _check_source)}


def load_template(path=None):
    path = Path(path or os.environ.get("ERROR_LIST_TEMPLATE") or DEFAULT_TEMPLATE)
    return parse_template(yaml.safe_load(path.read_text(encoding="utf-8")))


# ---------- 오류 모으기 ----------

def _edit_stage(wf, source):
    """수정 이력 source → 흐름의 단계 이름 (store.stage_of_source 하나로 판단). 'inspect'는 흐름의 표본 단계.
    입력 단계 수정이면 None"""
    st = stage_of_source(source)
    if st == "entry":
        return None
    if st == "inspect":
        sample = workflow.sample_stage(wf)
        return sample["name"] if sample else st
    return st


def _date(ts):
    return ts.astimezone(KST).date().isoformat() if ts is not None else ""


def _verdict(wf, logs, fixed_at, field, status):
    """검수 단계마다 마지막 판정. fixed_at = {(칸, 단계)} 그 단계가 고친 칸 (지금 세대 수정 이력).
    승인 뒤에 뒤 단계가 반려해 그 단계 앞으로 되돌아갔으면(아직 다시 안 봄) '재검수 전'"""
    idx = {s["name"]: s["index"] for s in wf["stages"]}
    rejects = [x for x in logs if x["action"] == "reject"]
    parts = []
    for st in wf["stages"][1:]:
        ds = [x for x in logs if x["stage"] == st["name"] and x["action"] in ("approve", "reject")]
        if not ds:
            continue
        last = ds[-1]
        if last["action"] == "reject":
            word = "불합격(반려)"
        elif any(r["id"] > last["id"] and idx.get(r["to_stage"], -1) < st["index"] for r in rejects):
            parts.append(f"{st['label']}: 재검수 전")
            continue
        else:
            word = "합격 (검수 수정)" if field and (field, st["name"]) in fixed_at else "합격"
        if len(ds) > 1:
            word += f" ({len(ds)}차)"
        parts.append(f"{st['label']}: {word}")
    if not parts:
        return "검사 제외" if status == "excluded" else "미판정"
    return " · ".join(parts)


class _Timeline:
    """칸 하나의 문서별 '그 시각 바로 전 값' 색인 — 철 안 중복 다시 보기용. 지금 값 개수(Counter)를 한 번 만들고,
    그 칸을 고친 문서만 시각에 맞춰 바꿔 센다 (고친 문서 수² — 문서 수×문서 수가 아니다)"""

    def __init__(self, docs, ed, name):
        self.cur = {d["doc_id"]: str((d.get("fields") or {}).get(name) or "").strip() for d in docs}
        self.count = Counter(v for v in self.cur.values() if v)
        self.edited = {did: by[name] for did, by in ed.items() if name in by and did in self.cur}

    def duplicated(self, did, value, ts):
        """ts 바로 전 철에서 value(앞뒤 빈칸 뗌)를 다른 문서도 가졌나"""
        v = value.strip()
        if not v:
            return False
        n = self.count[v] - (self.cur.get(did) == v)
        for o, es in self.edited.items():
            if o == did:
                continue
            then = next(((e["old"] or "").strip() for e in es if e["ts"] >= ts), self.cur[o])
            n += (then == v) - (self.cur[o] == v)
        return n > 0


def collect(vol, docs, edits, logs, schema, wf, status):
    """철 하나의 오류 행 목록. docs는 쪽 순서(export.ordered_docs), edits·logs는 지금 세대 기록(id 순),
    status는 {doc_id: positions의 status}. 행은 양식 원천 이름을 키로 갖는 dict (+ 담당자 _actor)"""
    flag_field = fields.flag_keys(schema)
    by = fields.by_name(schema)
    order = {f["name"]: i for i, f in enumerate(schema["fields"])}
    labels = {s["name"]: s["label"] for s in wf["stages"]}
    ed = {}  # doc_id -> 칸 -> [수정]
    for e in edits:
        ed.setdefault(e["doc_id"], {}).setdefault(e["field"], []).append(e)
    lg = {}
    for x in logs:
        lg.setdefault(x["doc_id"], []).append(x)
    unique = {f["name"]: _Timeline(docs, ed, f["name"]) for f in field_rules.ruled(schema) if f["validate"].unique}
    rows = []
    for seq, d in enumerate(docs, 1):
        did = d["doc_id"]
        cur = d.get("fields") or {}
        mine, dlogs = ed.get(did, {}), lg.get(did, [])
        errs = {}

        def add(field, reason, kind, errs=errs):
            e = errs.setdefault(field, {"reasons": [], "kinds": []})
            if reason not in e["reasons"]:
                e["reasons"].append(reason)
            if kind not in e["kinds"]:
                e["kinds"].append(kind)

        flagged = {}  # 칸 -> [(사유, 기계 검수인가)]
        for k, rs in (d.get("flags") or {}).items():
            for r in rs:
                flagged.setdefault(flag_field.get(k, k), []).append((r, field_rules.is_machine(r)))
        # (가) 고친 칸: 표시가 있거나 고치기 전 값이 규칙을 어김
        for name, es in mine.items():
            for r, machine in flagged.get(name, ()):
                add(name, r, "규칙" if machine else "표시")
            f = by.get(name)
            if f and isinstance(f.get("validate"), field_rules.Rules):
                before, t0 = es[0]["old"] or "", es[0]["ts"]
                ks = list(field_rules.check_value(f["validate"], before))
                if name in unique and unique[name].duplicated(did, before, t0):
                    ks.append("unique")
                for k in ks:
                    add(name, field_rules.reason(f, k), "규칙")
        # (라) 지금도 남은 기계 검수 위반 (고치지 않았으면 미조치)
        for name, rs in flagged.items():
            if any(machine for _, machine in rs):
                for r, machine in rs:
                    add(name, r, "규칙" if machine else "표시")
        # (나) 반려 뒤 다음 단계 기록 전까지 고친 칸
        doc_rows = []
        for i, x in enumerate(dlogs):
            if x["action"] != "reject":
                continue
            nxt = dlogs[i + 1] if i + 1 < len(dlogs) else None
            text = f"반려 · {labels.get(x['stage'], x['stage'])}" + (f": {x['reason']}" if x.get("reason") else "")
            touched = [n for n, es in mine.items()
                       if any(e["ts"] > x["ts"] and (nxt is None or e["ts"] < nxt["ts"]) for e in es)]
            for n in touched:
                for r, machine in flagged.get(n, ()):
                    add(n, r, "규칙" if machine else "표시")
                add(n, text, "반려")
            if not touched:
                doc_rows.append((x, nxt, text))
        # (다) 검수 단계에서 고친 칸
        fixed_at = set()
        for name, es in mine.items():
            for e in es:
                st = _edit_stage(wf, e["source"])
                if st:
                    fixed_at.add((name, st))
                    if not errs.get(name):
                        for r, machine in flagged.get(name, ()):
                            add(name, r, "규칙" if machine else "표시")
                    if not errs.get(name):
                        add(name, f"검수 수정 · {labels.get(st, st)}", "검수 수정")

        base = {"vol": vol, "doc.seq": seq, "doc.id": did, "doc.first_page": d["pages"][0], "doc.last_page": d["pages"][-1]}

        for name in sorted(errs, key=lambda n: (order.get(n, len(order)), n)):
            es = mine.get(name, [])
            last = es[-1] if es else None
            rows.append({**base, "field.name": name, "field.label": by[name]["label"] if name in by else name,
                         "error.reason": " / ".join(errs[name]["reasons"]), "error.kind": "·".join(errs[name]["kinds"]),
                         "value.before": (es[0]["old"] or "") if es else (cur.get(name) or ""),
                         "value.after": (last["new"] or "") if last else "",
                         "action": "수정" if last else "미조치",
                         "action.date": _date(last["ts"] if last else None),
                         "verdict": _verdict(wf, dlogs, fixed_at, name, status.get(did)), "_actor": last})
        for _, nxt, text in doc_rows:
            rows.append({**base, "field.name": "", "field.label": DOC_LABEL, "error.reason": text, "error.kind": "반려",
                         "value.before": "", "value.after": "", "action": "재제출" if nxt else "미조치",
                         "action.date": _date(nxt["ts"] if nxt else None),
                         "verdict": _verdict(wf, dlogs, fixed_at, None, status.get(did)), "_actor": nxt})
    return rows


# ---------- 엑셀 ----------

def masked_id(key, user_id):
    """가린 ID: 내보내기마다 새로 만든 비밀 키(key)로 사용자 id를 HMAC-SHA256 한 앞 MASK_LEN자리.
    키는 응답 뒤 버린다 → 사용자 id·로그인 이름으로 되돌리거나 다른 파일과 잇지 못한다. 한 파일 안에서는 같은 사람이 같은 값"""
    if user_id is None:
        return ""
    return "담당자-" + hmac.new(key, str(user_id).encode(), hashlib.sha256).hexdigest()[:MASK_LEN]


def actor(t, key, row):
    """담당자 칸. name: 사용자의 표시 이름(SSO 사용자는 ' (SSO)' — 수정 이력 화면과 같은 구별). 표시 이름이 없으면
    로그인 이름 대신 가린 ID. masked(기본): 가린 ID"""
    who = row.get("_actor")
    if not who:
        return ""
    if t["actor"] == "name" and who.get("name"):
        return who["name"] + (" (SSO)" if who.get("sso") else "")
    return masked_id(key, who.get("user_id"))


def build(t, rows, key):
    """오류 행 → 시트 {제목: [머리 행, 값 행...]} (export.to_xlsx로 쓴다). 형식 변환은 납품 양식과 같은 export.convert"""
    s = t["sheet"]
    out = [[c["name"] for c in s["columns"]]]
    for r in rows:
        vals = {**r, "actor": actor(t, key, r)}
        line = []
        for c in s["columns"]:
            v, _ = export.convert(c["value"] if c["source"] == "const" else vals.get(c["source"], ""), c)
            line.append(v[:CELL_TEXT_MAX] if isinstance(v, str) else v)
        out.append(line)
    return {s["title"]: out}
