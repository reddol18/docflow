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


def is_list_ref(val):
    """수신 값이 '수신처 참조'(실제 수신처는 쪽 아래 목록)인가"""
    val = val or ""
    return bool(re.search(r"수\s*신\s*처\s*참\s*조", val)) or norm(val) in ("수신처", "처참조")


# ---------- 수신·제목 줄 자르기 (https://github.com/reddol18/docflow/issues/35) ----------
# 손글씨 칸(value_box)과 달리 인쇄 글자 줄 전체를 자른다: 항목명 + 값, 값이 다음 줄로 이어지면 그 줄까지.
# '수신처 참조'면 머리부 대신 쪽 아래 수신처 목록 줄(bottom_list가 값을 가져오는 줄)을 자른다.

LINE_KINDS = ("recipient", "title")  # 잘라 둘 줄 종류 = 잘라낸 파일 {doc_id}_{종류}.png (스키마 칸의 crop:)
LINE_TOP = 0.6  # 항목명을 찾는 범위 (쪽 위 비율). 아래쪽의 다른 '수신' 줄(붙은 공문)은 보지 않는다
LABEL_ROW = re.compile(r"^\s*\[?\s*(제\s*목|경\s*유|참\s*조|수\s*신|발\s*신|문\s*서\s*번\s*호|시\s*행\s*일\s*자)")
WRAP_X = 0.75  # 제목 첫 줄 끝이 이보다 오른쪽이면 다음 줄로 이어질 수 있다 (쪽 너비 비율)
ENUM = re.compile(r"^\s*(\d{1,2}|[가-하])\s*[.)]")  # 본문 항목 '1.', '가.'


def _row_text(row):
    text, spans = "", []
    for it in row:
        if text:
            text += " "
        spans.append((len(text), len(text) + len(it["text"])))
        text += it["text"]
    return text, spans


