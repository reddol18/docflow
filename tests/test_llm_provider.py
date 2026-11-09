"""LLM 제공자 고르기 (https://github.com/reddol18/docflow/issues/13 완료 조건 1).
설정만 바꿔 openai / local / off가 각각 골라지는지, local은 현장 내부 주소만 받는지, off일 때 OpenAI 클라이언트를
한 번도 만들거나 부르지 않고 OCR → 문서 나누기 → 추출 → 판독 → 사람 검토 → 색인이 끝까지 도는지 본다.
실제 OpenAI·Ollama 호출 없음. 값은 지어낸 것."""
import asyncio
from types import SimpleNamespace

import numpy as np
import pytest

from app import errors, llm


class FakeOpenAI:
    """OpenAI 대신 끼우는 가짜. 만든 횟수·부른 인자를 센다"""
    made = []
    calls = []

    def __init__(self, **kw):
        FakeOpenAI.made.append(kw)

        def create(**a):
            FakeOpenAI.calls.append(("chat", a))
            return SimpleNamespace(usage=None, choices=[SimpleNamespace(
                finish_reason="stop", logprobs=None,
                message=SimpleNamespace(content='{"title": {"value": "가상 제목", "confidence": 0.9}}'))])

        def embed(**a):
            FakeOpenAI.calls.append(("embed", a))
            return SimpleNamespace(usage=None, data=[SimpleNamespace(embedding=[0.0]) for _ in a["input"]])
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
        self.embeddings = SimpleNamespace(create=embed)


@pytest.fixture
def fake_openai(monkeypatch):
    FakeOpenAI.made, FakeOpenAI.calls = [], []
    monkeypatch.setattr(llm, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(llm, "_client", None)
    return FakeOpenAI


def use(monkeypatch, env):
    """환경변수만으로 제공자를 바꾼 것과 같게: load_provider(env)를 PROVIDER로"""
    monkeypatch.setattr(llm, "PROVIDER", llm.load_provider(env))
    monkeypatch.setattr(llm, "_client", None)


LOCAL = {"LLM_PROVIDER": "local", "LLM_BASE_URL": "http://127.0.0.1:11434/v1", "EXTRACT_MODEL": "가상-추출",
         "CELL_MODEL": "가상-판독", "EMBED_MODEL": "가상-임베딩", "CHAT_MODEL": "가상-답변"}


# ---------- 설정 → 제공자 ----------

def test_default_is_openai_with_current_models():
    p = llm.load_provider({})
    assert p.name == "openai" and p.on and p.base_url is None
    assert p.models == {"extract": "gpt-4.1-mini", "cell": "gpt-4.1-mini", "embed": "text-embedding-3-small",
                        "chat": "gpt-4o-mini"}
    # 기존 배포 설정(기능별 모델 환경변수)도 그대로 먹는다. openai는 LLM_BASE_URL을 보지 않는다
    p = llm.load_provider({"LLM_PROVIDER": "openai", "EXTRACT_MODEL": "gpt-4o-mini", "LLM_BASE_URL": "https://x.example.com"})
    assert p.model("extract") == "gpt-4o-mini" and p.base_url is None


def test_local_provider():
    p = llm.load_provider({**LOCAL, "LLM_PROVIDER": " Local ", "CELL_MODEL": ""})
    assert p.name == "local" and p.base_url == "http://127.0.0.1:11434/v1" and p.key_file is None
    assert p.model("extract") == "가상-추출" and p.model("embed") == "가상-임베딩"
    with pytest.raises(llm.ProviderConfigError, match="CELL_MODEL"):  # 로컬에는 OpenAI 모델 이름을 기본값으로 두지 않는다
        p.model("cell")
    with pytest.raises(llm.ProviderConfigError, match="LLM_BASE_URL"):
        llm.load_provider({"LLM_PROVIDER": "local"})


def test_off_provider_and_unknown():
    p = llm.load_provider({"LLM_PROVIDER": "off", "EXTRACT_MODEL": "gpt-4.1-mini"})
    assert p.name == "off" and not p.on
    with pytest.raises(llm.LLMOff):
        p.model("extract")
    with pytest.raises(llm.ProviderConfigError, match="anthropic"):
        llm.load_provider({"LLM_PROVIDER": "anthropic"})


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:11434/v1", "http://localhost:11434/v1", "http://[::1]:8000/v1", "http://ollama:11434/v1",
    "http://ollama.llm.svc:11434/v1", "http://ollama.llm.svc.cluster.local:11434/v1", "https://10.1.2.3/v1",
    "http://172.20.0.5:8000/v1", "http://192.168.0.10/v1"])
