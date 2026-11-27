"""합성 공문서 쪽 그리기 — 깨끗한 쪽 이미지와 단어 상자·칸 정답 (https://github.com/reddol18/docflow/issues/139).

모든 기관·사람·주소·번호는 지어낸 것이다. 실제 자료(AI Hub 공개 데이터 등)는 읽지도 베끼지도 않는다.
지명은 실제 없는 것(CITY), 사람 이름은 음절 조합이다. 회사 이름은 흔한 낱말 조합이라 우연히 실제 상호와 같을 수 있다.

양식(FORMS, Case.form) — 첫 쪽 머리만 다르고 4칸(docno·date·recipient·title)은 같다
  gimhae      1990년대 시청 시행문 (김해 4칸 머리 세로줄, 오른쪽 접수란) — 기본. 같은 시드면 예전과 같은 쪽
  agency1990  1990년대 다른 기관(지어낸 도 사업소) 시행문 — 기관명 머리, 문서번호·시행일자 한 줄, 접수란은 쪽 아래
  draft2000   2000년대 기안문 — 가로 표 머리부(문서번호|시행일자|보존기간|기안자), 인쇄 문서번호 '부서명-번호'
쪽 종류
  first     시행문 첫 쪽 — 김해 4칸(문서번호·시행일자·수신·제목) 머리, 접수란, 본문, 발신 명의·관인, 서명란
  cont      본문이 이어지는 쪽 (머리 없음, 쪽 번호)
  attach    붙임 서식 쪽 ('[별지 제N호 서식]' 머리 + 표) — docflow 규칙상 문서 시작 쪽이 아니다
단어 상자는 annotation 하나당 [x0, y0, x1, y1]. 칸 값 단어에는 field(docno·date·recipient·title)를 붙인다.
손글씨 단어에는 cell(HAND_CELLS — 일련번호·시행일자·접수란·서명란)을 붙인다: 같은 cell 단어를 이으면 그 칸의 정답 글자,
상자를 합치면 칸 상자 (https://github.com/reddol18/docflow/issues/80 조건 4). 그림·난수 순서는 바꾸지 않는다.
"""
import functools
import random
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H = 2464, 3504  # AI Hub 88 쪽 크기와 같게
SYS_FONTS = Path("C:/Windows/Fonts")


@dataclass
class Fonts:
    print: str = str(SYS_FONTS / "batang.ttc")  # 바탕(명조) — 인쇄 글자
    hand: list = field(default_factory=list)  # 손글씨 글꼴 경로들 (fonts.py로 받음). 비면 인쇄 글꼴로 대신 그린다

    def pick_hand(self, r):
        return r.choice(self.hand) if self.hand else self.print


SURN = "김이박최정강조윤장임한오서신권황안송류홍"
GIVEN = "민서준도현지우하윤예은성진영수경호태희나라연철석재동"
CITY = ["한빛시", "새솔시", "가람군", "누리시"]  # 실제 없는 지명
DEPT = [("건축", "건축과"), ("지경", "지역경제과"), ("도시", "도시과"), ("환경", "환경보호과")]
COMPANY = ["가온건설", "다솜산업", "마루상사", "바름개발", "새빛전기", "한결유통"]
CASES = [
    ("건축허가", "건축허가 신청에 대한 처리 결과", "건축법 제8조"),
    ("옥외광고물 표시허가", "옥외광고물 표시허가 신청 처리", "옥외광고물등관리법 제3조"),
    ("도로점용허가", "도로점용허가 신청에 대한 회신", "도로법 제40조"),
    ("공장설립 승인", "공장설립 승인 신청 검토 결과 통보", "공업배치및공장설립에관한법률 제13조"),
    ("영업신고 수리", "식품접객업 영업신고 수리 통보", "식품위생법 제22조"),
]
FIELDS = ("docno", "date", "recipient", "title")
FORMS = ("gimhae", "agency1990", "draft2000")
PROVINCE = ["새벌도", "한누리도"]  # 실제 없는 도 이름 (agency1990·draft2000 기관)
# agency1990 기관: (단위 기관 이름, 문서번호 약칭) — 도 이름과 이어 '새벌도 수자원관리사업소'
UNIT = [("수자원관리사업소", "수관"), ("산림환경연구소", "산연"), ("도로관리사업소", "도관"), ("보건환경연구원", "보연")]
# draft2000 기관·부서: 도시·군(CITY) + 부서명. 문서번호는 '부서명-일련번호'
DEPT2000 = ["건축과", "지역경제과", "도시계획과", "환경위생과", "민원봉사과"]
YEARS = {"gimhae": (1991, 1998), "agency1990": (1990, 1999), "draft2000": (2001, 2009)}
# 양식별 칸 정의 (app/field_schemas/ 파일 이름 — https://github.com/reddol18/docflow/issues/9 설정 검증을 시험이 돌린다)
SCHEMA_FILE = {"gimhae": "gimhae.yaml", "agency1990": "synth_agency1990.yaml", "draft2000": "synth_draft2000.yaml"}
# 손글씨 칸 (annotation.cell): 한 칸 = hand() 한 번. serial·date는 운영 판독 칸(문서번호 일련번호·시행일자)의 손글씨 부분,
# 나머지는 운영이 읽지 않는 칸 — 접수란 번호·일자, 서명란 이름(기안자·검토자·결재권자), 붙임 서식 검토자 이름
SIGN_CELLS = ("sign_drafter", "sign_reviewer", "sign_approver")
HAND_CELLS = ("serial", "date", "recv_no", "recv_date", *SIGN_CELLS, "sign_attach", "sign_table")  # sign_table: draft2000 머리 표 기안자


