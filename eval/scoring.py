"""채점 규칙 — PoC records_poc/scripts/rescore.py(·gt_draft.norm_date·score_extract.norm)에서 옮겨 온 것
(https://github.com/reddol18/docflow/issues/8 조건 3).

동작은 옛 규칙과 같다 (실제 7철 결과로 같은 점수인지 확인 — docs/design.md #8 절). 저장소 밖 PoC 폴더 없이 돈다.
  norm       주식회사=(주), ⓐ=@ 로 맞춘 뒤 공백·따옴표·마침표·쉼표·쌍점·괄호·물결·붙임표를 지운다
  match      정답의 '?'는 아무 글자 하나
  gt_serial  정답 문서번호 끝 '-'(또는 '~') 뒤 일련번호 숫자. 끝이 '-'면 빈칸(예측도 비어야 맞음), '-'가 없으면 None(채점 제외)
  pdate      예측 날짜 → YYYY-MM-DD, 일자가 없으면 YYYY-MM, 못 읽으면 빈칸 (옛 규칙 그대로 1990~2005년만 고쳐 읽는다)
  load_gt    정답 CSV(PoC review.csv·합성 gt.csv와 같은 열) → (rows, idx, bnd, src). 경로는 부르는 쪽이 준다
실제 정답 CSV는 저장소 밖에 둔다 (eval/score.py --gt).
"""
import csv
import re
from pathlib import Path


def norm(s):
    # 주식회사=(주), ⓐ=@(아파트) 로 맞춘 뒤 괄호·구분기호를 지운다
    s = (s or "").replace("주식회사", "(주)").replace("ⓐ", "@")
    return re.sub(r"[\s\"'“”‘’.,:：()~\-]", "", s)


def _sp(word):
    """'경유' → 글자 사이 공백 허용 정규식."""
    return r"\s*".join(map(re.escape, word))


_AN = re.compile(r"\[\s*제\s*\d+\s*안\s*\]")
_VIA = re.compile(_sp("경유"))
_DATE = re.compile(r"(?<!\d)['`‘’]?\s*((?:19|20)?\d{2})(?:\s*[.,·]\s*|\s+)(\d{1,2})\s*[.,]?\s*(\d{1,2})?")


def norm_date(raw):
    """날짜 글자 → (YYYY-MM-DD 또는 '', 사유). 사유 '일자 빠짐(YYYY-MM)'이면 연·월만 읽힘"""
    if not raw:
        return "", "날짜 없음"
    s = _AN.sub(" ", raw)
    s = _VIA.sub(" ", s)
    if re.search(r"(?<!\d)199\s+\d", s):  # '199 8 . 16' = 연도 끝자리(손글씨)가 라벨에 없음
        return "", "연도 끝자리 빠짐"
    m = _DATE.search(s)
    if not m:
        return "", "날짜 형식 못 읽음"
    y, mo, d = m.group(1), int(m.group(2)), m.group(3)
    y = int(y)
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


def match(pred, gt):
    """gt의 '?'는 아무 글자 하나."""
    if "?" in gt:
        return re.fullmatch("".join("." if c == "?" else re.escape(c) for c in gt), pred) is not None
    return pred == gt


def pdate(s):
    """예측 날짜 → YYYY-MM-DD, 일자가 없으면 YYYY-MM, 못 읽으면 빈칸."""
    s = (s or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}(-\d{2})?", s):
        return s
    full, msg = norm_date(s.replace("(", " ").replace(")", " "))
    if full:
        return full
    m = re.match(r"일자 빠짐\((\d{4}-\d{2})\)", msg)
    return m.group(1) if m else ""


def gt_serial(docno):
    """정답 문서번호의 일련번호. 문서번호가 없거나 '-'가 없으면 None(채점 제외)."""
    if not docno or not re.search(r"[-~]", docno):
        return None
    return re.sub(r"[^\d?]", "", re.split(r"[-~]", docno)[-1])


def ok_field(f, pred, gt):
    if f == "date":
        return match(pdate(pred), gt)
    return match(norm(pred), norm(gt))


def pct(a, b):
    return f"{a:3d}/{b:3d} {a / b:4.0%}" if b else "  -/  0     "


def default_gt(results):
    """정답 CSV를 안 줬을 때: 결과 파일(<work>/platform/<철>.json) 옆 work의 gt/review.csv. 결과가 여러 work에 걸치면 멈춘다"""
    off = [x for x in results if Path(x).resolve().parent.name != "platform"]
    if off:
        raise SystemExit(f"결과 파일이 <work>/platform/ 아래가 아니다 ({len(off)}개) — --gt로 정답 CSV를 준다")
    gts = {Path(x).resolve().parent.parent / "gt" / "review.csv" for x in results}
    if len(gts) != 1:
        raise SystemExit(f"결과 파일이 work {len(gts)}곳에 걸친다 — --gt로 정답 CSV를 준다")
    p = gts.pop()
    if not p.exists():
        raise SystemExit(f"정답 파일이 없다: {p} — --gt로 정답 CSV를 준다")
    return p


def load_gt(paths):
    """정답 CSV 하나 또는 여럿(합성 철마다 gt.csv) → (rows, idx, bnd, src).
    idx = is_start=Y 쪽, bnd = 확인 열에 '경계'가 있는 쪽의 is_start, src = note에 'Claude 판독'이면 claude 아니면 human"""
    if isinstance(paths, (str, Path)):
        paths = [paths]
    rows = []
    for p in paths:
        with Path(p).open(encoding="utf-8-sig", newline="") as f:
            rows.extend(csv.DictReader(f))
    idx = {r["page"]: r for r in rows if r["is_start"] == "Y"}
    bnd = {r["page"]: r["is_start"] == "Y" for r in rows if "경계" in r["확인"]}
    src = {r["page"]: "claude" if "Claude 판독" in r["note"] else "human" for r in rows}
    return rows, idx, bnd, src