def test_local_url_internal_allowed(url):
    assert llm.load_provider({**LOCAL, "LLM_BASE_URL": url}).base_url == url


@pytest.mark.parametrize("url, why", [
    ("https://api.openai.com/v1", "내부 서버 전용"), ("https://llm.example.com/v1", "내부 서버 전용"),
    ("http://8.8.8.8/v1", "내부 서버 전용"), ("http://172.32.0.1/v1", "내부 서버 전용"),
    ("http://169.254.169.254/latest", "내부 서버 전용"), ("http://0.0.0.0:11434/v1", "내부 서버 전용"),
    ("ftp://127.0.0.1/v1", "http/https만"), ("file:///etc/passwd", "http/https만"), ("127.0.0.1:11434", "http/https만"),
    ("http://127.0.0.1:port/v1", "읽을 수 없음")])
def test_local_url_rejected(url, why):
    with pytest.raises(llm.ProviderConfigError, match=why):
        llm.load_provider({**LOCAL, "LLM_BASE_URL": url})


def test_local_key_file(tmp_path, monkeypatch, fake_openai):
    with pytest.raises(llm.ProviderConfigError, match="LLM_API_KEY_FILE"):
        llm.load_provider({**LOCAL, "LLM_API_KEY_FILE": str(tmp_path / "없는-파일")})
    key = tmp_path / "api-key"
    key.write_text("fake-local-key\n")
    use(monkeypatch, {**LOCAL, "LLM_API_KEY_FILE": str(key)})
    llm.client()
    assert fake_openai.made[-1]["api_key"] == "fake-local-key"


def test_require_models_and_kinds(monkeypatch):
    from test_fields import other

    from app import graph
    assert graph.llm_kinds(graph.SCHEMA) == {"extract", "cell", "embed", "chat"}  # 김해: 추출·손글씨 칸 있음
    assert graph.llm_kinds(other()) == {"extract", "embed", "chat"}  # 손글씨 규칙 칸 없음
    use(monkeypatch, {**LOCAL, "CELL_MODEL": "", "CHAT_MODEL": ""})
    llm.require_models({"extract", "embed"})
    with pytest.raises(llm.ProviderConfigError, match="CELL_MODEL, CHAT_MODEL"):
        llm.require_models(graph.llm_kinds(graph.SCHEMA))
    use(monkeypatch, {})
    llm.require_models({"cell"})  # openai는 기본값이 있어 보지 않는다


def test_client_per_provider(monkeypatch, tmp_path, fake_openai):
    key = tmp_path / "key"
    key.write_text("fake-key-for-test\n")
    monkeypatch.setenv("OPENAI_KEY_FILE", str(key))
    use(monkeypatch, {})
    llm.client()
    assert fake_openai.made[-1]["api_key"] == "fake-key-for-test" and "base_url" not in fake_openai.made[-1]
    use(monkeypatch, {**LOCAL, "LLM_BASE_URL": "http://127.0.0.1:8001/v1"})
    llm.client()
    assert fake_openai.made[-1]["base_url"] == "http://127.0.0.1:8001/v1" and fake_openai.made[-1]["api_key"] == "unused"
    use(monkeypatch, {"LLM_PROVIDER": "off"})
    with pytest.raises(llm.LLMOff):
        llm.client()
    assert len(fake_openai.made) == 2


