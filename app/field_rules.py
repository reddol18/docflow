"""칸 검증 규칙 — 기계 검수 (https://github.com/reddol18/docflow/issues/87).

스키마 칸마다 `validate:`로 적는다. 사람 검토 전에 돈다(그래프 validate 노드 → 검토 표시 '기계 검수'), 사람이 값을
고치면 철 전체를 다시 본다(검토 제출·문서 수정·검사·단계 승인 — 모두 app/flags.refresh), 출력 전 검증(app/export.build)도
이 모듈의 violations를 그대로 쓴다 (따로 구현하지 않는다).

  validate:
    required: true             빈칸이면 위반
    length: 23                 고정 길이 (글자 수)
    max_length: 100            최대 길이
    pattern: "[A-Z0-9]{23}"    정규식 — 값 전체가 맞아야 한다(fullmatch). 플래그 없음: \\d·\\w는 유니코드 숫자·글자도
                               받으므로 영문·숫자만이면 [A-Z0-9]처럼 직접 쓴다
    code_table: codes/x.txt    코드표 파일 (스키마 파일 폴더 기준 상대 경로, 그 폴더 밖 금지)
    unique: volume             같은 철 안에서 같은 값 금지

빈 값에는 required만 본다 (빈칸 허용 칸은 나머지 규칙을 건너뛴다).

코드표 파일: UTF-8 글자 파일, 한 줄에 코드 하나. 코드 뒤에 탭을 두고 설명을 적어도 된다(검사는 코드만 본다).
빈 줄과 '#'로 시작하는 줄은 건너뛴다. 같은 코드가 두 번 있거나 코드가 하나도 없으면 시작할 때 멈춘다.

정규식 ReDoS 막기 (Python re에는 시간 제한이 없다)
  1. 시작할 때 거부: 패턴 길이 PATTERN_MAX 초과, 길이가 바뀌는 반복 안에 다시 길이가 바뀌는 반복이나 선택(|)이 있는 패턴
     — 예 (a+)+, (a|ab)*, (\\w+\\s?)* . 고정 횟수 반복 안의 반복((\\d{2}-){3})과 문자 클래스 반복([A-Z0-9]+)은 받는다.
  2. 검사할 때: 값이 VALUE_MAX자를 넘으면 정규식을 돌리지 않고 위반('형식 검사 길이 초과')으로 본다.
     중첩이 없는 패턴의 최악 비용은 길이의 다항식이라 이 상한으로 묶인다.

스키마 검사(시작할 때 멈춤): 모르는 규칙 키, 잘못된 값 형식, 깨진 정규식·위험한 정규식, length > max_length,
코드표 파일 없음·경로 이탈·코드가 길이·정규식과 안 맞음, 숨김·철 단위 칸의 규칙, 고정값 칸의 unique.
이 모듈은 app의 다른 모듈을 불러오지 않는다 (app/fields.py가 스키마를 읽을 때 부른다).
"""
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

try:
    import re._parser as _sre  # Python 3.11+
except ImportError:  # pragma: no cover
    import sre_parse as _sre  # noqa: F401

KEYS = ("required", "length", "max_length", "pattern", "code_table", "unique")
LEN_MAX = 10000  # 길이 규칙 값의 상한 (칸 입력 상한보다 넉넉히 — 틀린 숫자를 막는 정도)
PATTERN_MAX = 200  # 정규식 패턴 길이 상한
VALUE_MAX = 200  # 정규식을 돌리는 값 길이 상한 (넘으면 위반)
CODE_FILE_MAX = 1_000_000  # 코드표 파일 크기 상한 (바이트)
MACHINE = "기계 검수"  # 검토 표시 사유 머리 — 이것으로 시작하는 사유는 다시 계산할 때 바꿔 끼운다

# 규칙 → 위반 설명 (검토 화면에 보이는 글. 값은 싣지 않는다)
TEXT = {"required": "필수 칸 빈칸", "length": "길이 {n}자 아님", "max_length": "{n}자 초과", "pattern": "형식(정규식) 불일치",
        "pattern_long": "형식 검사 길이 초과", "code_table": "코드표에 없는 값", "unique": "철 안 중복"}


class RuleError(ValueError):
    pass


