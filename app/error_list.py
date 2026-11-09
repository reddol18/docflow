"""검수 오류목록 — 철 단위 오류목록 엑셀 (https://github.com/reddol18/docflow/issues/88).

새로 저장하는 것은 없다. 이미 있는 기록에서 만든다:
  - 문서의 지금 검토 표시 (documents.flags — 확신도·김해 규칙·기계 검수)
  - 수정 이력 edits (칸·이전 값·새 값·사용자·시각·단계 source) — 지금 실행 세대(stage_gen.since 뒤)만
  - 단계 승인·반려 기록 stage_log — 지금 세대(gen)만 (https://github.com/reddol18/docflow/issues/86)
  - 칸 검증 규칙 (app/field_rules) — 고치기 전 값을 다시 검사해, 고쳐서 지금은 사라진 기계 검수 위반도 찾는다

행 하나 = 오류 하나. 오류는 (문서, 칸)마다 모은다 — 그 칸에 다음 중 하나라도 있으면 오류 행:
  표시      지금 검토 표시가 붙어 있다 (기계 검수 사유는 '규칙')
  규칙      처음 고치기 전 값이 칸 검증 규칙을 어겼다 (철 안 중복은 그 시각 다른 문서 값과 비교)
  반려      검수 단계가 반려한 뒤 다음 단계 기록 전까지 고친 칸 (반려 사유)
  검수 수정  검수 단계(표본 검사·설정 흐름 검수)에서 고친 칸 — 위 이유가 하나도 없을 때만
그 밖의 입력 단계 수정(표시도 규칙 위반도 반려도 없는 칸)은 오류가 아니라 입력 작업이라 넣지 않는다.
반려했는데 그 뒤 고친 칸이 없으면 문서 전체 행('(문서)') 하나.

같은 (문서, 칸)의 수정은 한 행으로 묶는다: 수정 전 값 = 첫 수정의 이전 값, 수정 후 값 = 마지막 수정의 새 값,
담당자·조치일 = 마지막 수정. 재검수 판정 = 조치 뒤(마지막 수정 시각보다 늦은) 검수 단계마다 마지막 판정 —
설정 흐름의 입력 다음 단계들 순서로 '단계 표시명: 합격 / 합격 (검수 수정) / 불합격(반려)', 재검수 차례면 '(2차)'.
조치 뒤 판정이 아직 없는 단계는 '재검수 전', 판정이 하나도 없으면 '미판정'(표본 검사 목록 밖이면 '검사 제외').

이 모듈은 DB를 부르지 않는다 (순수 함수) — 값은 app/main.py가 store에서 읽어 넘긴다.
"""
import datetime as dt
import hashlib
import hmac
import os
from pathlib import Path

import yaml

from app import export, field_rules, fields

DEFAULT_TEMPLATE = Path(__file__).parent / "export_templates" / "error_list.yaml"
KST = dt.timezone(dt.timedelta(hours=9))
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

def _stage_labels(wf):
    labels = {s["name"]: s["label"] for s in wf["stages"]}
    sample = next((s for s in wf["stages"] if s["select"] == "sample"), None)
    labels.setdefault("inspect", sample["label"] if sample else "표본 검사")  # 이력 source 'inspect' = 표본 검사 단계
    return labels


def _review_stage(source):
    """검수 단계에서 고친 수정이면 그 단계 이름 (inspect·stage:<단계>), 입력 단계 수정이면 None"""
    if source == "inspect":
        return "inspect"
    if source.startswith("stage:"):
        return source[len("stage:"):]
    return None


def _date(ts):
    return ts.astimezone(KST).date().isoformat() if ts is not None else ""


def _verdict(wf, labels, logs, since, field, status):
    """조치(since 시각) 뒤 검수 단계마다 마지막 판정. since가 None이면 조치가 없음 — 마지막 판정 그대로"""
    parts = []
    for st in wf["stages"][1:]:
        last = None
        for x in logs:
            if x["stage"] == st["name"] and x["action"] in ("approve", "reject"):
                last = x
        if last is None:
            continue
        if since is not None and last["ts"] <= since:
            parts.append(f"{st['label']}: 재검수 전")
            continue
        if last["action"] == "approve":
            word = "합격 (검수 수정)" if field and field in (last.get("fixed_fields") or ()) else "합격"
        else:
            word = "불합격(반려)"
        if last["round"] > 1:
            word += f" ({last['round']}차)"
        parts.append(f"{st['label']}: {word}")
    if not parts:
        return "검사 제외" if status == "excluded" else "미판정"
    return " · ".join(parts)


def _value_at(by_field, cur, field, ts):
    """칸 값의 그 시각 바로 전 모습: ts 이후 첫 수정의 이전 값, 없으면 지금 값. 같은 시각(한 번에 제출한 검토)도 이전 값"""
    for e in by_field.get(field, ()):
        if e["ts"] >= ts:
            return e["old"] or ""
    return cur or ""


