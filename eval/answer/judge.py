"""답변 심판 채점 — Ragas 네 지표 + 인용 정확도·'모른다' (https://github.com/reddol18/docflow/issues/21 3단계).

입력: eval/answer/gen_answers.py 결과(<work>/answers/<label>.json — 합성 질의·합성 철 글뿐).
심판: OpenAI 호환 주소 + 모델 이름 (--base-url·--model, 코드 수정 없이 바꿈). 목표 qwen3:30b-a3b-instruct-2507(Ollama),
      대안 qwen3:32b(--system-prompt /no_think). 주소는 https 또는 http 루프백(SSH 터널·이 PC Ollama)만.
지표 (답 있는 질의만, 질문·답·넘긴 문서 글·참고 답):
  faithfulness       Ragas 근거 충실도 — 답의 진술 중 넘긴 문서로 뒷받침되는 비율
  answer_relevancy   Ragas 질문 적합도 — 답에서 지은 질문 3개와 원래 질문의 임베딩 코사인 평균(회피 답이면 0).
                     임베딩은 text-embedding-3-small(OpenAI, 질문·지은 질문만 보냄 — 합성 글, 1회 약 $0.0001).
                     --embed-base-url로 OpenAI 호환 로컬 임베딩(폐쇄망)으로 바꿀 수 있다
  context_precision  Ragas 문맥 정밀도(참고 답 기준) — 가져온 5건의 쓸모 판정으로 낸 평균 정밀도
  context_recall     Ragas 문맥 재현율 — 참고 답 진술 중 가져온 문서에서 확인되는 비율
  citation_accuracy  직접: 답의 근거 표시마다 (가져온 문서인가) ∧ (심판이 그 문서가 해당 내용을 뒷받침한다고 봄) 비율.
                     근거 표시 없이 값을 답했으면 0, 답을 피한 답(refusal)이면 해당 없음
  refusal            직접: 답이 '자료에 없어 답할 수 없다'로 피했는가 (모든 질의). '모른다' 비율 = 답 없는 질의 중 refusal 비율
이진 판정: 지시문 파일 thresholds로 연속 점수를 P(통과)/F(실패)로, 해당 없음은 N (binarize). 사람 채점표와 같은 P/F/N.
지시문: eval/answer/judge_prompts_ko.yaml — Ragas 지시문을 한국어로 바꾸고 직접 지표 지시문도 둔다. 판·sha256을 결과에 남긴다.
      ragas_examples(선택)가 있으면 그 Ragas 지시문의 예시(few-shot)도 바꾼다 (Ragas 입력·출력 모델로 검사).
--metrics: 고친 지표만 재채점할 때 (예: faithfulness,answer_relevancy). refusal은 늘 함께 채점하고, 고르지 않은 지표는 N.
심판이 OpenAI로 새지 않게: OPENAI_API_KEY·OPENAI_BASE_URL을 프로세스 환경에서 뺀 뒤 Ragas를 부르고, 심판 클라이언트는 심판 주소에만,
      임베딩 클라이언트는 /embeddings에만 요청을 보내게 httpx 훅에서 막는다(어기면 멈춤). Ragas 사용 통계 전송은 끈다(RAGAS_DO_NOT_TRACK).
결과: <답변 폴더>/judge/<label>.json — 질의별 점수·P/F/N·이유·초, 지표별 평균·통과율, 심판 모델(응답 이름·Ollama 다이제스트),
      지시문 판·해시, 토큰(심판·임베딩), 임베딩 비용. 저장소 밖에만 쓴다.

실행 (심판 환경 eval/answer/requirements.lock):
  $py = 'F:\\dev\\venvs\\docflow-judge\\Scripts\\python.exe'
  & $py -I -X utf8 eval/answer/judge.py --answers <work>\\answers\\gpt4omini_20261122.json --base-url http://127.0.0.1:18434/v1 `
      --model qwen3:30b-a3b-instruct-2507 --label qwen3_30b
"""
import argparse
import asyncio
import hashlib
import json
import math
import os
import statistics
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

APP_ROOT = Path(__file__).resolve().parents[2]
PROMPTS = Path(__file__).resolve().parent / "judge_prompts_ko.yaml"
RAGAS_METRICS = ("faithfulness", "answer_relevancy", "context_precision", "context_recall")
METRICS = (*RAGAS_METRICS, "citation_accuracy", "refusal")
LOOPBACK = ("127.0.0.1", "localhost")
OPENAI_BASE = "https://api.openai.com/v1"
EMBED_PRICE = {"text-embedding-3-small": 0.02}  # USD / 1M 토큰 (app/llm.py PRICE와 같다)


