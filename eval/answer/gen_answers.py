"""합성 철 /ask 답변 만들기 (https://github.com/reddol18/docflow/issues/21 2단계 — 심판 채점 전 답변 파일).

합성 답변 질의(eval/data/synth_answer_queries.jsonl, eval/answer/make_queries.py)마다 /ask와 같은 길로 답한다:
  검색   app/main.rank와 같은 셈을 메모리 안에서 (eval/embed_compare.app_rank → app/retrieval.rank — 문서 글은
         app/graph.doc_text 꼴, 벡터 코사인 30 + 낱말 30 → RRF, 문서번호 정확 일치는 맨 앞).
         /ask는 연도 칸 없이 상위 5건 (app/main.ask_answer: hybrid(k=5)).
         Postgres·Qdrant에 넣지 않는다 — 이 PC에 운영 색인이 없고, 합성 철 평가가 운영 모음을 건드리지 않게.
  칸 값  합성 정답(gt) 4칸 — 운영에서는 추출(gpt-4.1-mini) 값이 들어간다
  근거   app/main.ask_answer와 같다: 문서 첫 쪽 OCR 앞 40줄을 이은 글(body). 메시지는 app/llm.answer_messages 그대로,
         호출 설정(temperature 0, max_tokens ANSWER_MAX_TOKENS)도 app/llm.answer와 같다. 답의 근거 표시는
         app/llm.drop_unknown_citations로 넘긴 문서만 남기고(지운 수는 경고), app/llm.citations로 인용 문서를 뽑는다
결과: <work>/answers/<label>.json — 질의별 답·가져온 문서(키·순위·넘긴 글)·인용·모델 판(API가 돌려준 이름)·토큰·비용·지연, 합계.
OpenAI에는 합성 철 글과 합성 질의만 간다 (eval/compare_common.synthetic_vols, make_queries.load_queries).

모델·지시문 비교 (https://github.com/reddol18/docflow/issues/22 조건 3):
  --reuse-retrieval <지난 답변.json>  검색을 다시 하지 않고 그 파일의 질의별 가져온 문서(같은 순서)를 그대로 넘긴다 —
                     임베딩 호출 없음, 답변 모델·지시문만 바뀐 비교가 된다. 넘길 글이 그 파일의 context와 다르면 멈춘다
  --base-url         OpenAI 호환 답변 모델 (https 또는 http 루프백 — 이 PC Ollama·SSH 터널). 토큰 단가 없음(0원, GPU 시간은 따로)
  --answer-prompt    답변 지시문 판 (ANSWER_PROMPTS, 기본 current = 운영 app/llm.ANSWER_PROMPT)

실행 (doc_platform 폴더, 합성 work의 평가 가상환경 — eval/embed_compare.py와 같은 환경):
  $py = '..\\work_synth\\w47\\promptfoo\\.venv\\Scripts\\python.exe'
  & $py -I -X utf8 eval/answer/gen_answers.py --work ..\\work_synth\\w48_21\\work --label gpt4omini --max-usd 1
  # 두 번째 후보 (같은 검색 결과로 답만):
  & $py -I -X utf8 eval/answer/gen_answers.py --work ..\\work_synth\\w48_21\\work --label gpt41mini_current `
      --model gpt-4.1-mini --reuse-retrieval ..\\work_synth\\w48_21\\work\\answers\\gpt4omini_20261122.json --max-usd 0.2
"""
import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

EVAL =Path(__file__).resolve().parents[1]
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


def answer_prompts():
    """답변 지시문 판 {이름: system 글}. current = 운영 지시문. 새 판은 여기에 더한다 (이름은 결과 파일·행렬 표에 남는다)"""
    from app import llm  # noqa: PLC0415
    return {"current": llm.ANSWER_PROMPT}


def prompt_name_of(answers):
    """답변 파일의 지시문 판 이름 — 이름이 없는 옛 파일(#21)은 지시문 해시로 찾고, 모르면 'sha:<앞 8자>'"""
    if answers.get("answer_prompt"):
        return answers["answer_prompt"]
    sha = (answers.get("settings") or {}).get("answer_prompt_sha256") or ""
    for name, text in answer_prompts().items():
        if ec.digest(text) == sha:
            return name
    return f"sha:{sha[:8]}"


def reuse_hits(prev_row, bodies):
    """지난 답변 파일의 질의 한 줄 → 같은 문서·같은 순서의 hits (ask_hits 꼴). 넘길 글이 그때 context와 다르면 멈춘다
    (work의 OCR·칸 값이 바뀐 것 — 같은 검색 결과라고 할 수 없다)"""
    hits = []
    for c in prev_row["retrieved"]:
        vol, doc_id = c["key"].split("/", 1)
        if (vol, doc_id) not in bodies:
            raise SystemExit(f"지난 검색 결과의 문서가 work에 없다: {c['key']}")
        h = {"doc_id": doc_id, "rrf": c.get("rrf"), "vector_rank": c.get("vector_rank"), "keyword_rank": c.get("keyword_rank"),
             "vol": vol, "fields": c["fields"], "body": bodies[(vol, doc_id)]}
        if context_of(h) != c["context"]:
            raise SystemExit(f"{prev_row['id']} {c['key']}: 넘길 글이 지난 답변 파일과 다르다 — 같은 work로 만든 파일을 쓴다")
        hits.append(h)
    return hits