def test_local_calls_use_local_models_without_budget(monkeypatch, fake_openai):
    from app import store
    use(monkeypatch, LOCAL)
    monkeypatch.setattr(store, "spent_today", lambda: pytest.fail("로컬 제공자는 한도(OpenAI 사용액)를 보지 않는다"))
    logged = []
    monkeypatch.setattr(store, "add_usage", lambda *a: logged.append(a))
    out = llm.extract("v", ["가상 줄"])
    assert out["title"] == {"value": "가상 제목", "confidence": 0.9}
    assert llm.embed("v", ["가"]) == [[0.0]]
    assert [c[1]["model"] for c in fake_openai.calls] == ["가상-추출", "가상-임베딩"]
    assert [(a[2], a[5]) for a in logged] == [("가상-추출", 0.0), ("가상-임베딩", 0.0)]  # usage 없는 응답도 0원으로 남김


def test_openai_budget_and_max_tokens(monkeypatch, fake_openai):
    """openai는 한도를 보고, 추출·답변에 출력 상한을 건다"""
    from app import store
    monkeypatch.setattr(store, "add_usage", lambda *a: None)
    monkeypatch.setattr(llm, "_key", lambda: "fake-key-for-test")
    seen = []
    monkeypatch.setattr(store, "spent_today", lambda: seen.append(1) or 0.0)
    llm.extract("v", ["가상 줄"])
    llm.answer("가상 질문", [{"doc_id": "v1-0001", "body": "가상 본문"}])
    assert len(seen) == 2
    (_, ex), (_, an) = fake_openai.calls
    assert ex["max_tokens"] == 512 == llm.EXTRACT_MAX_TOKENS and an["max_tokens"] == 1024 == llm.ANSWER_MAX_TOKENS
    monkeypatch.setattr(store, "spent_today", lambda: llm.BUDGET)
    with pytest.raises(llm.BudgetExceeded):
        llm.extract("v", ["가상 줄"])


def test_openai_price_table_kept():
    """비용 계산(평가 하네스 gate.py가 씀)은 그대로"""
    assert llm.PRICE["gpt-4.1-mini"] == (0.40, 1.60)
    assert set(llm.OPENAI_MODELS.values()) <= set(llm.PRICE)


def test_llm_off_error_code():
    assert errors.public(llm.LLMOff("LLM_PROVIDER=off")) == {"code": "llm_off", "message": errors.MESSAGES["llm_off"]}
    assert "LLM_PROVIDER" not in errors.MESSAGES["llm_off"]  # 설정 이름은 서버 로그에만


# ---------- off: OpenAI 0회로 흐름이 끝까지 ----------

def _fake_volume(tmp_path, monkeypatch):
    from app import layout, ocr, store
    monkeypatch.setattr(store, "DATA", tmp_path)
    monkeypatch.setattr(store, "spent_today", lambda: pytest.fail("off면 사용액도 보지 않는다"))
    monkeypatch.setattr(store, "add_usage", lambda *a: pytest.fail("off면 사용량을 남길 호출이 없다"))
    (tmp_path / "pages" / "v1").mkdir(parents=True)
    for p in ["v1-0001", "v1-0002", "v1-0003"]:
        (tmp_path / "pages" / "v1" / f"{p}.jpg").write_bytes(b"x")
    starts = {"v1-0001", "v1-0003"}
    monkeypatch.setattr(ocr, "ocr_page", lambda path: {"lines": [f"가상 줄 {path.stem}"], "items": []})
    monkeypatch.setattr(ocr, "read_image", lambda path: np.zeros((100, 100, 3), np.uint8))
    monkeypatch.setattr(layout, "is_start", lambda d: (d["page"] in starts, {}))
    monkeypatch.setattr(layout, "value_box", lambda items, k, ratio: ((0, 0, 10, 10), None))
    monkeypatch.setattr(layout, "fix_recipient", lambda d, v: (v, ""))
    rec = SimpleNamespace(events=[], saved=[], vectors=[], dropped=[])
    monkeypatch.setattr(store, "event", lambda vol, node, status, doc=None, **k: rec.events.append((node, status, k)))
    monkeypatch.setattr(store, "save_documents", lambda docs: rec.saved.extend(docs))
    monkeypatch.setattr(store, "upsert_vectors", lambda docs, vecs: rec.vectors.append(docs))
    monkeypatch.setattr(store, "delete_doc_vectors", lambda ids: rec.dropped.append(list(ids)))
    return rec


