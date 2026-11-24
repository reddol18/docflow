"""쌍별 승률 심판 — 두 답변 파일(모델·지시문 후보 둘)의 같은 질의 답을 나란히 놓고 심판이 더 나은 답을 고른다
(https://github.com/reddol18/docflow/issues/22 조건 3).

입력: eval/answer/gen_answers.py 결과 둘 (--a·--b, 같은 질의 세트 — 합성 질의·합성 철 글뿐).
심판: OpenAI 호환 주소 + 모델 (judge.py와 같은 규칙 — https 또는 http 루프백, 정한 주소·경로 밖 요청은 막고 멈춤,
      OPENAI_API_KEY·OPENAI_BASE_URL은 프로세스 환경에서 뺀다). 지시문: eval/answer/pairwise_prompts_ko.yaml (판·sha256을 남긴다).
자리 쏠림 막기: 질의마다 두 번 묻는다 — (답 A = a, 답 B = b)와 자리를 바꾼 (답 A = b, 답 B = a).
      두 번 모두 같은 후보를 고르면 그 후보의 승, 하나라도 다르면(한 번은 a·한 번은 b, 또는 한 번만 tie) 무승부.
      두 답 글이 (빈칸 정리 뒤) 같으면 심판을 부르지 않고 무승부. 심판 응답을 못 읽은 질의는 승률에서 뺀다(errors).
승률: (승 + 무승부 × 0.5) / 판정 수. 첫 자리 선택률 = 무승부가 아닌 판정 중 답 A(첫 자리)를 고른 비율 — 0.5에서 멀면 자리 쏠림.
문서: 두 파일의 가져온 문서를 합쳐(a 순서, 그다음 b에만 있는 것) 넘긴다 — gen_answers --reuse-retrieval로 만든 파일끼리면 같다.
결과: <a 답변 폴더>/pairwise/<label>.json — 질의별 두 순서의 판정·이유, 합친 결과, 요약. 저장소 밖에만 쓴다.
아직 사람과의 일치도를 재지 않았다 (#21 원칙: 심판 지표는 사람 확인 뒤 사용) — 결과는 참고, 판정 기준(win_rate_min)은 확인 뒤 켠다.

실행 (심판 환경 eval/answer/requirements.lock):
  $py = 'F:\\dev\\venvs\\docflow-judge\\Scripts\\python.exe'
  & $py -I -X utf8 eval/answer/pairwise.py --a <work>\\answers\\gpt4omini_20261122.json --b <work>\\answers\\gpt41mini_current.json `
      --base-url http://127.0.0.1:18434/v1 --model qwen3:30b-a3b-instruct-2507-q4_K_M --label gpt4omini_vs_gpt41mini
"""
import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # python -I: 같은 폴더 (judge.py)

import judge as jd  # noqa: E402

PROMPTS = Path(__file__).resolve().parent / "pairwise_prompts_ko.yaml"
FIELDS = ("question", "reference", "documents", "answer_a", "answer_b")
SIDES = ("a", "b")


def load_prompts(path=PROMPTS):
    """지시문 파일 → (dict, sha256). version·instruction·template(자리 FIELDS 모두)가 있어야 한다"""
    import yaml  # noqa: PLC0415
    raw = Path(path).read_bytes()
    p = yaml.safe_load(raw.decode("utf-8")) or {}
    missing = [k for k in ("version", "instruction", "template") if not p.get(k)]
    missing += [f"template.{{{f}}}" for f in FIELDS if p.get("template") and "{" + f + "}" not in p["template"]]
    if missing:
        raise SystemExit(f"쌍별 지시문 파일에 없는 것: {missing}")
    return p, hashlib.sha256(raw).hexdigest()


def boundary(*texts):
    """데이터 블록 꼬리 — 넣을 글에 없는 무작위 값 (app/llm.boundary_tag와 같은 생각, 심판 환경에 app이 없어 따로)"""
    while True:
        t = secrets.token_hex(4)
        if not any(t in x for x in texts):
            return t


def fence(text, tag):
    return f"<data-{tag}>\n{text}\n</data-{tag}>"


