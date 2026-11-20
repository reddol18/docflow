"""임베딩 모델 비교 — 합성 철 (https://github.com/reddol18/docflow/issues/80 조건 5).

합성 철 문서를 지금 임베딩(text-embedding-3-small, 1536차원)과 후보 임베딩으로 각각 색인해, 지어낸 검색어
(eval/data/synth_search_queries.jsonl)의 /search 상위 5개가 얼마나 겹치는지 잰다 (2026-11-07 결정의 잣대).
Qdrant(1536 고정, app/store.py)에 넣지 않고 메모리 안에서 같은 순위 계산을 한다 — 후보 차원(bge-m3 1024 등)이 달라도
운영 모음을 건드리지 않는다.
  문서 글   app/graph.doc_text와 같다: 스키마 search.embed 칸('표시명: 값' 줄) + '본문: ' + 첫 쪽 OCR 앞 60줄.
            칸 값은 합성 정답(gt) — 운영에서는 추출 값이 들어간다 (모든 후보에 같은 글).
  /search   app/main.ranked와 같은 셈: 벡터(코사인, 연도 칸 거르기) 30개 + 낱말(store.keyword_search — search_text에
            질의 낱말이 든 수, 대소문자 무시) 30개 → RRF(1/(60+순위)). 질의 낱말은 main.query_words와 같다.
  겹침     질의마다 |기준 상위 5 ∩ 후보 상위 5| / 5 의 평균 (/search RRF와 벡터만 각각). 정답(검색어 match 규칙을 합성 정답에
            맞춘 문서)이 있는 질의는 recall@5도 낸다.

실행 (doc_platform 폴더, 합성 work의 평가 가상환경):
  $py = '..\\work_synth\\w47\\promptfoo\\.venv\\Scripts\\python.exe'
  & $py -I -X utf8 eval/embed_compare.py --openai --model text-embedding-3-small --label te3small --work ..\\work_synth\\w47
  & $py -I -X utf8 eval/embed_compare.py --base-url http://127.0.0.1:11434/v1 --model bge-m3 --label local_bge_m3 `
        --work ..\\work_synth\\w47 --baseline ..\\work_synth\\w47\\embed\\te3small.json
결과: <work>/embed/<label>.json (질의별 순위·차원·토큰·비용). 합성 철만 받는다 (eval/compare_common.py).
"""
import argparse
import json
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # python -I: eval 폴더

import compare_common as cc  # noqa: E402

from app import fields  # noqa: E402

QUERIES = Path(__file__).resolve().parent / "data" / "synth_search_queries.jsonl"
SCHEMA = fields.load(fields.DIR / "gimhae.yaml")
CANDIDATES = 30  # app/main.CANDIDATES
BATCH = 64  # app/graph.index 묶음
GT_FIELDS = ("docno", "date", "recipient", "title")


def query_words(q):
    """app/main.query_words와 같다: 두 글자 이상, 연도(1997·1997년)는 뺀다"""
    return [w for w in re.split(r"\s+", q) if len(w) >= 2 and not re.fullmatch(r"\d{4}년?", w)]


def rrf(*lists):
    """app/main.rrf와 같다: {(vol, doc_id): Σ 1/(60+순위)} (순위는 1부터)"""
    score = {}
    for lst in lists:
        for rank, k in enumerate(lst):
            score[k] = score.get(k, 0) + 1 / (60 + rank + 1)
    return score


def load_docs(work, vols):
    """[{key, text, search_text, year, values}] — 운영 split 문서. 칸 값은 그 문서 시작 쪽의 합성 정답 (없으면 빈 값)"""
    gt = cc.load_gt(work)
    docs = []
    for vol in vols:
        for d in cc.stored_docs(work, vol):
            row = gt.get(d["doc_id"]) or {}
            values = {k: row.get(k, "") for k in GT_FIELDS}
            body = " ".join(cc.page_ocr(work, vol, d["pages"][0])["lines"][:60])
            docs.append({"key": (vol, d["doc_id"]), "values": values,
                         "text": f"{fields.embed_text(SCHEMA, values)}\n본문: {body}",
                         "search_text": fields.search_text(SCHEMA, values), "year": fields.year_of(SCHEMA, values)})
    return docs


