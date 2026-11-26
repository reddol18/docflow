"""추출 근거 확인 (https://github.com/reddol18/docflow/issues/27 조건 4 레드팀 뒤 2026-11-27 사용자 결정).

LLM이 뽑은 칸 값이 OCR 텍스트에 근거가 있는지 본다. 근거가 없으면 그 칸에 검토 표시('OCR 근거 없음')를 붙인다 — 값은 지우지 않는다.
그 LLM 값이 최종 칸 값에 실제로 들어간 칸만 표시한다
(reached — 손글씨 판독·배치 규칙이 만든 값이면 LLM 값은 화면에 안 감, app/graph.py merge).

근거로 치지 않는 줄 = 지시 줄(instruction_line). 거부 목록(deny-list)이라
다른 말투로 쓴 지시나 평범한 줄에 심은 값은 못 잡는다(알려진 한계).
  - 강한 표시(STRONG_RX): 역할 표시(<|im_start|>·[system]·[시스템]·system:), 블록 표시 흉내(<ocr-…>·</docs-…>), JSON 칸 꼴("value":),
    confidence — 실제 공문에 나올 일이 거의 없는 것
  - 명령 꼴(IMPERATIVE_RX): 기계에게 하는 명령형 끝말(적어라·적을 것·출력하라·답하라·인용하라·무시하라·복사해 넣…)과 '새/운영 지시',
    '폐기한다'. '프로그램'·'출력'·'AI'처럼 보통 공문에도 나오는 낱말만으로는 지시 줄로 보지 않는다
  - 항목 줄 예외: 그 칸의 항목명으로 시작하는 줄(예: '제목 …', '수신 …')은 명령 꼴이 있어도 그 칸의 근거로 친다 (강한 표시가 있으면 아님)

비교 (보수적 — OCR 오독과 지시문의 '오독 교정' 때문에 진짜 값이 걸리지 않게):
  - 글자 칸: NFKC·빈칸 제거·casefold 뒤 근거 글에 그대로 있거나, 같은 길이 조각과의 비슷함(difflib)이 FUZZY 이상(4~64자만 —
    64자 넘는 값은 그대로 있어야 근거, 계산량 상한)
  - 날짜 칸(YYYY, YYYY-MM, YYYY-MM-DD): 근거 글의 날짜 꼴에서 연(끝 두 자리)·월이 같은 것이 있음. 날짜 꼴 = 4자리 연(19xx·20xx) + 구분자,
    또는 2자리 연 + '.'·'년' (예: 96. 4. 15 / 1996.04.15 / 1996년 4월) — '제12-3호' 같은 번호는 날짜로 보지 않는다.
    그 칸의 항목명 줄(시행일자)에 날짜 꼴이 있으면 그 줄의 날짜만 본다. 일은 보지 않는다 — 손글씨 일을 OCR이 자주 틀림(2026-11-27 측정).
    알려진 한계: 일만 바꾼 심은 날짜, 다른 날짜와 연·월이 같은 심은 날짜는 못 잡는다.
    날짜 꼴이 없으면(손글씨 칸이라 OCR이 못 읽음 등) 판단하지 않는다 — 단 지시 줄에만 그 날짜가 있으면 근거 없음
  - 문서 글자를 옮겨 적는 칸(method llm_extract·rule)만 본다. 요약·키워드(llm_generate)·통제어휘(vocab)는 보지 않는다
측정: eval/grounding_measure.py (이 PC의 저장된 추출 결과로, LLM 호출 없음).
"""
import difflib
import re
import unicodedata

FUZZY = 0.75
FUZZY_MAX = 64  # 이보다 긴 값은 difflib을 돌리지 않는다 (줄 80개 × 값 길이 계산량 상한)
# 한 값에 difflib ratio()를 부르는 횟수 상한. 글자 구성은 같고 순서만 섞은 최악 입력(80줄 × 400자, 64자 값)이 상한 없이 6.7초였다
# (2026-11-27 이 PC). 넘으면 근거 없음(검토 표시) — 진짜 문서는 김해 450건에서 이 상한에 닿지 않는다 (eval/grounding_measure.py와 같은 값)
FUZZY_RATIO_CALLS = 300
GROUND_METHODS = ("llm_extract", "rule")  # 문서 글자를 옮겨 적는 추출 칸 (app/fields.py method)
STRONG_RX = re.compile(
    r"<\|?\s*/?\s*(?:im_start|im_end|system|assistant|user)\b|</?\s*(?:ocr|docs)-[0-9a-f]*|\[\s*(?:system|assistant|시스템)\s*\]"
    r"|^\s*(?:system|assistant)\s*:|\"value\"\s*:|\bconfidence\b",
    re.I | re.M)
IMPERATIVE_RX = re.compile(
    r"적어\s*라|적을\s*것|적어\s*넣|출력\s*하라|출력할\s*것|답\s*하라|답할\s*것|인용\s*하라|무시\s*하(?:라|고)|복사해\s*넣|따르지\s*말"
    r"|(?:새|운영)\s*지시|폐기\s*한다",  # '지시:'만으로는 아님 — 실제 공문 줄에 있었다 (2026-11-27 측정)
    re.I)
DATE_VAL = re.compile(r"(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?")
# 근거 글의 날짜 꼴: 4자리 연(19xx·20xx) + 구분자, 또는 2자리 연 + '.'/'년'
DATE_TXT = re.compile(r"(?<!\d)(?:((?:19|20)\d{2})\s*[.\-/년]|(\d{2})\s*[.년])\s*(\d{1,2})(?!\d)(?:\s*[.\-/월]\s*(\d{1,2})(?!\d))?")