def collect(vol, docs, edits, logs, schema, wf, status):
    """철 하나의 오류 행 목록. docs는 쪽 순서(export.ordered_docs), edits·logs는 지금 세대 기록(id 순),
    status는 {doc_id: positions의 status}. 행은 양식 원천 이름을 키로 갖는 dict (+ 담당자 _actor_id·_actor_name)"""
    flag_field = fields.flag_keys(schema)
    by = fields.by_name(schema)
    order = {f["name"]: i for i, f in enumerate(schema["fields"])}
    labels = _stage_labels(wf)
    ed = {}  # doc_id -> 칸 -> [수정]
    for e in edits:
        ed.setdefault(e["doc_id"], {}).setdefault(e["field"], []).append(e)
    lg = {}
    for x in logs:
        lg.setdefault(x["doc_id"], []).append(x)
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

        # 표시: 지금 검토 표시 (표시 키 → 사람이 고치는 칸)
        for k, rs in (d.get("flags") or {}).items():
            for r in rs:
                add(flag_field.get(k, k), r, "규칙" if field_rules.is_machine(r) else "표시")
        # 규칙: 처음 고치기 전 값이 칸 검증 규칙을 어겼나 (고쳐서 사라진 기계 검수 위반)
        for name, es in mine.items():
            f = by.get(name)
            if not f or not isinstance(f.get("validate"), field_rules.Rules):
                continue
            r, before, t0 = f["validate"], es[0]["old"] or "", es[0]["ts"]
            ks = list(field_rules.check_value(r, before))
            if r.unique and before.strip():
                others = {_value_at(ed.get(o["doc_id"], {}), (o.get("fields") or {}).get(name), name, t0).strip()
                          for o in docs if o["doc_id"] != did}
                if before.strip() in others:
                    ks.append("unique")
            for k in ks:
                add(name, field_rules.reason(f, k), "규칙")
        # 반려: 반려 뒤 다음 단계 기록 전까지 고친 칸
        doc_rows = []
        for i, x in enumerate(dlogs):
            if x["action"] != "reject":
                continue
            nxt = dlogs[i + 1] if i + 1 < len(dlogs) else None
            text = f"반려 · {labels.get(x['stage'], x['stage'])}" + (f": {x['reason']}" if x.get("reason") else "")
            touched = [n for n, es in mine.items()
                       if any(e["ts"] > x["ts"] and (nxt is None or e["ts"] < nxt["ts"]) for e in es)]
            for n in touched:
                add(n, text, "반려")
            if not touched:
                doc_rows.append((x, nxt, text))
        # 검수 수정: 검수 단계에서 고쳤는데 다른 이유가 없는 칸
        for name, es in mine.items():
            for e in es:
                st = _review_stage(e["source"])
                if st and not errs.get(name):
                    add(name, f"검수 수정 · {labels.get(st, st)}", "검수 수정")

        base = {"vol": vol, "doc.seq": seq, "doc.id": did, "doc.first_page": d["pages"][0], "doc.last_page": d["pages"][-1]}
        for name in sorted(errs, key=lambda n: (order.get(n, len(order)), n)):
            es = mine.get(name, [])
            last = es[-1] if es else None
            rows.append({**base, "field.name": name, "field.label": by[name]["label"] if name in by else name,
                         "error.reason": " / ".join(errs[name]["reasons"]), "error.kind": "·".join(errs[name]["kinds"]),
                         "value.before": (es[0]["old"] or "") if es else (cur.get(name) or ""),
                         "value.after": (last["new"] or "") if last else "",
                         "action": "수정" if last else "수정 없음",
                         "action.date": _date(last["ts"] if last else None),
                         "verdict": _verdict(wf, labels, dlogs, last["ts"] if last else None, name, status.get(did)),
                         "_actor_id": last["user_id"] if last else None, "_actor_name": last["user_name"] if last else None})
        for x, nxt, text in doc_rows:
            rows.append({**base, "field.name": "", "field.label": DOC_LABEL, "error.reason": text, "error.kind": "반려",
                         "value.before": "", "value.after": "", "action": "재제출" if nxt else "미조치",
                         "action.date": _date(nxt["ts"] if nxt else None),
                         "verdict": _verdict(wf, labels, dlogs, (nxt or x)["ts"], None, status.get(did)),
                         "_actor_id": nxt["user_id"] if nxt else None, "_actor_name": nxt["user_name"] if nxt else None})
    return rows


# ---------- 엑셀 ----------

def masked_id(key, user_id):
    """가린 ID: 내보내기마다 새로 만든 비밀 키(key)로 사용자 id를 HMAC-SHA256 한 앞 MASK_LEN자리.
    키는 응답 뒤 버린다 → 사용자 id·로그인 이름으로 되돌리거나 다른 파일과 잇지 못한다. 한 파일 안에서는 같은 사람이 같은 값"""
    if user_id is None:
        return ""
    return "담당자-" + hmac.new(key, str(user_id).encode(), hashlib.sha256).hexdigest()[:MASK_LEN]


def actor(t, key, row):
    if t["actor"] == "name":
        return row.get("_actor_name") or ""
    return masked_id(key, row.get("_actor_id"))


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
