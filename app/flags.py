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
    "rules": (),           # 추가 규칙 이름 (RULES 참고). 빈 값이면 기존 규칙만
}
# 김해 7개 철 164건 스윕(eval/flag_whatif.py --sweep)에서 '표시 없이 틀린 문서' 3% 이하 중 표시 비율이 가장 낮은 설정.
# 9.8%(16건) → 2.4%(4건), 대신 표시된 문서 80% → 90%.
# 4칸이 모두 맞는 문서가 25%뿐이라 '표시 80% 이하'와 함께 만족하는 설정은 없었다.
# 표시 비율을 낮추려면 표시 규칙이 아니라 정답률(특히 수신 46%, issues/1)을 올려야 한다.
# 주의: 규칙 일부(doc_head, rec_sign 가운데 직위)는 같은 164건의 놓친 사례를 보고 만들었다 → 다른 기관 자료로 재확인(issues/8).
DEFAULT = {**LEGACY, "conf_serial": 0.95, "conf_date": 0.9, "conf_llm": 0.9, "rules": ("doc_head", "rec_sign")}

# 추가 규칙 설명 (eval/flag_whatif.py 출력용)
# 시험했지만 스윕의 최선 경계에 한 번도 들지 못해 뺀 후보: 일련번호 순서와 날짜 순서 모순(철 전체 / 바로 앞뒤 번호),
# 연도가 앞뒤 문서와 다름, 수신 3글자 이하. 표시만 늘리고 '표시 없이 틀림'은 같은 설정보다 줄이지 못했다.
RULES = {
    "doc_head": "문서번호: 부서 약칭(한글)으로 시작하지 않음 → 앞부분을 못 읽음",
    "day1": "일자: 일이 1일 → 일이 비어 있는 칸을 1로 채웠을 가능성",
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
        if info["b_date"] and info["b_date"] != f["date"]:
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


