"""칸 이름을 바꾼 김해 매핑으로 eval/schema_replay.py를 돌려, 칸 값·검토 표시가 김해 매핑과 같은지 본다
(https://github.com/reddol18/docflow/issues/72 완료 조건 3). LLM 호출 없음.

검토 표시·손글씨 칸·수신 보정은 칸 이름이 아니라 rule 이름으로 칸을 찾아야 한다 — 칸 이름만 바꾼 매핑에서
결과가 하나라도 달라지면 어딘가 아직 김해 칸 이름에 묶여 있다는 뜻이다.

  1) 김해 매핑(gimhae.yaml) 그대로 schema_replay.py → A
  2) 칸 이름을 RENAME으로 바꾼 매핑(임시 폴더, 기본 스키마 isad_g.yaml 사본과 함께)을 FIELD_SCHEMA로 주고,
     입력 JSON의 칸 키(fields·b)도 같은 이름으로 바꿔 schema_replay.py → B
  3) B의 칸·표시 키를 김해 이름으로 되돌려 A와 문서마다 비교 (fields·flags 전부)

실행 (doc_platform 폴더, 시험 환경):
  uv run --no-project --with-requirements requirements-test.txt python -I eval/rename_replay.py ../work/platform/*.json
입력은 /volumes/{vol}/documents JSON (실제 자료 — 저장소에 넣지 않는다). 중간 파일은 임시 폴더에만 쓴다.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = ROOT / "app" / "field_schemas"
RENAME = {"docno": "doc_number", "serial": "serial_no", "date": "issued", "recipient": "addressee", "title": "subject"}
BACK = {v: k for k, v in RENAME.items()}


def renamed_schema(raw, m=RENAME):
    """김해 매핑 dict → 칸 이름만 바꾼 매핑 (rule·isad·지시문은 그대로). 칸을 가리키는 키(flag_key·derive_from·search)도 바꾼다"""
    s = json.loads(json.dumps(raw))
    s["name"] += " (칸 이름 바꿈 시험)"
    for f in s["fields"]:
        for k in ("name", "flag_key", "derive_from"):
            if k in f:
                f[k] = m.get(f[k], f[k])
    se = s.get("search") or {}
    for k, v in se.items():
        se[k] = [m.get(x, x) for x in v] if isinstance(v, list) else m.get(v, v)
    return s


def rename_keys(d, m):
    return {m.get(k, k): v for k, v in d.items()}


def rename_doc(d, m):
    """문서 dict의 칸 키(fields·b·flags)를 바꾼다. hw(잘라낸 칸)·info는 칸 이름이 아니라 그대로"""
    out = dict(d)
    for k in ("fields", "b", "flags"):
        if isinstance(d.get(k), dict):
            out[k] = rename_keys(d[k], m)
    return out


def write_schema(tmp):
    """임시 폴더에 기본 스키마 사본 + 바꾼 매핑 → 매핑 경로"""
    shutil.copy(SCHEMAS / "isad_g.yaml", tmp / "isad_g.yaml")
    raw = yaml.safe_load((SCHEMAS / "gimhae.yaml").read_text(encoding="utf-8"))
    p = tmp / "gimhae_renamed.yaml"
    p.write_text(yaml.safe_dump(renamed_schema(raw), allow_unicode=True, sort_keys=False), encoding="utf-8")
    return p


def run_replay(out, paths, schema=None):
    env = {**os.environ, "PYTHONUTF8": "1"}
    env.pop("FIELD_SCHEMA", None)
    if schema:
        env["FIELD_SCHEMA"] = str(schema)
    subprocess.run([sys.executable, "-I", str(ROOT / "eval" / "schema_replay.py"), str(ROOT), str(out), *map(str, paths)],
                   check=True, env=env)


def compare(a_docs, b_docs):
    """→ (문서 수, 칸 값이 다른 문서, 표시가 다른 문서, 표시된 문서 수)"""
    fields_diff, flags_diff = [], []
    for a, b in zip(a_docs, b_docs, strict=True):
        b = rename_doc(b, BACK)
        if a["fields"] != b["fields"]:
            fields_diff.append(a["doc_id"])
        if a["flags"] != b["flags"]:
            flags_diff.append(a["doc_id"])
    return len(a_docs), fields_diff, flags_diff, sum(bool(a["flags"]) for a in a_docs)


def main():
    sys.stdout.reconfigure(encoding="utf-8")  # python -I는 PYTHONUTF8을 보지 않는다 (Windows 콘솔 cp949에서 깨짐 방지)
    paths = [Path(p) for p in sys.argv[1:]]
    if not paths:
        sys.exit(__doc__)
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        schema = write_schema(tmp)
        (tmp / "in").mkdir()
        renamed_in = []
        for p in paths:
            q = tmp / "in" / p.name
            q.write_text(json.dumps([rename_doc(d, RENAME) for d in json.loads(p.read_text(encoding="utf-8"))],
                                    ensure_ascii=False), encoding="utf-8")
            renamed_in.append(q)
        run_replay(tmp / "a", paths)
        run_replay(tmp / "b", renamed_in, schema)
        total = flagged = 0
        bad = False
        for p in paths:
            a = json.loads((tmp / "a" / p.name).read_text(encoding="utf-8"))
            b = json.loads((tmp / "b" / p.name).read_text(encoding="utf-8"))
            n, fd, gd, fl = compare(a, b)
            total, flagged = total + n, flagged + fl
            bad |= bool(fd or gd)
            print(f"{p.name}: {n}건, 표시 {fl}건 | 칸 이름 바꿈과 다른 문서: 칸 값 {len(fd)} · 표시 {len(gd)}")
        print(f"합계 {total}건, 표시 {flagged}건: {'다름 있음' if bad else '칸 값·표시 모두 같음'}")
        sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
