"""검토 표시 규칙. 그래프(validate 노드)와 평가 스크립트(eval/flag_whatif.py)가 같은 코드를 쓴다.

app.fields(스키마 도우미)·app.field_rules(칸 검증 규칙) 말고는 표준 라이브러리만 → 저장된 결과만으로 규칙·기준값을 바꿔
다시 계산할 수 있다. 칸 검증 규칙 위반(기계 검수)은 machine·refresh (https://github.com/reddol18/docflow/issues/87).
문서 dict는 graph.merge가 만드는 모양: fields / b / hw / info.
"""
import collections
import datetime as dt
import re
from typing import NamedTuple

from app import field_rules
from app.fields import by_rule, flag_key, is_shown

LEGACY = {
    "conf_serial": 0.9,   # 손글씨 판독 확신도가 이보다 낮으면 표시
    "conf_date": 0.8,
    "conf_llm": 0.8,      # 수신·제목: GPT 자기 확신도가 이보다 낮으면 표시
    "date_flow_days": 45,  # 앞뒤 3건 날짜 범위에서 이만큼 벗어나면 '흐름'
    "mismatch_date": True,  # 일자 B 값과 손글씨 판독이 다르면 표시 (끄는 쪽도 스윕에서 시험)
    "rules": (),           # 추가 규칙 이름 (RULES 참고). 빈 값이면 기존 규칙만
}
# 기본값 선택 근거와 한계는 eval/flag_whatif.py 출력(--sweep, --loo)과 issues/2 PR 참고.
# 요약: 김해 7개 철 164건에서 '표시 없이 틀린 문서' 3% 이하 중 표시 비율이 가장 낮은 설정.
# 9.8%(16건) → 2.4%(4건, 목표까지 여유 1건), 대신 표시된 문서 80% → 90%.
# 기준값·규칙 선택 자체가 같은 164건(한 기관)에서 한 것이라 낙관적이다:
# 철 하나 빼고 고르기(--loo)로 재면 표시 없이 틀림 6.7%(11건), 표시 88% → 목표 3%를 안정적으로 맞추지 못한다.
# 표시 80% 이하에서는 이전보다 낮은 기준값·규칙 끄기까지 넣어도 최선이 9.1%(--sweep). 다른 자료로 재확인(issues/8).
DEFAULT = {**LEGACY, "conf_serial": 0.95, "conf_date": 0.9, "conf_llm": 0.9, "rules": ("doc_head", "rec_sign")}

# 추가 규칙 (eval/flag_whatif.py --sweep이 모든 조합을 시험한다)
RULES = {
    "doc_head": "문서번호: 부서 약칭(한글)으로 시작하지 않음 → 앞부분을 못 읽음",
    "day1": "일자: 일이 1일 → 일이 비어 있는 칸을 1로 채웠을 가능성",
    "order": "일자: 같은 분류번호·연도를 일련번호로 줄 세웠을 때 바로 앞 번호보다 이르거나 바로 뒤 번호보다 늦음(3일 여유)",
    "rec_sign": "수신: 끝에 결재란 직위(국장·실장·부시장·과장·계장 등)가 섞임",
    "rec_part": "수신: 주소만 있고 받는 곳 이름이 없거나, 업체·대표만 있고 주소가 없음",
}

SIGN = re.compile(r"(?:^|\s)(국\s*장|실\s*장|부\s*시\s*장|부\s*군\s*수|과\s*장|계\s*장|담\s*당|국|과)$"
                  r"|\s(국장|실장|부시장|부군수)\s")  # 끝에 붙거나, 낱말 하나로 가운데 끼어든 결재란 직위
ADDR = re.compile(r"(시|군|구|읍|면|동|리|가)\s*\d|번지|\d+-\d+")
PLACE = re.compile(r"[가-힣]+(시|군|구|읍|면|동|리)(\s|$|\d)")
TAIL = re.compile(r"(대표|사장|귀하|\(주\)|㈜|주식회사|상사|산업|공업|기업|금속|정밀|화학|기계)")


def _date(s):
    try:
        return dt.date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _serial(s):
    return int(s) if re.fullmatch(r"\d{1,5}", s or "") else None


ROLES = ("docno", "serial", "date", "recipient")  # 규칙이 보는 칸 = 스키마에서 이 rule 이름을 가진 칸 (app/graph.py RULES)
# 손글씨 칸 판독 값을 쓰는 규칙 — 이 칸들의 확신도는 LLM 자기 확신도가 아니라 판독(info.conf_*)으로 본다 (app/graph.py HW_CELLS 키)
HW_RULES = frozenset({"docno", "serial", "date"})
LLM_METHODS = ("llm_extract", "vocab")  # LLM 확신도를 보는 칸의 추출 방식 (llm_generate 요약은 원문에 맞춰 볼 값이 아니라 뺀다)