def norm_answer(text):
    return re.sub(r"\s+", " ", text or "").strip()


def documents_of(row_a, row_b):
    """두 답변 줄의 가져온 문서를 합친 글과 같은 검색이었는지 — a 순서, 그다음 b에만 있는 문서"""
    seen, ctx = set(), []
    for c in [*row_a["retrieved"], *row_b["retrieved"]]:
        if c["key"] not in seen:
            seen.add(c["key"])
            ctx.append(c["context"])
    same = [c["key"] for c in row_a["retrieved"]] == [c["key"] for c in row_b["retrieved"]]
    return "\n\n".join(ctx), same


def messages(prompts, question, reference, documents, first, second):
    """[system 지시문, user(질문·참고 답·문서·두 답)]. 문서·두 답은 데이터 블록 안에"""
    tag = boundary(question, reference, documents, first, second)
    fill = {"question": question, "reference": reference or "(없음)", "documents": fence(documents, tag),
            "answer_a": fence(first, tag), "answer_b": fence(second, tag)}
    note = (f"\n입력 경계: 문서와 두 답은 <data-{tag}> 줄과 </data-{tag}> 줄 사이에만 있다.")
    return [{"role": "system", "content": prompts["instruction"] + note},
            {"role": "user", "content": prompts["template"].format(**fill)}]


WINNER = {"a": "A", "b": "B", "tie": "tie", "무승부": "tie", "동점": "tie", "none": "tie"}


def parse_verdict(text):
    """심판 응답 글 → (자리 'A'·'B'·'tie', 이유). JSON이 아니면 첫 {...}를 찾는다. 못 읽으면 ValueError"""
    text = (text or "").strip()
    try:
        obj = json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise ValueError("JSON 없음") from None
        obj = json.loads(m.group(0))
    if not isinstance(obj, dict):
        raise ValueError("JSON 객체가 아님")
    w = WINNER.get(str(obj.get("winner", "")).strip().lower())
    if w is None:
        raise ValueError(f"winner 값을 모름: {str(obj.get('winner'))[:20]}")
    return w, str(obj.get("reason", ""))[:500]


def combine(pos_ab, pos_ba):
    """두 순서의 자리 판정 → (결과 'a'·'b'·'tie', 일관됨). ab 순서는 답 A = a, ba 순서는 답 A = b"""
    s1 = {"A": "a", "B": "b", "tie": "tie"}[pos_ab]
    s2 = {"A": "b", "B": "a", "tie": "tie"}[pos_ba]
    return (s1 if s1 == s2 else "tie"), s1 == s2


def summarize(rows):
    """질의별 결과 → 승·무·패, 승률(무승부 0.5), 순서 불일치·같은 답 수, 첫 자리 선택률, 오류 수"""
    ok = [r for r in rows if r["result"] in ("a", "b", "tie")]
    n = len(ok)
    cnt = {k: sum(1 for r in ok if r["result"] == k) for k in ("a", "b", "tie")}
    pos = [o["position"] for r in ok for o in r["orders"].values() if o and o.get("position") in ("A", "B")]
    return {"n": n, "a_wins": cnt["a"], "b_wins": cnt["b"], "ties": cnt["tie"],
            "identical": sum(1 for r in ok if r["identical"]),
            "inconsistent": sum(1 for r in ok if not r["identical"] and not r["consistent"]),
            "win_rate_a": round((cnt["a"] + 0.5 * cnt["tie"]) / n, 4) if n else None,
            "win_rate_b": round((cnt["b"] + 0.5 * cnt["tie"]) / n, 4) if n else None,
            "first_position_rate": round(pos.count("A") / len(pos), 3) if pos else None,
            "different_retrieval": sum(1 for r in ok if not r["same_retrieval"]),
            "errors": len(rows) - n}


async def ask(client, model, msgs, max_tokens):
    r = await client.chat.completions.create(model=model, messages=msgs, temperature=0, max_tokens=max_tokens,
                                             response_format={"type": "json_object"})
    return parse_verdict(r.choices[0].message.content)