@pytest.mark.parametrize("mode", ["human", "auto"])
def test_off_runs_whole_flow_without_openai(tmp_path, monkeypatch, fake_openai, mode):
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.types import Command

    from app import graph
    use(monkeypatch, {"LLM_PROVIDER": "off"})
    rec = _fake_volume(tmp_path, monkeypatch)
    g = graph.build(MemorySaver())
    c = {"configurable": {"thread_id": "v1"}}
    g.invoke({"vol": "v1", "review_mode": mode}, c)

    # auto여도 LLM을 건너뛴 문서는 사람 검토에서 멈춘다 — 모든 문서에 'LLM 꺼짐' 표시
    state = g.get_state(c)
    assert state.next == ("review",) and rec.saved == []
    flagged = state.tasks[0].interrupts[0].value["flagged"]
    assert [d["doc_id"] for d in flagged] == ["v1-0001", "v1-0003"]
    for d in flagged:
        assert {k: v for k, v in d["flags"].items() if graph.OFF_FLAG in v}.keys() == {"serial", "date", "recipient", "title"}
        assert d["fields"]["title"] == "" and d["fields"]["docno"] == "" and d["fields"]["date"] == ""  # 가짜 값 없이 빈 칸
        assert d["hw"] == {"docno": {"text": "", "conf": 0.0}, "date": {"text": "", "conf": 0.0}}

    g.invoke(Command(resume={"v1-0001": {"title": "사람이 쓴 가상 제목"}}), c)  # 사람이 채우고 이어 간다
    state = g.get_state(c)
    assert fake_openai.made == [] and fake_openai.calls == []  # OpenAI 클라이언트를 만들지도 부르지도 않음
    assert not state.next and state.values["indexed"] == 2 and state.values["embedded"] == 0  # 끝까지 돎
    assert [d["doc_id"] for d in rec.saved] == ["v1-0001", "v1-0003"] and rec.saved[0]["fields"]["title"] == "사람이 쓴 가상 제목"
    assert rec.vectors == [] and rec.dropped == [["v1-0001", "v1-0003"]]  # 벡터는 넣지 않고 예전 벡터는 지움
    assert all(d["info"]["llm_skipped"] == ["extract", "read"] and d["reviewed"] for d in rec.saved)
    for node in ("extract", "read"):  # 진행 화면(events)에 건너뜀이 드러난다
        assert sum(n == node and s == "end" and k.get("skipped") == "llm_off" for n, s, k in rec.events) == 2
    assert any(n == "index" and k.get("embedded") == 0 and k.get("indexed") == 2 for n, _, k in rec.events)