class Targets(NamedTuple):
    """규칙이 볼 칸 (targets()가 스키마에서 만든다)
    roles {역할: 칸 이름}            ROLES 중 스키마에 있는 것. 없는 역할의 규칙은 건너뛴다
    llm   {규칙 안 키: 칸 이름}       LLM 확신도를 볼 칸. 규칙 안 키는 역할 칸이면 역할 이름(사유가 같은 목록에 붙게), 아니면 'llm:칸'
    keys  {규칙 안 키: 표시 키}       결과 키 (칸의 flag_key)"""
    roles: dict
    llm: dict
    keys: dict


# 김해 칸 이름 그대로인 문서용 (flags(docs) 기본값 — 규칙 시험·옛 호출). 스키마가 있으면 targets(schema)를 쓴다
LEGACY_TARGETS = Targets(roles={r: r for r in ROLES}, llm={"recipient": "recipient", "llm:title": "title"},
                         keys={"docno": "serial", "serial": "serial", "date": "date", "recipient": "recipient",
                               "llm:title": "title"})


def targets(schema):
    """스키마에서 규칙이 볼 칸을 rule 이름으로 찾는다 (칸 이름은 기관마다 다르다 — https://github.com/reddol18/docflow/issues/72).
    LLM 확신도는 화면에 보이는 건 단위 칸(숨김·철 단위 제외) 중 prompt가 있는 추출 칸만 — 손글씨 규칙 칸(HW_RULES, 판독
    확신도로 따로 봄)과 요약(llm_generate) 제외. 다른 표시 원천(예: 칸 검증 규칙)을 더할 때도 표시 키는 flag_key로 맞춘다."""
    roles = {r: f["name"] for r in ROLES if (f := by_rule(schema, r))}
    role_of = {n: r for r, n in roles.items()}
    llm = {role_of.get(f["name"], "llm:" + f["name"]): f["name"] for f in schema["fields"]
           if "prompt" in f and is_shown(f) and f.get("rule") not in HW_RULES and (f["method"] in LLM_METHODS or "rule" in f)}
    by = {f["name"]: f for f in schema["fields"]}
    return Targets(roles, llm, {k: flag_key(by[n]) for k, n in {**roles, **llm}.items()})


def for_schema(docs, schema, cfg=None):
    """스키마 칸 이름을 쓰는 문서(graph.merge 모양)에 규칙을 돌려 문서마다 {표시 키: [사유]}"""
    return flags(docs, cfg, targets(schema))


def flags(docs, cfg=None, t=LEGACY_TARGETS):
    """docs(철 하나, 쪽 순서)에 대해 문서마다 {표시 키: [사유]}를 돌려준다.
    t(Targets)가 역할 → 칸 이름을 정한다 — 문서 칸 키로 규칙을 고르지 않는다. 기본은 김해 칸 이름(LEGACY_TARGETS)."""
    c = {**DEFAULT, **(cfg or {})}
    rules = set(c["rules"])
    roles = t.roles

    def v(d, r):  # 역할 r 칸의 값
        return d["fields"].get(roles[r]) or ""
    years = collections.Counter(v(d, "date")[:4] for d in docs if v(d, "date")) if "date" in roles else {}
    ok_years = {y for y, n in years.items() if n >= 3}
    out = []
    for i, d in enumerate(docs):
        info = d.get("info") or {}
        nb = docs[max(0, i - 3):i] + docs[i + 1:i + 4]
        fl = {k: [] for k in dict.fromkeys(("serial", "date", "recipient", *t.llm))}

        # ---- 일자
        if "date" in roles:
            date = v(d, "date")
            day = _date(date) if date else None
            if not day:
                fl["date"].append("형식")
            else:
                if date[:4] not in ok_years:
                    fl["date"].append("범위")
                nd = [x for x in (_date(v(o, "date")) for o in nb) if x]
                w = dt.timedelta(c["date_flow_days"])
                if len(nd) >= 2 and not (min(nd) - w <= day <= max(nd) + w):
                    fl["date"].append("흐름")
                if "day1" in rules and day.day == 1:
                    fl["date"].append("1일")
                if "order" in rules and "serial" in roles and _order_conflict(d, day, docs, v):
                    fl["date"].append("순서")
            if info["conf_date"] and info["conf_date"] < c["conf_date"]:
                fl["date"].append("확신도")

        # ---- 일련번호
        if "serial" in roles:
            serial = v(d, "serial")
            year = v(d, "date")[:4] if "date" in roles else ""
            if info["blank"]:
                if info["conf_serial"] < c["conf_serial"]:
                    fl["serial"].append("빈칸")
            elif _serial(serial) is None:
                fl["serial"].append("형식")
            else:
                n = int(serial)
                ns = [_serial(v(o, "serial")) for o in nb
                      if _serial(v(o, "serial")) is not None and o["info"]["prefix"] == info["prefix"]
                      and (v(o, "date")[:4] if "date" in roles else "") == year]
                if len(ns) >= 2:
                    lo, hi = min(ns), max(ns)
                    m = max(50, 0.3 * hi)
                    if not (lo - m <= n <= hi + m):
                        fl["serial"].append("흐름")
            if info["conf_serial"] and info["conf_serial"] < c["conf_serial"]:
                fl["serial"].append("확신도")
            docno = v(d, "docno") if "docno" in roles else ""
            if "doc_head" in rules and docno and not re.match(r"[가-힣]", docno):
                fl["serial"].append("앞부분 없음")

        # ---- 불일치: GPT 텍스트 추출(B)과 손글씨 칸 판독이 다름. 비전 판독은 틀려도 확신도가 높아서
        # 확신도만으로는 틀린 일련번호의 56%, 일자의 23%만 잡혔다 (6개 철 129건)
        if "serial" in roles and info["b_serial"] and info["b_serial"] != v(d, "serial"):
            fl["serial"].append("불일치")
        if "date" in roles and c["mismatch_date"] and info["b_date"] and info["b_date"] != v(d, "date"):
            fl["date"].append("불일치")

        # ---- LLM 확신도 (김해: 수신·제목)
        b = d.get("b") or {}
        for k, name in t.llm.items():
            if name in b and b[name]["confidence"] < c["conf_llm"]:
                fl[k].append("LLM")
        # ---- 수신
        if "recipient" in roles:
            rec = v(d, "recipient").strip()
            if "rec_sign" in rules and SIGN.search(rec) and not re.search(r"(시장|군수|지사|청장|서장|본부장)\s*\S*과\s*장$", rec):
                fl["recipient"].append("결재란")
            if "rec_part" in rules and rec and rec != "내부결재":
                has_addr = bool(ADDR.search(rec) or PLACE.search(rec))
                if has_addr and not TAIL.search(rec) and not re.search(r"(장|관|처장|소장)$", rec):
                    fl["recipient"].append("이름 없음")
                elif not has_addr and re.search(r"대표$", rec):
                    fl["recipient"].append("주소 없음")
        o = {}
        for k, reasons in fl.items():
            if reasons:
                o.setdefault(t.keys.get(k, k), []).extend(reasons)
        out.append(o)
    return out