class JudgeLeak(RuntimeError):
    """심판·임베딩 클라이언트가 정한 곳 밖으로 요청하려 함 (Ragas가 OpenAI로 몰래 넘어가는 것 등)"""


def check_base_url(url, what="--base-url"):
    """https, 또는 http면 루프백(SSH 터널·이 PC)만. 사용자·비밀번호 거부 → 정리한 주소"""
    try:
        u = urlsplit(url)
        _ = u.port
    except ValueError as e:
        raise SystemExit(f"{what}를 읽을 수 없음: {e}") from None
    if u.username or u.password:
        raise SystemExit(f"{what}에 사용자·비밀번호를 넣지 않는다 — 키는 환경변수로")
    if not (u.scheme == "https" or (u.scheme == "http" and (u.hostname or "") in LOOPBACK)):
        raise SystemExit(f"{what}는 https://… 또는 http://127.0.0.1:<포트>/v1(SSH 터널·이 PC)만 받는다")
    return url.rstrip("/")


def load_prompts(path=PROMPTS):
    """지시문 파일 → (dict, sha256). 필요한 키가 모두 있어야 한다"""
    import yaml  # noqa: PLC0415
    raw = Path(path).read_bytes()
    p = yaml.safe_load(raw.decode("utf-8"))
    need_ragas = {"faithfulness.statement_generator_prompt", "faithfulness.nli_statement_prompt", "answer_relevancy.prompt",
                  "context_precision.prompt", "context_recall.prompt"}
    missing = (need_ragas - set(p.get("ragas") or {})) | ({"refusal", "citation_support"} - set(p.get("custom") or {})) \
        | (set(METRICS) - {"refusal"} - set(p.get("thresholds") or {}))
    if missing or not p.get("version"):
        raise SystemExit(f"지시문 파일에 없는 키: {sorted(missing) or ['version']}")
    ex = p.get("ragas_examples") or {}  # 선택 (ko-2부터): Ragas 예시를 바꾼다
    bad = sorted(set(ex) - need_ragas) + sorted(k for k, v in ex.items() if not (
        isinstance(v, list) and v and all(isinstance(e, dict) and set(e) == {"input", "output"} for e in v)))
    if bad:
        raise SystemExit(f"ragas_examples 키·형식이 틀렸다 (키는 ragas와 같고, 값은 input·output 목록): {bad}")
    return p, hashlib.sha256(raw).hexdigest()


def apply_prompts(metrics, prompts):
    """Ragas 지표 객체의 지시문(과 ragas_examples가 있으면 예시)을 한국어로 바꾼다.
    {이름: 지표}의 속성이 없거나 예시가 Ragas 입력·출력 모델에 안 맞으면 멈춘다 (Ragas 판이 바뀐 것)"""
    for key, text in prompts["ragas"].items():
        name, attr = key.split(".", 1)
        prompt = getattr(metrics[name], attr, None)
        if prompt is None or not hasattr(prompt, "instruction"):
            raise SystemExit(f"Ragas 지표 {name}에 지시문 {attr}이 없다 — Ragas 판 확인 (requirements.lock)")
        prompt.instruction = text
    for key, examples in (prompts.get("ragas_examples") or {}).items():
        name, attr = key.split(".", 1)
        prompt = getattr(metrics[name], attr)
        if not (hasattr(prompt, "input_model") and hasattr(prompt, "output_model")):
            raise SystemExit(f"Ragas 지표 {name}.{attr}에 입력·출력 모델이 없다 — Ragas 판 확인")
        try:
            prompt.examples = [(prompt.input_model.model_validate(e["input"]), prompt.output_model.model_validate(e["output"]))
                               for e in examples]
        except ValueError as e:  # pydantic ValidationError는 ValueError
            raise SystemExit(f"ragas_examples {key}가 Ragas 모델에 맞지 않다: {str(e)[:300]}") from None


def parse_metrics(text):
    """--metrics → 채점할 지표 집합 (refusal은 늘 — 충실도 건너뛰기·이진 판정에 쓴다). 빈 값이면 전부"""
    want = {m.strip() for m in text.split(",") if m.strip()}
    if not want:
        return set(METRICS)
    bad = sorted(want - set(METRICS))
    if bad:
        raise SystemExit(f"모르는 지표: {bad} (가능: {', '.join(METRICS)})")
    return want | {"refusal"}