def load_queries(path=QUERIES):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def relevant(q, docs):
    """검색어 match 규칙(칸 → 정규식, year → 연도)을 합성 정답에 맞춘 문서 키"""
    m = q.get("match") or {}
    out = []
    for d in docs:
        if not m:
            break
        if m.get("year") and d["year"] != m["year"]:
            continue
        if all(re.search(rx, d["values"].get(k, "")) for k, rx in m.items() if k != "year"):
            out.append(d["key"])
    return out


def keyword_rank(words, docs, year=None):
    """app/store.keyword_search와 같다: search_text에 든 낱말 수(ILIKE 부분 일치) > 0, 점수 내림차순 → doc_id → vol"""
    if not words:
        return []
    hits = []
    for d in docs:
        if year and d["year"] != year:
            continue
        hay = d["search_text"].lower()
        s = sum(w.lower() in hay for w in words)
        if s:
            hits.append((-s, d["key"][1], d["key"][0], d["key"]))
    return [h[3] for h in sorted(hits)[:CANDIDATES]]


def vector_rank(qv, docs, vecs, year=None):
    """코사인 상위 CANDIDATES (Qdrant COSINE과 같은 순서), 연도 칸 거르기"""
    import numpy as np  # noqa: PLC0415
    m = np.asarray(vecs, dtype=np.float64)
    m = m / np.linalg.norm(m, axis=1, keepdims=True)
    q = np.asarray(qv, dtype=np.float64)
    sims = m @ (q / np.linalg.norm(q))
    order = [i for i in np.argsort(-sims, kind="stable") if not year or docs[i]["year"] == year]
    return [docs[i]["key"] for i in order[:CANDIDATES]], [float(sims[i]) for i in order[:CANDIDATES]]


def rank_all(queries, docs, dvecs, qvecs):
    """질의별 {vector, keyword, rrf} 순위 (키는 'vol/doc_id' 글자)"""
    out = {}
    for q, qv in zip(queries, qvecs):
        year = q.get("year")
        v, sims = vector_rank(qv, docs, dvecs, year)
        kw = keyword_rank(query_words(q["q"]), docs, year)
        score = rrf(v, kw)
        order = sorted(score, key=score.get, reverse=True)
        out[q["id"]] = {"vector": ["/".join(k) for k in v[:10]], "keyword": ["/".join(k) for k in kw[:10]],
                        "rrf": ["/".join(k) for k in order[:10]], "top_sim": round(sims[0], 4) if sims else None}
    return out


def overlap(a, b, k=5):
    a, b = a[:k], b[:k]
    n = max(len(a), len(b))
    return len(set(a) & set(b)) / min(k, n) if n else 1.0


def recall5(ranked, rel):
    return len(set(ranked[:5]) & set(rel)) / min(5, len(rel)) if rel else None


def summarize(ranks, queries, docs, base=None):
    """{유형: {n, recall5_rrf, recall5_vec, overlap5_rrf, overlap5_vec}} (유형 '전체' 포함)"""
    rel = {q["id"]: ["/".join(k) for k in relevant(q, docs)] for q in queries}
    out = {}
    for t in ["전체", *dict.fromkeys(q["type"] for q in queries)]:
        qs = [q for q in queries if t == "전체" or q["type"] == t]
        row = {"n": len(qs)}
        for m, key in (("rrf", "recall5_rrf"), ("vector", "recall5_vec")):
            vals = [recall5(ranks[q["id"]][m], rel[q["id"]]) for q in qs if rel[q["id"]]]
            row[key] = round(statistics.mean(vals), 3) if vals else None
        if base:
            for m, key in (("rrf", "overlap5_rrf"), ("vector", "overlap5_vec")):
                row[key] = round(statistics.mean(overlap(base[q["id"]][m], ranks[q["id"]][m]) for q in qs), 3)
        out[t] = row
    return out, rel


