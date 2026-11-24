"""합성 철 검색 채점 (https://github.com/reddol18/docflow/issues/61 — 검색 개선 측정, 2026-11-24 결정: 합성 데이터로).

합성 질의 세트마다 /search 순위를 메모리 안에서 다시 내고(Postgres·Qdrant에 넣지 않음) 정답 문서와 맞춰 본다.
  순위     벡터(코사인 30) · 낱말(30) · rrf(예전 /search = RRF만) · app(/search = app/retrieval.rank, --fusion 설정:
           질의 다듬기·질의 연도·낱말 가중, 문서번호 정확 일치 고정, '없음'이면 빈 목록).
           합치기는 운영과 같은 app/retrieval 함수 (eval/embed_compare.app_rank) — 복사본이 아니다.
           설정 후보 16개(VARIANTS)의 머리 지표도 함께 낸다 (설정별 표)
  문서 글  --compose: current(= app/graph.doc_text 꼴: 4칸 + 첫 쪽 OCR 60줄) · fields(4칸만) · body(본문만) ·
           rich(current + 한글 날짜·기관 줄)
  지표     유형별 recall@1·@5·@10, MRR(전체 순위), 근거 모두 상위 5 (정답이 있는 질의만)
           '없음' 임계값 훑기: 벡터 1위 유사도 < t 이면 '없음'(app/retrieval.no_match) — 답 없음 질의의 정밀도·재현율,
           답 있는 질의를 잘못 '없음'으로 본 수
  임계값   --pool [--max-false N] A.json B.json: 같은 모델·글·설정 결과를 합쳐 보수적 임계값(잘못 '없음' N 이하인 가장 큰 값),
           하나 빼고 고르기, 회사·대표·도시 없음(합격 조건 밖)을 따로
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


def _date_ko(v):
    """'1995-12-24' → '1995년 12월 24일' (아는 부분만)"""
    p = [x for x in str(v or "").split("-") if x.isdigit()]
    return " ".join(f"{int(x) if i else x}{u}" for i, (x, u) in enumerate(zip(p, ("년", "월", "일"))))


def _org(d):
    """첫 쪽 OCR 첫 줄(기관 이름 — '새 솔 시'처럼 글자 사이가 벌어진 머리)을 공백 없이. 10자 넘으면 기관 줄이 아니라고 본다"""
    first = re.sub(r"\s+", "", (d["lines"] or [""])[0])
    return first if 0 < len(first) <= 10 else ""


def _rich(d):
    """current + 한글 날짜·기관 줄: 질의의 '1995년 12월 24일'·'새솔시'가 문서 글에 그대로 있게"""
    head = ec.fields.embed_text(ec.SCHEMA, d["values"])
    extra = [f"시행일자: {_date_ko(d['values'].get('date'))}"] if _date_ko(d["values"].get("date")) else []
    extra += [f"기관: {_org(d)}"] if _org(d) else []
    return "\n".join([head, *extra, "본문: " + " ".join(d["lines"])])


# 문서 글 짜기 (4단계 후보). current = 운영 app/graph.doc_text 꼴
COMPOSE = {"current": lambda d: d["text"],
           "fields": lambda d: ec.fields.embed_text(ec.SCHEMA, d["values"]),
           "body": lambda d: "본문: " + " ".join(d["lines"]),
           "rich": _rich}
METHODS = ("vector", "keyword", "rrf", "app")
# 6단계 설정 후보: 질의 다듬기 × 질의 연도 × 낱말 가중 (kw0 = 문서번호 아닌 질의는 벡터만)
VARIANTS = {c.name: c for c in (retrieval.Config(clean, year, w) for clean in (False, True) for year in (False, True)
                                for w in (1.0, 0.5, 0.25, 0.0))}
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


def predict_none(row, t):
    """이 질의를 /search가 '없음'으로 볼지 — app/retrieval.no_match (문서번호 없음·후보 없음 + 임계값).
    _ranking이 없는 줄(저장된 결과 파일)은 저장한 docno_miss·pinned로 같은 규칙"""
    r = row.get("_ranking")
    if r is not None:
        return retrieval.no_match(r, row["top_sim"], t)
    if row["docno_miss"]:
        return True
    if row["pinned"] or t is None or row["top_sim"] is None:
        return False
    return row["top_sim"] < t


def sweep(rows, thresholds=SWEEP):
    """임계값마다 {t, none_n, tp, precision, recall, false_none} — 답 없음 = gold 없는 질의. 결과가 앞 줄과 같으면 뺀다"""
    neg = [r for r in rows if not r["answerable"]]
    out, last = [], None
    for t in thresholds:
        pred = [r for r in rows if predict_none(r, t)]
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


EXCLUDED_NONE = ("없는 회사", "없는 대표", "없는 도시")  # 합격 조건에서 빼고 따로 보고 (2026-11-24 결정)


def scored_sims(rows):
    """임계값이 가를 답 있는 질의의 1위 유사도 — 문서번호가 맞았거나 이미 '없음'(문서번호 없음·후보 없음)인 질의는 빼고"""
    return sorted(r["top_sim"] for r in rows if r["answerable"] and r["top_sim"] is not None
                  and not r["pinned"] and not predict_none(r, None))


def pick_threshold(rows, max_false=0):
    """보수적 임계값: 답 있는 질의를 잘못 '없음'으로 보는 수가 max_false 이하인 가장 큰 값 = (max_false+1)번째로 작은
    답 있는 질의 1위 유사도 (그보다 작으면 '없음'). 답 있는 질의가 없으면 None"""
    sims = scored_sims(rows)
    return sims[max_false] if len(sims) > max_false else None


def loo(rows, max_false=0):
    """하나 빼고 고르기: 질의마다 나머지로 임계값을 골라 그 질의를 판정 → {tp, n_neg, false_none, ids}"""
    tp, fn_ids, hit_ids = 0, [], []
    for i, r in enumerate(rows):
        t = pick_threshold(rows[:i] + rows[i + 1:], max_false)
        if predict_none(r, t):
            if r["answerable"]:
                fn_ids.append(r["id"])
            else:
                tp += 1
                hit_ids.append(r["id"])
    return {"tp": tp, "n_neg": sum(not r["answerable"] for r in rows), "false_none": len(fn_ids),
            "false_none_ids": fn_ids, "none_ids": hit_ids}


def none_report(rows, t):
    """임계값 t에서 답 없음 질의 판정 — 합격 조건 몫(EXCLUDED_NONE 뺀 것)과 뺀 몫 따로"""
    neg = [r for r in rows if not r["answerable"]]
    core = [r for r in neg if not any(x in r["type"] for x in EXCLUDED_NONE)]
    excl = [r for r in neg if r not in core]
    pos = [r for r in rows if r["answerable"]]
    return {"t": t, "all": f"{sum(predict_none(r, t) for r in neg)}/{len(neg)}",
            "core": f"{sum(predict_none(r, t) for r in core)}/{len(core)}",
            "excluded": f"{sum(predict_none(r, t) for r in excl)}/{len(excl)}",
            "false_none": [r["id"] for r in pos if predict_none(r, t)],
            "per_query": [(r["id"], r["type"], r["top_sim"], predict_none(r, t)) for r in neg]}


def pool_main(paths, max_false):
    """저장한 결과 파일(같은 모델·설정)을 합쳐 '없음' 임계값을 고른다 (7단계)"""
    rows, meta = [], set()
    for p in paths:
        res = json.loads(Path(p).read_text(encoding="utf-8"))
        meta.add((res["model"], res["compose"], res["fusion"]))
        rows += [{**r, "id": f"{r['id']}"} for r in res["rows"]]
    if len(meta) != 1:
        raise SystemExit(f"모델·글 짜기·설정이 다른 결과는 합치지 않는다: {sorted(meta)}")
    t = pick_threshold(rows, max_false)
    rep, lo = none_report(rows, t), loo(rows, max_false)
    print(f"모델·글·설정 {meta.pop()}, 질의 {len(rows)}, 잘못 '없음' 허용 {max_false} → 임계값 {t}")
    print(sweep_table(sweep(rows)))
    print()
    print(f"고른 임계값 {t}: 답 없음 {rep['all']} (합격 조건 몫 {rep['core']}, 회사·대표·도시 없음 {rep['excluded']}), "
          f"잘못 '없음' {rep['false_none']}")
    print(f"하나 빼고 고르기: 답 없음 {lo['tp']}/{lo['n_neg']} {lo['none_ids']}, 잘못 '없음' {lo['false_none']} {lo['false_none_ids']}")
    print("| id | 유형 | 1위 유사도 | '없음' |\n|---|---|---|---|")
    for qid, typ, sim, pred in rep["per_query"]:
        print(f"| {qid} | {typ} | {sim} | {pred} |")
    return {"threshold": t, "report": rep, "loo": lo}


# ---------- 순위 ----------

BASE = retrieval.Config()  # 예전 /search: 그대로 나눈 낱말, RRF 가중 1


def rank_queries(queries, docs, dvecs, qvecs, cfg=None):
    """질의별 줄: 네 방식 전체 순위('vol/doc_id'), 1위 유사도, 문서번호, 지표.
    vector·keyword·rrf = 예전 /search (다듬지 않은 낱말, 문서번호 고정 없음), app = cfg로 낸 /search 순서
    (문서번호 고정, '없음'이면 빈 목록 — 임계값 없이)"""
    cfg = cfg or retrieval.SEARCH
    rows = []
    for q, qv in zip(queries, qvecs):
        b, _ = ec.app_rank(q["q"], qv, docs, dvecs, q["year"], cfg=BASE)
        r, sims = ec.app_rank(q["q"], qv, docs, dvecs, q["year"], cfg=cfg)
        miss = retrieval.no_match(r)  # /search: 문서번호 질의인데 그 번호가 없거나 후보가 없으면 빈 목록
        ranks = {"vector": b.vector, "keyword": b.keyword, "rrf": retrieval.fuse(b.vector, b.keyword)[1],
                 "app": [] if miss else r.order}
        ranks = {m: ["/".join(k) for k in v] for m, v in ranks.items()}
        rows.append({"id": q["id"], "type": q["type"], "q": q["q"], "year": q["year"], "answerable": q["answerable"],
                     "gold": q["gold"], "top_sim": round(sims[0], 4) if sims else None, "year_used": r.year,
                     "terms": retrieval.query_terms(q["q"], cfg),
                     "docnos": [" ".join(d) for d in r.docnos], "pinned": ["/".join(k) for k in r.pinned],
                     "docno_miss": miss, "ranks": ranks,
                     "metrics": {m: query_metrics(ranks[m], q["gold"]) for m in METHODS} if q["gold"] else None,
                     "_ranking": r})
    return rows


def variant_row(rows):
    """한 설정의 머리 지표: 근거 모두 상위5, 평균 R@5·MRR, 문서번호 질의 MRR, 유형별 R@5 (app 순위)"""
    s = summarize(rows, ("app",))
    dn = [r["metrics"]["app"]["mrr"] for r in rows if r["gold"] and r["docnos"]]
    return {"all5": s[ALL]["app"]["all5"], "n": s[ALL]["app"]["n"], "r5": s[ALL]["app"]["r5"], "mrr": s[ALL]["app"]["mrr"],
            "docno_mrr": round(statistics.mean(dn), 3) if dn else None,
            "type_r5": {t: v["app"]["r5"] for t, v in s.items() if t != ALL},
            "type_all5": {t: v["app"]["all5"] for t, v in s.items() if t != ALL}}


def variants_table(vs):
    types = list(next(iter(vs.values()))["type_r5"])
    lines = ["| 설정 | 근거 모두 상위5 | R@5 | MRR | 문서번호 MRR | " + " | ".join(f"{t} R@5" for t in types) + " |",
             "|---|---|---|---|---|" + "---|" * len(types)]
    for name, v in vs.items():
        lines.append(f"| {name} | {v['all5']}/{v['n']} | {v['r5']} | {v['mrr']} | {v['docno_mrr']} | "
                     + " | ".join(str(v["type_r5"][t]) for t in types) + " |")
    return "\n".join(lines)


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
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "--pool":  # --pool [--max-false N] 결과.json ...
        rest = argv[1:]
        max_false = 0
        if rest[:1] == ["--max-false"]:
            if len(rest) < 2 or not rest[1].isdigit():
                raise SystemExit("--pool --max-false <0 이상 정수> 결과.json ...")
            max_false, rest = int(rest[1]), rest[2:]
        if not rest:
            raise SystemExit("--pool에 결과 파일(JSON)을 하나 이상 준다")
        pool_main(rest, max_false)
        return 0
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--work", required=True, help="합성 work 폴더 (eval/synth/to_work.py 출력)")
    ap.add_argument("--queries", required=True, help="합성 질의 파일 (synth_answer_queries·synth_search_queries 형식)")
    ap.add_argument("--model", default=llm.OPENAI_MODELS["embed"], help="임베딩 모델")
    ap.add_argument("--base-url", default="", help="OpenAI 호환 임베딩 주소 (https 또는 http://127.0.0.1:<포트>/v1). 비우면 OpenAI")
    ap.add_argument("--compose", default="current", choices=sorted(COMPOSE), help="문서 글 짜기")
    ap.add_argument("--fusion", default=retrieval.SEARCH.name, choices=sorted(VARIANTS),
                    help="app 순위의 검색 설정 (질의 다듬기·질의 연도·낱말 가중, retrieval.Config.name). 기본: 운영 설정")
    ap.add_argument("--max-usd", type=float, default=0.2, help="OpenAI 지출 상한 (이번 실행, 캐시에 없는 글만 부른다)")
    ap.add_argument("--label", default="", help="결과 파일 이름 (비우면 <질의 파일>_<모델>_<compose>)")
    a = ap.parse_args(argv)
    vols = cc.synthetic_vols(a.work)  # 합성 철이 아니면 여기서 멈춘다 (부르기 전)
    docs = ec.load_docs(a.work, vols)
    queries = load_set(a.queries, docs)  # 합성 질의가 아니면 여기서 멈춘다 (부르기 전)
    label = cc.label_ok(a.label or f"{Path(a.queries).stem}_{model_dir(a.model)}_{a.compose}_{a.fusion}")
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
    rows = rank_queries(queries, docs, dvecs, qvecs, VARIANTS[a.fusion])
    summary = summarize(rows)
    sw = sweep(rows)
    vs = {name: variant_row(rank_queries(queries, docs, dvecs, qvecs, cfg)) for name, cfg in VARIANTS.items()}
    res = {"label": label, "issue": "https://github.com/reddol18/docflow/issues/61", "model": a.model,
           "where": cc.check_base_url(a.base_url).split("/v1")[0] if remote else "OpenAI", "compose": a.compose,
           "fusion": a.fusion, "queries_file": Path(a.queries).name, "queries_sha256": ec.digest(queries),
           "vols": vols, "docs": len(docs), "dim": len(dvecs[0]) if dvecs else None,
           "embedded": m1 + m2,
           "cached": len({compose(d) for d in docs}) + len({q["q"] for q in queries}) - m1 - m2, "tokens": t1 + t2,
           "usd": round(u1 + u2, 6), "wall_s": round(time.time() - t0, 1),
           "summary": summary, "sweep": sw, "variants": vs, "rows": [{k: v for k, v in r.items() if k != "_ranking"} for r in rows]}
    p = out / f"{label}.json"
    p.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"합성 철 {len(vols)}개, 문서 {len(docs)}, 질의 {len(queries)}, 차원 {res['dim']}, "
          f"새로 임베딩 {res['embedded']} (캐시 {res['cached']}), 토큰 {res['tokens']:,}, 비용 ${res['usd']:.6f}")
    print(type_table(summary))
    print()
    print("설정별 (app 순위)")
    print(variants_table(vs))
    print()
    print(f"'없음' 훑기 ({a.fusion})")
    print(sweep_table(sw))
    print()
    print(unanswerable_table(rows))
    print(f"결과: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