def machine(docs, schema):
    """칸 검증 규칙(스키마 validate — app/field_rules.py) 위반 → 문서마다 {표시 키: [field_rules.reason 사유]}.
    표시 키는 칸의 flag_key. 규칙이 없는 스키마는 모두 빈 dict (https://github.com/reddol18/docflow/issues/87)"""
    out = []
    for vs in field_rules.violations(docs, schema):
        o = {}
        for f, k in vs:
            o.setdefault(flag_key(f), []).append(field_rules.reason(f, k))
        out.append(o)
    return out


def refresh(docs, schema):
    """사람이 값을 고친 뒤 기계 검수 표시를 다시 계산한다 (철 전체 — 철 안 중복은 다른 문서 값에 달려 있다).
    다른 표시(확신도·김해 규칙)는 그대로 둔다. 표시가 바뀐 문서만 새 dict, 나머지는 같은 객체를 돌려준다
    (부른 쪽이 바뀐 문서만 저장한다). 바뀐 문서가 없으면 docs 그대로 — 규칙 없는 스키마는 지금과 같다.
    검토 제출(graph.review)·문서 수정·검사·단계 승인(main._edit_document)이 이것 하나를 부른다."""
    # 기계 검수 사유는 서버 상수 머리(field_rules.PREFIX)로 가린다 — 스키마 규칙을 빼거나 바꾼 뒤 남은 예전 사유도 지운다.
    # 바뀐 문서가 없으면 같은 목록 그대로 (규칙 없는 김해·ISAD(G)는 기계 검수 사유가 없어 늘 그대로)
    out, changed = [], False
    for d, mf in zip(docs, machine(docs, schema)):
        old = d.get("flags") or {}
        fl = {}
        for k, rs in old.items():
            keep = [x for x in rs if not field_rules.is_machine(x)]
            if keep:
                fl[k] = keep
        for k, rs in mf.items():
            fl.setdefault(k, []).extend(rs)
        same = fl == old
        changed = changed or not same
        out.append(d if same else dict(d, flags=fl))
    return out if changed else docs


def _order_conflict(d, day, docs, v, slack_days=3):
    """일련번호는 시간 순으로 매기므로, 같은 분류번호·연도에서 바로 앞 번호 문서보다 날짜가 이르거나
    바로 뒤 번호 문서보다 늦으면 어긋남. (철 전체와 비교하면 다른 문서의 틀린 번호 하나가 여러 문서를 끌고 들어간다)
    v(문서, 역할) = 역할 칸 값"""
    s, pre = _serial(v(d, "serial")), d["info"]["prefix"]
    if s is None or not pre:
        return False
    same = [(_serial(v(o, "serial")), _date(v(o, "date"))) for o in docs
            if o is not d and o["info"]["prefix"] == pre]
    same = [(n, x) for n, x in same if n is not None and x is not None and x.year == day.year and n != s]
    prev = max((p for p in same if p[0] < s), default=None)
    nxt = min((p for p in same if p[0] > s), default=None)
    w = dt.timedelta(slack_days)
    return bool((prev and prev[1] > day + w) or (nxt and nxt[1] < day - w))
