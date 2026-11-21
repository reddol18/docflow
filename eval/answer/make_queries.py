"""합성 철 답변 평가 질의 세트 만들기 (https://github.com/reddol18/docflow/issues/21 1단계).

합성 철(eval/synth/bundle.py 출력)의 생성기 정답 — 쪽 라벨(그린 글자 순서대로의 단어), gt.csv(4칸), meta.json(쪽 종류) —
에서 문서별 사실표를 만들고, 그 사실로 질문·참고 답·근거 문서를 짓는다. 추측해 쓴 질문은 없다.
  사실표   문서(시작 쪽)마다 도시·부서·문서번호·시행일자·수신·제목·신청 건·근거 법령·신청인(회사·대표)·위치·처리결과,
           이어지는 쪽(cont)·붙임(attach) 여부, 운영 문서 나누기(work/platform)에서 따로 나뉜 문서인지(indexed)
  질의     답 있음: 유형별 틀(처리결과·신청인·시행일자·부서·위치·수신·요약·여러 문서)에 사실을 넣고, 질문의 단서가 사실표 전체에서
           한 문서(여러 문서 질의는 정해진 묶음)만 가리키는지 확인한다. 근거 문서는 운영에서 따로 나뉜 문서만 고른다
           (/ask는 문서 첫 쪽 OCR만 근거로 넘기므로, 처리결과·요약은 본문이 다음 쪽으로 넘어가지 않은 문서만).
           답 없음: 그럴듯하지만 사실표에 없는 단서(없는 문서번호·회사·건 종류·도시·연도, 있는 회사의 없는 대표)
  출력     JSONL 한 줄에 id·type·question·answer·source_docs('철/doc_id' — /ask 근거 표시와 같은 꼴)·answerable
같은 철·같은 --seed면 같은 질의 세트가 나온다. 합성 철(meta.json synthetic=true, 이름 SYNTH…)만 받는다.

실행 (doc_platform 폴더, 앱 의존성 없음 — 표준 라이브러리만):
  python -I -X utf8 eval/answer/make_queries.py <합성 철 폴더> <합성 work 폴더> --out eval/data/synth_answer_queries.jsonl
"""
import argparse
import csv
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path

SYNTH_PREFIX = "SYNTH"
QUERY_ID = re.compile(r"a\d{2,3}")
QUERY_KEYS = {"id", "type", "question", "answer", "source_docs", "answerable"}
NO_ANSWER = "문서 목록에 없어 답할 수 없음"
# 유형별 답 있는 질의 수 (합 32) + 답 없음 10 = 42
PLAN = {"처리결과": 5, "신청인": 5, "시행일자": 4, "부서": 4, "위치": 3, "수신": 3, "요약": 4, "여러 문서": 4}
N_UNANSWERABLE = 10

# 그린 글자에서 사실을 뽑는 규칙 (eval/synth/render.py body_lines·sign_block·first_page가 그리는 꼴)
RX = {
    "case": re.compile(r"제출한 (.+?) 신청에 대하여"),
    "law": re.compile(r"신청에 대하여 (.+?) 및 관계 규정"),
    "applicant": re.compile(r"신청인 : 주식회사 (\S+) \(대표 (\S+)\)"),
    "addr": re.compile(r"위 치 : (\S+ \S+ \d+)"),
    "result": re.compile(r"처리결과 : (.+?) 3\. "),
    "dept": re.compile(r"(\S+과)-\d+ \( \d{4}\."),
}


class NotSynthetic(SystemExit):
    """합성 철이 아닌 입력"""


def page_words(label):
    """쪽 라벨(AI Hub 88 꼴) → 그린 순서대로 단어를 이은 글"""
    return " ".join(a["annotation.text"] for a in label["annotations"])


def read_bundle(d):
    """합성 철 폴더 → (meta, {쪽: 글}, gt 시작 쪽 줄 {쪽: 줄}). 합성 철이 아니면 NotSynthetic"""
    d = Path(d)
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    if meta.get("synthetic") is not True or meta.get("vol") != d.name or not d.name.startswith(SYNTH_PREFIX):
        raise NotSynthetic(f"합성 철이 아니다 (meta.json synthetic·vol, 이름 {SYNTH_PREFIX}…): {d}")
    texts = {p["page"]: page_words(json.loads((d / f"{p['page']}.json").read_text(encoding="utf-8"))) for p in meta["pages"]}
    with (d / "gt.csv").open(encoding="utf-8-sig", newline="") as f:
        gt = {r["page"]: r for r in csv.DictReader(f) if r["is_start"] == "Y"}
    return meta, texts, gt