@dataclass(frozen=True)
class Rules:
    """읽어 둔 칸 규칙. pattern은 컴파일한 것, codes는 코드 집합"""
    required: bool = False
    length: int | None = None
    max_length: int | None = None
    pattern: re.Pattern | None = None
    codes: frozenset | None = None
    unique: bool = False

    def names(self):
        return [k for k in KEYS if getattr(self, "codes" if k == "code_table" else k) not in (None, False)]


# ---------- 읽기·검사 ----------

def _int(raw, k):
    v = raw[k]
    if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= LEN_MAX:
        raise RuleError(f"validate.{k}는 1~{LEN_MAX} 정수")
    return v


_VAR = (_sre.MAX_REPEAT, _sre.MIN_REPEAT) + ((_sre.POSSESSIVE_REPEAT,) if hasattr(_sre, "POSSESSIVE_REPEAT") else ())


def _risky(items, in_var_repeat=False):
    """길이가 바뀌는 반복 안에 다시 길이가 바뀌는 반복·선택이 있으면 True (지수 되짚기 모양)"""
    for op, av in items:
        if op in _VAR:
            lo, hi, sub = av
            var = lo != hi
            if in_var_repeat and var:
                return True
            if _risky(sub, in_var_repeat or (var and hi > 1)):
                return True
        elif op == _sre.BRANCH:
            if in_var_repeat:
                return True
            if any(_risky(b, in_var_repeat) for b in av[1]):
                return True
        elif op == _sre.SUBPATTERN:
            if _risky(av[-1], in_var_repeat):
                return True
        elif op in (_sre.ASSERT, _sre.ASSERT_NOT):
            if _risky(av[1], in_var_repeat):
                return True
        elif op == _sre.GROUPREF_EXISTS:
            raise RuleError("validate.pattern: 조건 그룹은 쓰지 않는다")
        elif op == _sre.GROUPREF:
            raise RuleError("validate.pattern: 역참조는 쓰지 않는다")
    return False


def compile_pattern(p):
    if not isinstance(p, str) or not p:
        raise RuleError("validate.pattern은 글자")
    if len(p) > PATTERN_MAX:
        raise RuleError(f"validate.pattern은 {PATTERN_MAX}자까지")
    try:
        rx = re.compile(p)
        tree = _sre.parse(p)
    except re.error as e:
        raise RuleError(f"validate.pattern 정규식 오류: {e}") from None
    if _risky(list(tree)):
        raise RuleError("validate.pattern: 반복 안에 다시 반복·선택(|)이 있어 되짚기가 폭증할 수 있음 — 문자 클래스로 쓴다")
    return rx


def code_path(ref, root):
    """코드표 경로: 스키마 폴더(root) 기준 상대 경로만. 절대 경로·'..'·폴더 밖은 거부"""
    if not isinstance(ref, str) or not ref.strip():
        raise RuleError("validate.code_table은 파일 경로 글자")
    if root is None:
        raise RuleError("validate.code_table은 파일에서 읽은 스키마에서만 (스키마 폴더 기준 경로)")
    win, posix = PureWindowsPath(ref), PurePosixPath(ref)
    if win.is_absolute() or win.drive or win.root or posix.is_absolute() or ".." in win.parts or ".." in posix.parts:
        raise RuleError(f"validate.code_table은 스키마 폴더 안 상대 경로 ({ref!r})")
    base = Path(root).resolve()
    p = (base / ref).resolve()
    if not p.is_relative_to(base):  # 링크로 폴더 밖을 가리키는 경우
        raise RuleError(f"validate.code_table이 스키마 폴더 밖 ({ref!r})")
    if not p.is_file():
        raise RuleError(f"validate.code_table 파일 없음 ({ref!r})")
    if p.stat().st_size > CODE_FILE_MAX:
        raise RuleError(f"validate.code_table 파일이 {CODE_FILE_MAX}바이트 넘음 ({ref!r})")
    return p


def read_codes(path):
    """코드표 파일 → 코드 집합 (형식은 모듈 머리말)"""
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise RuleError(f"validate.code_table은 UTF-8 ({Path(path).name})") from None
    codes = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        code = line.split("\t", 1)[0].strip()
        if not code:
            raise RuleError(f"validate.code_table: 코드가 빈 줄 ({Path(path).name})")
        codes.append(code)
    if not codes:
        raise RuleError(f"validate.code_table: 코드 없음 ({Path(path).name})")
    dup = sorted({c for c in codes if codes.count(c) > 1})
    if dup:
        raise RuleError(f"validate.code_table: 같은 코드가 두 번 ({Path(path).name}, {len(dup)}개)")
    return frozenset(codes)