def load_reuse(path, queries, sel=None):
    """--reuse-retrieval 파일 → {질의 id: 줄}. 질의 세트 해시는 전체(queries)로, 빠진 질의는 이번에 고른 것(sel)으로 본다"""
    prev = json.loads(Path(path).read_text(encoding="utf-8"))
    if prev.get("queries_sha256") != ec.digest(queries):
        raise SystemExit("--reuse-retrieval 파일의 질의 세트가 이번 질의 세트와 다르다")
    rows = {r["id"]: r for r in prev["rows"]}
    missing = [q["id"] for q in (queries if sel is None else sel) if q["id"] not in rows]
    if missing:
        raise SystemExit(f"--reuse-retrieval 파일에 없는 질의: {missing[:5]}")
    return prev, rows


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
    lat = [r["latency_ms"] for r in ok if r.get("latency_ms") is not None]
    return {"n": len(rows), "failed": len(rows) - len(ok),
            "usd_per_query": round(sum(r["usd"] for r in ok) / len(ok), 8) if ok else None,
            "latency_ms_mean": round(sum(lat) / len(lat)) if lat else None,
            "latency_ms_p95": cc.p95(lat) if lat else None,
            "prompt_tokens": sum(r["usage"]["prompt_tokens"] for r in ok),
            "completion_tokens": sum(r["usage"]["completion_tokens"] for r in ok),
            "usd": round(sum(r["usd"] for r in ok), 6),
            "models": sorted({r["model"] for r in ok}),
            "retrieved_all_sources": f"{len(hit)}/{len(ans)}",
            "dropped_citations": sum(r["dropped_citations"] for r in ok)}


def answer_one(client, model, q, hits, budget, system_prompt=None, priced=True):
    """한 질의 답변 — app/llm.answer와 같은 메시지·설정 (system_prompt를 주면 system 글만 그 판으로).
    priced(OpenAI)면 예산(--max-usd)을 넘을 호출은 부르지 않는다. 로컬·원격 GPU 모델(priced=False)은 0원.
    latency_ms = 호출 한 번의 벽시계 시간 (재시도 포함)"""
    from app import llm  # noqa: PLC0415
    messages = llm.answer_messages(q["question"], hits, ec.SCHEMA)
    if system_prompt is not None:
        messages[0] = {"role": "system", "content": system_prompt}
    est = llm.estimate_usd(model, llm.tokens_max(*(m["content"] for m in messages), messages=len(messages)),
                           llm.ANSWER_MAX_TOKENS) if priced else 0.0
    if not budget.reserve(est):
        raise SystemExit(f"OpenAI 지출 상한 ${budget.cap}을 넘어 멈춤 (쓴 돈 ${budget.spent:.6f})")
    usd = 0.0
    try:
        t0 = time.perf_counter()
        try:
            r = client.chat.completions.create(model=model, temperature=0, max_tokens=llm.ANSWER_MAX_TOKENS, messages=messages)
        except Exception as e:  # noqa: BLE001 — SDK 오류 문구는 찍지 않고 종류만 (키 일부가 들 수 있다)
            return {"error": type(e).__name__}
        latency_ms = round((time.perf_counter() - t0) * 1000)
        u = r.usage
        if priced:
            pin, pout = llm.price_of(model)
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
            "usd": round(usd, 8), "latency_ms": latency_ms}


PAID_SUFFIXES = (".openai.com", ".openai.azure.com")  # 단가가 붙는 OpenAI·Azure OpenAI — --base-url(0원)로 못 부른다


def paid_host(base_url):
    """주소의 호스트가 OpenAI(지역 엔드포인트·Azure 포함)면 True — 대소문자·끝의 '.'·포트 무관"""
    host = (urlsplit(base_url).hostname or "").rstrip(".").lower()
    return host == "openai.com" or host.endswith(PAID_SUFFIXES)


