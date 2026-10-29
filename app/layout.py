"""OCR 결과(단어 상자)에 거는 규칙들. LLM은 쓰지 않는다.

PoC 스크립트에서 가져왔다. 파일 경로 대신 쪽 dict({"page","lines","items"})를 받도록만 바꿨다.
  문서 시작 판별·항목 값 자르기   scripts/gt_draft.py
  손글씨 칸 위치 찾기            scripts/crop_fields.py (머리부 비율을 인자로 받게 바꿈)
  수신 보정                     scripts/recipient_fix.py
  일련번호·날짜 풀이, 비교용 정규화 scripts/hw_score.py, scripts/score_extract.py
"""
import re


def sp(word):
    """'문서번호' → 글자 사이 공백 허용 정규식."""
    return r"\s*".join(map(re.escape, word))


FIELDS = {
    # 첫 글자가 틀리게 읽혀도('둔서번호', '근서번흐'), 끝 글자 호/흐, 일자/인지 혼동도 잡는다
    "docno": re.compile(r"(?:[가-힣]\s*)?서\s*번\s*[호흐]\s*[:：]?\s*"),
    "date": re.compile(r"시\s*행\s*[일인입]\s*[자지]\s*[:：]?\s*"),
    "recipient": re.compile(r"(?<![가-힣])" + sp("수신") + r"(?!\s*자)\s*[:：]?\s*"),
    "title": re.compile(r"(?<![가-힣])" + sp("제목") + r"\s*[:：]?\s*"),
}
# 값 뒤에 붙어 나오는 결재란·처리란 단어. 여기서 값을 끊는다.
STOP = re.compile(
    r"^(취\s*급|보\s*존|보존기간|공개여부|선\s*결|전\s*결|접\s*수|처리과|결재|지\s*시|"
    r"시\s*장|군\s*수|부시장|부군수|과\s*장|국\s*장|실\s*장|계\s*장|담당|경\s*유|참\s*조|기\s*안|협\s*조)"
)
ORG = re.compile(r"^\s*김\s*해\s*[시군]\s*$|^\s*(경\s*상\s*남\s*도|[가-힣]{2,6}(시|군|도|청|공사|공단))\s*$")
POST = re.compile(r"우\s*[)）:,.\-]?\s*\(?\s*\d{3}\s*[-~]\s*\d{3}")  # '(우)621-701', '우,621-180'
AN = re.compile(r"\[\s*제\s*\d+\s*안\s*\]")
ATTACH = re.compile(r"\[?\s*별\s*지\s*제?\s*\d+\s*호")  # '[별지 제34호 서식]' = 첨부 서식, 시작 쪽 아님
DATE = re.compile(r"(?<!\d)['`‘’]?\s*((?:19|20)?\d{2})(?:\s*[.,·]\s*|\s+)(\d{1,2})\s*[.,]?\s*(\d{1,2})?")


def page_size(items):
    return (max((it["box"][2] for it in items), default=2464),
            max((it["box"][3] for it in items), default=3504))


def rows_of(items):
    """items(line 번호 포함)를 줄별로 모은다."""
    rows = {}
    for it in items:
        rows.setdefault(it["line"], []).append(it)
    return [sorted(rows[k], key=lambda it: it["box"][0]) for k in sorted(rows)]


def field_value(row, rx, page_w):
    """한 줄(단어 목록)에서 항목명 오른쪽 값을 상자 좌표로 잘라낸다.
    단어 사이 간격이 쪽 너비 5%를 넘거나 결재란 단어가 나오면 멈춘다."""
    text, spans = "", []
    for it in row:
        if text:
            text += " "
        spans.append((len(text), len(text) + len(it["text"])))
        text += it["text"]
    m = rx.search(text)
    if not m:
        return None
    end = m.end()
    nxt = {id(a): b["text"] for a, b in zip(row, row[1:])}  # '실','장'처럼 나뉘어 읽힌 결재란 단어
    out, last_x1 = [], None
    for it, (s, e) in zip(row, spans):
        if e <= end:
            last_x1 = it["box"][2]
            continue
        piece = it["text"][max(0, end - s):] if s < end else it["text"]
        if last_x1 is not None and it["box"][0] - last_x1 > 0.05 * page_w and out:
            break
        if (STOP.match(piece) or STOP.match(piece + nxt.get(id(it), ""))) and out:
            break
        if piece.strip():
            out.append(piece)
        last_x1 = it["box"][2]
    return " ".join(out).strip(" :：")