def embed(client, model, texts, priced, budget):
    """묶음별 임베딩 → (벡터들, 토큰, 비용). OpenAI는 묶음마다 예상 상한(UTF-8 바이트 + 형식 토큰)을 예약"""
    from app import llm  # noqa: PLC0415
    vecs, tokens, usd = [], 0, 0.0
    for i in range(0, len(texts), BATCH):
        chunk = texts[i:i + BATCH]
        est = llm.estimate_usd(model, llm.tokens_max(*chunk, messages=len(chunk)), 0) if priced else 0.0
        if priced and not budget.reserve(est):
            raise SystemExit(f"OpenAI 지출 상한 ${budget.cap}을 넘어 멈춤 (쓴 돈 ${budget.spent:.6f})")
        cost = 0.0
        try:
            r = client.embeddings.create(model=model, input=chunk)
            t = getattr(r.usage, "prompt_tokens", 0) or 0  # 로컬 서버는 usage를 안 줄 수 있다
            tokens += t
            if priced:
                cost = t * llm.price_of(model)[0] / 1e6 if r.usage else est
            vecs += [d.embedding for d in sorted(r.data, key=lambda d: d.index)]
        finally:
            if priced:
                budget.settle(est, cost)
        usd += cost
    return vecs, tokens, usd


def table(results):
    lines = ["| 후보 | 차원 | /search 상위5 겹침 (기준 대비) | 벡터만 상위5 겹침 | /search recall@5 | 벡터만 recall@5 "
             "| 토큰 | 비용 | 시간 |", "|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        s = r["summary"]["전체"]
        lines.append(f"| {r['model']} ({r['where']}) | {r['dim']} | {s.get('overlap5_rrf', '기준')} | "
                     f"{s.get('overlap5_vec', '기준')} | {s['recall5_rrf']} | {s['recall5_vec']} | {r['tokens']:,} | "
                     f"${r['usd']:.5f} | {r['wall_s']}s |")
    return "\n".join(lines)


def type_table(res):
    lines = ["| 유형 | n | /search 겹침 | 벡터 겹침 | /search recall@5 | 벡터 recall@5 |", "|---|---|---|---|---|---|"]
    for t, s in res["summary"].items():
        lines.append(f"| {t} | {s['n']} | {s.get('overlap5_rrf', '-')} | {s.get('overlap5_vec', '-')} | "
                     f"{s['recall5_rrf']} | {s['recall5_vec']} |")
    return "\n".join(lines)


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "--table":
        print(table([json.loads(Path(p).read_text(encoding="utf-8")) for p in argv[1:]]))
        return 0
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    cc.add_args(ap)
    ap.add_argument("--queries", default=str(QUERIES))
    ap.add_argument("--baseline", default="", help="기준 결과 파일 (지금 임베딩) — 주면 상위 5개 겹침을 낸다")
    a = ap.parse_args(argv)
    label = cc.label_ok(a.label)
    vols = cc.synthetic_vols(a.work, [v for v in a.vols.split(",") if v.strip()])
    out = cc.out_dir(a.work, "embed")
    docs, queries = load_docs(a.work, vols), load_queries(a.queries)
    base = None
    if a.baseline:
        b = json.loads(Path(a.baseline).read_text(encoding="utf-8"))
        if sorted(b["vols"]) != sorted(vols) or sorted(b["ranks"]) != sorted(q["id"] for q in queries):
            raise SystemExit("기준 결과와 철·검색어가 다르다")
        base = b["ranks"]
    client, priced = cc.make_client(a.openai, a.base_url)
    budget = cc.Budget(a.max_usd)
    t0 = time.time()
    dvecs, t1, u1 = embed(client, a.model, [d["text"] for d in docs], priced, budget)
    qvecs, t2, u2 = embed(client, a.model, [q["q"] for q in queries], priced, budget)
    wall = round(time.time() - t0, 1)
    ranks = rank_all(queries, docs, dvecs, qvecs)
    summary, rel = summarize(ranks, queries, docs, base)
    res = {"label": label, "model": a.model, "where": "OpenAI" if a.openai else cc.check_base_url(a.base_url).split("/v1")[0],
           "vols": vols, "docs": len(docs), "dim": len(dvecs[0]), "tokens": t1 + t2, "usd": round(u1 + u2, 6), "wall_s": wall,
           "baseline": a.baseline or None, "summary": summary, "relevant": rel, "ranks": ranks}
    p = out / f"{label}.json"
    p.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"합성 철 {len(vols)}개, 문서 {len(docs)}, 검색어 {len(queries)}, 차원 {res['dim']}")
    print(table([res]))
    print()
    print(type_table(res))
    print(f"결과: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
