"""promptfoo 결과 → 후보(제공자 × 지시문 판)마다 철 단위 점수·건당 비용·지연, 그리고 통과 기준 판정.

실행 (run.ps1이 부른다):
  python -I -X utf8 eval/promptfoo/gate.py <promptfoo -o 결과.json> [--criteria criteria.yaml] [--out 폴더] [--expect-stored]

후보마다 LLM 응답(추출 값 b)만 바꾸고 나머지(손글씨 판독·머리부 비율·칸 위치 실패)는 저장된 철 결과 그대로 둔 채
수신을 b로 다시 보정(layout.fix_recipient, app/graph.py extract와 같음)하고 app/graph.py merge·validate를 다시 돌린다
(eval/schema_replay.py replay()를 그대로 씀) → eval/score.py metrics()로 채점.
그래서 칸별 최종 정답률·검토 표시 재현율·표시 없이 틀린 비율이 eval/score.py 단독 실행과 같은 정의다.
  --expect-stored  점수가 저장된 b·hw를 같은 길(replay + 수신 다시 보정)로 다시 조립한 결과의 점수와 같은지 확인
                   (replay 제공자로 채점 연결 검사 — 다른 점은 응답 글자를 읽는 부분뿐)
  --baseline-results <지난 결과.json>  기준 후보(criteria.yaml baseline) 줄을 지난 실행 결과에서 가져온다 → 기준 모델을 다시 부르지 않고
                   로컬 모델만 돌려 비교 (https://github.com/reddol18/docflow/issues/13). 이번 결과에 있는 철만 가져온다.
                   종료 코드 2: 이번 결과에 기준이 이미 있음, 기준 모델 id·temperature·response_format·apiBaseUrl이 이번 설정과 다름,
                   같은 판 후보의 지시문(prompt.raw)이 문서마다 기준과 다름, 후보 문서 묶음이 기준과 다름.
                   그 밖의 설정 차이(max_tokens 등)는 gate.md에 경고로 남긴다.
건당 비용: openai:chat:<모델>의 모델이 app/llm.py PRICE에 있으면 그 단가, 로컬 모델(apiBaseUrl이 127.0.0.1·localhost,
또는 label local-ollama)·replay는 0. 그 밖의 단가 모르는 모델이 있으면 0원으로 치지 않고 종료 코드 2.
종료 코드: 0 모두 통과, 1 기준을 어긴 후보가 있음(또는 --expect-stored 불일치), 2 입력 문제(기준 후보 없음·단가 모름 등)
"""
import argparse
import json
import math
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))  # python -I 로 돌려도 옆 모듈을 찾게
import common  # noqa: E402
import yaml  # noqa: E402
from prompts import SCHEMA  # noqa: E402
from schema_replay import replay  # noqa: E402

from app import fields, graph, layout, llm  # noqa: E402

EMPTY = {f["name"]: {"value": "", "confidence": 0.0} for f in fields.prompt_fields(SCHEMA)}
# 채점 칸 = eval/score.py FIELDS와 같은 정의 (score는 정답을 읽으므로 metrics()에서만 늦게 불러온다 → 여기서 다시 셈)
FIELDS = list(fields.editable(SCHEMA))
OPENAI = "openai:chat:"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def metrics(docs):
    # 채점기(eval/score.py)는 정답·PoC 채점 규칙(records_poc/scripts/rescore.py)을 불러오므로 쓸 때 불러온다
    from score import metrics as m  # noqa: PLC0415
    return m(docs)