def is_nan(v):
    return v is None or (isinstance(v, float) and math.isnan(v))


def binarize(metric, value, thresholds, answerable, refused):
    """연속 점수 → 'P'·'F'·'N'(해당 없음). 사람 채점표와 같은 규칙:
    refusal은 답 없는 질의에서만(피했으면 P). 나머지는 답 있는 질의에서만. 근거 충실도·인용 정확도는 답을 피했으면 N"""
    if metric == "refusal":
        if answerable or refused is None:
            return "N"
        return "P" if refused else "F"
    if not answerable:
        return "N"
    if metric in ("faithfulness", "citation_accuracy") and refused:
        return "N"
    if is_nan(value):
        return "N"
    return "P" if value >= thresholds[metric] - 1e-9 else "F"


def citation_value(cited, retrieved, supported, refused):
    """인용 정확도 점수: 답을 피했으면 None, 근거 표시 없이 답했으면 0.0, 아니면 (가져온 문서 ∧ 뒷받침) 비율.
    supported: {문서 키: 참·거짓} — 가져온 문서에 대해서만 심판이 판정한다"""
    if refused:
        return None
    if not cited:
        return 0.0
    ok = sum(1 for c in cited if c in set(retrieved) and supported.get(c))
    return ok / len(cited)


def summarize(rows, thresholds):
    """질의별 결과 → 지표별 {n(판정 수), pass_rate, mean(연속 점수 평균), errors}, '모른다' 비율"""
    out = {}
    for m in METRICS:
        vals = [r["scores"].get(m) for r in rows]
        bins = [r["binary"][m] for r in rows if r["binary"][m] in ("P", "F")]
        nums = [v for v in vals if not is_nan(v)]
        out[m] = {"n": len(bins), "pass_rate": round(bins.count("P") / len(bins), 3) if bins else None,
                  "mean": round(statistics.mean(nums), 4) if nums else None,
                  "errors": sum(1 for r in rows if m in r.get("errors", {})),
                  "threshold": thresholds.get(m)}
    un = [r for r in rows if not r["answerable"] and r["scores"].get("refusal") is not None]
    ans = [r for r in rows if r["answerable"] and r["scores"].get("refusal") is not None]
    out["refusal"]["dont_know_rate_unanswerable"] = round(sum(r["scores"]["refusal"] for r in un) / len(un), 3) if un else None
    out["refusal"]["refusal_rate_answerable"] = round(sum(r["scores"]["refusal"] for r in ans) / len(ans), 3) if ans else None
    return out


class Meter:
    """httpx 응답 훅: 토큰·모델 이름을 세고, 정한 호스트·경로 밖 요청은 막는다"""

    def __init__(self, host, paths):
        self.host, self.paths = host, tuple(paths)
        self.prompt_tokens = self.completion_tokens = self.calls = 0
        self.models = set()

    async def on_request(self, request):
        if request.url.host != self.host or not request.url.path.endswith(self.paths):
            raise JudgeLeak(f"허용 밖 요청 {request.url.host}{request.url.path} (허용 {self.host} {self.paths})")

    async def on_response(self, response):
        await response.aread()
        self.calls += 1
        try:
            body = response.json()
        except ValueError:
            return
        u = body.get("usage") or {}
        self.prompt_tokens += u.get("prompt_tokens") or 0
        self.completion_tokens += u.get("completion_tokens") or 0
        if body.get("model"):
            self.models.add(body["model"])

    def dump(self):
        return {"calls": self.calls, "prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
                "models": sorted(self.models)}


def _user_env(name):
    try:
        import winreg  # noqa: PLC0415
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return winreg.QueryValueEx(k, name)[0] or None
    except (ImportError, OSError):
        return None


def take_env(name):
    """환경변수 값을 꺼내고 프로세스 환경에서 지운다 (Ragas·SDK가 기본값으로 집어 가지 않게). 값은 찍지 않는다"""
    v = os.environ.pop(name, None)
    return v or _user_env(name)


def ollama_digest(base_url, model, timeout=10):
    """Ollama면 /api/tags에서 그 모델의 다이제스트, 아니면 None (주소는 check_base_url을 지난 것)"""
    import urllib.request  # noqa: PLC0415
    root = base_url[:-3] if base_url.endswith("/v1") else base_url
    try:
        with urllib.request.urlopen(f"{root}/api/tags", timeout=timeout) as r:  # noqa: S310 — 검사한 주소
            tags = json.load(r).get("models", [])
    except Exception:  # noqa: BLE001 — Ollama가 아니면 없음
        return None
    for m in tags:
        if m.get("name") == model or m.get("model") == model:
            return m.get("digest")
    return None


