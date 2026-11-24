"""합성 철 /ask 답변 만들기 (https://github.com/reddol18/docflow/issues/21 2단계 — 심판 채점 전 답변 파일).

합성 답변 질의(eval/data/synth_answer_queries.jsonl, eval/answer/make_queries.py)마다 /ask와 같은 길로 답한다:
  검색   app/main.ranked와 같은 셈을 메모리 안에서 (eval/embed_compare.py와 같은 함수 — 문서 글은 app/graph.doc_text 꼴,
         벡터 코사인 30 + 낱말 30 → RRF). /ask는 연도 칸 없이 상위 5건 (app/main.ask_answer: hybrid(k=5)).
         Postgres·Qdrant에 넣지 않는다 — 이 PC에 운영 색인이 없고, 합성 철 평가가 운영 모음을 건드리지 않게.
  칸 값  합성 정답(gt) 4칸 — 운영에서는 추출(gpt-4.1-mini) 값이 들어간다
  근거   app/main.ask_answer와 같다: 문서 첫 쪽 OCR 앞 40줄을 이은 글(body). 메시지는 app/llm.answer_messages 그대로,
         호출 설정(temperature 0, max_tokens ANSWER_MAX_TOKENS)도 app/llm.answer와 같다. 답의 근거 표시는
         app/llm.drop_unknown_citations로 넘긴 문서만 남기고(지운 수는 경고), app/llm.citations로 인용 문서를 뽑는다
결과: <work>/answers/<label>.json — 질의별 답·가져온 문서(키·순위·넘긴 글)·인용·모델 판(API가 돌려준 이름)·토큰·비용, 합계.
OpenAI에는 합성 철 글과 합성 질의만 간다 (eval/compare_common.synthetic_vols, make_queries.load_queries).

실행 (doc_platform 폴더, 합성 work의 평가 가상환경 — eval/embed_compare.py와 같은 환경):
  $py = '..\\work_synth\\w47\\promptfoo\\.venv\\Scripts\\python.exe'
  & $py -I -X utf8 eval/answer/gen_answers.py --work ..\\work_synth\\w48_21\\work --label gpt4omini --max-usd 1
"""
import argparse
import json
import sys
import time
from pathlib import Path

EVAL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVAL))  # python -I: eval 폴더 (compare_common·embed_compare)
sys.path.insert(0, str(Path(__file__).resolve().parent))

import compare_common as cc  # noqa: E402
import embed_compare as ec  # noqa: E402
import make_queries as mq  # noqa: E402

from app import fields  # noqa: E402

QUERIES = EVAL / "data" / "synth_answer_queries.jsonl"
TOP_K = 5  # app/main.ask_answer: hybrid(q, ..., 5)
BODY_LINES = 40  # app/main.ask_answer: load_ocr(...)["lines"][:40]
BODY_CHARS = 600  # app/llm.answer_messages: d['body'][:600]


def ask_hits(order, docs_by_key, bodies, scores, v, kw, k=TOP_K):
    """RRF 순위 → /ask에 넘기는 문서 [{vol, doc_id, rrf, vector_rank, keyword_rank, fields, body}] (app/main.hybrid·ask_answer 꼴).
    칸은 스키마의 보이는 칸만"""
    vr = {key: i + 1 for i, key in enumerate(v)}
    kr = {key: i + 1 for i, key in enumerate(kw)}
    shown = fields.shown(ec.SCHEMA)
    out = []
    for key in order[:k]:
        d = docs_by_key[key]
        out.append({"doc_id": key[1], "rrf": round(scores[key], 5), "vector_rank": vr.get(key), "keyword_rank": kr.get(key),
                    "vol": key[0], "fields": {c: d["values"].get(c, "") for c in shown}, "body": bodies[key]})
    return out


def context_of(hit, schema=None):
    """문서 하나가 답변 메시지에 들어가는 글 — app/llm.answer_messages의 문서 목록 한 항목과 같다 (시험이 맞춰 본다)"""
    from app import llm  # noqa: PLC0415
    schema = schema or ec.SCHEMA
    lab = fields.labels(schema)
    return (f"[{llm.cite_key(hit['vol'], hit['doc_id'])}] "
            + " / ".join(f"{lab[k]} {(hit.get('fields') or {}).get(k, '')}" for k in fields.shown(schema))
            + f"\n본문 앞부분: {hit['body'][:BODY_CHARS]}")


def cited_keys(text):
    from app import llm  # noqa: PLC0415
    return [llm.cite_key(v, d) for v, d in llm.citations(text)]


def check_sources(queries, keys):
    """질의의 근거 문서가 모두 이번 색인(합성 work 문서)에 있는가 — 없으면 멈춘다 (다른 work로 만든 질의 세트)"""
    have = {"/".join(k) for k in keys}
    missing = sorted({s for q in queries for s in q["source_docs"]} - have)
    if missing:
        raise SystemExit(f"질의 근거 문서가 work에 없다 {len(missing)}건 (예: {missing[0]}) — 같은 work로 만든 질의 세트를 쓴다")


def totals(rows):
    """질의별 결과 → 합계 (토큰·비용·실패·검색 재현)"""
    ok = [r for r in rows if "error" not in r]
    ans = [r for r in ok if r["answerable"]]
    hit = [r for r in ans if set(r["source_docs"]) <= {c["key"] for c in r["retrieved"]}]
    return {"n": len(rows), "failed": len(rows) - len(ok),
            "prompt_tokens": sum(r["usage"]["prompt_tokens"] for r in ok),
            "completion_tokens": sum(r["usage"]["completion_tokens"] for r in ok),
            "usd": round(sum(r["usd"] for r in ok), 6),
            "models": sorted({r["model"] for r in ok}),
            "retrieved_all_sources": f"{len(hit)}/{len(ans)}",
            "dropped_citations": sum(r["dropped_citations"] for r in ok)}