def load_results(path):
    """promptfoo -o 결과 → (결과 줄 목록, {제공자 label: config}, {제공자 label: id}).
    config로 로컬 모델(apiBaseUrl)을 가린다. promptfoo는 --filter-providers로 거른 제공자도 config.providers에 모두 남긴다"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data.get("results", data) if isinstance(data, dict) else data
    rows = rows.get("results", rows) if isinstance(rows, dict) else rows
    provs = ((data.get("config") or {}).get("providers") or []) if isinstance(data, dict) else []
    provs = [p for p in provs if isinstance(p, dict)]
    cfg = {p.get("label") or p.get("id"): p.get("config") or {} for p in provs}
    ids = {p.get("label") or p.get("id"): p.get("id") for p in provs}
    return rows, cfg, ids


def key_of(r):
    prov = r.get("provider") or {}
    prov = prov.get("label") or prov.get("id") if isinstance(prov, dict) else str(prov)
    prompt = r.get("prompt") or {}
    return prov, prompt.get("label") or prompt.get("id") or "?"


def price_of(r, cfg=None):
    """(입력, 출력) USD / 1M 토큰. 로컬 모델·replay = (0, 0), 단가 모르는 원격 모델 = None.
    모델 = 'openai:chat:' 뒤 전부 (llama3.1:8b, ft:gpt-4.1-mini:... 처럼 ':'가 든 이름도 그대로)"""
    prov = r.get("provider") or {}
    pid = prov.get("id", "") if isinstance(prov, dict) else str(prov)
    label = prov.get("label") if isinstance(prov, dict) else None
    if pid.startswith("file://"):  # replay.py — LLM 호출 없음
        return (0, 0)
    if not pid.startswith(OPENAI):
        return None
    base = ((cfg or {}).get(label or pid) or {}).get("apiBaseUrl") or ""
    if label == "local-ollama" or (base and urlparse(base).hostname in LOCAL_HOSTS):
        return (0, 0)
    return llm.PRICE.get(pid[len(OPENAI):])


def unpriced(rows, cfg=None):
    """단가를 모르는 제공자 id 목록"""
    return sorted({(r.get("provider") or {}).get("id", "?") for r in rows if price_of(r, cfg) is None})


def group(rows):
    """{(제공자, 판): {doc_id: 결과 줄}}"""
    out = {}
    for r in rows:
        out.setdefault(key_of(r), {})[r["vars"]["doc_id"]] = r
    return out


def response_of(r):
    """후보 응답 글자. 제공자 오류(응답 없음)면 None.
    promptfoo는 단언(check.py)이 실패·예외를 내도 r["error"]를 채우므로 그것만으로 응답을 버리지 않는다 — 응답이 있으면 읽는다."""
    if r is None:
        return None
    resp = r.get("response") or {}
    if resp.get("error"):
        return None
    o = resp.get("output")
    return o if isinstance(o, str) else (json.dumps(o, ensure_ascii=False) if o is not None else None)


def assert_failed(results):
    """응답은 있는데 단언(check.py)이 실패·예외를 낸 줄 수 (참고용 — 판정은 gate.py 채점으로)"""
    return sum(1 for r in results.values() if r.get("error") and response_of(r) is not None)


def _recipient_fixer(ocrs):
    def fix(d, b):
        return layout.fix_recipient(ocrs[d["doc_id"]], graph._llm_of(b, "recipient"))
    return fix


def assemble(data, b_of=None):
    """{vol: (저장 문서, {doc_id: OCR})} → ({doc_id: 문서}, {vol: [문서]}).
    기준(저장 b, --expect-stored)과 후보가 같은 길: schema_replay.replay + 수신을 b로 다시 보정"""
    by_vol = {vol: replay(graph, stored, b_of=b_of, recipient_of=_recipient_fixer(ocrs))
              for vol, (stored, ocrs) in data.items()}
    return {x["doc_id"]: x for ds in by_vol.values() for x in ds}, by_vol


def rebuild(results, data):
    """후보 응답으로 철마다 다시 조립. 반환: ({doc_id: 문서}, {vol: [문서]}, 읽기 실패 수)"""
    errors = 0

    def b_of(d):
        nonlocal errors
        out = response_of(results.get(d["doc_id"]))
        try:
            if out is None:
                raise ValueError("응답 없음")
            # 운영과 같은 판정 (https://github.com/reddol18/docflow/issues/27): 틀린 칸은 빈 값, JSON 객체가 아니면 읽기 실패
            return dict(fields.validate_extract(SCHEMA, out))
        except (ValueError, TypeError):
            errors += 1
            return json.loads(json.dumps(EMPTY))

    docs, by_vol = assemble(data, b_of)
    return docs, by_vol, errors


def rate(pair):
    a, b = pair
    return 100.0 * a / b if b else 0.0


def cost_latency(results, cfg=None):
    """단가 모르는 원격 모델이 섞이면 ValueError (main은 그 전에 unpriced()로 종료 코드 2)"""
    usd, lat, n = 0.0, [], 0
    for r in results.values():
        n += 1
        tu = (r.get("response") or {}).get("tokenUsage") or {}
        price = price_of(r, cfg)
        if price is None:
            raise ValueError(f"단가 모름: {(r.get('provider') or {}).get('id')}")
        pi, po = price
        usd += ((tu.get("prompt") or 0) * pi + (tu.get("completion") or 0) * po) / 1e6
        if r.get("latencyMs") is not None and not (r.get("response") or {}).get("cached"):
            lat.append(r["latencyMs"])
    lat.sort()
    p95 = lat[min(len(lat) - 1, math.ceil(0.95 * len(lat)) - 1)] if lat else None
    return {"usd_per_doc": usd / n if n else 0.0, "usd": usd, "ms_mean": sum(lat) / len(lat) if lat else None,
            "ms_p95": p95, "n": n, "n_timed": len(lat)}


def summary(m):
    return {"fields": {f: rate(m["fields"]["final"][f]) for f in FIELDS},
            "b_fields": {f: rate(m["fields"]["B"][f]) for f in FIELDS},
            # 틀린 칸이 하나도 없으면 재현율은 정의되지 않는다 → None (표에 '-')
            "flag_recall": {k: rate(m["flag"][k]["recall"]) if m["flag"][k]["recall"][1] else None
                            for k in ("serial", "date")},
            "silent": rate(m["doc"]["silent"]), "all_ok": rate(m["doc"]["all_ok"])}


def judge(cand, base, crit):
    """기준을 어긴 이유 목록 (빈 목록 = 통과)"""
    ex = crit.get("extract") or {}
    why = []
    lim = ex.get("silent_wrong_pp_max")
    if lim is not None and cand["s"]["silent"] - base["s"]["silent"] > lim:
        why.append(f"표시 없이 틀린 문서 {base['s']['silent']:.1f}% → {cand['s']['silent']:.1f}% (+{lim}%p 초과)")
    lim = ex.get("field_drop_pp_max")
    if lim is not None:
        for f in FIELDS:
            if base["s"]["fields"][f] - cand["s"]["fields"][f] > lim:
                why.append(f"{f} 정답률 {base['s']['fields'][f]:.1f}% → {cand['s']['fields'][f]:.1f}% (-{lim}%p 초과)")
    lim = ex.get("parse_error_max")
    if lim is not None and cand["errors"] > lim:
        why.append(f"응답 읽기 실패 {cand['errors']}건 (> {lim})")
    return why


# 기준 제공자 설정이 지난 결과와 이번 설정(promptfooconfig.yaml)에서 다를 때: 이 값들이 다르면 같은 기준이 아니다 → 종료 코드 2.
# 그 밖의 값(max_tokens 등)은 다르면 gate.md에 경고로 남긴다 (지난 기준 파일은 max_tokens가 없어 promptfoo 기본 1024, 지금은 512)
STRICT_CFG = ("temperature", "response_format", "apiBaseUrl")


def add_baseline(groups, cfg, ids, path, want):
    """지난 결과(path)에서 기준 후보 want=(제공자 label, 판) 줄만 가져와 groups({(제공자, 판): {doc_id: 줄}})에 붙인다
    (이번 결과에 있는 철만). cfg·ids = 이번 결과의 {label: config}·{label: id} — 기준 제공자도 거르기 전 설정이 들어 있다.
    확인: 이번 결과에 기준이 없을 것, 모델 id·STRICT_CFG가 같을 것, 같은 판 후보는 문서마다 지시문(prompt.raw)이 같을 것,
    후보마다 문서 묶음이 기준과 같을 것. 반환: (groups, 경고 목록, 오류 글 또는 None)"""
    if want in groups:
        return groups, [], f"이번 결과에 기준 {want}이 이미 있다 — --baseline-results를 빼거나 기준을 거르고 돌린다"
    vols = {r["vars"]["vol"] for g in groups.values() for r in g.values()}
    brows, bcfg, _ = load_results(path)
    base = {r["vars"]["doc_id"]: r for r in brows if key_of(r) == want and r["vars"]["vol"] in vols}
    if not base:
        return groups, [], f"{Path(path).name}에 기준 {want} 결과가 없다 (철 {sorted(vols)})"
    used = {(r.get("provider") or {}).get("id") for r in base.values()}
    if want[0] not in ids or used != {ids[want[0]]}:
        return groups, [], f"기준 모델이 다르다: 지난 결과 {sorted(map(str, used))} / 이번 설정 {ids.get(want[0])}"
    warns = []
    old, now = bcfg.get(want[0]) or {}, cfg.get(want[0]) or {}
    for k in sorted(set(old) | set(now)):
        if old.get(k) == now.get(k):
            continue
        if k in STRICT_CFG:
            return groups, [], f"기준 설정 {k}가 다르다: 지난 결과 {old.get(k)} / 이번 설정 {now.get(k)}"
        warns.append(f"기준 설정 {k}: 지난 결과 {old.get(k, '없음(promptfoo 기본)')} / 이번 설정 {now.get(k, '없음')}")
    for k, g in groups.items():
        if set(g) != set(base):  # 같은 문서끼리가 아니면 비교가 틀린다
            return groups, warns, f"후보 {k}의 문서 {len(g)}건이 기준 문서 {len(base)}건과 다르다"
        if k[1] == want[1]:  # 같은 판이면 문서마다 모델에 간 지시문(시스템+사용자 글자)이 같아야 한다
            diff = sum(1 for d, r in g.items() if (r.get("prompt") or {}).get("raw") != (base[d].get("prompt") or {}).get("raw"))
            if diff:
                return groups, warns, f"후보 {k}의 지시문이 기준과 {diff}건 다르다 (지시문·문서 글자가 바뀜) — 기준을 다시 만든다"
    return {**groups, want: base}, warns, None


def _md(head, body):
    return "\n".join(["| " + " | ".join(head) + " |", "|" + "---|" * len(head)] + ["| " + " | ".join(c) + " |" for c in body])


def _pct(x):
    return "-" if x is None else f"{x:.1f}%"


def b_table(rows):
    """텍스트 추출(B)만의 칸별 정답률 — 손글씨 판독·규칙 합침 전 (docs/design.md '전체 680쪽' 표의 B 열과 같은 정의)"""
    return _md(["후보", "판"] + FIELDS, [[r["provider"], r["prompt"]] + [_pct(r["s"]["b_fields"][f]) for f in FIELDS] for r in rows])


def fmt_ms(x):
    return "-" if x is None else f"{x / 1000:.1f}s"


def table(rows):
    head = ["후보", "판"] + FIELDS + ["일련번호 표시 재현율", "일자 표시 재현율", "표시 없이 틀림",
                                   "읽기 실패", "건당 비용", "평균 지연", "p95 지연", "판정"]
    return _md(head, [[r["provider"], r["prompt"]] + [_pct(r["s"]["fields"][f]) for f in FIELDS] + [
        *(_pct(r["s"]["flag_recall"][k]) for k in ("serial", "date")), _pct(r["s"]["silent"]),
        str(r["errors"]), f"${r['c']['usd_per_doc']:.5f}", fmt_ms(r["c"]["ms_mean"]), fmt_ms(r["c"]["ms_p95"]),
        r["verdict"]] for r in rows])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("--criteria", default=str(common.HERE / "criteria.yaml"))
    ap.add_argument("--out", default=str(common.WORK / "promptfoo"))
    ap.add_argument("--expect-stored", action="store_true")
    ap.add_argument("--baseline-results", default="")
    a = ap.parse_args()
    crit = yaml.safe_load(Path(a.criteria).read_text(encoding="utf-8"))
    rows_, cfg, ids = load_results(a.results)
    groups = group(rows_)
    if not groups:
        print("결과가 비었다", file=sys.stderr)
        return 2
    warns = []
    if a.baseline_results:
        b = crit.get("baseline") or {}
        groups, warns, err = add_baseline(groups, cfg, ids, a.baseline_results, (b.get("provider"), b.get("prompt")))
        if err:
            print(err, file=sys.stderr)
            return 2
        rows_ = [r for g in groups.values() for r in g.values()]
    bad = unpriced(rows_, cfg)
    if bad:  # 0원으로 치면 비용 기준·표가 조용히 틀린다
        print(f"단가를 모르는 모델: {bad} — app/llm.py PRICE에 넣거나 로컬 모델이면 apiBaseUrl을 127.0.0.1로", file=sys.stderr)
        return 2
    vols = sorted({r["vars"]["vol"] for g in groups.values() for r in g.values()})
    data = {vol: common.load_vol(vol) for vol in vols}  # 철마다 한 번만 읽어 후보 모두에 쓴다
    out = Path(a.out)
    rows = []
    for (prov, prompt), res in sorted(groups.items()):
        docs, by_vol, errors = rebuild(res, data)
        d = out / "docs" / re.sub(r"[^\w.-]+", "_", f"{prov}__{prompt}")
        d.mkdir(parents=True, exist_ok=True)
        for vol, ds in by_vol.items():
            (d / f"{vol}.json").write_text(json.dumps(ds, ensure_ascii=False, indent=1), encoding="utf-8")
        m = metrics(docs)
        rows.append({"provider": prov, "prompt": prompt, "m": m, "s": summary(m), "c": cost_latency(res, cfg),
                     "errors": errors, "assert_failed": assert_failed(res), "verdict": ""})

    code = 0
    print(f"철 {vols}, 정답 시작 쪽 {rows[0]['m']['n_gt']}건, 문서 {rows[0]['m']['n_docs']}건\n")
    if a.expect_stored:
        # 기준 = 저장된 b·hw를 같은 길(replay + 수신 다시 보정)로 다시 조립한 것 (저장 당시 검토 표시 규칙은 그 뒤 바뀌었다)
        ref = summary(metrics(assemble(data)[0]))
        for r in rows:
            same = r["s"] == ref
            r["verdict"] = "저장 결과와 같음" if same else "저장 결과와 다름"
            code |= 0 if same else 1
    else:
        b = crit.get("baseline") or {}
        base = next((r for r in rows if r["provider"] == b.get("provider") and r["prompt"] == b.get("prompt")), None)
        if base is None:
            print(f"기준 후보 {b} 결과가 없다 — 기준과 함께 돌려야 비교할 수 있다", file=sys.stderr)
            return 2
        for r in rows:
            if r is base:
                r["verdict"] = "기준"
                continue
            why = judge(r, base, crit)
            r["verdict"] = "통과" if not why else "실패: " + "; ".join(why)
            code |= 1 if why else 0
    md = table(rows) + "\n\n텍스트 추출(B)만 칸별 정답률 (손글씨 판독·규칙 합침 전, 판정과 별개):\n\n" + b_table(rows)
    if a.baseline_results:
        md += f"\n\n기준 줄은 지난 결과에서 가져옴 (다시 부르지 않음): {Path(a.baseline_results).name}"
        md += "".join(f"\n\n경고: {w}" for w in warns)
    ans = (crit.get("answer") or {})
    note = ("\n\n답변 채점(Ragas·쌍별 승률): " + ("기준 " + json.dumps(ans, ensure_ascii=False)
                                           if any(v is not None for v in ans.values()) else
                                           "아직 없음 — https://github.com/reddol18/docflow/issues/21"))
    af = [f"{r['provider']}·{r['prompt']} {r['assert_failed']}건" for r in rows if r["assert_failed"]]
    if af:  # 응답은 읽었다 — 읽기 실패 열과 다를 수 있다 (check.py는 문서 하나만 본다)
        note += "\n\n단언(check.py) 실패·예외가 난 응답 (참고, 판정과 별개): " + ", ".join(af)
    print(md + note)
    (out / "gate.md").write_text(md + note + "\n", encoding="utf-8")
    (out / "gate.json").write_text(json.dumps([{k: r[k] for k in ("provider", "prompt", "s", "c", "errors", "assert_failed",
                                                                   "verdict")}
                                               for r in rows], ensure_ascii=False, indent=1), encoding="utf-8")
    return code


if __name__ == "__main__":
    sys.exit(main())
