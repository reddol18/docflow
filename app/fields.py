"""색인 칸 스키마 — 칸 목록·표시명·단위·추출 방식·표기 규칙을 설정 파일로 (https://github.com/reddol18/docflow/issues/9).

스키마 파일(`app/field_schemas/*.yaml`)은 두 가지다.
  기본 스키마  `isad_g.yaml` — 칸마다 ISAD(G) 영역(area)이 적혀 있다. 그대로 써도 된다.
  기관 매핑    `gimhae.yaml` 등 — `base:`로 기본 스키마를 가리키고, 칸마다 `isad:`로 기본 스키마의 칸에 잇는다.
쓰는 스키마는 환경변수 FIELD_SCHEMA(파일 경로). 없으면 김해 매핑(지금 운영 자료).

추출 방식(method)
  rule         코드 규칙이 만든다 (`rule:` 이름 — app/graph.py RULES)
  llm_extract  LLM이 OCR 텍스트에서 뽑는다 (`prompt:` 한 줄이 추출 지시문에 들어간다)
  llm_generate LLM이 문장을 지어 쓴다 (요약 등, `prompt:`)
  vocab        LLM이 통제어휘(`vocab:`) 중에서 고른다. 목록에 없는 답은 빈칸
  fixed        사업 고정값(`value:`) — 모든 건에 같은 값
  manual       사람이 입력 (처음엔 빈칸)
  hidden       화면·검색 결과에 안 보이는 칸 (`rule:`로 값을 만들 수 있다)
rule 칸도 `prompt:`를 가질 수 있다 — 그 규칙이 LLM 추출값을 재료로 쓸 때 (김해 문서번호·일자·수신).

표기 규칙(normalize)은 칸 값에 차례로 적용하는 후처리다 (NORMALIZE).
"""
import json
import os
import re
from pathlib import Path

import yaml

DIR = Path(__file__).parent / "field_schemas"
BASE = DIR / "isad_g.yaml"  # 기본 스키마 (ISAD(G))
DEFAULT = DIR / "gimhae.yaml"  # 지금 운영 자료(김해 인·허가)의 기관 매핑

METHODS = ("rule", "llm_extract", "llm_generate", "vocab", "fixed", "manual", "hidden")
LLM = ("llm_extract", "llm_generate", "vocab")
LEVELS = ("volume", "document")  # 철 / 건
# ISAD(G) 2판 7개 영역
AREAS = {"identity": "식별", "context": "배경", "content": "내용과 구조", "access": "열람과 이용 조건",
         "allied": "관련 자료", "notes": "주기", "control": "기술 통제"}
NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
# 문서 dict·검색 결과에 같은 이름의 키가 따로 있다
RESERVED = {"doc_id", "vol", "pages", "flags", "reviewed", "edited", "rrf", "vector_rank", "keyword_rank", "body",
            "score", "year"}
FIELD_KEYS = {"name", "label", "level", "method", "area", "isad", "prompt", "vocab", "value", "rule", "normalize",
              "derive_from", "flag_key", "note"}
TOP_KEYS = {"name", "base", "fields", "extract", "search", "note"}


class SchemaError(ValueError):
    pass


# ---------- 표기 규칙 ----------

UNKNOWN = re.compile(r"(미상|불명|알\s*수\s*없음|unknown)", re.I)
DECADE = re.compile(r"(\d{3})0\s*년대")
LOC_WORDS = re.compile(r"(서고|서가|층|열|번|제)")


def date_unknown(v):
    """생산시기 미상 → 9999-99-99"""
    return "9999-99-99" if UNKNOWN.fullmatch(v.strip()) else v


def decade(v):
    """'1950년대' → '1950-00-00 ~ 1959-00-00'"""
    m = DECADE.fullmatch(v.strip())
    return f"{m.group(1)}0-00-00 ~ {m.group(1)}9-00-00" if m else v