def answer_one(client, model, q, hits, budget):
    """한 질의 답변 — app/llm.answer와 같은 메시지·설정. 예산(--max-usd)을 넘을 호출은 부르지 않는다"""
    from app import llm  # noqa: PLC0415
    messages = llm.answer_messages(q["question"], hits, ec.SCHEMA)
    est = llm.estimate_usd(model, llm.tokens_max(*(m["content"] for m in messages), messages=len(messages)), llm.ANSWER_MAX_TOKENS)
    if not budget.reserve(est):
        raise SystemExit(f"OpenAI 지출 상한 ${budget.cap}을 넘어 멈춤 (쓴 돈 ${budget.spent:.6f})")
    usd = 0.0
    try:
        try:
            r = client.chat.completions.create(model=model, temperature=0, max_tokens=llm.ANSWER_MAX_TOKENS, messages=messages)
        except Exception as e:  # noqa: BLE001 — SDK 오류 문구는 찍지 않고 종류만 (키 일부가 들 수 있다)
            return {"error": type(e).__name__}
        pin, pout = llm.price_of(model)
        u = r.usage
        usd = (u.prompt_tokens * pin + u.completion_tokens * pout) / 1e6 if u else est
    finally:
        budget.settle(est, usd)
    raw = r.choices[0].message.content or ""
    text, dropped = llm.drop_unknown_citations(raw, hits)
    return {"answer": text, "raw_answer": raw, "dropped_citations": dropped,
            "warnings": [llm.UNCITED_WARNING.format(n=dropped)] if dropped else [], "cited": cited_keys(text),
            "model": r.model, "system_fingerprint": getattr(r, "system_fingerprint", None),
            "finish_reason": r.choices[0].finish_reason,
            "usage": {"prompt_tokens": u.prompt_tokens if u else 0, "completion_tokens": u.completion_tokens if u else 0},
            "usd": round(usd, 8)}


def main(argv=None):
    from app import llm  # noqa: PLC0415
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--work", required=True, help="합성 work 폴더 (eval/synth/to_work.py 출력)")
    ap.add_argument("--queries", default=str(QUERIES))
    ap.add_argument("--label", required=True)
    ap.add_argument("--model", default=llm.OPENAI_MODELS["chat"], help="답변 모델 (기본: app CHAT_MODEL 기본값)")
    ap.add_argument("--embed-model", default=llm.OPENAI_MODELS["embed"])
    ap.add_argument("--max-usd", type=float, default=1.0, help="OpenAI 지출 상한 (이번 실행, 임베딩 포함)")
    a = ap.parse_args(argv)
    label = cc.label_ok(a.label)
    vols = cc.synthetic_vols(a.work)
    docs, queries = ec.load_docs(a.work, vols), mq.load_queries(a.queries)  # 합성이 아니면 여기서 멈춘다 (부르기 전)
    check_sources(queries, [d["key"] for d in docs])
    out = cc.out_dir(a.work, "answers")
    bodies = {d["key"]: " ".join(cc.page_ocr(a.work, d["key"][0], d["key"][1])["lines"][:BODY_LINES]) for d in docs}
    by_key = {d["key"]: d for d in docs}
    client, _ = cc.make_client(True, None)
    budget = cc.Budget(a.max_usd)
    t0 = time.time()
    dvecs, et1, eu1 = ec.embed(client, a.embed_model, [d["text"] for d in docs], True, budget)
    qvecs, et2, eu2 = ec.embed(client, a.embed_model, [q["question"] for q in queries], True, budget)
    rows = []
    for q, qv in zip(queries, qvecs):
        v, _ = ec.vector_rank(qv, docs, dvecs)
        kw = ec.keyword_rank(ec.query_words(q["question"]), docs)
        scores = ec.rrf(v, kw)
        order = sorted(scores, key=scores.get, reverse=True)
        hits = ask_hits(order, by_key, bodies, scores, v, kw)
        res = answer_one(client, a.model, q, hits, budget)
        rows.append({"id": q["id"], "type": q["type"], "question": q["question"], "answerable": q["answerable"],
                     "expected_answer": q["answer"], "source_docs": q["source_docs"],
                     "retrieved": [{"key": llm.cite_key(h["vol"], h["doc_id"]), "rank": i, "rrf": h["rrf"],
                                    "vector_rank": h["vector_rank"], "keyword_rank": h["keyword_rank"],
                                    "fields": h["fields"], "context": context_of(h)} for i, h in enumerate(hits, 1)],
                     **res})
        print(f"{q['id']} {res.get('model', res.get('error'))} 인용 {res.get('cited', [])}", flush=True)
    tot = totals(rows)
    tot.update(embed_tokens=et1 + et2, embed_usd=round(eu1 + eu2, 6), total_usd=round(tot["usd"] + eu1 + eu2, 6),
               wall_s=round(time.time() - t0, 1))
    res = {"label": label, "issue": "https://github.com/reddol18/docflow/issues/21", "chat_model": a.model,
           "embed_model": a.embed_model, "vols": vols, "docs": len(docs), "queries_file": Path(a.queries).name,
           "queries_sha256": ec.digest(queries), "settings": {"top_k": TOP_K, "body_lines": BODY_LINES, "body_chars": BODY_CHARS,
                                                              "temperature": 0, "max_tokens": llm.ANSWER_MAX_TOKENS,
                                                              "answer_prompt_sha256": ec.digest(llm.ANSWER_PROMPT)},
           "totals": tot, "rows": rows}
    p = out / f"{label}.json"
    p.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(tot, ensure_ascii=False))
    print(f"결과: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