def test_off_flags_other_schema(monkeypatch):
    """김해 rule 칸이 없는 스키마: LLM을 건너뛴 문서는 'LLM 꺼짐'으로 표시되고 auto여도 사람 검토로.
    건너뛴 추출은 확신도 0(fields.parse_extract — 실제 흐름과 같은 b)이지만 'LLM'(낮은 확신도) 사유는 붙이지 않는다 —
    부르지 않은 것이지 LLM이 낮게 본 값이 아니다. 건너뛰지 않은 문서는 LLM 추출 칸(llm_extract·vocab)마다 확신도로 표시"""
    from test_fields import other

    from app import fields, graph
    s = other()
    monkeypatch.setattr(graph, "SCHEMA", s)
    empty = {"subject": "", "kind": "", "made": "", "shelf": "", "owner": "가상시"}
    off = {"doc_id": "v1-0001", "fields": empty, "b": fields.parse_extract(s, "{}"), "info": {"llm_skipped": ["extract"]}}
    sure = {k: {"value": "가", "confidence": 0.99} for k in ("subject", "kind", "made")}
    ok = {"doc_id": "v1-0002", "fields": empty, "b": sure, "info": {"llm_skipped": []}}
    low = {**ok, "doc_id": "v1-0003", "b": {**sure, "kind": {"value": "가", "confidence": 0.1}}}
    out = graph.validate({"docs": [off, ok, low]})["docs"]
    assert all(b["confidence"] == 0.0 for b in off["b"].values())
    assert out[0]["flags"] == {"kind": [graph.OFF_FLAG], "made": [graph.OFF_FLAG], "subject": [graph.OFF_FLAG]}
    assert out[1]["flags"] == {}  # 확신도 높은 LLM 값 — 표시 없음
    assert out[2]["flags"] == {"kind": ["LLM"]}  # 김해 아닌 스키마도 LLM 확신도 표시 (https://github.com/reddol18/docflow/issues/72)
    assert graph.route_review({"review_mode": "auto", "docs": out}) == "review"
    assert graph.route_review({"review_mode": "auto", "docs": out[1:2]}) == "index"


def test_off_edit_drops_vector(monkeypatch, fake_openai):
    """off에서 칸을 고치면 저장·이력은 남기고, 그 문서의 예전 벡터는 지운다 (임베딩 호출 없음)"""
    from app import main, store
    use(monkeypatch, {"LLM_PROVIDER": "off"})
    doc = {"doc_id": "v1-0001", "vol": "v1", "pages": ["v1-0001"],
           "fields": {"docno": "", "serial": "", "date": "", "recipient": "", "title": ""}}
    updated = []
    monkeypatch.setattr(main, "G", SimpleNamespace(
        get_state=lambda c: SimpleNamespace(interrupts=(), values={"docs": [doc]}),
        update_state=lambda c, v, as_node: updated.append(v)))
    rec = SimpleNamespace(saved=[], edits=[], dropped=[])
    monkeypatch.setattr(store, "save_documents", lambda docs: rec.saved.extend(docs))
    monkeypatch.setattr(store, "add_edits", lambda vol, rows, uid, source: rec.edits.extend(rows))
    monkeypatch.setattr(store, "event", lambda *a, **k: None)
    monkeypatch.setattr(store, "upsert_vectors", lambda *a: pytest.fail("off면 벡터를 넣지 않는다"))
    monkeypatch.setattr(store, "delete_doc_vectors", lambda ids: rec.dropped.append(list(ids)))
    d, changed, _ = main._edit_document("v1", "v1-0001", {"title": "가상 제목"}, SimpleNamespace(id=1, username="가상"))
    assert changed == ["title"] and d["fields"]["title"] == "가상 제목" and rec.saved == [d]
    assert rec.edits == [("v1-0001", "title", "", "가상 제목")] and rec.dropped == [["v1-0001"]]
    assert fake_openai.made == []


def test_off_search_and_ask_unavailable(monkeypatch, fake_openai):
    from app import main, store
    use(monkeypatch, {"LLM_PROVIDER": "off"})
    monkeypatch.setattr(store, "keyword_search", lambda *a, **k: pytest.fail("off면 낱말 검색으로 바꿔 답하지 않는다 (정하지 않음)"))
    for call in (lambda: main.search("가상", None), lambda: main.ask("가상", None)):
        with pytest.raises(llm.LLMOff):
            call()
    assert fake_openai.made == []
    # 한 곳(예외 처리기)에서 503 + 공개 문구로 — 설정 이름은 응답에 넣지 않는다
    assert main.app.exception_handlers[llm.LLMOff] is main.llm_off
    req = SimpleNamespace(method="GET", url=SimpleNamespace(path="/search"))
    r = asyncio.run(main.llm_off(req, llm.LLMOff("LLM_PROVIDER=off")))
    assert r.status_code == 503 and b"LLM_PROVIDER" not in r.body and errors.MESSAGES["llm_off"].encode() in r.body