def norm_date(raw):
    if not raw:
        return "", "날짜 없음"
    s = AN.sub(" ", raw)
    s = re.sub(sp("경유"), " ", s)
    if re.search(r"(?<!\d)199\s+\d", s):  # '199 8 . 16' = 연도 끝자리(손글씨)가 빠짐
        return "", "연도 끝자리 빠짐"
    m = DATE.search(s)
    if not m:
        return "", "날짜 형식 못 읽음"
    y, mo, d = int(m.group(1)), int(m.group(2)), m.group(3)
    if y < 100:
        y += 1900 if y >= 50 else 2000
    if not (1990 <= y <= 2005 and 1 <= mo <= 12):
        return "", "날짜 값 이상"
    if not d:
        return "", f"일자 빠짐({y}-{mo:02d})"
    d = int(d)
    if not 1 <= d <= 31:
        return "", "일자 값 이상"
    return f"{y}-{mo:02d}-{d:02d}", ""


def date_of(s):
    return norm_date((s or "").replace("(", " ").replace(")", " "))[0]


def serial_of(s):
    """문자열에서 일련번호(마지막 '-' 뒤 숫자, 없으면 숫자 전부)."""
    s = s or ""
    parts = re.split(r"[-~―—]", s)
    tail = parts[-1] if len(parts) > 1 else s
    return re.sub(r"\D", "", tail)


def prefix_of(docno):
    m = re.search(r"(\d{5})\s*[-~]", docno or "")
    return m.group(1) if m else ""


def norm(s):
    # 비교용: 주식회사=(주), ⓐ=@(아파트) 로 맞춘 뒤 괄호·구분기호를 지운다
    s = (s or "").replace("주식회사", "(주)").replace("ⓐ", "@")
    return re.sub(r"[\s\"'“”‘’.,:：()~\-]", "", s)


# ---------- 문서 시작 판별 (gt_draft.analyze) ----------

def is_start(d):
    """쪽 하나가 새 문서의 첫 쪽인지와 근거 신호."""
    items = d["items"]
    page_w, page_h = page_size(items)
    rows = rows_of(items)
    top = [ln for ln, row in zip(d["lines"], rows) if row and row[0]["box"][1] < 0.25 * page_h]
    sig = []
    if any(ORG.match(ln) for ln in top):
        sig.append("ORG")
    if any(POST.search(ln) for ln in top):
        sig.append("POST")
    for key, tag in [("docno", "DOCNO"), ("date", "DATE")]:
        if any(field_value(row, FIELDS[key], page_w) is not None for row in rows):
            sig.append(tag)
    # 머리부(기관명·우편번호) + 문서번호나 시행일자, 또는 문서번호 + 시행일자(머리부 없는 기안 표지).
    # 맨 위 3줄에 '별지 제N호'가 있으면 첨부 서식으로 보고 뺀다.
    head = "ORG" in sig or "POST" in sig
    start = (("DOCNO" in sig or "DATE" in sig) and head) or ("DOCNO" in sig and "DATE" in sig)
    if any(ATTACH.search(ln) for ln in d["lines"][:3]):
        start = False
        sig.append("ATTACH")
    return start, sig


# ---------- 손글씨 칸 위치 (crop_fields) ----------

LABELS = {"docno": "문서번호", "date": "시행일자"}
MAX_W = 0.32  # 값 영역 최대 너비 (쪽 너비 비율)
GAP = 0.05  # 이보다 멀리 떨어진 상자는 결재란으로 보고 멈춤


def label_rx(word):
    # 첫 글자나 끝 글자가 빠져 읽혀도 잡는다 ("서번호", "시행일")
    full = r"\s*".join(word)
    alts = [full, r"\s*".join(word[1:]), r"\s*".join(word[:3])]
    return re.compile(r"^\s*(?:" + "|".join(alts) + r")\s*[:：;]?\s*")


