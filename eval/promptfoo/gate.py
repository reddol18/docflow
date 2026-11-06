"""promptfoo 결과 → 후보(제공자 × 지시문 판)마다 철 단위 점수·건당 비용·지연, 그리고 통과 기준 판정.

실행 (run.ps1이 부른다):
  python -I -X utf8 eval/promptfoo/gate.py <promptfoo -o 결과.json> [--criteria criteria.yaml] [--out 폴더] [--expect-stored]

후보마다 LLM 응답(추출 값 b)만 바꾸고 나머지(손글씨 판독·머리부 비율·칸 위치 실패)는 저장된 철 결과 그대로 둔 채
app/graph.py merge·validate를 다시 돌린다 (eval/schema_replay.py와 같은 방식) → eval/score.py metrics()로 채점.
그래서 칸별 최종 정답률·검토 표시 재현율·표시 없이 틀린 비율이 eval/score.py 단독 실행과 같은 정의다.
  --expect-stored  점수가 저장된 b·hw를 지금 코드로 다시 조립한 결과(eval/schema_replay.py)의 점수와 같은지 확인
                   (replay 제공자로 채점 연결 검사)
종료 코드: 0 모두 통과, 1 기준을 어긴 후보가 있음(또는 --expect-stored 불일치), 2 입력 문제(기준 후보 없음 등)
"""
import argparse
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # python -I 로 돌려도 옆 모듈을 찾게
import common  # noqa: E402
import yaml  # noqa: E402
from prompts import SCHEMA  # noqa: E402
from schema_replay import replay  # noqa: E402

from app import fields, graph, layout, llm  # noqa: E402

EMPTY = {f["name"]: {"value": "", "confidence": 0.0} for f in fields.prompt_fields(SCHEMA)}
FIELDS = list(fields.editable(SCHEMA))  # 채점 칸 = eval/score.py FIELDS와 같은 정의


def metrics(docs):
    # 채점기(eval/score.py)는 정답·PoC 채점 규칙(records_poc/scripts/rescore.py)을 불러오므로 쓸 때 불러온다
    from score import metrics as m  # noqa: PLC0415
    return m(docs)


