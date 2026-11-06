"""검색 평가 채점기 (https://github.com/reddol18/docflow/issues/20). LLM 채팅 호출 없음 — 질의 임베딩만.

벡터만 / 낱말만 / RRF(실제 /search와 같은 합치기) 세 방식을 질의 세트(`eval/data/search_queries.jsonl`)로 잰다.
- 답 있는 질의: recall@5·recall@10, MRR, nDCG@10 (정답은 이진)
  recall@k = 상위 k 안의 정답 수 / min(k, 정답 수) — 정답이 k보다 많은 주제 질의도 1까지 갈 수 있게.
  분모가 k마다 달라 recall@5 > recall@10이 될 수 있다(정답 39건 질의: 5/5=1.0, 9/10=0.9). @5와 @10을 서로 비교하지 말 것
- 순위는 /search와 같은 `main.ranked()`로 낸다(후보 수 `main.CANDIDATES`). 조건 질의는 year로 연도 칸을 채운다.
  시험용 철(vol이 test로 시작, 실제 문서의 복제)은 정답 세트 밖이라 순위에서 뺀다
- 답 없음 질의: 순위 지표가 없다. 대신 1위 점수(벡터 유사도·RRF)를 답 있는 질의와 비교한다 → '관련 문서 없음' 임계값 근거

돌리는 곳: 색인(Postgres·Qdrant)과 임베딩 키가 있는 docflow 파드 안.
  kubectl -n docs cp eval/data/search_queries.jsonl <파드>:/tmp/q.jsonl
  kubectl -n docs exec -i deploy/docflow -- python - /tmp/q.jsonl < eval/search_score.py
  (`python -I`를 쓰지 않는 예외: 파드 안 /app에서 표준 입력으로 돌려 `app` 패키지를 불러와야 한다. 데이터 폴더를 읽지 않는다)
결과: 표준 출력에 마크다운 표와 마지막 줄 JSON(질의별 순위)
"""
import json
import math
import statistics
import sys


def recall_at(ranked, rel, k):
    if not rel:
        return None
    return len(set(ranked[:k]) & set(rel)) / min(k, len(rel))


def mrr(ranked, rel):
    for i, d in enumerate(ranked, 1):
        if d in rel:
            return 1 / i
    return 0.0 if rel else None


def ndcg_at(ranked, rel, k=10):
    if not rel:
        return None
    dcg = sum(1 / math.log2(i + 1) for i, d in enumerate(ranked[:k], 1) if d in rel)
    ideal = sum(1 / math.log2(i + 1) for i in range(1, min(k, len(rel)) + 1))
    return dcg / ideal


METRICS = {"recall@5": lambda r, g: recall_at(r, g, 5), "recall@10": lambda r, g: recall_at(r, g, 10),
           "MRR": mrr, "nDCG@10": ndcg_at}


def score(results, queries):
    """results = {qid: {방식: [doc_id 순위]}} → {방식: {지표: 평균}} (답 있는 질의만), 유형별도"""
    out = {}
    for method in ("vector", "keyword", "rrf"):
        for group in ["전체", *sorted({q["type"] for q in queries if q["relevant"]})]:
            qs = [q for q in queries if q["relevant"] and (group == "전체" or q["type"] == group)]
            row = {}
            for name, f in METRICS.items():
                vals = [f(results[q["id"]][method], set(q["relevant"])) for q in qs]
                row[name] = round(statistics.mean(vals), 3) if vals else None
            row["n"] = len(qs)
            out.setdefault(method, {})[group] = row
    return out


def top_scores(tops, queries):
    """1위 점수 분포: 답 있음 vs 답 없음 (벡터 유사도, RRF)"""
    def stats(xs):
        return {"min": round(min(xs), 4), "median": round(statistics.median(xs), 4), "max": round(max(xs), 4)} if xs else None
    out = {}
    for key in ("vector", "rrf"):
        for label, cond in (("답 있음", True), ("답 없음", False)):
            out[f"{key} {label}"] = stats([tops[q["id"]][key] for q in queries if bool(q["relevant"]) == cond])
    return out


def table(s):
    names = list(METRICS)
    lines = ["| 방식 | 유형 | n | " + " | ".join(names) + " |", "|---|---|---|" + "---|" * len(names)]
    for method, groups in s.items():
        for group, row in groups.items():
            lines.append(f"| {method} | {group} | {row['n']} | " + " | ".join(str(row[n]) for n in names) + " |")
    return "\n".join(lines)


def run(path):  # pragma: no cover — 파드 안에서만 (색인·임베딩 필요)
    from app import llm, main, store
    store.pool.open()
    queries = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    vecs = llm.embed("eval", [q["q"] for q in queries])  # 한 번에 (질의 임베딩만)
    raw = {}
    for q, vec in zip(queries, vecs):
        year = q.get("year")  # 조건 질의: 화면의 연도 칸에 넣은 것처럼 year_from=year_to=연도
        raw[q["id"]] = main.ranked(q["q"], vec, year, year)
    ids = {d for *_, order in raw.values() for d in order}  # RRF 순서에 벡터·낱말 후보가 모두 들어 있다
    test = {d for d, row in store.get_documents(list(ids)).items() if row["vol"].startswith("test")}
    results, tops = {}, {}
    for qid, (v, kw, fused, order) in raw.items():
        v = [r for r in v if r["doc_id"] not in test]  # 시험용 철(복제 문서)은 정답 세트 밖이라 순위에서 뺀다
        kw = [r for r in kw if r["doc_id"] not in test]
        rr = [d for d in order if d not in test]
        results[qid] = {"vector": [r["doc_id"] for r in v], "keyword": [r["doc_id"] for r in kw], "rrf": rr}
        tops[qid] = {"vector": v[0]["score"] if v else 0.0, "rrf": fused[rr[0]] if rr else 0.0}
    s = score(results, queries)
    print(table(s))
    print()
    print("| 1위 점수 | min | median | max |\n|---|---|---|---|")
    for k, st in top_scores(tops, queries).items():
        print(f"| {k} | {st['min']} | {st['median']} | {st['max']} |" if st else f"| {k} | - | - | - |")
    print()
    print(json.dumps({"score": s, "ranks": results, "tops": tops}, ensure_ascii=False))


if __name__ == "__main__":
    run(sys.argv[1])