def make_prompt(instruction, data, out_model):
    """직접 지표용 지시문: Ragas BasePrompt.to_string과 같은 모양 (지시문 + 출력 JSON 형식 + 입력)"""
    schema = json.dumps(out_model.model_json_schema(), ensure_ascii=False)
    return (f"{instruction}\n출력은 다음 JSON Schema를 따르는 JSON 하나로만 답한다:\n{schema}\n"
            f"-----------------------------\n입력: {json.dumps(data, ensure_ascii=False, indent=2)}\n출력: ")


async def judge_row(row, metrics, llm, prompts, models, only=None):
    """질의 하나 → {scores, binary, reasons, errors, seconds}. only(지표 집합)가 있으면 그 지표만 (나머지는 N)"""
    only = set(METRICS) if only is None else only
    t0 = time.time()
    scores, reasons, errors = {}, {}, {}
    ctx = [c["context"] for c in row["retrieved"]]
    keys = [c["key"] for c in row["retrieved"]]

    async def run(name, coro):
        try:
            return await coro
        except JudgeLeak:
            raise
        except Exception as e:  # noqa: BLE001 — 심판 응답이 형식에 안 맞는 등. 그 지표만 해당 없음으로
            errors[name] = f"{type(e).__name__}: {str(e)[:200]}"
            return None

    ref = await run("refusal", llm.agenerate(make_prompt(prompts["custom"]["refusal"], {"answer": row["answer"]},
                                                          models["Refusal"]), models["Refusal"]))
    refused = None if ref is None else bool(ref.refused)
    scores["refusal"] = None if refused is None else int(refused)
    if ref is not None:
        reasons["refusal"] = ref.reason
    if row["answerable"]:
        args = {"faithfulness": dict(user_input=row["question"], response=row["answer"], retrieved_contexts=ctx),
                "answer_relevancy": dict(user_input=row["question"], response=row["answer"]),
                "context_precision": dict(user_input=row["question"], reference=row["expected_answer"], retrieved_contexts=ctx),
                "context_recall": dict(user_input=row["question"], retrieved_contexts=ctx, reference=row["expected_answer"])}
        for m in RAGAS_METRICS:
            if m not in only:
                continue
            if m == "faithfulness" and refused:
                scores[m] = None  # 답을 피한 답 — 진술이 없어 NaN이 되거나 '없다'를 판정하게 된다 (사람 표도 N)
                continue
            r = await run(m, metrics[m].ascore(**args[m]))
            scores[m] = None if r is None or is_nan(r.value) else round(float(r.value), 4)
        if "citation_accuracy" in only:
            supported = {}
            for c in dict.fromkeys(row.get("cited") or []):
                if c in keys:
                    s = await run("citation_accuracy", llm.agenerate(make_prompt(
                        prompts["custom"]["citation_support"],
                        {"answer": row["answer"], "cited_document": c, "document_text": ctx[keys.index(c)]},
                        models["Support"]), models["Support"]))
                    supported[c] = bool(s and s.supported)
                    if s is not None:
                        reasons.setdefault("citation_accuracy", {})[c] = s.reason
            scores["citation_accuracy"] = citation_value(row.get("cited") or [], keys, supported, refused)
    th = prompts["thresholds"]
    binary = {m: binarize(m, scores.get(m), th, row["answerable"], refused) for m in METRICS}
    return {"scores": scores, "binary": binary, "reasons": reasons, "errors": errors, "seconds": round(time.time() - t0, 1)}


def pick(rows, ids):
    if not ids:
        return rows
    want = [i.strip() for i in ids.split(",") if i.strip()]
    have = {r["id"]: r for r in rows}
    bad = [i for i in want if i not in have]
    if bad:
        raise SystemExit(f"답변 파일에 없는 id: {bad}")
    return [have[i] for i in want]


