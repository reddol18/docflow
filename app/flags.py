"""검토 표시 규칙. 그래프(validate 노드)와 평가 스크립트(eval/flag_whatif.py)가 같은 코드를 쓴다.

외부 의존 없음(표준 라이브러리만) → 저장된 결과만으로 규칙·기준값을 바꿔 다시 계산할 수 있다.
문서 dict는 graph.merge가 만드는 모양: fields / b / hw / info.
"""
import collections
import datetime as dt
import re

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


def flags(docs, cfg=None):
    """docs(철 하나, 쪽 순서)에 대해 문서마다 {칸: [사유]}를 돌려준다."""
    c = {**DEFAULT, **(cfg or {})}
    rules = set(c["rules"])
    years = collections.Counter(d["fields"]["date"][:4] for d in docs if d["fields"]["date"])
    ok_years = {y for y, n in years.items() if n >= 3}
    out = []
    for i, d in enumerate(docs):
        f, info = d["fields"], d["info"]
        nb = docs[max(0, i - 3):i] + docs[i + 1:i + 4]
        fl = {k: [] for k in ["serial", "date", "recipient", "title"]}

        # ---- 일자
        day = _date(f["date"]) if f["date"] else None
        if not day:
            fl["date"].append("형식")
        else:
            if f["date"][:4] not in ok_years:
                fl["date"].append("범위")
            nd = [x for x in (_date(o["fields"]["date"]) for o in nb) if x]
            w = dt.timedelta(c["date_flow_days"])
            if len(nd) >= 2 and not (min(nd) - w <= day <= max(nd) + w):
                fl["date"].append("흐름")
            if "day1" in rules and day.day == 1:
                fl["date"].append("1일")
            if "order" in rules and _order_conflict(d, day, docs):
                fl["date"].append("순서")
        if info["conf_date"] and info["conf_date"] < c["conf_date"]:
            fl["date"].append("확신도")

        # ---- 일련번호
        if info["blank"]:
            if info["conf_serial"] < c["conf_serial"]:
                fl["serial"].append("빈칸")
        elif _serial(f["serial"]) is None:
            fl["serial"].append("형식")
        else:
            n = int(f["serial"])
            ns = [_serial(o["fields"]["serial"]) for o in nb
                  if _serial(o["fields"]["serial"]) is not None
                  and o["info"]["prefix"] == info["prefix"] and o["fields"]["date"][:4] == f["date"][:4]]
            if len(ns) >= 2:
                lo, hi = min(ns), max(ns)
                m = max(50, 0.3 * hi)
                if not (lo - m <= n <= hi + m):
                    fl["serial"].append("흐름")
        if info["conf_serial"] and info["conf_serial"] < c["conf_serial"]:
            fl["serial"].append("확신도")
        if "doc_head" in rules and f["docno"] and not re.match(r"[가-힣]", f["docno"]):
            fl["serial"].append("앞부분 없음")

        # ---- 불일치: GPT 텍스트 추출(B)과 손글씨 칸 판독이 다름. 비전 판독은 틀려도 확신도가 높아서
        # 확신도만으로는 틀린 일련번호의 56%, 일자의 23%만 잡혔다 (6개 철 129건)
        if info["b_serial"] and info["b_serial"] != f["serial"]:
            fl["serial"].append("불일치")
        if c["mismatch_date"] and info["b_date"] and info["b_date"] != f["date"]:
            fl["date"].append("불일치")

        # ---- 수신·제목
        for k in ["recipient", "title"]:
            if d["b"][k]["confidence"] < c["conf_llm"]:
                fl[k].append("LLM")
        rec = (f["recipient"] or "").strip()
        if "rec_sign" in rules and SIGN.search(rec) and not re.search(r"(시장|군수|지사|청장|서장|본부장)\s*\S*과\s*장$", rec):
            fl["recipient"].append("결재란")
        if "rec_part" in rules and rec and rec != "내부결재":
            has_addr = bool(ADDR.search(rec) or PLACE.search(rec))
            if has_addr and not TAIL.search(rec) and not re.search(r"(장|관|처장|소장)$", rec):
                fl["recipient"].append("이름 없음")
            elif not has_addr and re.search(r"대표$", rec):
                fl["recipient"].append("주소 없음")
        out.append({k: v for k, v in fl.items() if v})
    return out


def _order_conflict(d, day, docs, slack_days=3):
    """일련번호는 시간 순으로 매기므로, 같은 분류번호·연도에서 바로 앞 번호 문서보다 날짜가 이르거나
    바로 뒤 번호 문서보다 늦으면 어긋남. (철 전체와 비교하면 다른 문서의 틀린 번호 하나가 여러 문서를 끌고 들어간다)"""
    s, pre = _serial(d["fields"]["serial"]), d["info"]["prefix"]
    if s is None or not pre:
        return False
    same = [(_serial(o["fields"]["serial"]), _date(o["fields"]["date"])) for o in docs
            if o is not d and o["info"]["prefix"] == pre]
    same = [(n, x) for n, x in same if n is not None and x is not None and x.year == day.year and n != s]
    prev = max((p for p in same if p[0] < s), default=None)
    nxt = min((p for p in same if p[0] > s), default=None)
    w = dt.timedelta(slack_days)
    return bool((prev and prev[1] > day + w) or (nxt and nxt[1] < day - w))