def group_pages(meta):
    """meta.pages → [(시작 쪽, [그 문서의 쪽 종류...])] — first 쪽에서 새 문서"""
    docs = []
    for p in meta["pages"]:
        if p["kind"] == "first":
            docs.append((p["page"], [p]))
        elif docs:
            docs[-1][1].append(p)
    return docs


def _one(rx, text, what, page):
    m = rx.search(text)
    if not m:
        raise ValueError(f"{page}: '{what}'를 그린 글자에서 찾지 못함")
    return m.groups() if rx.groups > 1 else m.group(1)


def doc_facts(meta, texts, gt, indexed=()):
    """합성 철 하나 → 문서별 사실 [dict]. indexed: 운영 나누기에서 따로 나뉜 문서 id (work/platform)"""
    vol = meta["vol"]
    out = []
    for start, pages in group_pages(meta):
        kinds = [p["kind"] for p in pages]
        first = texts[start]
        body = " ".join(texts[p["page"]] for p in pages if p["kind"] in ("first", "cont"))
        row = gt[start]
        company, rep = _one(RX["applicant"], first, "신청인", start)
        y, m, dd = (int(x) for x in row["date"].split("-"))
        out.append({
            "key": f"{vol}/{start}", "vol": vol, "doc_id": start, "city": first.split(" ", 1)[0], "year": y,
            "date": row["date"], "date_ko": f"{y}년 {m}월 {dd}일", "docno": row["docno"], "recipient": row["recipient"],
            "title": row["title"], "case": _one(RX["case"], first, "신청 건", start), "law": _one(RX["law"], first, "법령", start),
            "company": company, "rep": rep, "addr": _one(RX["addr"], first, "위치", start),
            "result": _one(RX["result"], body, "처리결과", start).strip(), "dept": _one(RX["dept"], body, "부서", start),
            "cont": "cont" in kinds, "attach": "attach" in kinds, "indexed": start in set(indexed),
        })
    return out


def load_facts(bundles_dir, work_dir):
    """합성 철 폴더 전부 + 합성 work(to_work.py 출력)의 문서 나누기 → 사실표"""
    facts = []
    for d in sorted(p for p in Path(bundles_dir).iterdir() if (p / "meta.json").exists()):
        meta, texts, gt = read_bundle(d)
        plat = Path(work_dir) / "platform" / f"{meta['vol']}.json"
        indexed = [x["doc_id"] for x in json.loads(plat.read_text(encoding="utf-8"))] if plat.exists() else []
        facts += doc_facts(meta, texts, gt, indexed)
    if not facts:
        raise SystemExit(f"합성 철이 없다: {bundles_dir}")
    return facts


def matches(facts, **cond):
    return [f for f in facts if all(f[k] == v for k, v in cond.items())]


def unique_by(facts, f, keys):
    """f를 keys 값으로 가리키면 사실표 전체(나뉘지 않은 문서 포함)에서 f 하나뿐인가"""
    return len(matches(facts, **{k: f[k] for k in keys})) == 1


def josa(word, with_final, without):
    """받침 따라 조사 (한글 끝 글자만 본다. 아니면 with_final)"""
    ch = word[-1]
    if "가" <= ch <= "힣":
        return with_final if (ord(ch) - 0xAC00) % 28 else without
    return with_final


def summary_of(f):
    return (f"{f['city']} {f['dept']}{josa(f['dept'], '이', '가')} 주식회사 {f['company']}(대표 {f['rep']})의 {f['case']} 신청"
            f"(위치 {f['addr']})을 {f['law']}에 따라 검토해 '{f['result']}'로 처리했음을 알린 문서 ({f['date_ko']} 시행)")


# 유형 → (고를 수 있는 문서 조건, 단서 칸, 질문, 답)
SINGLE = {
    "처리결과": (lambda f: not f["cont"], ("docno",),
                 lambda f: f"문서번호 {f['docno']} 문서에서 {f['case']} 신청의 처리결과는 무엇인가?", lambda f: f["result"]),
    "신청인": (lambda f: True, ("city", "date", "case"),
               lambda f: f"{f['city']}에서 {f['date_ko']}에 시행한 {f['case']} 관련 문서의 신청인 회사와 대표자는 누구인가?",
               lambda f: f"주식회사 {f['company']}, 대표 {f['rep']}"),
    "시행일자": (lambda f: True, ("company", "rep", "case"),
                 lambda f: f"주식회사 {f['company']}(대표 {f['rep']})의 {f['case']} 신청을 처리한 문서는 언제 시행되었나?",
                 lambda f: f["date_ko"]),
    "부서": (lambda f: True, ("city", "company", "case", "year"),
             lambda f: f"{f['city']}{josa(f['city'], '이', '가')} {f['year']}년에 주식회사 {f['company']}의 {f['case']} 신청을 "
                       "처리한 문서는 어느 부서에서 냈나?",
             lambda f: f["dept"]),
    "위치": (lambda f: True, ("company", "rep", "case"),
             lambda f: f"주식회사 {f['company']} 대표 {f['rep']}{josa(f['rep'], '이', '가')} 낸 {f['case']} 신청의 위치(주소)는 어디인가?",
             lambda f: f["addr"]),
    "수신": (lambda f: True, ("docno",), lambda f: f"문서번호 {f['docno']} 문서의 수신자는 누구인가?", lambda f: f["recipient"]),
    "요약": (lambda f: not f["cont"], ("docno",), lambda f: f"문서번호 {f['docno']} 문서의 내용을 요약해 줘.", summary_of),
}