def location(v):
    """위치를 '서고-서가-층-열'로. 숫자·기호 네 마디를 찾으면 '-'로 잇고, 아니면 그대로 둔다 (형식 검사는 출력 단계)"""
    parts = [p for p in re.split(r"[\s\-_/.,]+", LOC_WORDS.sub(" ", v)) if p]
    return "-".join(parts) if len(parts) == 4 and all(re.fullmatch(r"[0-9A-Za-z]+", p) for p in parts) else v


HANJA_TABLE = DIR / "hanja_hangul.tsv"  # Unicode Unihan kHangul 대표 음 (scripts/build_hanja_table.py)
_hanja = None


def hanja_hangul(v):
    """한자 → 한글 음 (글자마다 대표 음 하나, 두음법칙 없음). 표에 없는 글자는 그대로 둔다"""
    global _hanja
    if _hanja is None:  # 다 만든 뒤 한 번에 넣는다 — 문서 하위 그래프가 여러 스레드에서 돌 때 덜 찬 표를 보지 않게
        table = {}
        for line in HANJA_TABLE.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#"):
                h, k = line.split("\t")
                table[ord(h)] = k
        _hanja = table
    return v.translate(_hanja)


NORMALIZE = {"date_unknown": date_unknown, "decade": decade, "location": location, "hanja_hangul": hanja_hangul,
             "strip": str.strip}


def normalize(field, value):
    v = "" if value is None else str(value)
    for n in field.get("normalize", ()):
        v = NORMALIZE[n](v)
    return v


def normalize_input(s, values):
    """사람이 고친 값 {칸: 값}에도 같은 표기 규칙을 적용한다 (검토·수정·검사 입력). 모르는 칸은 그대로"""
    by = by_name(s)
    return {k: normalize(by[k], v) if k in by else v for k, v in values.items()}


# ---------- 읽기·검사 ----------

def _str(f, k, where):
    if not isinstance(f.get(k), str) or not f[k].strip():
        raise SchemaError(f"{where}: {k} 없음")


def _field(f, i, base_names):
    where = f"fields[{i}]"
    if not isinstance(f, dict):
        raise SchemaError(f"{where}: 항목이 아님")
    extra = set(f) - FIELD_KEYS
    if extra:
        raise SchemaError(f"{where}: 모르는 키 {sorted(extra)}")
    for k in ("name", "label", "level", "method"):
        _str(f, k, where)
    where = f"칸 {f['name']}"
    if not NAME.fullmatch(f["name"]) or f["name"] in RESERVED:
        raise SchemaError(f"{where}: 이름은 영문 소문자·숫자·_ (예약어 제외)")
    if f["level"] not in LEVELS:
        raise SchemaError(f"{where}: level은 {LEVELS} 중 하나")
    m = f["method"]
    if m not in METHODS:
        raise SchemaError(f"{where}: 모르는 추출 방식 {m!r}")
    if m in LLM:
        _str(f, "prompt", where)
    if "prompt" in f and m not in (*LLM, "rule"):
        raise SchemaError(f"{where}: prompt는 LLM·규칙 칸만")
    if m == "vocab" and (not isinstance(f.get("vocab"), list) or not f["vocab"]
                        or not all(isinstance(x, str) and x for x in f["vocab"])):
        raise SchemaError(f"{where}: vocab 목록 없음")
    if m == "fixed" and not isinstance(f.get("value"), str):
        raise SchemaError(f"{where}: 고정값(value) 없음")
    if m == "rule":
        _str(f, "rule", where)
    if "rule" in f and m not in ("rule", "hidden"):
        raise SchemaError(f"{where}: rule은 rule·hidden 칸만")
    if f["level"] == "volume" and m not in ("fixed", "manual"):
        raise SchemaError(f"{where}: 철 단위 칸은 지금 fixed·manual만 (철 단위 추출은 아직 없음)")
    norm = f.get("normalize", [])
    if not isinstance(norm, list) or any(n not in NORMALIZE for n in norm):
        raise SchemaError(f"{where}: 모르는 표기 규칙 {norm!r} (가능: {sorted(NORMALIZE)})")
    if base_names is None:
        if f.get("area") not in AREAS:
            raise SchemaError(f"{where}: 기본 스키마 칸에는 ISAD(G) 영역(area: {list(AREAS)})이 있어야 함")
    elif f.get("isad") not in base_names:
        raise SchemaError(f"{where}: isad가 기본 스키마 칸 이름이 아님 ({f.get('isad')!r})")