def load_results(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data.get("results", data)
    rows = rows.get("results", rows) if isinstance(rows, dict) else rows
    return rows


def key_of(r):
    prov = r.get("provider") or {}
    prov = prov.get("label") or prov.get("id") if isinstance(prov, dict) else str(prov)
    prompt = r.get("prompt") or {}
    return prov, prompt.get("label") or prompt.get("id") or "?"


def model_of(r):
    prov = r.get("provider") or {}
    pid = prov.get("id", "") if isinstance(prov, dict) else str(prov)
    return pid.split(":")[-1]


def group(rows):
    """{(제공자, 판): {doc_id: 결과 줄}}"""
    out = {}
    for r in rows:
        out.setdefault(key_of(r), {})[r["vars"]["doc_id"]] = r
    return out


def output_of(r):
    resp = r.get("response") or {}
    o = resp.get("output")
    return o if isinstance(o, str) else (json.dumps(o, ensure_ascii=False) if o is not None else None)


def rebuild(results, vols):
    """후보 응답으로 철마다 merge·validate. 반환: ({doc_id: 문서}, {vol: [문서]}, 읽기 실패 수)"""
    docs, by_vol, errors = {}, {}, 0
    for vol in vols:
        merged = []
        for d in common.stored_docs(vol):
            r = results.get(d["doc_id"])
            out = output_of(r) if r else None
            try:
                if r is None or r.get("error") or out is None:
                    raise ValueError("응답 없음")
                b = fields.parse_extract(SCHEMA, out)
            except (ValueError, TypeError):
                b, errors = json.loads(json.dumps(EMPTY)), errors + 1
            rec, why = layout.fix_recipient(common.ocr(vol, d["doc_id"]), graph._llm_of(b, "recipient"))
            s = {"vol": vol, "doc_id": d["doc_id"], "pages": d["pages"], "b": b, "hw": d["hw"],
                 "recipient": rec, "recipient_fix": why,
                 "ratio_i": graph.HEAD_RATIOS.index(d["info"]["head_ratio"]), "box_fail": d["info"]["box_fail"]}
            merged.extend(graph.merge(s)["docs"])
        merged = graph.validate({"docs": merged})["docs"]
        by_vol[vol] = merged
        docs.update({d["doc_id"]: d for d in merged})
    return docs, by_vol, errors


def rate(pair):
    a, b = pair
    return 100.0 * a / b if b else 0.0


def cost_latency(results):
    usd, lat, n = 0.0, [], 0
    for r in results.values():
        n += 1
        tu = (r.get("response") or {}).get("tokenUsage") or {}
        pi, po = llm.PRICE.get(model_of(r), (0, 0))
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


def fmt_ms(x):
    return "-" if x is None else f"{x / 1000:.1f}s"


def table(rows):
    head = ["후보", "판"] + [f"{f}" for f in FIELDS] + ["일련번호 표시 재현율", "일자 표시 재현율", "표시 없이 틀림",
                                                     "읽기 실패", "건당 비용", "평균 지연", "p95 지연", "판정"]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in rows:
        s = r["s"]
        lines.append("| " + " | ".join([r["provider"], r["prompt"]] + [f"{s['fields'][f]:.1f}%" for f in FIELDS] + [
            *("-" if s["flag_recall"][k] is None else f"{s['flag_recall'][k]:.1f}%" for k in ("serial", "date")),
            f"{s['silent']:.1f}%",
            str(r["errors"]), f"${r['c']['usd_per_doc']:.5f}", fmt_ms(r["c"]["ms_mean"]), fmt_ms(r["c"]["ms_p95"]),
            r["verdict"]]) + " |")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("--criteria", default=str(common.HERE / "criteria.yaml"))
    ap.add_argument("--out", default=str(common.WORK / "promptfoo"))
    ap.add_argument("--expect-stored", action="store_true")
    a = ap.parse_args()
    crit = yaml.safe_load(Path(a.criteria).read_text(encoding="utf-8"))
    groups = group(load_results(a.results))
    if not groups:
        print("결과가 비었다", file=sys.stderr)
        return 2
    vols = sorted({r["vars"]["vol"] for g in groups.values() for r in g.values()})
    out = Path(a.out)
    rows = []
    for (prov, prompt), res in sorted(groups.items()):
        docs, by_vol, errors = rebuild(res, vols)
        d = out / "docs" / re.sub(r"[^\w.-]+", "_", f"{prov}__{prompt}")
        d.mkdir(parents=True, exist_ok=True)
        for vol, ds in by_vol.items():
            (d / f"{vol}.json").write_text(json.dumps(ds, ensure_ascii=False, indent=1), encoding="utf-8")
        m = metrics(docs)
        rows.append({"provider": prov, "prompt": prompt, "m": m, "s": summary(m), "c": cost_latency(res),
                     "errors": errors, "verdict": ""})

    code = 0
    print(f"철 {vols}, 정답 시작 쪽 {rows[0]['m']['n_gt']}건, 문서 {rows[0]['m']['n_docs']}건\n")
    if a.expect_stored:
        # 기준 = 저장된 b·hw를 지금 코드로 다시 조립한 것 (저장 당시 검토 표시 규칙은 그 뒤 바뀌었다)
        ref = summary(metrics({x["doc_id"]: x for v in vols for x in replay(graph, common.stored_docs(v))}))
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
    md = table(rows)
    ans = (crit.get("answer") or {})
    note = ("\n\n답변 채점(Ragas·쌍별 승률): " + ("기준 " + json.dumps(ans, ensure_ascii=False)
                                           if any(v is not None for v in ans.values()) else
                                           "아직 없음 — https://github.com/reddol18/docflow/issues/21"))
    print(md + note)
    (out / "gate.md").write_text(md + note + "\n", encoding="utf-8")
    (out / "gate.json").write_text(json.dumps([{k: r[k] for k in ("provider", "prompt", "s", "c", "errors", "verdict")}
                                               for r in rows], ensure_ascii=False, indent=1), encoding="utf-8")
    return code


if __name__ == "__main__":
    sys.exit(main())