def multi_groups(facts):
    """여러 문서 질의 후보: (단서, 묶음) — 묶음이 2~3건이고 모두 운영에서 따로 나뉜 문서"""
    out = []
    for keys in (("company", "case"), ("city", "year", "case")):
        groups = {}
        for f in facts:
            groups.setdefault(tuple(f[k] for k in keys), []).append(f)
        for val, g in sorted(groups.items()):
            if 2 <= len(g) <= 3 and all(f["indexed"] for f in g):
                out.append((dict(zip(keys, val)), g))
    return out


def multi_question(cond, g):
    g = sorted(g, key=lambda f: f["date"])
    if "company" in cond:
        q = f"주식회사 {cond['company']}의 {cond['case']} 신청을 처리한 문서를 모두 찾아 각각의 시행일자를 알려 줘."
        a = f"{len(g)}건: " + ", ".join(f"{f['date_ko']}({f['docno']})" for f in g)
    else:
        q = (f"{cond['city']}{josa(cond['city'], '이', '가')} {cond['year']}년에 시행한 {cond['case']} 관련 문서는 몇 건이고, "
             "각각 신청인은 누구인가?")
        a = f"{len(g)}건: " + ", ".join(f"주식회사 {f['company']}(대표 {f['rep']}, {f['docno']})" for f in g)
    return q, a, [f["key"] for f in g]


# 답 없음 질의 재료 — 사실표에 없는지 build에서 확인한다
ABSENT_COMPANY = ["하늘토건", "푸른산업", "동녘상사"]
ABSENT_CASE = [("개발행위허가", "토지 형질변경"), ("농지전용허가", "농지 전용"), ("하천점용허가", "하천 점용")]
ABSENT_CITY = ["다온시", "별빛군"]
ABSENT_REP = ["홍길동", "남궁민수"]


def unanswerable(facts, r):
    """그럴듯하지만 사실표에 없는 단서로 묻는 질의 N_UNANSWERABLE개 → [(유형, 질문)]"""
    docnos = {f["docno"] for f in facts}
    companies = {f["company"] for f in facts}
    cases = {f["case"] for f in facts}
    cities = sorted({f["city"] for f in facts})
    years = {f["year"] for f in facts}
    reps = {f["rep"] for f in facts}
    for name in ABSENT_COMPANY + [c for c, _ in ABSENT_CASE] + ABSENT_CITY + ABSENT_REP:
        if name in companies | cases | set(cities) | reps:
            raise ValueError(f"답 없음 재료 {name!r}가 사실표에 있다")
    out = []
    # 없는 문서번호 2: 있는 부서 약칭·분류번호 꼴, 일련번호는 생성 범위(100~2999) 안에서 없는 값
    f0 = r.choice(facts)
    prefix = f0["docno"].rsplit("-", 1)[0]
    serials = [s for s in range(100, 3000) if f"{prefix}-{s}" not in docnos]
    for s in r.sample(serials, 2):
        out.append(("없는 문서번호", f"문서번호 {prefix}-{s} 문서의 처리결과는 무엇인가?"))
    case = r.choice(sorted(cases))
    for c in ABSENT_COMPANY[:2]:
        out.append(("없는 회사", f"주식회사 {c}의 {case} 신청은 어떻게 처리되었나?"))
    for c, what in ABSENT_CASE[:2]:
        out.append(("없는 건 종류", f"{r.choice(cities)}에서 {c}({what}) 신청을 처리한 문서의 시행일자는 언제인가?"))
    out.append(("없는 도시", f"{ABSENT_CITY[0]}에서 {case} 신청을 처리한 문서는 어느 부서에서 냈나?"))
    y = next(y for y in (1990, 1999, 1989) if y not in years)
    out.append(("없는 연도", f"{y}년에 시행한 {case} 관련 문서의 신청인은 누구인가?"))
    comp = r.choice(sorted(companies))
    for rep in ABSENT_REP[:2]:
        if matches(facts, company=comp, rep=rep):
            raise ValueError("답 없음 재료가 사실표에 있다")
        out.append(("없는 대표", f"주식회사 {comp} 대표 {rep}{josa(rep, '이', '가')} 낸 신청의 처리결과는 무엇인가?"))
    assert len(out) == N_UNANSWERABLE
    return out