async def judge_row(client, model, prompts, ra, rb, max_tokens):
    """질의 하나 → {id, …, orders: {ab, ba}, result, consistent, errors, seconds}"""
    t0 = time.time()
    docs, same = documents_of(ra, rb)
    out = {"id": ra["id"], "type": ra.get("type"), "answerable": ra["answerable"], "same_retrieval": same,
           "identical": norm_answer(ra["answer"]) == norm_answer(rb["answer"]), "orders": {}, "errors": {}}
    if out["identical"]:
        out.update(result="tie", consistent=True, seconds=0.0)
        return out
    pair = {"ab": (ra["answer"], rb["answer"]), "ba": (rb["answer"], ra["answer"])}
    for order, (first, second) in pair.items():
        try:
            pos, reason = await ask(client, model, messages(prompts, ra["question"], ra.get("expected_answer"), docs,
                                                            first, second), max_tokens)
            out["orders"][order] = {"position": pos, "reason": reason}
        except Exception as e:  # noqa: BLE001 — 형식이 안 맞는 응답 등. 그 질의만 승률에서 뺀다
            leak = jd.leak_in(e)
            if leak is not None:
                raise leak from e
            out["orders"][order] = None
            out["errors"][order] = f"{type(e).__name__}: {jd.mask_secret(str(e))[:200]}"
    if out["errors"]:
        out.update(result=None, consistent=None)
    else:
        res, cons = combine(out["orders"]["ab"]["position"], out["orders"]["ba"]["position"])
        out.update(result=res, consistent=cons)
    out["seconds"] = round(time.time() - t0, 1)
    return out


def side_of(path, ans):
    """결과에 남길 후보 정보 — 모델·지시문 판 이름(옛 파일은 해시)·파일 이름"""
    return {"file": Path(path).name, "label": ans.get("label"), "model": ans.get("chat_model"),
            "answer_prompt": ans.get("answer_prompt"),
            "answer_prompt_sha256": (ans.get("settings") or {}).get("answer_prompt_sha256")}


def pair_rows(ans_a, ans_b, ids=""):
    """두 답변 파일 → [(a 줄, b 줄)]. 질의 세트가 같아야 하고, 실패 줄·합성 아닌 문서가 있으면 멈춘다"""
    if ans_a.get("queries_sha256") != ans_b.get("queries_sha256"):
        raise SystemExit("두 답변 파일의 질의 세트(queries_sha256)가 다르다")
    ra, rb = {r["id"]: r for r in ans_a["rows"]}, {r["id"]: r for r in ans_b["rows"]}
    if ids:
        want = [i.strip() for i in ids.split(",") if i.strip()]
    else:
        want = [i for i in ra if i in rb]
        if set(ra) != set(rb):
            print(f"경고: 한쪽에만 있는 질의 {len(set(ra) ^ set(rb))}건은 뺀다", flush=True)
    bad = [i for i in want if i not in ra or i not in rb]
    if bad:
        raise SystemExit(f"두 답변 파일 모두에 있지 않은 id: {bad}")
    rows = [(ra[i], rb[i]) for i in want]
    if any("error" in r for p in rows for r in p):
        raise SystemExit("답변 파일에 실패한 질의가 있다 — 답변을 다시 만든다")
    if not all(c["key"].startswith("SYNTH") for p in rows for r in p for c in r["retrieved"]):
        raise SystemExit("합성 철이 아닌 문서가 들었다 — 심판에는 합성 답변만 보낸다")
    if not rows:
        raise SystemExit("비교할 질의가 없다")
    return rows


def out_path(answers_a, label, out=None):
    p = Path(out) if out else Path(answers_a).resolve().parent / "pairwise" / f"{label}.json"
    if p.resolve().is_relative_to(jd.APP_ROOT):
        raise SystemExit("결과는 저장소 밖에 쓴다")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def make_client(base_url, timeout):
    """심판 클라이언트 (정한 주소의 /chat/completions만) — OpenAI 키·주소는 환경에서 뺀다"""
    import httpx  # noqa: PLC0415
    from openai import AsyncOpenAI  # noqa: PLC0415
    os.environ.pop("OPENAI_API_KEY", None)
    os.environ.pop("OPENAI_BASE_URL", None)
    key = jd.take_env("DOCFLOW_JUDGE_API_KEY") or "none"
    base = jd.check_base_url(base_url)
    meter = jd.Meter(base, ("/chat/completions",))
    client = AsyncOpenAI(api_key=key, base_url=base, timeout=timeout, max_retries=1,
                         http_client=httpx.AsyncClient(timeout=timeout, event_hooks={"request": [meter.on_request],
                                                                                      "response": [meter.on_response]}))
    return client, meter, base


