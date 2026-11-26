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
        try:
            yield m
        except BaseException:  # 요청은 이미 갔을 수 있다 — 예상 비용 그대로 장부에 (모자라게 세지 않게)
            metered.last = {"model": model, "prompt": 0, "completion": 0, "usd": est}
            with _ledger_path().open("a", encoding="utf-8") as f:
                f.write(json.dumps({"kind": f"{kind}:failed", "model": model, "usd": est}) + "\n")
            raise
        tin, tout = llm._tokens(m.usage)
        pi, po = llm.price_of(model)
        usd = (tin * pi + tout * po) / 1e6 if m.usage is not None else est
        metered.last = {"model": model, "prompt": tin, "completion": tout, "usd": usd}
        with _ledger_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps({"kind": kind, "model": model, "usd": usd}) + "\n")
    metered.last = None
    return metered


def production_fields(lines, b, failed):
    """운영 흐름(app/graph.py extract → merge)과 같은 최종 칸 값과 'OCR 근거 없음' 표시 칸.
    수신은 layout.fix_recipient(배치 규칙)를 거치고, 문서번호·시행일자는 손글씨 판독 없이(hw 없음 — 판독이 못 읽은 경우와 같음)
    규칙 값을 만든다. 표시는 LLM 값이 최종 값에 들어간 칸만 (graph.merge와 같은 grounding.reached).
    사례에는 OCR 상자(items)가 없어 배치 규칙 중 상자를 보는 것(참조 부서장·잘림)은 아무것도 붙이지 않는다"""
    from app import graph, layout  # noqa: PLC0415 — 무거운 모듈은 추출 사례에서만
    b = dict(b)
    rec, _ = layout.fix_recipient({"lines": lines, "items": []}, (b.get("recipient") or {}).get("value") or "")
    rule_values, _ = graph.rule_values_of(b, {}, rec, 1)
    final = fields.assemble(rt.SCHEMA, b, rule_values)
    cand = grounding.check_extract(fields.prompt_fields(rt.SCHEMA), b, lines, failed)
    flagged = [n for n in cand if grounding.reached((b.get(n) or {}).get("value"), final.get(n))]
    shown = {f["name"] for f in fields.prompt_fields(rt.SCHEMA)}
    return {k: v for k, v in final.items() if k in shown}, flagged


def run_case(case, real_client, cap=rt.CAP_USD):
    """사례 하나를 운영 함수로 돌린다 → (출력 dict, 사용량 dict|None). real_client는 OpenAI SDK 클라이언트(시험은 가짜)"""
    rec = _Recorder(real_client)
    meter = _meter(cap)
    saved = llm.PROVIDER, llm._metered, llm.client
    # 운영 기본 모델 (EXTRACT_MODEL·CHAT_MODEL 환경변수는 무시, 후보 답변 모델만 DOCFLOW_RT27_CHAT_MODEL)
    llm.PROVIDER = rt.harness_provider()
    llm._metered, llm.client = meter, (lambda: rec)
    out = {"kind": case["kind"], "id": case["id"]}
    try:
        if case["kind"] == "extract":
            try:
                res = llm.extract(f"redteam27-{case['id']}", case["lines"], rt.SCHEMA)
                out["fields"] = {k: v["value"] for k, v in res.items()}
                out["failed"] = list(getattr(res, "failed", ()))
                out["fields"], out["flagged"] = production_fields(case["lines"], res, out["failed"])
            except llm.ExtractInvalid as e:  # 운영: 그 문서의 추출 칸 모두 '추출 실패'(빈 값)
                out["fields"], out["failed"], out["invalid"] = {}, ["*"], str(e)
        else:
            out["screen"], out["warnings"] = llm.answer(case["question"], case["docs"], rt.SCHEMA)
    except llm.BudgetExceeded as e:  # 우리 코드의 문구(상한·장부)만 — 외부 응답 글은 call_api가 이름만 남긴다
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
    try:
        real = llm.make_client(rt.harness_provider())
        out, usage = run_case(case, real)
    except Exception as e:  # noqa: BLE001 — 예외 문구(401의 키 일부 등)는 결과·화면에 남기지 않고 이름만
        return {"output": json.dumps({"kind": case["kind"], "id": case["id"], "error": type(e).__name__}),
                "error": type(e).__name__}
    r = {"output": json.dumps(out, ensure_ascii=False)}
    if usage:
        r["tokenUsage"] = {"prompt": usage["prompt"], "completion": usage["completion"],
                           "total": usage["prompt"] + usage["completion"]}
        r["cost"] = usage["usd"]
    return r