def main(argv=None):
    from app import llm  # noqa: PLC0415
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--work", required=True, help="합성 work 폴더 (eval/synth/to_work.py 출력)")
    ap.add_argument("--queries", default=str(QUERIES))
    ap.add_argument("--label", required=True)
    ap.add_argument("--model", default=llm.OPENAI_MODELS["chat"], help="답변 모델 (기본: app CHAT_MODEL 기본값)")
    ap.add_argument("--embed-model", default=llm.OPENAI_MODELS["embed"])
    ap.add_argument("--max-usd", type=float, default=1.0, help="OpenAI 지출 상한 (이번 실행, 임베딩 포함)")
    ap.add_argument("--base-url", default="", help="OpenAI 호환 답변 모델 주소 (https 또는 http://127.0.0.1:<포트>/v1). 비우면 OpenAI")
    ap.add_argument("--reuse-retrieval", default="", help="지난 답변 JSON — 검색을 다시 하지 않고 그 가져온 문서를 그대로 넘긴다")
    ap.add_argument("--answer-prompt", default="current", help="답변 지시문 판 (answer_prompts())")
    ap.add_argument("--ids", default="", help="쉼표로 질의 고르기 (시험 실행)")
    a = ap.parse_args(argv)
    label = cc.label_ok(a.label)
    prompts = answer_prompts()
    if a.answer_prompt not in prompts:
        raise SystemExit(f"모르는 답변 지시문 판: {a.answer_prompt} (가능: {', '.join(prompts)})")
    system_prompt = prompts[a.answer_prompt]
    vols = cc.synthetic_vols(a.work)
    docs, queries = ec.load_docs(a.work, vols), mq.load_queries(a.queries)  # 합성이 아니면 여기서 멈춘다 (부르기 전)
    check_sources(queries, [d["key"] for d in docs])
    q_digest = ec.digest(queries)  # 질의 세트 전체의 해시 (--ids로 골라도 — 같은 세트에서 골랐음을 남긴다)
    if a.ids:
        want = [i.strip() for i in a.ids.split(",") if i.strip()]
        by_id = {q["id"]: q for q in queries}
        bad = [i for i in want if i not in by_id]
        if bad:
            raise SystemExit(f"질의 세트에 없는 id: {bad}")
        sel = [by_id[i] for i in want]
    else:
        sel = queries
    prev_rows = load_reuse(a.reuse_retrieval, queries, sel)[1] if a.reuse_retrieval else None
    out = cc.out_dir(a.work, "answers")
    bodies = {d["key"]: " ".join(cc.page_ocr(a.work, d["key"][0], d["key"][1])["lines"][:BODY_LINES]) for d in docs}
    by_key = {d["key"]: d for d in docs}
    remote = bool(a.base_url)
    # 0원으로 세지 않게 — 대소문자·:443·경로 차이로 빠져나가지 않도록 호스트로 본다
    if remote and paid_host(cc.check_base_url(a.base_url)):
        raise SystemExit("OpenAI는 --base-url 없이 부른다 (단가·지출 상한 적용)")
    # 검색을 다시 할 때만 OpenAI 임베딩 클라이언트가 필요하다 (답변이 로컬 모델이어도)
    eclient = cc.make_client(True, None)[0] if prev_rows is None else None
    client = cc.make_client(False, a.base_url)[0] if remote else (eclient or cc.make_client(True, None)[0])
    budget = cc.Budget(a.max_usd)
    t0 = time.time()
    if prev_rows is None:
        dvecs, et1, eu1 = ec.embed(eclient, a.embed_model, [d["text"] for d in docs], True, budget)
        qvecs, et2, eu2 = ec.embed(eclient, a.embed_model, [q["question"] for q in sel], True, budget)
    else:
        qvecs, et1, eu1, et2, eu2 = [None] * len(sel), 0, 0.0, 0, 0.0
    rows = []
    for q, qv in zip(sel, qvecs):
        if prev_rows is None:
            # app/main.rank와 같은 셈 (RRF + 문서번호 정확 일치 고정, https://github.com/reddol18/docflow/issues/61).
            # /ask는 문서번호가 없어도 '없음'으로 비우지 않는다 (2026-11-24 결정)
            r, _ = ec.app_rank(q["question"], qv, docs, dvecs)  # 질의 연도로 비면 연도 없이 다시 (app_rank 안에서)
            hits = ask_hits(r.order, by_key, bodies, r.score, r.vector, r.keyword)
        else:
            hits = reuse_hits(prev_rows[q["id"]], bodies)
        res = answer_one(client, a.model, q, hits, budget, system_prompt=system_prompt, priced=not remote)
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
    # 검색을 다시 쓴 후보 답변은 #22 모델 비교, 검색부터 한 답변은 #21 심판 검증용
    issue = "https://github.com/reddol18/docflow/issues/" + ("22" if prev_rows is not None else "21")
    res = {"label": label, "issue": issue, "chat_model": a.model,
           "answer_prompt": a.answer_prompt, "base_url": cc.check_base_url(a.base_url) if remote else cc.OPENAI_BASE,
           "priced": not remote, "reuse_retrieval": Path(a.reuse_retrieval).name if a.reuse_retrieval else None,
           "ids": [q["id"] for q in sel] if a.ids else None,
           # 검색을 다시 썼으면 임베딩을 부르지 않았다 — 그때 모델은 reuse_retrieval 파일에 있다
           "embed_model": a.embed_model if prev_rows is None else None, "vols": vols, "docs": len(docs),
           "queries_file": Path(a.queries).name,
           "queries_sha256": q_digest, "settings": {"top_k": TOP_K, "body_lines": BODY_LINES, "body_chars": BODY_CHARS,
                                                    "temperature": 0, "max_tokens": llm.ANSWER_MAX_TOKENS,
                                                    "answer_prompt_sha256": ec.digest(system_prompt)},
           "totals": tot, "rows": rows}
    p = out / f"{label}.json"
    p.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(tot, ensure_ascii=False))
    print(f"결과: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
