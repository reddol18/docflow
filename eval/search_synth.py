"""합성 철 검색 채점 (https://github.com/reddol18/docflow/issues/61 — 검색 개선 측정, 2026-11-24 결정: 합성 데이터로).

합성 질의 세트마다 /search 순위를 메모리 안에서 다시 내고(Postgres·Qdrant에 넣지 않음) 정답 문서와 맞춰 본다.
  순위     벡터(코사인 30) · 낱말(30) · rrf(예전 /search = RRF만) · app(지금 /search = app/retrieval.rank:
           문서번호 정확 일치 고정 → RRF, 문서번호 질의인데 그 번호가 없으면 빈 목록 '없음').
           합치기는 운영과 같은 app/retrieval 함수 (eval/embed_compare.app_rank) — 복사본이 아니다
  문서 글  --compose (지금은 current = app/graph.doc_text 꼴: 4칸 + 첫 쪽 OCR 60줄, eval/embed_compare.load_docs)
  지표     유형별 recall@1·@5·@10, MRR(전체 순위), 근거 모두 상위 5 (정답이 있는 질의만)
           '없음' 임계값 훑기: 벡터 1위 유사도 < t 이면 '없음'(app/retrieval.no_match) — 답 없음 질의의 정밀도·재현율,
           답 있는 질의를 잘못 '없음'으로 본 수
  질의     (A) eval/data/synth_answer_queries.jsonl (id a…, 근거 source_docs — eval/answer/make_queries.load_queries)
           (B) eval/data/synth_search_queries.jsonl (id s…, match 규칙 — eval/embed_compare.load_queries)
           둘 다 합성 형식이 아니면 부르기 전에 멈춘다. 철도 합성만 (eval/compare_common.synthetic_vols)
  캐시     <work>/embed_cache/<모델>/<sha256(글)> — 같은 모델·같은 글은 다시 부르지 않는다 (재측정 0원)
  비용     OpenAI는 --max-usd(기본 0.2)를 넘을 호출은 부르지 않는다. --base-url(이 PC Ollama 등)은 0원

실행 (doc_platform 폴더, 합성 work의 평가 가상환경 — eval/embed_compare.py와 같은 환경):
  $py = '..\\work_synth\\w47\\promptfoo\\.venv\\Scripts\\python.exe'
  & $py -I -X utf8 eval/search_synth.py --work ..\\work_synth\\w48_21\\work --queries eval/data/synth_answer_queries.jsonl
  & $py -I -X utf8 eval/search_synth.py --work ..\\work_synth\\w47 --queries eval/data/synth_search_queries.jsonl
  & $py -I -X utf8 eval/search_synth.py --work ... --model bge-m3 --base-url http://127.0.0.1:11434/v1
결과: <work>/search_eval/<label>.json (질의별 전체 순위·1위 유사도·지표, 유형별 표, 임계값 훑기)
"""
import argparse
import hashlib
import json
import re
import statistics
import sys
import time
from pathlib import Path

EVAL = Path(__file__).resolve().parent
sys.path.insert(0, str(EVAL / "answer"))  # make_queries
sys.path.insert(0, str(EVAL))  # python -I: eval 폴더 (compare_common·embed_compare·search_score)

import compare_common as cc  # noqa: E402
import embed_compare as ec  # noqa: E402
import gen_answers as ga  # noqa: E402
import make_queries as mq  # noqa: E402
import search_score as ss  # noqa: E402

from app import llm, retrieval  # noqa: E402

COMPOSE = {"current": lambda d: d["text"]}  # 문서 글 짜기 (4단계에서 더한다)
METHODS = ("vector", "keyword", "rrf", "app")
FUSIONS = ("app", "rrf")  # '없음' 훑기·머리 지표에 쓸 순위
ALL = "답 있음 전체"
SWEEP = [None] + [round(0.10 + 0.01 * i, 2) for i in range(71)]  # 0.10 ~ 0.80


# ---------- 질의 세트 ----------

