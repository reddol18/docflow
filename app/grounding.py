"""추출 근거 확인 (https://github.com/reddol18/docflow/issues/27 조건 4 레드팀 뒤 2026-11-27 사용자 결정).

LLM이 뽑은 칸 값이 OCR 텍스트에 근거가 있는지 본다. 근거가 없으면 그 칸에 검토 표시('OCR 근거 없음')를 붙인다 — 값은 지우지 않는다.
근거로 치지 않는 줄: 지시처럼 보이는 줄(INSTR_RX — 역할 표시·블록 표시 흉내·"적어라/출력하라/답하라"·프로그램·AI·JSON 칸 꼴).
문서에 심어진 지시문이 값을 넣으려 하면 그 값은 지시 줄에만 있으므로 걸린다 (레드팀 E3: 가짜 system 줄의 수신 값).

비교 (보수적 — OCR 오독과 지시문의 '오독 교정' 때문에 진짜 값이 걸리지 않게):
  - 글자 칸: NFKC·빈칸 제거·casefold 뒤 근거 글에 그대로 있거나, 같은 길이 조각과의 비슷함(difflib)이 FUZZY 이상
  - 날짜 칸(YYYY, YYYY-MM, YYYY-MM-DD): 근거 글의 날짜 꼴(96. 4. 15 / 1996.04.15 / 1996년 4월 15일 …)에서 연(끝 두 자리)·월이
    같은 것이 있음 (일은 보지 않는다 — 손글씨 일을 OCR이 자주 틀림. 알려진 한계: 일만 바꾼 심은 날짜는 못 잡는다).
    날짜 꼴이 없으면(손글씨 칸이라 OCR이 못 읽음 등) 근거를 판단하지 않는다 — 단 지시 줄에만 그 날짜가 있으면 근거 없음
  - 문서 글자를 옮겨 적는 칸(method llm_extract·rule)만 본다. 요약·키워드(llm_generate)·통제어휘(vocab)는 보지 않는다
측정: eval/grounding_measure.py (이 PC의 저장된 추출 결과로, LLM 호출 없음).
"""
import difflib
import re
import unicodedata

FUZZY = 0.75
GROUND_METHODS = ("llm_extract", "rule")  # 문서 글자를 옮겨 적는 추출 칸 (app/fields.py method)
# 지시처럼 보이는 줄 — 실제 공문에 드문 말만 (넓히면 진짜 값 줄이 빠져 표시가 늘어난다. eval/grounding_measure.py로 잰다)
INSTR_RX = re.compile(
    r"<\|?\s*/?\s*(?:im_start|im_end|system|assistant|user)\b|</?\s*(?:ocr|docs)-|\[(?:system|시스템)\]|\bsystem\b|\bassistant\b"
    r"|프로그램|\bAI\b|인공지능|출력하|무시하|적어라|적을\s*것|적는다|답하라|인용하라|복사해|\bconfidence\b|\"value\"\s*:"
    r"|(?:새|운영)\s*지시|폐기한다",
    re.I)
DATE_VAL = re.compile(r"(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?")
# 근거 글의 날짜 꼴: 연(2·4자리) 구분 월 [구분 일]
DATE_TXT = re.compile(r"(?<!\d)(\d{4}|\d{2})\s*[.\-/년]\s*(\d{1,2})(?!\d)(?:\s*[.\-/월]\s*(\d{1,2})(?!\d))?")


def norm(s):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or "")).casefold()


def instruction_line(line):
    return bool(INSTR_RX.search(unicodedata.normalize("NFKC", line or "")))


def _fuzzy_in(v, t):
    if v in t:
        return True
    n = len(v)
    if n < 4 or len(t) < n:  # 짧은 값은 조금만 달라도 다른 값이다 — 그대로 있어야
        return False
    sm = difflib.SequenceMatcher(autojunk=False)
    sm.set_seq2(v)
    for i in range(len(t) - n + 1):
        sm.set_seq1(t[i:i + n])
        if sm.real_quick_ratio() >= FUZZY and sm.quick_ratio() >= FUZZY and sm.ratio() >= FUZZY:
            return True
    return False


def _dates(text):
    out = []
    for m in DATE_TXT.finditer(unicodedata.normalize("NFKC", text or "")):
        y, mo, d = m.group(1), int(m.group(2)), m.group(3)
        if 1 <= mo <= 12:
            out.append((y[-2:], mo, int(d) if d else None))
    return out


def _date_match(val, found):
    """연(끝 두 자리)·월이 같은 날짜 꼴이 있는가. 일은 보지 않는다 — 시행일자의 일은 손글씨가 많아 OCR이 자주 틀린다
    (2026-11-27 측정: 일만 다른 표시가 정답 맞은 날짜 6건에 붙었다)"""
    y, mo, _ = val
    return any(fy == y and (mo is None or fm == mo) for fy, fm, _ in found)


def grounded(value, lines, kind="text"):
    """값이 근거 줄(지시 줄이 아닌 줄)에 있는가. 빈 값은 True (볼 것 없음)"""
    v = norm(value)
    if not v:
        return True
    keep = [x for x in lines if not instruction_line(x)]
    instr = [x for x in lines if instruction_line(x)]
    m = DATE_VAL.fullmatch(v) if kind == "date" else None
    if m:
        val = (m.group(1)[-2:], int(m.group(2)) if m.group(2) else None, int(m.group(3)) if m.group(3) else None)
        found = _dates("\n".join(keep))
        if _date_match(val, found):
            return True
        if not found:  # 근거 글에 날짜 꼴이 없음(손글씨 칸) — 지시 줄에만 있지 않으면 판단하지 않는다
            return not _date_match(val, _dates("\n".join(instr)))
        return False
    return _fuzzy_in(v, norm("".join(keep)))


def check_extract(schema_fields, b, lines, failed=()):
    """운영 추출 결과 b({칸: {value, confidence}})의 근거 없는 칸 (app/graph.py extract와 레드팀 하네스가 같이 씀).
    lines = 문서 첫 쪽 OCR 줄 (추출 입력과 같게 앞 80줄). 추출 실패 칸(failed)은 빈 값이라 빼고 본다"""
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
        if not grounded(values.get(f["name"]) or "", lines, kind):
            out.append(f["name"])
    return out