def parse(s, base=None):
    """스키마 dict 검사. base(기본 스키마 dict)를 주면 기관 매핑으로 본다. 빠진 항목·모르는 방식은 SchemaError"""
    if not isinstance(s, dict):
        raise SchemaError("스키마가 객체가 아님")
    extra = set(s) - TOP_KEYS
    if extra:
        raise SchemaError(f"모르는 키 {sorted(extra)}")
    _str(s, "name", "스키마")
    fl = s.get("fields")
    if not isinstance(fl, list) or not fl:
        raise SchemaError("fields 없음")
    base_names = {f["name"] for f in base["fields"]} if base else None
    for i, f in enumerate(fl):
        _field(f, i, base_names)
    names = [f["name"] for f in fl]
    if len(set(names)) != len(names):
        raise SchemaError(f"칸 이름 중복: {sorted({n for n in names if names.count(n) > 1})}")
    by = {f["name"]: f for f in fl}
    for f in fl:
        for k in ("derive_from", "flag_key"):
            if k in f and f[k] not in by:
                raise SchemaError(f"칸 {f['name']}: {k}가 없는 칸 {f[k]!r}")
        if "derive_from" in f and "rule" not in f:
            raise SchemaError(f"칸 {f['name']}: derive_from에는 rule이 있어야 함")
    if any("prompt" in f for f in fl):
        ex = s.get("extract")
        if not isinstance(ex, dict) or set(ex) - {"preamble", "rules"}:
            raise SchemaError("extract(preamble·rules) 없음 — prompt가 있는 칸이 있다")
        for k in ("preamble", "rules"):
            _str(ex, k, "extract")
    shown = [f["name"] for f in fl if f["method"] != "hidden" and f["level"] == "document"]
    se = s.get("search") or {}
    if not isinstance(se, dict) or set(se) - {"keyword", "embed", "year"}:
        raise SchemaError("search는 keyword·embed·year만")
    for k in ("keyword", "embed"):
        v = se.setdefault(k, shown)
        # 숨김·철 단위 칸은 넣지 않는다 — 검색 순위로 숨긴 값이 드러나지 않게
        if not isinstance(v, list) or not v or any(n not in shown for n in v):
            raise SchemaError(f"search.{k}: 보이는 건 단위 칸 이름 목록이어야 함")
    if se.get("year") is not None and se["year"] not in by:
        raise SchemaError(f"search.year: 없는 칸 {se['year']!r}")
    se.setdefault("year", None)
    s["search"] = se
    return s


def _read(path):
    try:
        return yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise SchemaError(f"{Path(path).name}: YAML 오류 {e}") from None


def load(path=None):
    """스키마 파일을 읽고 검사한다. base:가 있으면 같은 폴더의 기본 스키마도 읽어 isad 연결을 확인한다"""
    path = Path(path or os.environ.get("FIELD_SCHEMA") or DEFAULT)
    s = _read(path)
    base = None
    if isinstance(s, dict) and s.get("base"):
        bp = path.parent / s["base"]
        if bp.parent != path.parent or not bp.is_file():
            raise SchemaError(f"base 파일 없음: {s['base']!r} (같은 폴더의 파일 이름)")
        base = parse(_read(bp))
    return parse(s, base)


# ---------- 쓰기 ----------

def by_name(s):
    return {f["name"]: f for f in s["fields"]}


def doc_fields(s):
    """문서마다 값을 두는 칸 (철 단위 고정값도 '전 건에 채움'이라 포함)"""
    return [f for f in s["fields"] if f["level"] == "document" or f["method"] == "fixed"]


def shown(s):
    """화면·검색 결과에 보이는 건 단위 칸 이름 (숨김 제외, 스키마 순서)"""
    return tuple(f["name"] for f in s["fields"] if f["method"] != "hidden" and f["level"] == "document")


