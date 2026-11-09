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

정규식 ReDoS 막기 (Python re에는 시간 제한이 없고, 새 의존성(re2 등)은 들이지 않는다) — 판정은 막는 쪽(fail-closed)
  1. 시작할 때 거부 (compile_pattern → _seq): 패턴 길이 PATTERN_MAX 초과, 그리고 되짚기가 길이에 따라 폭증하는 모양
     - 길이가 바뀌는 반복(*, +, {m,n}) 안에 다시 길이가 바뀌는 반복이나 선택(|)        예 (a+)+, (a|ab)*, (?>(a+)+b)
     - 고정 횟수 반복 {n>1} 안에 길이가 바뀌는 반복이나 선택                        예 (\\w*){30}, (a|.){60}
     - 글자 집합이 겹치는 가변 반복 둘이, 앞 반복의 집합에 들지 않는 고정 글자 없이 이어짐 예 a*a*b, \\w+\\w+, .*x.*
       (선택적 부분(?, {0,1})은 건너뛸 수 있다고 보고 앞뒤를 이어 본다)
     - 역참조·조건 그룹·앞뒤 보기(lookaround)·인라인 플래그((?i) 등)·이 판정이 모르는 구문은 모두 거부
     겹침 판정에서 \\d·\\w·\\s(와 부정)는 ASCII 밖 글자 전체를 포함한다고 넉넉히 본다 → [가-힣]+\\d+는 거부된다. [0-9]로 쓴다.
     받는 예: [A-Z0-9]{23}, [A-Z]{2}-[0-9]+, \\d{4}-\\d{2}-\\d{2}, (\\d{2}-){3}\\d{2}, [가-힣]+(시|군|구), a?b*c+
  2. 검사할 때: 값이 VALUE_MAX자를 넘으면 정규식을 돌리지 않고 위반('형식 검사 길이 초과')으로 본다.
     1을 통과한 패턴은 한 자리를 두고 다툴 수 있는 가변 반복이 하나뿐이라 되짚기가 대략 값 길이에 비례한다 — 엄밀한 증명은
     아니고, 허용 패턴의 상한 길이 최악 입력 시간을 시험(tests/test_field_rules.py)으로 잰다.