def out_path(answers, label, out=None):
    p = Path(out) if out else Path(answers).resolve().parent / "judge" / f"{label}.json"
    if p.resolve().is_relative_to(APP_ROOT):
        raise SystemExit("결과는 저장소 밖에 쓴다")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def build(a, prompts):
    """심판 LLM·임베딩·Ragas 지표를 만든다 — 심판이 OpenAI로 새지 않는지 확인까지"""
    os.environ["RAGAS_DO_NOT_TRACK"] = "true"  # Ragas 사용 통계(외부 전송) 끔 — ragas를 불러오기 전에
    import httpx  # noqa: PLC0415
    from openai import AsyncOpenAI  # noqa: PLC0415
    from pydantic import BaseModel  # noqa: PLC0415
    from ragas import _analytics  # noqa: PLC0415
    from ragas.embeddings.openai_provider import OpenAIEmbeddings  # noqa: PLC0415
    from ragas.llms import llm_factory  # noqa: PLC0415
    from ragas.metrics.collections import AnswerRelevancy, ContextPrecision, ContextRecall, Faithfulness  # noqa: PLC0415
    if not _analytics.do_not_track():
        raise SystemExit("Ragas 사용 통계 전송을 끄지 못했다")

    openai_key = take_env("OPENAI_API_KEY")  # 임베딩에만. 환경에서 지워 Ragas·SDK 기본 클라이언트가 못 쓰게
    os.environ.pop("OPENAI_BASE_URL", None)
    judge_key = take_env("DOCFLOW_JUDGE_API_KEY") or "none"
    base = check_base_url(a.base_url)
    jm = Meter(urlsplit(base).hostname, ("/chat/completions",))
    jclient = AsyncOpenAI(api_key=judge_key, base_url=base, timeout=a.timeout, max_retries=1,
                          http_client=httpx.AsyncClient(timeout=a.timeout, event_hooks={"request": [jm.on_request],
                                                                                         "response": [jm.on_response]}))
    kwargs = {"max_tokens": a.max_tokens, "temperature": 0.0}
    if a.system_prompt:
        kwargs["system_prompt"] = a.system_prompt
    llm = llm_factory(a.model, provider="openai", client=jclient, **kwargs)

    ebase = check_base_url(a.embed_base_url, "--embed-base-url")
    if ebase == OPENAI_BASE and not openai_key:
        raise SystemExit("OPENAI_API_KEY가 없다 (질문 적합도 임베딩용)")
    ekey = openai_key if ebase == OPENAI_BASE else (take_env("DOCFLOW_EMBED_API_KEY") or "none")
    em = Meter(urlsplit(ebase).hostname, ("/embeddings",))
    eclient = AsyncOpenAI(api_key=ekey, base_url=ebase, timeout=60, max_retries=2,
                          http_client=httpx.AsyncClient(timeout=60, event_hooks={"request": [em.on_request],
                                                                                  "response": [em.on_response]}))
    emb = OpenAIEmbeddings(client=eclient, model=a.embed_model)
    metrics = {"faithfulness": Faithfulness(llm=llm), "answer_relevancy": AnswerRelevancy(llm=llm, embeddings=emb),
               "context_precision": ContextPrecision(llm=llm), "context_recall": ContextRecall(llm=llm)}
    for name, m in metrics.items():  # 심판이 OpenAI로 몰래 바뀌지 않았는지 (기본값 대체 없음)
        if m.llm is not llm:
            raise SystemExit(f"Ragas {name}의 심판이 지정한 것이 아니다")
    if str(jclient.base_url).rstrip("/") != base:
        raise SystemExit("심판 클라이언트 주소가 다르다")
    apply_prompts(metrics, prompts)

    class Refusal(BaseModel):
        reason: str
        refused: int

    class Support(BaseModel):
        reason: str
        supported: int

    return llm, metrics, {"Refusal": Refusal, "Support": Support}, jm, em, base, ebase