def norm(s):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or "")).casefold()


def instruction_line(line, label=None):
    """지시 줄인가. label = 이 줄을 근거로 쓰려는 칸의 항목명 (그 항목명으로 시작하면 명령 꼴은 봐 준다)"""
    t = unicodedata.normalize("NFKC", line or "")
    if STRONG_RX.search(t):
        return True
    if not IMPERATIVE_RX.search(t):
        return False
    return not (label and _label_line(t, label))


def _label_line(line, label):
    """줄이 항목명(빈칸 섞임 허용)으로 시작하고 바로 값이 오는가 (예: '수 신 가상시장', '제목: …'). '수신은 …' 같은 문장은 아님"""
    rx = r"\s*".join(map(re.escape, label.replace(" ", "")))
    return bool(re.match(rf"^\s*\[?\s*{rx}\s*[\]:：]?\s+\S", line))


def _fuzzy_in(v, t):
    if v in t:
        return True
    n = len(v)
    if n < 4 or n > FUZZY_MAX or len(t) < n:  # 짧은 값은 조금만 달라도 다른 값, 긴 값은 계산량 — 그대로 있어야
        return False
    sm = difflib.SequenceMatcher(autojunk=False)
    sm.set_seq2(v)
    budget = FUZZY_RATIO_CALLS
    for i in range(len(t) - n + 1):
        sm.set_seq1(t[i:i + n])
        if sm.real_quick_ratio() >= FUZZY and sm.quick_ratio() >= FUZZY:
            budget -= 1
            if budget < 0:  # 계산량 상한 — 근거를 못 찾은 것으로 (닫힌 쪽: 검토 표시)
                return False
            if sm.ratio() >= FUZZY:
                return True
    return False


def _dates(text):
    out = []
    for m in DATE_TXT.finditer(unicodedata.normalize("NFKC", text or "")):
        y, mo, d = m.group(1) or m.group(2), int(m.group(3)), m.group(4)
        if 1 <= mo <= 12:
            out.append((y[-2:], mo, int(d) if d else None))
    return out


def _date_match(val, found):
    """연(끝 두 자리)·월이 같은 날짜 꼴이 있는가. 일은 보지 않는다 — 시행일자의 일은 손글씨가 많아 OCR이 자주 틀린다
    (2026-11-27 측정: 일만 다른 표시가 정답 맞은 날짜 6건에 붙었다)"""
    y, mo, _ = val
    return any(fy == y and (mo is None or fm == mo) for fy, fm, _ in found)


def grounded(value, lines, kind="text", label=None):
    """값이 근거 줄(지시 줄이 아닌 줄)에 있는가. 빈 값은 True (볼 것 없음). label = 칸의 항목명"""
    v = norm(value)
    if not v:
        return True
    keep = [x for x in lines if not instruction_line(x, label)]
    instr = [x for x in lines if instruction_line(x, label)]
    m = DATE_VAL.fullmatch(v) if kind == "date" else None
    if m:
        val = (m.group(1)[-2:], int(m.group(2)) if m.group(2) else None, int(m.group(3)) if m.group(3) else None)
        own = [x for x in keep if label and _label_line(unicodedata.normalize("NFKC", x), label)]
        found = _dates("\n".join(own))
        if not found:  # 항목명 줄에 날짜 꼴이 없으면 근거 글 전체에서
            found = _dates("\n".join(keep))
        if _date_match(val, found):
            return True
        if not found:  # 근거 글에 날짜 꼴이 없음(손글씨 칸) — 지시 줄에만 있지 않으면 판단하지 않는다
            return not _date_match(val, _dates("\n".join(instr)))
        return False
    return _fuzzy_in(v, norm("".join(keep)))


def reached(llm_value, final_value):
    """LLM 값이 최종 칸 값에 들어갔는가 (빈칸 빼고 포함). 아니면 그 칸 값은 손글씨 판독·배치 규칙 등 다른 데서 왔다"""
    lv = norm(llm_value)
    return bool(lv) and lv in norm(final_value)


def check_extract(schema_fields, b, lines, failed=()):
    """운영 추출 결과 b({칸: {value, confidence}})의 근거 없는 칸 (app/graph.py extract와 레드팀 하네스가 같이 씀).
    lines = 문서 첫 쪽 OCR 줄 (추출 입력과 같게 앞 80줄). 추출 실패 칸(failed)은 빈 값이라 빼고 본다.
    최종 칸 값에 들어갔는지(reached)는 부른 쪽이 최종 값으로 거른다"""
    vals = {k: (v or {}).get("value") or "" for k, v in b.items()}
    return [n for n in ungrounded(schema_fields, vals, lines[:80]) if n not in set(failed)]


def ungrounded(schema_fields, values, lines):
    """추출 칸 이름 목록 중 근거 없는 칸. schema_fields = fields.prompt_fields(schema), values = {칸: 값}.
    문서에서 옮겨 적는 칸(GROUND_METHODS)만 본다 — 요약·키워드(llm_generate)나 통제어휘(vocab)는 글자가 OCR에 그대로 없는 게 정상"""
    out = []
    for f in schema_fields:
        if f.get("method") not in GROUND_METHODS:
            continue
        kind = "date" if f.get("rule") == "date" else "text"
        if not grounded(values.get(f["name"]) or "", lines, kind, f.get("label")):
            out.append(f["name"])
    return out