def load_set(path, docs):
    """합성 질의 파일 → [{id, type, q, year, gold, answerable}]. id 첫 글자로 세트를 고른다(a = 답변 질의, s = 검색어).
    로더가 합성 형식을 확인한다 — 아니면 SystemExit (부르기 전)"""
    first = next((ln for ln in Path(path).read_text(encoding="utf-8").splitlines() if ln.strip()), "")
    try:
        qid = str(json.loads(first).get("id", ""))
    except (ValueError, AttributeError):
        raise ec.NotSyntheticQueries("질의 파일 첫 줄이 JSON 객체가 아니다") from None
    keys = {"/".join(d["key"]) for d in docs}
    if qid.startswith("a"):
        out = [{"id": q["id"], "type": q["type"], "q": q["question"], "year": None, "gold": list(q["source_docs"]),
                "answerable": q["answerable"]} for q in mq.load_queries(path)]
        missing = sorted({g for q in out for g in q["gold"]} - keys)
        if missing:
            raise SystemExit(f"질의 근거 문서가 work에 없다 {len(missing)}건 (예: {missing[0]}) — 같은 work로 만든 질의 세트를 쓴다")
        return out
    if qid.startswith("s"):
        return [{"id": q["id"], "type": q["type"], "q": q["q"], "year": q.get("year"),
                 "gold": ["/".join(k) for k in ec.relevant(q, docs)], "answerable": bool(q["match"])}
                for q in ec.load_queries(path)]
    raise ec.NotSyntheticQueries("합성 질의 파일이 아니다 (id가 a… 또는 s…) — 외부 임베딩에는 합성 질의만 보낸다")


# ---------- 임베딩 캐시 ----------