def compile_rules(raw, root=None):
    """스키마의 validate dict → Rules. 이미 읽은 것(Rules)은 그대로. 틀리면 RuleError"""
    if isinstance(raw, Rules):
        return raw
    if not isinstance(raw, dict) or not raw:
        raise RuleError("validate는 규칙 객체 (예: {required: true, length: 4})")
    extra = set(raw) - set(KEYS)
    if extra:
        raise RuleError(f"validate: 모르는 규칙 {sorted(extra)} (가능: {list(KEYS)})")
    req = raw.get("required", False)
    if not isinstance(req, bool):
        raise RuleError("validate.required는 true/false")
    length = _int(raw, "length") if "length" in raw else None
    mx = _int(raw, "max_length") if "max_length" in raw else None
    if length and mx and length > mx:
        raise RuleError(f"validate: length {length} > max_length {mx}")
    rx = compile_pattern(raw["pattern"]) if "pattern" in raw else None
    if "unique" in raw and raw["unique"] != "volume":
        raise RuleError("validate.unique는 volume (철 안 중복 금지)")
    codes = read_codes(code_path(raw["code_table"], root)) if "code_table" in raw else None
    r = Rules(required=req, length=length, max_length=mx, pattern=rx, codes=codes, unique="unique" in raw)
    if codes:  # 코드표의 코드가 같은 칸의 다른 규칙에 걸리면 어느 값도 통과할 수 없다 — 설정 충돌
        bad = [c for c in codes if check_value(r, c, skip=("code_table",))]
        if bad:
            raise RuleError(f"validate: 코드표 코드 {len(bad)}개가 같은 칸의 길이·정규식 규칙과 안 맞음")
    return r


# ---------- 검사 ----------

def check_value(r, value, skip=()):
    """값 하나에 대한 위반 규칙 이름 목록 (unique 제외 — 철 단위). 빈 값은 required만"""
    v = "" if value is None else str(value)
    if not v.strip():
        return ["required"] if r.required and "required" not in skip else []
    out = []
    if r.length is not None and len(v) != r.length and "length" not in skip:
        out.append("length")
    if r.max_length is not None and len(v) > r.max_length and "max_length" not in skip:
        out.append("max_length")
    if r.pattern is not None and "pattern" not in skip:
        if len(v) > VALUE_MAX:
            out.append("pattern_long")
        elif not r.pattern.fullmatch(v):
            out.append("pattern")
    if r.codes is not None and v not in r.codes and "code_table" not in skip:
        out.append("code_table")
    return out


def ruled(schema):
    """규칙이 있는 칸 목록"""
    return [f for f in schema["fields"] if isinstance(f.get("validate"), Rules)]


def violations(docs, schema):
    """docs(철 하나)의 문서마다 [(칸, 규칙 이름)] — 검토 표시(app/flags.machine)와 출력 전 검증(app/export.build)이 같이 쓴다"""
    out = [[] for _ in docs]
    for f in ruled(schema):
        r, name = f["validate"], f["name"]
        vals = [str((d.get("fields") or {}).get(name) or "") for d in docs]
        dup = set()
        if r.unique:
            seen = {}
            for v in vals:
                if v.strip():
                    seen[v] = seen.get(v, 0) + 1
            dup = {v for v, n in seen.items() if n > 1}
        for i, v in enumerate(vals):
            for k in check_value(r, v):
                out[i].append((f, k))
            if v in dup:
                out[i].append((f, "unique"))
    return out


def rule_key(k):
    """규칙 이름 (pattern_long은 pattern 규칙의 위반)"""
    return "pattern" if k == "pattern_long" else k


def reason(f, k):
    """검토 표시 사유: '기계 검수 · 칸 표시명 · 위반 설명' — 값은 싣지 않는다"""
    r = f["validate"]
    n = r.length if k == "length" else r.max_length if k == "max_length" else None
    return f"{MACHINE} · {f['label']} · {TEXT[k].format(n=n)}"