RX = {k: label_rx(v) for k, v in LABELS.items()}


def char_w(c):
    if "가" <= c <= "힣":
        return 1.0
    if c == " ":
        return 0.35
    return 0.55


def x_at(it, idx):
    """상자 안 글자 위치 idx의 x좌표를 글자 폭 가중치로 추정한다."""
    t = it["text"]
    tot = sum(map(char_w, t)) or 1
    x0, x1 = it["box"][0], it["box"][2]
    return x0 + (x1 - x0) * sum(map(char_w, t[:idx])) / tot


def find_label(items, key, page_h, head_ratio):
    """항목명 상자를 찾는다. 쪽 위쪽 head_ratio 안에서 가장 먼저 나오는 것."""
    for it in sorted(items, key=lambda it: it["box"][1]):
        if it["box"][1] > head_ratio * page_h:
            break
        m = RX[key].match(it["text"])
        if m:
            return it, m.end()
    return None, 0


def value_box(items, key, head_ratio):
    """항목명 오른쪽 손글씨 값 칸의 상자 [x0,y0,x1,y1]. 못 찾으면 (None, 사유)."""
    items = [it for it in items if it["text"].strip()]
    page_w, page_h = page_size(items)
    it, end = find_label(items, key, page_h, head_ratio)
    if not it:
        return None, "항목명 못 찾음"
    x0, y0, x1, y1 = it["box"]
    h = y1 - y0
    if end == len(it["text"]):  # 항목명만 따로 읽힘
        vx0, last = x1, x1
    else:  # 항목명과 값이 한 상자에 붙어 읽힘
        vx0, last = x_at(it, end), x1
    cy = (y0 + y1) / 2
    for o in sorted(items, key=lambda o: o["box"][0]):
        if o is it or o["box"][0] < last - 5:
            continue
        if abs((o["box"][1] + o["box"][3]) / 2 - cy) > 0.6 * h:
            continue
        if o["box"][0] - last > GAP * page_w or o["box"][0] > vx0 + MAX_W * page_w:
            break
        last = o["box"][2]
    vx1 = min(max(last, vx0) + 0.04 * page_w, vx0 + MAX_W * page_w)
    if vx1 - vx0 < 0.12 * page_w:  # 값이 안 읽혔으면 기본 너비
        vx1 = vx0 + 0.25 * page_w
    pad = 0.45 * h
    return [int(vx0 - 0.01 * page_w), int(cy - h / 2 - pad), int(vx1), int(cy + h / 2 + pad)], ""


# ---------- 수신 보정 (recipient_fix) ----------

LIST_HEAD = re.compile(r"^\s*(수\s*신\s*처|받\s*는\s*곳)\s*[:：;]?\s*(.*)$")
NOISE_TAIL = re.compile(r"\s+\d{1,3}\s*$")  # 줄 끝 쪽 번호
REF = re.compile(r"(?<![가-힣])" + sp("참조") + r"\s*[:：]?\s*")
OFFICE = re.compile(r"^[가-힣()·]{2,}(과장|소장|담당관|실장|국장|처장|청장|서장|원장|단장)$")


def bottom_list(lines):
    """쪽 아래쪽 '수신처:' / '받는곳' 목록. 없으면 빈칸."""
    tail = lines[-12:]
    for i, ln in enumerate(tail):
        m = LIST_HEAD.match(ln)
        if not m or not m.group(2).strip() or m.group(2).strip().startswith("참조"):
            continue
        out = [m.group(2)]
        for nxt in tail[i + 1:]:  # 다음 줄은 쉼표로 이어지는 목록일 때만 붙인다
            if re.fullmatch(r"[\s\d\W]*", nxt) or re.match(r"^\s*(붙\s*임|끝)", nxt) or not re.search(r"[,，·]", nxt):
                break
            out.append(nxt)
        return NOISE_TAIL.sub("", "".join(out)).strip(" :：,.")
    return ""