def editable(s):
    """사람이 고칠 수 있는 칸 (숨김·고정값 제외)"""
    return tuple(f["name"] for f in s["fields"]
                 if f["method"] not in ("hidden", "fixed") and f["level"] == "document")


def labels(s):
    return {f["name"]: f["label"] for f in s["fields"]}


def flag_keys(s):
    """{검토 표시 키: 사람이 고치는 칸} — 김해는 일련번호(serial) 표시를 문서번호(docno) 칸에서 고친다"""
    return {f["flag_key"]: f["name"] for f in s["fields"] if "flag_key" in f}


def prompt_fields(s):
    return [f for f in s["fields"] if "prompt" in f]


def extract_prompt(s):
    """LLM 추출 지시문. prompt가 있는 칸이 없으면 None (LLM을 부르지 않는다)"""
    items = prompt_fields(s)
    if not items:
        return None
    lines = []
    for f in items:
        extra = f" (다음 중 하나: {', '.join(f['vocab'])})" if f["method"] == "vocab" else ""
        lines.append(f"- {f['name']}: {f['prompt']}{extra}")
    first, *rest = (f["name"] for f in items)
    example = "{" + ", ".join([f'"{first}": {{"value": "...", "confidence": 0.0}}', *(f'"{n}": {{...}}' for n in rest)]) + "}"
    ex = s["extract"]
    return (f"{ex['preamble'].strip()}\n\n아래 {len(items)}칸을 뽑는다.\n" + "\n".join(lines)
            + f"\n\n{ex['rules'].strip()}\n출력: JSON 객체 하나\n{example}")


def parse_extract(s, content):
    """추출 응답 JSON 글자 → prompt 칸별 {value, confidence}. 모양이 틀린 칸은 빈 값·확신도 0, JSON 자체가 깨지면 ValueError.
    app/llm.extract와 모델 비교 하네스(eval/promptfoo/)가 같이 쓴다 (여기 두는 이유: openai를 불러오지 않는 가벼운 모듈)"""
    out = json.loads(content)
    if not isinstance(out, dict):
        out = {}

    def cell(v):
        v = v if isinstance(v, dict) else {}
        try:
            conf = float(v.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        return {"value": str(v.get("value") or ""), "confidence": conf}
    return {f["name"]: cell(out.get(f["name"])) for f in prompt_fields(s)}


def assemble(s, llm_out, rule_values):
    """칸 값 조립: 방식별 원천 → 통제어휘 확인 → 표기 규칙. llm_out = {칸: {value, confidence}}"""
    out = {}
    for f in doc_fields(s):
        m = f["method"]
        if m == "fixed":
            v = f["value"]
        elif "rule" in f:
            v = rule_values.get(f["rule"], "")
        elif m in LLM:
            v = str((llm_out.get(f["name"]) or {}).get("value") or "")
        else:
            v = ""
        if m == "vocab" and v not in f["vocab"]:
            v = ""
        out[f["name"]] = normalize(f, v)
    return out


def search_text(s, values):
    """낱말 검색 대상 글자 (search.keyword 칸을 공백으로 이음)"""
    return " ".join(str(values.get(k) or "") for k in s["search"]["keyword"])


def embed_text(s, values):
    """임베딩에 넣는 머리 (search.embed 칸 '표시명: 값' 줄)"""
    lab = labels(s)
    return "\n".join(f"{lab[k]}: {values.get(k) or ''}" for k in s["search"]["embed"])


def year_of(s, values):
    """연도 필터용 정수: search.year 칸 값 앞 네 자리. 미상(9999)은 None"""
    k = s["search"]["year"]
    v = str(values.get(k) or "") if k else ""
    return int(v[:4]) if v[:4].isdigit() and v[:4] != "9999" else None


def check_rules(s, known):
    """스키마의 rule 이름이 코드에 있는지 (graph가 불러올 때 확인)"""
    bad = sorted({f["rule"] for f in s["fields"] if "rule" in f} - set(known))
    if bad:
        raise SchemaError(f"모르는 rule {bad} (가능: {sorted(known)})")


SCHEMA = load()