async def run_all(rows, metrics, llm, prompts, models, deadline=None, save=None, only=None):
    """질의마다 채점하고 save(out, 끝났나)로 그때까지의 결과를 쓴다. deadline(epoch 초)을 넘기면 새 질의를 시작하지 않는다"""
    out = []
    for r in rows:
        if deadline and time.time() >= deadline:
            print(f"마감 시각을 넘겨 멈춤 — {len(out)}/{len(rows)}건 저장", flush=True)
            break
        res = await judge_row(r, metrics, llm, prompts, models, only)
        out.append({"id": r["id"], "type": r["type"], "answerable": r["answerable"], **res})
        print(f"{r['id']} {res['seconds']}s {res['binary']} 오류 {list(res['errors'])}", flush=True)
        if save:
            save(out, len(out) == len(rows))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--answers", required=True, help="gen_answers.py 결과 JSON")
    ap.add_argument("--base-url", required=True, help="심판 OpenAI 호환 주소 (https 또는 http://127.0.0.1:<포트>/v1)")
    ap.add_argument("--model", required=True, help="심판 모델 이름 (예: qwen3:30b-a3b-instruct-2507)")
    ap.add_argument("--label", required=True)
    ap.add_argument("--ids", default="", help="쉼표로 질의 고르기 (시험 실행)")
    ap.add_argument("--prompts", default=str(PROMPTS))
    ap.add_argument("--system-prompt", default="", help="심판 system 메시지 (qwen3:32b면 /no_think)")
    ap.add_argument("--max-tokens", type=int, default=2048)
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--embed-model", default="text-embedding-3-small")
    ap.add_argument("--embed-base-url", default=OPENAI_BASE, help="질문 적합도 임베딩 주소 (기본 OpenAI)")
    ap.add_argument("--out", default="")
    ap.add_argument("--deadline-utc", default="", help="이 시각(예: 2026-11-22T00:40:00Z)이 지나면 새 질의를 시작하지 않고 그때까지를 저장")
    ap.add_argument("--metrics", default="",
                    help="쉼표로 채점할 지표만 (예: faithfulness,answer_relevancy — refusal은 늘 함께). 빈 값이면 전부")
    a = ap.parse_args(argv)
    if not all(c.isalnum() or c in "._-" for c in a.label):
        raise SystemExit("--label은 영문·숫자·_.-만")
    only = parse_metrics(a.metrics)
    prompts, p_hash = load_prompts(a.prompts)
    ans = json.loads(Path(a.answers).read_text(encoding="utf-8"))
    rows = pick(ans["rows"], a.ids)
    if any("error" in r for r in rows):
        raise SystemExit("답변 파일에 실패한 질의가 있다 — 답변을 다시 만든다")
    if not all(c["key"].startswith("SYNTH") for r in rows for c in r["retrieved"]):
        raise SystemExit("합성 철이 아닌 문서가 들었다 — 심판·임베딩에는 합성 답변만 보낸다")
    p = out_path(a.answers, a.label, a.out)
    llm, metrics, models, jm, em, base, ebase = build(a, prompts)
    import ragas  # noqa: PLC0415
    digest = ollama_digest(base, a.model)  # 시작할 때 (마감·끊김 뒤에는 못 읽을 수 있다)
    deadline = None
    if a.deadline_utc:
        import datetime as dt  # noqa: PLC0415
        deadline = dt.datetime.fromisoformat(a.deadline_utc.replace("Z", "+00:00")).timestamp()
    t0 = time.time()
    holder = {}

    def save(out, done):
        wall = round(time.time() - t0, 1)
        edump = em.dump()
        e_usd = edump["prompt_tokens"] * EMBED_PRICE.get(a.embed_model, 0.0) / 1e6 if ebase == OPENAI_BASE else 0.0
        res = {"label": a.label, "issue": "https://github.com/reddol18/docflow/issues/21", "answers_file": Path(a.answers).name,
               "answers_label": ans.get("label"), "answer_model": sorted({r.get("model") for r in rows if r.get("model")}),
               "complete": done, "judged": len(out), "planned": len(rows),
               "metrics_judged": [m for m in METRICS if m in only],
               "judge": {"base_url": base, "model": a.model, "response_models": jm.dump()["models"],
                         "ollama_digest": digest, "system_prompt": a.system_prompt or None,
                         "max_tokens": a.max_tokens, "temperature": 0.0},
               "embedding": {"base_url": ebase, "model": a.embed_model, "usd": round(e_usd, 6), **edump},
               "prompts": {"file": Path(a.prompts).name, "version": prompts["version"], "sha256": p_hash},
               "ragas_version": ragas.__version__, "thresholds": prompts["thresholds"],
               "tokens": {"judge": jm.dump()}, "wall_s": wall,
               "seconds_per_question": round(wall / len(out), 1) if out else None,
               "summary": summarize(out, prompts["thresholds"]), "rows": out}
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
        holder["res"] = res

    out = asyncio.run(run_all(rows, metrics, llm, prompts, models, deadline, save, only))
    if not out:
        raise SystemExit("채점한 질의가 없다")
    if not holder["res"]["complete"]:
        save(out, False)
    res = holder["res"]
    print(json.dumps({k: res[k] for k in ("judge", "embedding", "tokens", "wall_s", "seconds_per_question")}, ensure_ascii=False))
    print(json.dumps(res["summary"], ensure_ascii=False))
    print(f"결과: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