def ref_office(d):
    """머리부 '참조' 줄의 부서장 값. 결재란 글자뿐이면 빈칸."""
    page_w, page_h = page_size(d["items"])
    for row in rows_of(d["items"]):
        if row[0]["box"][1] > 0.4 * page_h:
            break
        v = field_value(row, REF, page_w)
        if v is None:
            continue
        v = re.sub(r"\s", "", v)
        if OFFICE.match(v) and len(v) <= 12 and not STOP.match(v) and not v.startswith("담당자"):
            return v
        return ""
    return ""


def value_x0(row, rx):
    """항목명 뒤 첫 값 단어의 x 시작점."""
    text = " ".join(it["text"] for it in row)
    m = rx.search(text)
    pos = 0
    for it in row:
        e = pos + len(it["text"])
        if e > m.end() and it["text"][max(0, m.end() - pos):].strip():
            return it["box"][0] if pos >= m.end() else it["box"][2] - 1
        pos = e + 1
    return None


TITLE_ROW = re.compile(r"^\s*\[?\s*(제\s*목|경\s*유)")


def continuation(rows, i, x0, page_w, max_rows=3):
    """수신 값이 다음 줄로 이어지면 이어 붙인다. 값 칸 x0 근처에서 시작하는 단어만 쓰고,
    줄 맨 앞 '참조' 항목명은 건너뛴다. 제목·경유 줄이 나오면 멈춘다."""
    out = []
    for row in rows[i + 1:i + 1 + max_rows]:
        head = " ".join(it["text"] for it in row[:2])
        if TITLE_ROW.match(head):
            break
        cols = [it for it in row if it["box"][0] >= x0 - 0.02 * page_w]
        if not cols or cols[0]["box"][0] > x0 + 0.1 * page_w:
            continue  # 결재란만 있는 줄
        pieces, last = [], None
        for j, it in enumerate(cols):
            nxt = cols[j + 1]["text"] if j + 1 < len(cols) else ""
            if last is not None and it["box"][0] - last > 0.05 * page_w:
                break
            if STOP.match(it["text"]) or STOP.match(it["text"] + nxt):
                break
            pieces.append(it["text"])
            last = it["box"][2]
        if pieces:
            out.append(" ".join(pieces))
    return out


def rule_recipient(d):
    """규칙으로 읽은 수신 값 (여러 줄 이어 붙임). PoC gt_draft.analyze의 수신 부분."""
    page_w, _ = page_size(d["items"])
    rows = rows_of(d["items"])
    rx = FIELDS["recipient"]
    for i, row in enumerate(rows):
        v = field_value(row, rx, page_w)
        if v is None:
            continue
        v = re.sub(r"^\]\s*", "", v)
        if v and not re.match(r"처|내부\s*결재", v):
            x0 = value_x0(row, rx)
            more = continuation(rows, i, x0, page_w) if x0 is not None else []
            v = " ".join([v] + more)
        return v
    return ""


def fix_recipient(d, val):
    """LLM이 뽑은 수신 값을 규칙으로 보정한다. (새 값, 적용한 규칙 이름 또는 '')"""
    val = val or ""
    if re.search(r"수\s*신\s*처\s*참\s*조", val) or norm(val) in ("수신처", "처참조"):
        lst = bottom_list(d["lines"])
        if lst:
            return lst, "목록"
    if not val or re.match(r"내부\s*결", val):
        return val, ""
    office = ref_office(d)
    if office and norm(office) not in norm(val):
        return f"{val} {office}", "참조"
    # 잘림: 다음 줄의 상호·'대표'를 LLM이 빠뜨린 경우 규칙 값의 뒷부분을 붙인다
    rule = rule_recipient(d)
    nv, nr = norm(val), norm(rule)
    if nr.startswith(nv) and len(nr) > len(nv) and nr.endswith("대표"):
        k = 0  # rule 원문에서 B 값에 해당하는 앞부분을 건너뛴다
        for i, ch in enumerate(rule):
            if k >= len(nv):
                return f"{val} {rule[i:].strip()}", "잘림"
            k += len(norm(ch))
    return val, ""