def text_key(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def model_dir(model):
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", model):
        raise SystemExit("--model 이름은 영문·숫자·_.:- 80자까지")
    return model.replace(":", "_")


def embed_cached(texts, model, cdir, client_fn, budget):
    """글 → 벡터. 캐시(cdir/<sha256(글)>)에 없는 글만 부른다. client_fn()은 부를 글이 있을 때만 부른다 (캐시만이면 키도
    필요 없다). → (벡터들, 토큰, 비용, 부른 글 수)"""
    cdir = Path(cdir)
    keys = [text_key(t) for t in texts]
    have = {}
    for k in set(keys):
        p = cdir / k
        if p.exists():
            have[k] = json.loads(p.read_text(encoding="utf-8"))
    miss = list(dict.fromkeys(t for t, k in zip(texts, keys) if k not in have))
    tokens, usd = 0, 0.0
    if miss:
        client, priced = client_fn()
        vecs, tokens, usd = ec.embed(client, model, miss, priced, budget)
        cdir.mkdir(parents=True, exist_ok=True)
        for t, v in zip(miss, vecs):
            k = text_key(t)
            tmp = cdir / f"{k}.tmp"
            tmp.write_text(json.dumps(v), encoding="utf-8")
            tmp.replace(cdir / k)
            have[k] = v
    return [have[k] for k in keys], tokens, usd, len(miss)


# ---------- 지표 ----------

def query_metrics(ranking, gold):
    """한 질의 한 순위 → {r1, r5, r10, mrr, all5}. 정답 없으면 None. recall@k = 상위 k 안 정답 수 / min(k, 정답 수)
    (eval/search_score.py와 같은 정의)"""
    g = set(gold)
    if not g:
        return None
    return {"r1": ss.recall_at(ranking, g, 1), "r5": ss.recall_at(ranking, g, 5), "r10": ss.recall_at(ranking, g, 10),
            "mrr": ss.mrr(ranking, g), "all5": g <= set(ranking[:5])}


def summarize(rows, methods=METHODS):
    """{유형: {방식: {n, r1, r5, r10, mrr, all5 (개수)}}} — 정답 있는 질의만, '답 있음 전체' 포함"""
    out = {}
    ans = [r for r in rows if r["gold"]]
    for t in [ALL, *dict.fromkeys(r["type"] for r in ans)]:
        qs = [r for r in ans if t == ALL or r["type"] == t]
        out[t] = {}
        for m in methods:
            ms = [r["metrics"][m] for r in qs]
            out[t][m] = {"n": len(qs), **{k: round(statistics.mean(x[k] for x in ms), 3) for k in ("r1", "r5", "r10", "mrr")},
                         "all5": sum(x["all5"] for x in ms)}
    return out


def predict_none(row, fusion, t):
    """이 질의를 '없음'으로 볼지 — app은 app/retrieval.no_match(문서번호 없음 + 임계값), rrf는 임계값만"""
    if fusion == "app":
        return retrieval.no_match(row["_ranking"], row["top_sim"], t)
    return t is not None and row["top_sim"] is not None and row["top_sim"] < t


def sweep(rows, fusion, thresholds=SWEEP):
    """임계값마다 {t, none_n, tp, precision, recall, false_none} — 답 없음 = gold 없는 질의. 결과가 앞 줄과 같으면 뺀다"""
    neg = [r for r in rows if not r["answerable"]]
    out, last = [], None
    for t in thresholds:
        pred = [r for r in rows if predict_none(r, fusion, t)]
        tp = sum(not r["answerable"] for r in pred)
        row = {"t": t, "none_n": len(pred), "tp": tp,
               "precision": round(tp / len(pred), 3) if pred else None,
               "recall": round(tp / len(neg), 3) if neg else None,
               "false_none": len(pred) - tp,
               "false_none_ids": [r["id"] for r in pred if r["answerable"]]}
        sig = (row["tp"], row["false_none"])
        if sig != last:
            out.append(row)
            last = sig
    return out


# ---------- 순위 ----------

def rank_queries(queries, docs, dvecs, qvecs):
    """질의별 줄: 네 방식 전체 순위('vol/doc_id'), 1위 유사도, 문서번호, 지표"""
    rows = []
    for q, qv in zip(queries, qvecs):
        r, sims = ec.app_rank(q["q"], qv, docs, dvecs, q["year"])
        miss = retrieval.no_match(r)  # /search: 문서번호 질의인데 그 번호가 없으면 빈 목록
        ranks = {"vector": r.vector, "keyword": r.keyword, "rrf": retrieval.fuse(r.vector, r.keyword)[1],
                 "app": [] if miss else r.order}
        ranks = {m: ["/".join(k) for k in v] for m, v in ranks.items()}
        rows.append({"id": q["id"], "type": q["type"], "q": q["q"], "year": q["year"], "answerable": q["answerable"],
                     "gold": q["gold"], "top_sim": round(sims[0], 4) if sims else None,
                     "docnos": [" ".join(d) for d in r.docnos], "pinned": ["/".join(k) for k in r.pinned],
                     "docno_miss": miss, "ranks": ranks,
                     "metrics": {m: query_metrics(ranks[m], q["gold"]) for m in METHODS} if q["gold"] else None,
                     "_ranking": r})
    return rows


# ---------- 표 ----------

def type_table(summary, methods=METHODS):
    lines = ["| 유형 | n | 방식 | R@1 | R@5 | R@10 | MRR | 근거 모두 상위5 |", "|---|---|---|---|---|---|---|---|"]
    for t, by in summary.items():
        for m in methods:
            s = by[m]
            lines.append(f"| {t} | {s['n']} | {m} | {s['r1']} | {s['r5']} | {s['r10']} | {s['mrr']} | {s['all5']}/{s['n']} |")
    return "\n".join(lines)


def sweep_table(sw):
    lines = ["| 임계값 | '없음' 수 | 답 없음 맞힘 | 정밀도 | 재현율 | 잘못 '없음' |", "|---|---|---|---|---|---|"]
    for s in sw:
        t = "문서번호만" if s["t"] is None else s["t"]
        lines.append(f"| {t} | {s['none_n']} | {s['tp']} | {s['precision']} | {s['recall']} | {s['false_none']} |")
    return "\n".join(lines)


def unanswerable_table(rows):
    lines = ["| id | 유형 | 1위 유사도 | 문서번호 | app '없음' |", "|---|---|---|---|---|"]
    for r in rows:
        if not r["answerable"]:
            lines.append(f"| {r['id']} | {r['type']} | {r['top_sim']} | {', '.join(r['docnos']) or '-'} | {r['docno_miss']} |")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--work", required=True, help="합성 work 폴더 (eval/synth/to_work.py 출력)")
    ap.add_argument("--queries", required=True, help="합성 질의 파일 (synth_answer_queries·synth_search_queries 형식)")
    ap.add_argument("--model", default=llm.OPENAI_MODELS["embed"], help="임베딩 모델")
    ap.add_argument("--base-url", default="", help="OpenAI 호환 임베딩 주소 (https 또는 http://127.0.0.1:<포트>/v1). 비우면 OpenAI")
    ap.add_argument("--compose", default="current", choices=sorted(COMPOSE), help="문서 글 짜기")
    ap.add_argument("--fusion", default="app", choices=FUSIONS, help="'없음' 훑기에 쓸 순위")
    ap.add_argument("--max-usd", type=float, default=0.2, help="OpenAI 지출 상한 (이번 실행, 캐시에 없는 글만 부른다)")
    ap.add_argument("--label", default="", help="결과 파일 이름 (비우면 <질의 파일>_<모델>_<compose>)")
    a = ap.parse_args(argv)
    vols = cc.synthetic_vols(a.work)  # 합성 철이 아니면 여기서 멈춘다 (부르기 전)
    docs = ec.load_docs(a.work, vols)
    queries = load_set(a.queries, docs)  # 합성 질의가 아니면 여기서 멈춘다 (부르기 전)
    label = cc.label_ok(a.label or f"{Path(a.queries).stem}_{model_dir(a.model)}_{a.compose}")
    out = cc.out_dir(a.work, "search_eval")
    cdir = cc.out_dir(a.work, "embed_cache") / model_dir(a.model)
    remote = bool(a.base_url)
    if remote and ga.paid_host(cc.check_base_url(a.base_url)):  # 0원으로 세지 않게
        raise SystemExit("OpenAI는 --base-url 없이 부른다 (단가·지출 상한 적용)")
    budget = cc.Budget(a.max_usd)
    client = []

    def client_fn():  # 캐시에 없는 글이 있을 때만 만든다 (키를 읽지 않게)
        if not client:
            client.append(cc.make_client(not remote, a.base_url or None))
        return client[0]

    t0 = time.time()
    compose = COMPOSE[a.compose]
    dvecs, t1, u1, m1 = embed_cached([compose(d) for d in docs], a.model, cdir, client_fn, budget)
    qvecs, t2, u2, m2 = embed_cached([q["q"] for q in queries], a.model, cdir, client_fn, budget)
    rows = rank_queries(queries, docs, dvecs, qvecs)
    summary = summarize(rows)
    sw = sweep(rows, a.fusion)
    res = {"label": label, "issue": "https://github.com/reddol18/docflow/issues/61", "model": a.model,
           "where": cc.check_base_url(a.base_url).split("/v1")[0] if remote else "OpenAI", "compose": a.compose,
           "fusion": a.fusion, "queries_file": Path(a.queries).name, "queries_sha256": ec.digest(queries),
           "vols": vols, "docs": len(docs), "dim": len(dvecs[0]) if dvecs else None,
           "embedded": m1 + m2,
           "cached": len({compose(d) for d in docs}) + len({q["q"] for q in queries}) - m1 - m2, "tokens": t1 + t2,
           "usd": round(u1 + u2, 6), "wall_s": round(time.time() - t0, 1),
           "summary": summary, "sweep": sw, "rows": [{k: v for k, v in r.items() if k != "_ranking"} for r in rows]}
    p = out / f"{label}.json"
    p.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"합성 철 {len(vols)}개, 문서 {len(docs)}, 질의 {len(queries)}, 차원 {res['dim']}, "
          f"새로 임베딩 {res['embedded']} (캐시 {res['cached']}), 토큰 {res['tokens']:,}, 비용 ${res['usd']:.6f}")
    print(type_table(summary))
    print()
    print(f"'없음' 훑기 ({a.fusion})")
    print(sweep_table(sw))
    print()
    print(unanswerable_table(rows))
    print(f"결과: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
