"""레드팀 세트 제공자 (https://github.com/reddol18/docflow/issues/27 조건 4) — 운영 함수 llm.extract·llm.answer를 그대로 부른다.

운영과 다른 것은 둘뿐이다:
  - 비용 예약(llm._metered, Postgres)을 이 실행의 장부 파일로 바꾼다. 호출마다 운영과 같은 예약 상한(estimate_usd)을 더해
    누적이 CAP_USD($0.5)를 넘으면 부르지 않는다 (BudgetExceeded). 장부: 환경변수 DOCFLOW_RT27_LEDGER (run.ps1이 work/ 아래로)
  - 클라이언트를 감싸 원 응답(근거 없는 인용을 지우기 전)을 함께 돌려준다.
모델은 운영 설정(app/llm.py openai 기본값, k8s/docs.yaml은 덮어쓰지 않음) — 환경변수 EXTRACT_MODEL·CHAT_MODEL은 보지 않는다.
키는 OPENAI_API_KEY (run.ps1이 promptfoo를 부르는 동안만 넣는다). 출력은 JSON 글자 — 채점은 redteam27.get_assert.
"""
import contextlib
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import redteam27 as rt  # noqa: E402 — common을 거쳐 sys.path에 doc_platform

from app import fields, grounding, llm  # noqa: E402


class _Recorder:
    """client().chat.completions.create를 받아 진짜 클라이언트로 넘기고 응답을 남긴다"""

    def __init__(self, real):
        self.real, self.last = real, None
        self.chat = self.completions = self

    def create(self, **kw):
        self.last = self.real.chat.completions.create(**kw)
        return self.last


def _ledger_path():
    """장부 파일 — 없으면 상한을 셀 수 없으므로 부르지 않는다 (run.ps1 -RedTeam이 work/ 아래로 준다)"""
    p = os.environ.get("DOCFLOW_RT27_LEDGER")
    if not p:
        raise llm.BudgetExceeded("DOCFLOW_RT27_LEDGER(비용 장부)가 없어 부르지 않음 — run.ps1 -RedTeam으로 실행")
    return Path(p)


def spent(path=None):
    p = Path(path) if path else _ledger_path()
    if not p.exists():
        return 0.0
    return sum(json.loads(x)["usd"] for x in p.read_text(encoding="utf-8").splitlines() if x.strip())


def _meter(cap):
    """llm._metered 대신: 장부 누적 + 이번 예약 상한 > cap이면 부르지 않는다. 끝나면 실제 토큰 비용을 장부에"""
    @contextlib.contextmanager
    def metered(vol, kind, model, scope, tin_max, tout_max, user_id=None):
        est = llm.estimate_usd(model, tin_max, tout_max)
        if spent() + est > cap:
            raise llm.BudgetExceeded(f"레드팀 상한 ${cap} — 누적 ${spent():.6f} + 이번 예상 ${est:.6f}")
        m = llm._Meter()
        yield m
        tin, tout = llm._tokens(m.usage)
        pi, po = llm.price_of(model)
        usd = (tin * pi + tout * po) / 1e6 if m.usage is not None else est
        metered.last = {"model": model, "prompt": tin, "completion": tout, "usd": usd}
        with _ledger_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps({"kind": kind, "model": model, "usd": usd}) + "\n")
    metered.last = None
    return metered


def run_case(case, real_client, cap=rt.CAP_USD):
    """사례 하나를 운영 함수로 돌린다 → (출력 dict, 사용량 dict|None). real_client는 OpenAI SDK 클라이언트(시험은 가짜)"""
    rec = _Recorder(real_client)
    meter = _meter(cap)
    saved = llm.PROVIDER, llm._metered, llm.client
    llm.PROVIDER = llm.load_provider({"LLM_PROVIDER": "openai"})  # 운영 기본 모델 (환경변수 덮어쓰기 무시)
    llm._metered, llm.client = meter, (lambda: rec)
    out = {"kind": case["kind"], "id": case["id"]}
    try:
        if case["kind"] == "extract":
            try:
                res = llm.extract(f"redteam27-{case['id']}", case["lines"], rt.SCHEMA)
                out["fields"] = {k: v["value"] for k, v in res.items()}
                out["failed"] = list(getattr(res, "failed", ()))
                # 운영 근거 확인 (app/graph.py extract와 같은 함수) — 근거 없는 칸은 화면에 'OCR 근거 없음' 검토 표시
                out["flagged"] = grounding.check_extract(fields.prompt_fields(rt.SCHEMA), res, case["lines"], out["failed"])
            except llm.ExtractInvalid as e:  # 운영: 그 문서의 추출 칸 모두 '추출 실패'(빈 값)
                out["fields"], out["failed"], out["invalid"] = {}, ["*"], str(e)
        else:
            out["screen"], out["warnings"] = llm.answer(case["question"], case["docs"], rt.SCHEMA)
    except llm.BudgetExceeded as e:
        out["error"] = str(e)
    finally:
        llm.PROVIDER, llm._metered, llm.client = saved
    if rec.last is not None:
        ch = rec.last.choices[0]
        out["raw"] = ch.message.content or ""
        out["finish"] = getattr(ch, "finish_reason", None)
    out["usage"] = meter.last
    return out, meter.last


def call_api(prompt, options, context):
    """promptfoo python 제공자. prompt는 쓰지 않는다 (사례 id로 운영 함수가 메시지를 만든다)"""
    case = rt.BY_ID[context["vars"]["id"]]
    real = llm.make_client(llm.load_provider({"LLM_PROVIDER": "openai"}))
    out, usage = run_case(case, real)
    r = {"output": json.dumps(out, ensure_ascii=False)}
    if usage:
        r["tokenUsage"] = {"prompt": usage["prompt"], "completion": usage["completion"],
                           "total": usage["prompt"] + usage["completion"]}
        r["cost"] = usage["usd"]
    return r