def split_rows(rows):
    """OCR이 두 줄을 한 줄 번호로 묶은 경우('참 수 조:… 신:…')를 세로 위치로 다시 나눈다. 줄마다 x 순서"""
    out = []
    for row in rows:
        hs = sorted(it["box"][3] - it["box"][1] for it in row)
        h = hs[len(hs) // 2] or 1
        groups = []
        for it in sorted(row, key=lambda it: (it["box"][1] + it["box"][3]) / 2):
            cy = (it["box"][1] + it["box"][3]) / 2
            if groups and cy - groups[-1][0] <= 0.6 * h:
                g = groups[-1]
                g[1].append(it)
                g[0] = sum((o["box"][1] + o["box"][3]) / 2 for o in g[1]) / len(g[1])
            else:
                groups.append([cy, [it]])
        out += [(g[0], sorted(g[1], key=lambda it: it["box"][0])) for g in groups]
    return [row for _, row in sorted(out, key=lambda x: x[0])]


def _union(boxes):
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _labeled(row, rx, page_w, stop_words):
    """항목명 줄에서 항목명부터 값 끝까지의 상자들과 값 시작 x, 값 글자. 항목명이 줄 앞(두 단어 안)에 없으면 None"""
    text, spans = _row_text(row)
    m = rx.search(text)
    if not m or m.start() >= spans[min(1, len(spans) - 1)][1]:
        return None
    end = m.end()
    boxes, vals, vx0, last = [], [], None, None
    for it, (s, e) in zip(row, spans):
        if e <= m.start():
            continue
        if e <= end:  # 항목명만 든 상자
            boxes.append(it["box"])
            last = it["box"][2]
            continue
        piece = it["text"][max(0, end - s):]
        if last is not None and it["box"][0] - last > 0.05 * page_w and vals:
            break
        if stop_words and STOP.match(piece) and vals:
            break
        boxes.append(it["box"])
        if piece.strip():
            vals.append(piece)
            if vx0 is None:
                vx0 = x_at(it, end - s) if s < end else it["box"][0]
        last = it["box"][2]
    return boxes, vx0, " ".join(vals).strip(" :：")


# 항목명 첫 글자를 못 읽은 경우 ('신 내부결재', '스 신 부산시 …') — 왼쪽 항목명 열의 줄에만 쓴다
FRAG = {"recipient": re.compile(r"^\s*(?:(?:(?:[가-힣]\s*)?신|수)(?=[\s:：!]|$)|받\s*[는음](?:\s*[이곳](?=[\s:：]))?)\s*[!:：]?\s*"),
        "title": re.compile(r"^\s*(?:(?:[가-힣]\s*)?목|제)(?=[\s:：]|$)\s*[:：]?\s*")}


VIA = re.compile(r"^\s*(?:경\s*)?유\s*[.:：]?\s*$")
NO_LABEL = re.compile(r"^")


def _after_via(rows, top, col, page_w, left):
    """항목명 없이 값만 읽힌 수신 줄: 왼쪽 열의 '경유' 줄 다음, 결재란만 있는 줄을 건너뛴 첫 줄 (값 칸에서 시작)"""
    via = next((i for i in col if VIA.match(rows[i][0]["text"]) or VIA.match(_row_text(rows[i][:2])[0])), None)
    if via is None:
        return None, None
    for i in top:
        if i <= via:
            continue
        row = [it for it in rows[i] if it["box"][0] < 0.45 * page_w]
        if not row:
            continue  # 결재란만 있는 줄
        if row[0]["box"][0] <= left or LABEL_ROW.match(row[0]["text"]):
            return None, None  # 다음 항목명(참조·제목) — 값 줄이 없음
        return i, _labeled(row, NO_LABEL, page_w, True)
    return None, None


def _ref_boxes(rows, cand, page_w):
    """머리부 '참조' 줄이 부서장이면(fix_recipient가 수신 값에 붙이는 경우 — ref_office와 같은 판단) 그 상자들"""
    for i in cand:
        hit = _labeled(rows[i], REF, page_w, True)
        if hit is None:
            continue
        v = re.sub(r"\s", "", hit[2])
        if OFFICE.match(v) and len(v) <= 12 and not STOP.match(v) and not v.startswith("담당자"):
            return hit[0]
        return []
    return []


def _find_label(rows, cand, rx, page_w, stop_words):
    """후보 줄(cand = rows 번호) 가운데 항목명이 처음 나오는 줄. (번호, _labeled 결과) 또는 (None, None)"""
    for i in cand:
        hit = _labeled(rows[i], rx, page_w, stop_words)
        if hit is not None:
            return i, hit
    return None, None


def _next_rows(rows, i, vx0, page_w, h, max_rows, title):
    """항목명 줄 i 다음에 값이 이어지는 줄의 상자들. 값 칸 x(vx0) 근처에서 시작하는 단어만 쓴다.
    다른 항목명 줄(제목·경유 등)이나 본문 항목('1.')이 나오면 멈춘다. 제목은 줄 간격이 벌어져도 멈춘다 (본문과 빈 줄)"""
    out, prev_y = [], rows[i][0]["box"][1]
    for row in rows[i + 1:]:
        if len(out) >= max_rows:
            break
        head = " ".join(it["text"] for it in row[:2])
        if TITLE_ROW.match(head) or (title and LABEL_ROW.match(head)):
            break
        cols = [it for it in row if it["box"][0] >= vx0 - 0.03 * page_w]
        if not cols or cols[0]["box"][0] > vx0 + 0.1 * page_w:
            if title:
                break
            continue  # 결재란만 있는 줄
        y = min(it["box"][1] for it in cols)
        if title and (y - prev_y > 1.9 * h or ENUM.match(cols[0]["text"])):
            break
        if not title and y - prev_y > 3.5 * h:
            break
        boxes, last = [], None
        for j, it in enumerate(cols):
            nxt = cols[j + 1]["text"] if j + 1 < len(cols) else ""
            if last is not None and it["box"][0] - last > 0.05 * page_w:
                break
            if not title and (STOP.match(it["text"]) or STOP.match(it["text"] + nxt)):
                break
            boxes.append(it["box"])
            last = it["box"][2]
        if not boxes:
            continue
        out.append(boxes)
        prev_y = y
    return out


def _pad(box, page_w, h):
    pad = 0.45 * h
    return [int(box[0] - 0.01 * page_w), int(box[1] - pad), int(box[2] + 0.01 * page_w), int(box[3] + pad)]


def list_box(d):
    """쪽 아래 수신처 목록 줄(bottom_list가 읽는 줄)의 상자. 없으면 None"""
    items = [it for it in d["items"] if it["text"].strip()]
    if not items:
        return None
    page_w, _ = page_size(items)
    rows = rows_of(d["items"])  # d["lines"]와 줄마다 맞는다 (bottom_list와 같은 끝 12줄)
    n = len(rows)
    for i in range(max(0, n - 12), n):
        ln = _row_text(rows[i])[0]
        m = LIST_HEAD.match(ln)
        if not m or not m.group(2).strip() or m.group(2).strip().startswith("참조"):
            continue
        sel = [rows[i]]
        for row in rows[i + 1:]:  # bottom_list와 같은 규칙: 쉼표로 이어지는 목록 줄만
            nxt = _row_text(row)[0]
            if re.fullmatch(r"[\s\d\W]*", nxt) or re.match(r"^\s*(붙\s*임|끝)", nxt) or not re.search(r"[,，·]", nxt):
                break
            sel.append(row)
        boxes = [it["box"] for row in sel for it in row if it["text"].strip()
                 and not (re.fullmatch(r"\d{1,3}", it["text"].strip()) and it is row[-1] and len(row) > 1)]  # 줄 끝 쪽 번호
        h = min(b[3] - b[1] for b in boxes)
        return _pad(_union(boxes), page_w, h)
    return None


def line_box(d, kind):
    """수신·제목 줄 상자 [x0,y0,x1,y1]. (상자 또는 None, 출처 'head'|'list', 못 찾은 사유).
    d = 문서 첫 쪽 OCR. 수신이 '수신처 참조'고 쪽 아래 목록이 있으면 목록 줄을 자른다"""
    if kind not in LINE_KINDS:
        raise ValueError(kind)
    items = [it for it in d["items"] if it["text"].strip()]
    if not items:
        return None, "", "OCR 글자 없음"
    page_w, page_h = page_size(items)
    rows = split_rows(rows_of(items))
    title = kind == "title"
    top = [i for i, row in enumerate(rows) if row[0]["box"][1] <= LINE_TOP * page_h]
    i, hit = _find_label(rows, top, FIELDS[kind], page_w, not title)
    if hit is None and top:  # 항목명 첫 글자를 못 읽음 ('신', '스 신'): 왼쪽 항목명 열에서 끝 글자로 찾는다
        left = min(rows[i][0]["box"][0] for i in top) + 0.06 * page_w
        col = [i for i in top if rows[i][0]["box"][0] <= left]
        i, hit = _find_label(rows, col, FRAG[kind], page_w, not title)
        if hit is None and not title:  # 항목명을 아예 못 읽음: 경유 줄 바로 다음의 값 칸 줄
            i, hit = _after_via(rows, top, col, page_w, left)
    if hit is not None:
        boxes, vx0, val = hit
        if not title and (is_list_ref(val) or re.match(r"처\s*참\s*조", val)):
            lb = list_box(d)
            if lb:
                return lb, "list", ""
        h = min(b[3] - b[1] for b in boxes)
        # 제목은 첫 줄이 오른쪽 끝 가까이까지 찼을 때만 다음 줄로 넘어간다 (아니면 다음 줄은 본문)
        wraps = not title or max(b[2] for b in boxes) > WRAP_X * page_w
        if vx0 is not None and wraps and not re.match(r"내\s*부\s*결", val):
            for more in _next_rows(rows, i, vx0, page_w, h, 2 if title else 3, title):
                boxes += more
        if not title:
            boxes += _ref_boxes(rows, top, page_w)
        return _pad(_union(boxes), page_w, h), "head", ""
    return None, "", "항목명 못 찾음"


def fix_recipient(d, val):
    """LLM이 뽑은 수신 값을 규칙으로 보정한다. (새 값, 적용한 규칙 이름 또는 '')"""
    val = val or ""
    if is_list_ref(val):
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