스키마 검사(시작할 때 멈춤): 모르는 규칙 키, 잘못된 값 형식, 깨진 정규식·위험한 정규식, length > max_length,
코드표 파일 없음·경로 이탈·코드가 길이·정규식과 안 맞음, 사람이 고칠 수 없는 칸(숨김·고정값·철 단위)의 규칙.
앞뒤 빈칸: unique·code_table은 앞뒤 빈칸을 뗀 값으로 비교한다(대소문자는 그대로). length·pattern은 값 그대로.
이 모듈은 app의 다른 모듈을 불러오지 않는다 (app/fields.py가 스키마를 읽을 때 부른다).
"""
import re
from collections import Counter
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
MACHINE = "기계 검수"  # 검토 표시 사유 머리 (사람이 읽는 글). 기계 검수 사유인지는 머리가 아니라 machine_reasons로 가린다

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


MAXCH = 0x10FFFF
_ALL = ((0, MAXCH),)
_NONASCII = (128, MAXCH)
# 범주 글자의 ASCII 부분 (re의 유니코드 \d·\w·\s가 받는 ASCII 글자 — \s는 \x1c-\x1f도 받는다)
_CAT_ASCII = {"DIGIT": ((48, 57),), "SPACE": ((9, 13), (28, 32)), "WORD": ((48, 57), (65, 90), (95, 95), (97, 122))}
_REPEATS = {_sre.MAX_REPEAT, _sre.MIN_REPEAT} | ({_sre.POSSESSIVE_REPEAT} if hasattr(_sre, "POSSESSIVE_REPEAT") else set())
_CHAR_OPS = {_sre.LITERAL, _sre.NOT_LITERAL, _sre.IN, _sre.ANY}


def _norm(iv):
    out = []
    for lo, hi in sorted(iv):
        if out and lo <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return tuple(out)


def _complement(iv):
    out, at = [], 0
    for lo, hi in _norm(iv):
        if lo > at:
            out.append((at, lo - 1))
        at = hi + 1
    if at <= MAXCH:
        out.append((at, MAXCH))
    return tuple(out)


def _overlap(a, b):
    return any(lo1 <= hi2 and lo2 <= hi1 for lo1, hi1 in a for lo2, hi2 in b)


def _category(cat, over):
    """범주 글자 집합. over=True면 넉넉히(ASCII 밖 전부 포함), False면 좁게(ASCII 부분만) — 부정 클래스 안에서 뒤집을 때 쓴다"""
    name = str(cat).removeprefix("CATEGORY_")
    neg = name.startswith("NOT_")
    base = _CAT_ASCII.get(name.removeprefix("NOT_"))
    if base is None:
        raise RuleError(f"validate.pattern: 모르는 글자 범주 {cat}")
    if neg:  # \D의 넉넉한 집합 = \d의 좁은 집합의 여집합
        return _complement(base + ((_NONASCII,) if not over else ()))
    return _norm(base + ((_NONASCII,) if over else ()))


def _chars(op, av):
    """글자 하나 자리의 집합(넉넉하게)"""
    if op == _sre.LITERAL:
        return ((av, av),)
    if op == _sre.NOT_LITERAL:
        return _complement(((av, av),))
    if op == _sre.ANY:
        return _ALL
    neg = bool(av) and av[0][0] == _sre.NEGATE
    iv = []
    for o, a in av[1:] if neg else av:
        if o == _sre.LITERAL:
            iv.append((a, a))
        elif o == _sre.RANGE:
            iv.append(a)
        elif o == _sre.CATEGORY:
            iv.extend(_category(a, over=not neg))
        else:
            raise RuleError(f"validate.pattern: 문자 클래스 안의 모르는 구문 {o}")
    return _complement(iv) if neg else _norm(iv)


def _reject(why):
    raise RuleError(f"validate.pattern: {why} — 되짚기가 폭증할 수 있어 받지 않는다 (문자 클래스·고정 횟수로 쓴다)")


def _has_var(items):
    """가변 반복이나 선택이 들어 있나"""
    for op, av in items:
        if op in _REPEATS:
            if av[0] != av[1] or _has_var(av[2]):
                return True
        elif op == _sre.BRANCH:
            return True
        elif op == _sre.SUBPATTERN:
            if _has_var(av[-1]):
                return True
        elif op == _sre.ATOMIC_GROUP:
            if _has_var(av):
                return True
    return False


def _all_chars(items):
    """부분 패턴이 먹을 수 있는 글자 전체(넉넉하게)"""
    iv = []
    for op, av in items:
        if op in _CHAR_OPS:
            iv.extend(_chars(op, av))
        elif op in _REPEATS:
            iv.extend(_all_chars(av[2]))
        elif op == _sre.SUBPATTERN:
            iv.extend(_all_chars(av[-1]))
        elif op == _sre.ATOMIC_GROUP:
            iv.extend(_all_chars(av))
        elif op == _sre.BRANCH:
            for b in av[1]:
                iv.extend(_all_chars(b))
    return _norm(iv)


def _open(pending, s, what):
    if any(_overlap(s, p) for p in pending):
        _reject(f"글자 집합이 겹치는 가변 반복이 이어짐 ({what})")
    pending.append(s)


def _seq(items, pending):
    """차례로 이어지는 부분 패턴 검사. pending = 아직 끝나지 않은(뒤 글자와 다툴 수 있는) 가변 반복의 글자 집합 목록 — 고친다"""
    for op, av in items:
        if op in _CHAR_OPS:  # 고정 글자 하나: 이 글자를 먹을 수 없는 가변 반복은 여기서 끝난다
            c = _chars(op, av)
            pending[:] = [p for p in pending if _overlap(c, p)]
        elif op == _sre.AT:
            continue  # ^ $ \b 등 — 글자를 먹지 않는다
        elif op in _REPEATS:
            lo, hi, sub = av
            sub = list(sub)
            inner_var = _has_var(sub)
            if lo != hi:  # 가변 반복
                if hi > 1 and inner_var:
                    _reject("가변 반복 안에 다시 가변 반복·선택")
                if hi > 1 or not inner_var:
                    _seq(sub, [])  # 안쪽 구문 확인 (안쪽에 가변 부분 없음)
                    _open(pending, _all_chars(sub), "반복")
                else:  # 선택적 부분(?, {0,1}) 안에 가변 부분 — 거치는 길과 건너뛰는 길을 함께 본다
                    taken = list(pending)
                    _seq(sub, taken)
                    pending.extend(p for p in taken if p not in pending)
            elif lo > 1 and inner_var:
                _reject(f"고정 횟수 반복 {{{lo}}} 안에 가변 반복·선택")
            elif lo >= 1:
                _seq(sub, pending)  # 고정 횟수 — 한 번 이어 본 것과 같다
        elif op == _sre.SUBPATTERN:
            if av[1] or av[2]:
                raise RuleError("validate.pattern: 인라인 플래그((?i) 등)는 쓰지 않는다")
            _seq(list(av[-1]), pending)
        elif op == _sre.ATOMIC_GROUP:
            _seq(list(av), pending)
        elif op == _sre.BRANCH:
            branches = [list(b) for b in av[1]]
            var = False
            for b in branches:
                _seq(b, [])  # 갈래마다 따로 확인
                var = var or _has_var(b)
            s = _all_chars([(op, av)])
            if var:
                _open(pending, s, "선택")
            else:
                pending[:] = [p for p in pending if _overlap(s, p)]
        elif op in (_sre.GROUPREF, _sre.GROUPREF_EXISTS):
            raise RuleError("validate.pattern: 역참조·조건 그룹은 쓰지 않는다")
        elif op in (_sre.ASSERT, _sre.ASSERT_NOT):
            raise RuleError("validate.pattern: 앞뒤 보기(lookaround)는 쓰지 않는다")
        else:
            raise RuleError(f"validate.pattern: 모르는 구문 {op}")


def compile_pattern(p):
    """정규식 컴파일 + 위험한 모양 거부 (모듈 머리말)"""
    if not isinstance(p, str) or not p:
        raise RuleError("validate.pattern은 글자")
    if len(p) > PATTERN_MAX:
        raise RuleError(f"validate.pattern은 {PATTERN_MAX}자까지")
    try:
        rx = re.compile(p)
        tree = _sre.parse(p)
    except re.error as e:
        raise RuleError(f"validate.pattern 정규식 오류: {e}") from None
    if tree.state.flags & ~re.UNICODE:
        raise RuleError("validate.pattern: 인라인 플래그((?i) 등)는 쓰지 않는다")
    _seq(list(tree), [])
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
    dup = [c for c, n in Counter(codes).items() if n > 1]
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
    if r.codes is not None and v.strip() not in r.codes and "code_table" not in skip:  # 앞뒤 빈칸은 떼고 (대소문자는 그대로)
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
        if r.unique:  # 앞뒤 빈칸은 떼고 비교 (대소문자는 그대로)
            seen = Counter(v.strip() for v in vals if v.strip())
            dup = {v for v, n in seen.items() if n > 1}
        for i, v in enumerate(vals):
            for k in check_value(r, v):
                out[i].append((f, k))
            if v.strip() in dup:
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


def possible(f):
    """칸 f의 규칙이 낼 수 있는 위반 이름"""
    r = f["validate"]
    return ([k for k, on in (("required", r.required), ("length", r.length), ("max_length", r.max_length),
                             ("pattern", r.pattern), ("pattern_long", r.pattern), ("code_table", r.codes),
                             ("unique", r.unique)) if on])


def machine_reasons(schema):
    """이 스키마의 칸 규칙이 낼 수 있는 검토 표시 사유 전부 (정확한 글자). 기계 검수 표시인지는 이 집합으로 가린다 —
    다시 계산(flags.refresh)·미검토 판정(export)·화면(/schema checks)이 같이 쓴다. 글자 머리로 고르지 않으므로 다른 사유를
    잘못 지우지 않는다. 규칙 없는 스키마는 빈 집합"""
    return {reason(f, k) for f in ruled(schema) for k in possible(f)}