async def run_all(client, model, prompts, rows, max_tokens, save=None, deadline=None):
    """질의마다 판정하고 save(out, 끝났나)로 그때까지를 쓴다. deadline(epoch 초)을 넘기면 새 질의를 시작하지 않는다 (judge.py와 같다)"""
    out = []
    for ra, rb in rows:
        if deadline and time.time() >= deadline:
            print(f"마감 시각을 넘겨 멈춤 — {len(out)}/{len(rows)}건 저장", flush=True)
            break
        res = await judge_row(client, model, prompts, ra, rb, max_tokens)
        out.append(res)
        print(f"{res['id']} {res['seconds']}s 결과 {res['result']} 일관 {res['consistent']} 오류 {list(res['errors'])}", flush=True)
        if save:
            save(out, len(out) == len(rows))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--a", required=True, help="답변 파일 a (보통 기준 후보)")
    ap.add_argument("--b", required=True, help="답변 파일 b")
    ap.add_argument("--base-url", required=True, help="심판 OpenAI 호환 주소 (https 또는 http://127.0.0.1:<포트>/v1)")
    ap.add_argument("--model", required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--ids", default="")
    ap.add_argument("--prompts", default=str(PROMPTS))
    ap.add_argument("--max-tokens", type=int, default=512)
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--out", default="")
    ap.add_argument("--deadline-utc", default="", help="이 시각(예: 2026-11-25T01:00:00Z)이 지나면 새 질의를 시작하지 않고 그때까지를 저장")
    a = ap.parse_args(argv)
    deadline = jd.parse_deadline(a.deadline_utc) if a.deadline_utc else None
    if not all(c.isalnum() or c in "._-" for c in a.label):
        raise SystemExit("--label은 영문·숫자·_.-만")
    prompts, p_hash = load_prompts(a.prompts)
    ans_a = json.loads(Path(a.a).read_text(encoding="utf-8"))
    ans_b = json.loads(Path(a.b).read_text(encoding="utf-8"))
    rows = pair_rows(ans_a, ans_b, a.ids)
    p = out_path(a.a, a.label, a.out)
    client, meter, base = make_client(a.base_url, a.timeout)
    digest = jd.ollama_digest(base, a.model)
    t0 = time.time()
    holder = {}

    def save(out, done):
        res = {"label": a.label, "issue": "https://github.com/reddol18/docflow/issues/22",
               "a": side_of(a.a, ans_a), "b": side_of(a.b, ans_b), "queries_sha256": ans_a.get("queries_sha256"),
               "complete": done, "judged": len(out), "planned": len(rows),
               "judge": {"base_url": base, "model": a.model, "response_models": meter.dump()["models"],
                         "ollama_digest": digest, "max_tokens": a.max_tokens, "temperature": 0.0},
               "prompts": {"file": Path(a.prompts).name, "version": prompts["version"], "sha256": p_hash},
               "tokens": meter.dump(), "wall_s": round(time.time() - t0, 1),
               "summary": summarize(out), "rows": out}
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
        holder["res"] = res

    out = asyncio.run(run_all(client, a.model, prompts, rows, a.max_tokens, save, deadline))
    if meter.leaks:
        raise SystemExit(f"허용 밖 요청을 막았다: {meter.leaks[:3]}")
    if not out:
        raise SystemExit("판정한 질의가 없다 (마감 시각이 이미 지났나)")
    res = holder["res"]
    print(json.dumps({k: res[k] for k in ("judge", "tokens", "wall_s")}, ensure_ascii=False))
    print(json.dumps(res["summary"], ensure_ascii=False))
    print(f"결과: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