def build(facts, seed=21):
    """사실표 → 질의 목록 (답 있음 PLAN, 답 없음 N_UNANSWERABLE). 한 문서는 답 있는 질의 하나에만 쓴다"""
    r = random.Random(seed)
    used, qs = set(), []
    for typ, n in PLAN.items():
        if typ == "여러 문서":
            cands = [(c, g) for c, g in multi_groups(facts) if not any(f["key"] in used for f in g)]
            # 회사+건 묶음과 도시+연도+건 묶음을 반반
            pick = []
            for kind in ("company", "city"):
                pool = [x for x in cands if kind in x[0]]
                r.shuffle(pool)
                for c, g in pool:
                    if len(pick) < n and sum(kind in p[0] for p in pick) < n // 2 \
                            and not any(f["key"] in used for f in g):
                        pick.append((c, g))
                        used.update(f["key"] for f in g)
            if len(pick) < n:
                raise ValueError(f"여러 문서 질의 묶음이 모자람 ({len(pick)}/{n})")
            for c, g in pick:
                q, a, src = multi_question(c, g)
                qs.append({"type": typ, "question": q, "answer": a, "source_docs": src, "answerable": True})
            continue
        ok, keys, qf, af = SINGLE[typ]
        pool = [f for f in facts if f["indexed"] and ok(f) and f["key"] not in used and unique_by(facts, f, keys)]
        if len(pool) < n:
            raise ValueError(f"{typ} 질의 후보가 모자람 ({len(pool)}/{n})")
        for f in r.sample(pool, n):
            used.add(f["key"])
            qs.append({"type": typ, "question": qf(f), "answer": af(f), "source_docs": [f["key"]], "answerable": True})
    for typ, q in unanswerable(facts, r):
        qs.append({"type": f"답 없음({typ})", "question": q, "answer": NO_ANSWER, "source_docs": [], "answerable": False})
    return [{"id": f"a{i:02d}", **q} for i, q in enumerate(qs, 1)]


def load_queries(path):
    """합성 답변 질의 파일만 받는다: 줄마다 칸이 QUERY_KEYS와 같고 id 'a숫자', 근거 문서가 합성 철(SYNTH…).
    실제 자료 질의 세트(eval/data/search_queries.jsonl: q·relevant·must_cite 칸)는 여기서 멈춘다 — 외부로 보내지 않게"""
    qs = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            q = json.loads(line)
        except ValueError:
            raise NotSynthetic(f"질의 파일 {n}번째 줄이 JSON이 아니다") from None
        if (not isinstance(q, dict) or set(q) != QUERY_KEYS or not QUERY_ID.fullmatch(str(q["id"]))
                or not isinstance(q["question"], str) or not isinstance(q["source_docs"], list)
                or not all(isinstance(s, str) and s.startswith(SYNTH_PREFIX) for s in q["source_docs"])
                or q["answerable"] is not bool(q["source_docs"])):
            raise NotSynthetic(f"합성 답변 질의 형식이 아닌 줄 {n} — 외부 모델에는 합성 질의(synth_answer_queries.jsonl 꼴)만 보낸다")
        qs.append(q)
    if not qs or len({q["id"] for q in qs}) != len(qs):
        raise NotSynthetic("질의가 없거나 id가 겹친다")
    return qs


def stats(qs):
    return {"total": len(qs), "answerable": sum(q["answerable"] for q in qs),
            "unanswerable": sum(not q["answerable"] for q in qs),
            "types": dict(Counter(q["type"].split("(")[0] for q in qs))}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("bundles", help="bundle.py 출력 폴더 (철 폴더들이 든 곳)")
    ap.add_argument("work", help="to_work.py 출력 work 폴더 (문서 나누기 결과 platform/)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=21)
    ap.add_argument("--facts", default="", help="사실표를 JSON으로도 쓴다 (저장소 밖)")
    a = ap.parse_args(argv)
    facts = load_facts(a.bundles, a.work)
    qs = build(facts, a.seed)
    Path(a.out).write_text("".join(json.dumps(q, ensure_ascii=False) + "\n" for q in qs), encoding="utf-8")
    if a.facts:
        Path(a.facts).write_text(json.dumps(facts, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"문서 {len(facts)} (따로 나뉜 문서 {sum(f['indexed'] for f in facts)}), 질의 {json.dumps(stats(qs), ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