def person(r):
    return r.choice(SURN) + r.choice(GIVEN) + r.choice(GIVEN)


class Page:
    def __init__(self, fonts, r, typewriter=False):
        self.img = Image.new("RGB", (W, H), (255, 255, 255))
        self.d = ImageDraw.Draw(self.img)
        self.fonts = fonts
        self.r = r
        self.tw = random.Random(r.random()) if typewriter else None  # 타자기: 글자마다 높이·농도를 흔든다
        self.hand_font = fonts.pick_hand(r)
        self.ink = r.choice([(20, 30, 90), (25, 25, 30), (35, 40, 110)])  # 이 쪽에 쓴 볼펜 색
        self.ann = []

    def _add(self, text, box, ttype, fld, cell=None):
        a = {"text": text, "ttype": ttype, "box": [int(v) for v in box]}
        if fld:
            a["field"] = fld
        if cell:
            a["cell"] = cell
        self.ann.append(a)

    @staticmethod
    @functools.lru_cache(maxsize=256)
    def _font(path, size):
        """글꼴 파일·크기별로 한 번만 연다 (손글씨는 글자마다 크기를 바꿔 부른다)."""
        return ImageFont.truetype(path, size)

    def text(self, xy, s, size=46, fld=None):
        """인쇄 글자. 띄어쓰기 단위로 상자. 끝 x를 돌려준다."""
        f = self._font(self.fonts.print, size)
        x, y = xy
        gap = int(size * 0.35)
        for w in s.split(" "):
            if not w:
                x += gap
                continue
            if self.tw is None:
                bb = self.d.textbbox((x, y), w, font=f)
                self.d.text((x, y), w, font=f, fill=(25, 25, 25))
            else:
                cx, bbs = x, []
                for ch in w:
                    dy = self.tw.gauss(0, size * 0.03)
                    g = int(min(110, max(0, self.tw.gauss(25, 22))))  # 옅은 글쇠도 낡기 전에는 읽힐 만큼
                    b = self.d.textbbox((cx, y + dy), ch, font=f)
                    self.d.text((cx, y + dy), ch, font=f, fill=(g, g, g))
                    bbs.append(b)
                    cx = b[2] + self.tw.uniform(-1, 2)
                bb = (bbs[0][0], min(b[1] for b in bbs), bbs[-1][2], max(b[3] for b in bbs))
            self._add(w, bb, "textType1", fld)
            x = bb[2] + gap
        return x

    def spaced(self, xy, s, size, letter_gap=0):
        """'수 신'처럼 글자 사이를 띄운 항목명 — 상자는 한 단어로."""
        f = self._font(self.fonts.print, size)
        x, y = xy
        boxes = []
        for ch in s:
            if ch == " ":
                x += size // 2
                continue
            bb = self.d.textbbox((x, y), ch, font=f)
            self.d.text((x, y), ch, font=f, fill=(25, 25, 25))
            boxes.append(bb)
            x = bb[2] + letter_gap
        self._add(s.replace(" ", ""), (boxes[0][0], min(b[1] for b in boxes), boxes[-1][2], max(b[3] for b in boxes)),
                  "textType1", None)
        return x

    def hand(self, xy, s, size, fld=None, cell=None):
        """손글씨: 글자마다 크기·기울기·위치를 흔든다. 띄어쓰기 단위로 상자.
        cell: 손글씨 칸 이름(HAND_CELLS) — 한 번 부른 글(s)이 칸 하나다.
        비전 판독 비교(https://github.com/reddol18/docflow/issues/80 조건 4)의 정답 단위"""
        r = self.r
        x, y = xy
        for w in s.split(" "):
            if not w:
                continue
            boxes = []
            for ch in w:
                f = self._font(self.hand_font, int(size * r.uniform(0.9, 1.12)))
                bb = f.getbbox(ch)
                tile = Image.new("L", (bb[2] - bb[0] + 12, bb[3] - bb[1] + 12), 0)
                ImageDraw.Draw(tile).text((6 - bb[0], 6 - bb[1]), ch, font=f, fill=255)
                tile = tile.rotate(r.uniform(-7, 7), expand=True, resample=Image.BICUBIC)
                ox, oy = int(x), int(y + r.uniform(-5, 5) + (size - tile.size[1]) * 0.5)
                self.img.paste(Image.new("RGB", tile.size, self.ink), (ox, oy), tile)
                tb = tile.getbbox() or (0, 0, 1, 1)
                boxes.append((ox + tb[0], oy + tb[1], ox + tb[2], oy + tb[3]))
                x = ox + tb[2] + r.uniform(-2, 6)
            self._add(w, (min(b[0] for b in boxes), min(b[1] for b in boxes),
                          max(b[2] for b in boxes), max(b[3] for b in boxes)), "handwritten", fld, cell)
            x += size * r.uniform(0.35, 0.6)
        return x

    def line(self, a, b, width=3):
        self.d.line([a, b], fill=(30, 30, 30), width=width)

    def rect(self, box, width=3):
        self.d.rectangle(box, outline=(30, 30, 30), width=width)

    def seal(self, center, size, text):
        """붉은 관인(네모 도장). 라벨에는 넣지 않는다 (도장 글자는 판독 대상 아님)."""
        r = self.r
        cx, cy = center
        layer = Image.new("L", (size, size), 0)
        ld = ImageDraw.Draw(layer)
        ld.rectangle((6, 6, size - 6, size - 6), outline=255, width=14)
        f = self._font(self.fonts.print, size // 3)
        for i, ch in enumerate(list(text.replace(" ", ""))[:4]):  # 오른쪽 세로줄부터 읽는다
            ld.text((size * (0.55 if i < 2 else 0.18), size * (0.15 + 0.38 * (i % 2))), ch, font=f, fill=255)
        layer = layer.rotate(r.uniform(-8, 8), resample=Image.BICUBIC)
        holes = np.random.default_rng(r.randint(0, 2**31)).random((size, size)) > 0.25
        layer = Image.fromarray(np.minimum(np.asarray(layer), holes.astype(np.uint8) * 255))
        self.img.paste(Image.new("RGB", (size, size), (200, 40, 40)), (cx - size // 2, cy - size // 2), layer)


@dataclass
class Case:
    """문서 한 건의 내용 (쪽들이 같이 쓴다)."""
    city: str
    dept: tuple
    case: tuple
    company: str
    rep: str
    year: int
    month: int
    day: int
    classno: str
    serial: str
    addr: str
    recipient: str
    title: str
    form: str = "gimhae"
    org: str = ""  # 발신 기관 (agency1990·draft2000). gimhae는 city

    @property
    def docno(self):
        if self.form == "draft2000":
            return f"{self.dept[1]}-{self.serial}"
        return f"{self.dept[0]} {self.classno}-{self.serial}"

    @property
    def fields(self):
        return {"docno": self.docno,
                "date": f"{self.year:04d}-{self.month:02d}-{self.day:02d}",
                "recipient": self.recipient, "title": self.title}

    @property
    def sender(self):
        """발신 명의 (서명란 위 큰 글자·관인)"""
        return f"{self.org or self.city}장"

    @property
    def us(self):
        """본문의 '우리 시' 자리"""
        return {"agency1990": "우리 사업소", "draft2000": "우리 시"}.get(self.form, "우리 시")


def new_case(r, city=None, year=None):
    city = city or r.choice(CITY)
    company, rep = r.choice(COMPANY), person(r)
    case = r.choice(CASES)
    kind = r.random()
    if kind < 0.6:
        recipient = f"주식회사 {company} 대표 {rep}"
    elif kind < 0.75:
        recipient = f"{rep} 귀하"
    elif kind < 0.9:
        recipient = "내부결재"
    else:
        recipient = "수신처 참조"
    title = case[1] + (" 알림" if r.random() < 0.4 else "")
    return Case(city=city, dept=r.choice(DEPT), case=case, company=company, rep=rep,
                year=year or r.randint(1991, 1998), month=r.randint(1, 12), day=r.randint(1, 28),
                classno=r.choice(["55142", "56120", "13010", "45010"]), serial=str(r.randint(100, 2999)),
                addr=f"{city} {r.choice(['중앙로', '새터길', '솔내로'])} {r.randint(1, 300)}",
                recipient=recipient, title=title)


def new_org(r, form, city):
    """철 하나의 발신 기관 — agency1990은 '도 + 사업소', draft2000은 시·군"""
    return f"{r.choice(PROVINCE)} {r.choice(UNIT)[0]}" if form == "agency1990" else city


def new_case_form(r, form, city=None, year=None, org=None):
    """agency1990·draft2000 문서 한 건. gimhae는 new_case (난수 순서를 바꾸지 않으려고 따로 둔다)."""
    if form == "gimhae":
        return new_case(r, city=city, year=year)
    if form not in FORMS:
        raise ValueError(f"모르는 양식 {form!r} (가능: {FORMS})")
    city = city or r.choice(CITY)
    company, rep = r.choice(COMPANY), person(r)
    case = r.choice(CASES)
    kind = r.random()
    if kind < 0.6:
        recipient = f"주식회사 {company} 대표이사 {rep}" if form == "draft2000" else f"{company} 대표 {rep}"
    elif kind < 0.75:
        recipient = f"{rep} 귀하"
    elif kind < 0.9:
        recipient = "내부결재"
    else:
        recipient = "수신자 참조" if form == "draft2000" else "수신처 참조"
    lo, hi = YEARS[form]
    if form == "agency1990":
        org = org or new_org(r, form, city)
        abbr = dict(UNIT)[org.split(" ")[-1]]
        dept = (abbr, org)
        title = case[1] + (" 통보" if r.random() < 0.4 and not case[1].endswith("통보") else "")
        classno = r.choice(["1420", "2231", "3015", "7110"])
    else:
        org = org or new_org(r, form, city)
        dept = ("", r.choice(DEPT2000))
        title = case[1] + (" 알림" if r.random() < 0.3 and not case[1].endswith("통보") else "")
        classno = ""
    return Case(city=city, dept=dept, case=case, company=company, rep=rep,
                year=year or r.randint(lo, hi), month=r.randint(1, 12), day=r.randint(1, 28),
                classno=classno, serial=str(r.randint(100, 2999) if form == "agency1990" else r.randint(1000, 19999)),
                addr=f"{city} {r.choice(['중앙로', '새터길', '솔내로', '가람대로'])} {r.randint(1, 300)}",
                recipient=recipient, title=title, form=form, org=org)


def body_lines(c, r):
    return [
        f"1. 귀사에서 {c.year}. {c.month}. {max(c.day - r.randint(3, 15), 1)}. {c.us}에 제출한 {c.case[0]} 신청에 대하여",
        f"   {c.case[2]} 및 관계 규정에 따라 검토한 결과를 다음과 같이 알려 드립니다.",
        "2. 처리 내용",
        f"   가. 신청인 : 주식회사 {c.company} (대표 {c.rep})",
        f"   나. 위   치 : {c.addr}",
        f"   다. 처리결과 : {r.choice(['허가', '조건부 허가', '수리', '보완 요구'])}",
        "3. 위 내용과 관련하여 이의가 있을 때에는 이 통지를 받은 날부터 60일 이내에",
        "   행정심판을 청구할 수 있음을 알려 드립니다.",
    ]


def first_page(c, fonts, r, typewriter=False, cont=False, attach=False):
    """시행문 첫 쪽. cont면 본문 끝을 다음 쪽으로 넘기고, attach면 '붙임'을 적는다."""
    p = Page(fonts, r, typewriter)
    p.spaced((W // 2 - 260, 150), c.city, 110, letter_gap=60)
    p.text((520, 320), f"(우) {r.randint(600, 699)}-{r.randint(100, 999):03d}  {c.city} 시청로 {r.randint(1, 99)}  /  "
                       f"전화 (0{r.randint(500, 599)}) {r.randint(20, 99)}-{r.randint(1000, 9999)}  /  "
                       f"전송 {r.randint(20, 99)}-{r.randint(1000, 9999)}", 34)
    p.line((150, 400), (W - 150, 400), 5)

    lx, vx, y0, step = 170, 470, 450, 95
    p.spaced((lx, y0), "문서번호", 46, letter_gap=4)
    xe = p.text((vx, y0), f"{c.dept[0]} {c.classno}-", 50, fld="docno")
    p.hand((xe, y0 - 18), c.serial, 78, fld="docno", cell="serial")
    p.spaced((lx, y0 + step), "시행일자", 46, letter_gap=4)
    xe = p.hand((vx, y0 + step - 18), f"{c.year % 100}. {c.month}. {c.day}.", 78, fld="date", cell="date")
    p.text((xe + 30, y0 + step), "( 보존 : 3년 )", 40)
    p.spaced((lx, y0 + 2 * step), "(경 유)", 46, letter_gap=8)
    p.spaced((lx, y0 + 3 * step), "수 신", 46, letter_gap=30)
    p.text((vx, y0 + 3 * step), c.recipient, 48, fld="recipient")
    p.spaced((lx, y0 + 4 * step), "참 조", 46, letter_gap=30)

    bx0, by0, bx1, by1 = 1450, 440, W - 160, 880  # 접수란
    p.rect((bx0, by0, bx1, by1))
    p.line((bx0 + 140, by0), (bx0 + 140, by1))
    p.spaced((bx0 + 30, by0 + 60), "선결", 40)
    p.spaced((bx0 + 30, by0 + 260), "접수", 40)
    rows = ["일자", "시간", "번호", "처리과", "담당자"]
    rh = (by1 - by0) / len(rows)
    for i, lab in enumerate(rows):
        yy = int(by0 + i * rh)
        if i:
            p.line((bx0 + 140, yy), (bx1, yy), 2)
        p.text((bx0 + 170, yy + 18), lab, 36)
    p.line((bx0 + 400, by0), (bx0 + 400, by1), 2)
    if r.random() < 0.7:  # 받는 쪽이 적는 접수 번호·일자 — 손글씨, 4칸 아님
        p.hand((bx0 + 440, by0 + 2 * rh + 4), str(r.randint(10, 999)), 60, cell="recv_no")
        p.hand((bx0 + 440, by0 + 4), f"{c.month}. {min(c.day + r.randint(1, 3), 28)}.", 60, cell="recv_date")

    p.line((150, 960), (W - 150, 960), 5)
    p.spaced((lx, 1000), "제 목", 50, letter_gap=30)
    p.text((vx, 1000), c.title, 52, fld="title")
    p.line((150, 1090), (W - 150, 1090), 2)

    lines = body_lines(c, r)
    if cont:
        lines = lines[:5]
    elif attach:
        lines += ["", f"붙임  {c.case[0]} 검토서 1부.  끝."]
    else:
        lines += ["", "끝."]
    y = 1180
    for ln in lines:
        if ln:
            lead = len(ln) - len(ln.lstrip(" "))
            p.text((220 + lead * 22, y), ln.strip(), 46)
        y += 92
    if cont:
        p.text((W // 2 - 60, H - 260), "- 1 -", 40)
        return p
    sign_block(p, c, r)
    return p


def sign_block(p, c, r, top=2420):
    if c.form == "gimhae":
        p.spaced((W // 2 - 330, top), f"{c.city}장", 96, letter_gap=70)
        p.seal((W // 2 + 380, top + 40), 240, f"{c.city}장인")
    else:  # 기관 이름이 길다 — 글자 사이를 좁히고 가운데로
        n = len(c.sender.replace(" ", ""))
        xe = p.spaced((W // 2 - n * 50, top), c.sender, 84, letter_gap=14)
        p.seal((min(xe + 170, W - 170), top + 40), 220, c.sender.split(" ")[-1] + "인")
    fy = top + 380
    p.line((150, fy), (W - 150, fy), 2)
    for i, role in enumerate(["기안자", "검토자", "결재권자"]):
        x = 200 + i * 720
        p.text((x, fy + 40), role, 38)
        p.hand((x + 180, fy + 20), person(r), 64, cell=SIGN_CELLS[i])
    p.text((200, fy + 160), f"{c.dept[1]}-{c.serial}  ( {c.year}. {c.month}. {c.day}. )   접수   "
                            f"{r.choice(['민원실', '건축과', '종합민원실'])}-{r.randint(1, 999)}", 34)
    p.text((200, fy + 230), f"{c.addr}  /  전화 {r.randint(20, 99)}-{r.randint(1000, 9999)}  /  {r.choice(['공개', '부분공개'])}", 34)


def agency_first_page(c, fonts, r, typewriter=False, cont=False, attach=False):
    """agency1990: 지어낸 도 사업소 시행문 첫 쪽. 김해 꼴과 다르게 기관명이 머리 맨 위에 가로로,
    문서번호·시행일자가 한 줄(일련번호·일자는 손글씨), 그 아래 수신·경유·제목, 접수란은 쪽 아래 서명란 옆 도장꼴."""
    p = Page(fonts, r, typewriter)
    p.text((170, 140), r.choice(["맑은 물 푸른 숲 우리 손으로", "함께 가꾸는 새 고장", "친절 봉사 신속 정확"]), 38)
    n = len(c.org.replace(" ", ""))
    p.spaced((W // 2 - n * 48, 220), c.org, 88, letter_gap=12)
    p.line((150, 360), (W - 150, 360), 6)
    p.line((150, 372), (W - 150, 372), 2)
    y0, step = 430, 100
    p.spaced((170, y0), "문서번호", 46, letter_gap=4)
    xe = p.text((440, y0), f"{c.dept[0]} {c.classno}-", 50, fld="docno")
    p.hand((xe, y0 - 18), c.serial, 78, fld="docno", cell="serial")
    p.spaced((1350, y0), "시행일자", 46, letter_gap=4)
    p.hand((1620, y0 - 18), f"{c.year % 100}. {c.month}. {c.day}.", 78, fld="date", cell="date")
    p.spaced((170, y0 + step), "수 신", 46, letter_gap=30)
    p.text((440, y0 + step), c.recipient, 48, fld="recipient")
    p.spaced((170, y0 + 2 * step), "경 유", 46, letter_gap=30)
    p.spaced((1350, y0 + 2 * step), "참 조", 46, letter_gap=30)
    p.line((150, y0 + 3 * step - 20), (W - 150, y0 + 3 * step - 20), 2)
    ty = y0 + 3 * step + 10
    p.spaced((170, ty), "제 목", 50, letter_gap=30)
    p.text((440, ty), c.title, 52, fld="title")
    p.line((150, ty + 90), (W - 150, ty + 90), 4)

    lines = body_lines(c, r)
    if cont:
        lines = lines[:5]
    elif attach:
        lines += ["", f"붙임  {c.case[0]} 검토서 1부.  끝."]
    else:
        lines += ["", "끝."]
    y = ty + 180
    for ln in lines:
        if ln:
            lead = len(ln) - len(ln.lstrip(" "))
            p.text((220 + lead * 22, y), ln.strip(), 46)
        y += 92
    if cont:
        p.text((W // 2 - 60, H - 260), "- 1 -", 40)
        return p
    sign_block(p, c, r, top=2300)
    bx0, by0 = W - 760, H - 520  # 접수란 (받는 쪽이 찍는 고무인 꼴)
    p.rect((bx0, by0, bx0 + 560, by0 + 230), 3)
    p.line((bx0, by0 + 115), (bx0 + 560, by0 + 115), 2)
    p.text((bx0 + 20, by0 + 35), "접수", 38)
    p.text((bx0 + 20, by0 + 150), "번호", 38)
    if r.random() < 0.7:
        p.hand((bx0 + 160, by0 + 20), f"{c.month}. {min(c.day + r.randint(1, 3), 28)}.", 60, cell="recv_date")
        p.hand((bx0 + 160, by0 + 135), str(r.randint(10, 999)), 60, cell="recv_no")
    return p


def draft_first_page(c, fonts, r, typewriter=False, cont=False, attach=False):
    """draft2000: 2000년대 기안문 첫 쪽. 맨 위 기관명·'기 안 문', 가로 표 머리부
    (윗줄 항목명 문서번호|시행일자|보존기간|기안자, 아랫줄 값 — 문서번호·일자는 인쇄, 기안자는 손글씨),
    표 아래 수신·(경유)·제목, 본문, 서명란."""
    p = Page(fonts, r, typewriter)
    p.text((170, 150), c.org, 44)
    p.spaced((W // 2 - 280, 230), "기 안 문", 100, letter_gap=40)
    x0, x1, ya, yb, yc = 150, W - 150, 420, 520, 640  # 표: 항목명 줄 ya~yb, 값 줄 yb~yc
    cols = [x0, 820, 1430, 1830, x1]
    p.rect((x0, ya, x1, yc), 4)
    p.line((x0, yb), (x1, yb), 2)
    for cx in cols[1:-1]:
        p.line((cx, ya), (cx, yc), 2)
    for i, lab in enumerate(["문서번호", "시행일자", "보존기간", "기안자"]):
        p.spaced((cols[i] + 40, ya + 25), lab, 42, letter_gap=6)
    p.text((cols[0] + 30, yb + 32), c.docno, 48, fld="docno")
    p.text((cols[1] + 30, yb + 32), f"{c.year}. {c.month}. {c.day}.", 48, fld="date")
    p.text((cols[2] + 30, yb + 32), r.choice(["3년", "5년", "10년", "영구"]), 46)
    p.hand((cols[3] + 30, yb + 18), person(r), 66, cell="sign_table")
    y0, step = 720, 100
    p.spaced((170, y0), "수 신", 46, letter_gap=30)
    p.text((440, y0), c.recipient, 48, fld="recipient")
    p.spaced((170, y0 + step), "(경 유)", 46, letter_gap=8)
    p.spaced((170, y0 + 2 * step), "제 목", 50, letter_gap=30)
    p.text((440, y0 + 2 * step), c.title, 52, fld="title")
    p.line((150, y0 + 2 * step + 90), (W - 150, y0 + 2 * step + 90), 2)

    lines = body_lines(c, r)
    if cont:
        lines = lines[:5]
    elif attach:
        lines += ["", f"붙임  {c.case[0]} 검토서 1부.  끝."]
    else:
        lines += ["", "끝."]
    y = y0 + 2 * step + 180
    for ln in lines:
        if ln:
            lead = len(ln) - len(ln.lstrip(" "))
            p.text((220 + lead * 22, y), ln.strip(), 46)
        y += 92
    if cont:
        p.text((W // 2 - 60, H - 260), "- 1 -", 40)
        return p
    sign_block(p, c, r, top=2350)
    return p


FIRST_PAGE = {"gimhae": first_page, "agency1990": agency_first_page, "draft2000": draft_first_page}


def cont_page(c, fonts, r, typewriter=False, attach=False):
    """첫 쪽 본문이 이어지는 쪽 — 머리 없이 본문 나머지와 발신 명의."""
    p = Page(fonts, r, typewriter)
    lines = body_lines(c, r)[5:] + ["", f"붙임  {c.case[0]} 검토서 1부.  끝." if attach else "끝."]
    y = 300
    for ln in lines:
        if ln:
            lead = len(ln) - len(ln.lstrip(" "))
            p.text((220 + lead * 22, y), ln.strip(), 46)
        y += 92
    sign_block(p, c, r, top=1500)
    p.text((W // 2 - 60, H - 260), "- 2 -", 40)
    return p


def attach_page(c, fonts, r, typewriter=False):
    """붙임 서식: '[별지 제N호 서식]' 머리 + 검토서 표 (시작 쪽 아님)."""
    p = Page(fonts, r, typewriter)
    p.text((170, 160), f"[별지 제{r.randint(2, 40)}호 서식]", 40)
    p.spaced((W // 2 - 300, 300), f"{c.case[0]} 검토서", 72, letter_gap=10)
    rows = [("신청인", f"주식회사 {c.company}"), ("대표자", c.rep), ("위치", c.addr),
            ("신청일", f"{c.year}. {c.month}. {max(c.day - 5, 1)}."), ("관계 법령", c.case[2]),
            ("검토 의견", r.choice(["관계 법령에 저촉되지 않음", "보완 후 처리 가능", "현지 확인 결과 이상 없음"]))]
    x0, x1, xm, y = 200, W - 200, 700, 520
    for lab, val in rows:
        p.rect((x0, y, x1, y + 160), 2)
        p.line((xm, y), (xm, y + 160), 2)
        p.text((x0 + 40, y + 55), lab, 44)
        p.text((xm + 40, y + 55), val, 44)
        y += 160
    p.text((x0, y + 80), "검토자", 40)
    p.hand((x0 + 200, y + 60), person(r), 64, cell="sign_attach")
    return p
