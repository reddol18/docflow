"""LLM 제공자 고르기 (https://github.com/reddol18/docflow/issues/13 완료 조건 1).
설정만 바꿔 openai / local / off가 각각 골라지는지, off일 때 OpenAI 클라이언트를 한 번도 만들거나 부르지 않고
OCR → 문서 나누기 → 추출 → 판독 → 색인이 끝까지 도는지 본다. 실제 OpenAI·Ollama 호출 없음. 값은 지어낸 것."""
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import HTTPException

from app import errors, llm


class FakeOpenAI:
    """OpenAI 대신 끼우는 가짜. 만든 횟수·부른 횟수를 센다"""
    made = []
    calls = []

    def __init__(self, **kw):
        FakeOpenAI.made.append(kw)

        def create(**a):
            FakeOpenAI.calls.append(("chat", a["model"]))
            return SimpleNamespace(usage=None, choices=[SimpleNamespace(
                message=SimpleNamespace(content='{"title": {"value": "가상 제목", "confidence": 0.9}}'), logprobs=None)])

        def embed(**a):
            FakeOpenAI.calls.append(("embed", a["model"]))
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


# ---------- 설정 → 제공자 ----------

def test_default_is_openai_with_current_models():
    p = llm.load_provider({})
    assert p.name == "openai" and p.on and p.base_url is None
    assert p.models == {"extract": "gpt-4.1-mini", "cell": "gpt-4.1-mini", "embed": "text-embedding-3-small",
                        "chat": "gpt-4o-mini"}
    # 기존 배포 설정(기능별 모델 환경변수)도 그대로 먹는다
    assert llm.load_provider({"LLM_PROVIDER": "openai", "EXTRACT_MODEL": "gpt-4o-mini"}).model("extract") == "gpt-4o-mini"


def test_local_provider():
    p = llm.load_provider({"LLM_PROVIDER": " Local ", "LLM_BASE_URL": "http://127.0.0.1:11434/v1",
                           "EXTRACT_MODEL": "가상-모델:1b", "EMBED_MODEL": "가상-임베딩"})
    assert p.name == "local" and p.base_url == "http://127.0.0.1:11434/v1"
    assert p.model("extract") == "가상-모델:1b" and p.model("embed") == "가상-임베딩"
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


def test_client_per_provider(monkeypatch, tmp_path, fake_openai):
    key = tmp_path / "key"
    key.write_text("fake-key-for-test\n")
    monkeypatch.setenv("OPENAI_KEY_FILE", str(key))
    monkeypatch.delenv("LLM_API_KEY_FILE", raising=False)
    use(monkeypatch, {})
    llm.client()
    assert fake_openai.made[-1]["api_key"] == "fake-key-for-test" and "base_url" not in fake_openai.made[-1]
    use(monkeypatch, {"LLM_PROVIDER": "local", "LLM_BASE_URL": "http://127.0.0.1:8001/v1"})
    llm.client()
    assert fake_openai.made[-1]["base_url"] == "http://127.0.0.1:8001/v1" and fake_openai.made[-1]["api_key"] == "unused"
    use(monkeypatch, {"LLM_PROVIDER": "off"})
    with pytest.raises(llm.LLMOff):
        llm.client()
    assert len(fake_openai.made) == 2


def test_local_calls_use_local_models_without_budget(monkeypatch, fake_openai):
    from app import store
    use(monkeypatch, {"LLM_PROVIDER": "local", "LLM_BASE_URL": "http://127.0.0.1:11434/v1",
                      "EXTRACT_MODEL": "가상-추출", "EMBED_MODEL": "가상-임베딩", "CHAT_MODEL": "가상-답변"})
    monkeypatch.setattr(store, "spent_today", lambda: pytest.fail("로컬 제공자는 한도(OpenAI 사용액)를 보지 않는다"))
    logged = []
    monkeypatch.setattr(store, "add_usage", lambda *a: logged.append(a))
    out = llm.extract("v", ["가상 줄"])
    assert out["title"] == {"value": "가상 제목", "confidence": 0.9}
    assert llm.embed("v", ["가"]) == [[0.0]]
    assert [c[1] for c in fake_openai.calls] == ["가상-추출", "가상-임베딩"]
    assert [(a[2], a[5]) for a in logged] == [("가상-추출", 0.0), ("가상-임베딩", 0.0)]  # usage 없는 응답도 0원으로 남김


def test_openai_price_table_kept():
    """비용 계산(평가 하네스 gate.py가 씀)은 그대로"""
    assert llm.PRICE["gpt-4.1-mini"] == (0.40, 1.60)
    assert set(llm.OPENAI_MODELS.values()) <= set(llm.PRICE)


def test_llm_off_error_code():
    assert errors.public(llm.LLMOff("x")) == {"code": "llm_off", "message": errors.MESSAGES["llm_off"]}


# ---------- off: OpenAI 0회로 흐름이 끝까지 ----------

def test_off_runs_whole_flow_without_openai(tmp_path, monkeypatch, fake_openai):
    from langgraph.checkpoint.memory import MemorySaver

    from app import graph, layout, ocr, store
    use(monkeypatch, {"LLM_PROVIDER": "off"})
    monkeypatch.setattr(store, "DATA", tmp_path)
    monkeypatch.setattr(store, "spent_today", lambda: pytest.fail("off면 사용액도 보지 않는다"))
    monkeypatch.setattr(store, "add_usage", lambda *a: pytest.fail("off면 사용량을 남길 호출이 없다"))
    pages = ["v1-0001", "v1-0002", "v1-0003"]
    (tmp_path / "pages" / "v1").mkdir(parents=True)
    for p in pages:
        (tmp_path / "pages" / "v1" / f"{p}.jpg").write_bytes(b"x")
    starts = {"v1-0001", "v1-0003"}
    monkeypatch.setattr(ocr, "ocr_page", lambda path: {"lines": [f"가상 줄 {path.stem}"], "items": []})
    monkeypatch.setattr(ocr, "read_image", lambda path: np.zeros((100, 100, 3), np.uint8))
    monkeypatch.setattr(layout, "is_start", lambda d: (d["page"] in starts, {}))
    monkeypatch.setattr(layout, "value_box", lambda items, k, ratio: ((0, 0, 10, 10), None))
    monkeypatch.setattr(layout, "fix_recipient", lambda d, v: (v, ""))
    events, saved, vectors = [], [], []
    monkeypatch.setattr(store, "event", lambda vol, node, status, doc=None, **k: events.append((node, status, k)))
    monkeypatch.setattr(store, "save_documents", lambda docs: saved.extend(docs))
    monkeypatch.setattr(store, "upsert_vectors", lambda docs, vecs: vectors.append(docs))

    g = graph.build(MemorySaver())
    g.invoke({"vol": "v1", "review_mode": "auto"}, {"configurable": {"thread_id": "v1"}})
    state = g.get_state({"configurable": {"thread_id": "v1"}})

    assert fake_openai.made == [] and fake_openai.calls == []  # OpenAI 클라이언트를 만들지도 부르지도 않음
    assert not state.next and state.values["indexed"] == 2 and state.values["embedded"] == 0  # 끝까지 돎
    assert [d["doc_id"] for d in saved] == ["v1-0001", "v1-0003"] and vectors == []
    for d in saved:
        assert d["info"]["llm_skipped"] == ["extract", "read"]
        assert d["fields"]["title"] == "" and d["fields"]["docno"] == "" and d["fields"]["date"] == ""  # 가짜 값 없이 빈 칸
        assert d["hw"] == {"docno": {"text": "", "conf": 0.0}, "date": {"text": "", "conf": 0.0}}
        assert d["flags"]  # 빈 칸은 검토 표시 → 사람 검토로
    for node in ("extract", "read"):  # 진행 화면(events)에 건너뜀이 드러난다
        assert sum(n == node and s == "end" and k.get("skipped") == "llm_off" for n, s, k in events) == 2
    assert any(n == "index" and k.get("embedded") == 0 and k.get("indexed") == 2 for n, _, k in events)


def test_off_search_and_ask_unavailable(monkeypatch, fake_openai):
    from app import main, store
    use(monkeypatch, {"LLM_PROVIDER": "off"})
    monkeypatch.setattr(store, "keyword_search", lambda *a, **k: pytest.fail("off면 낱말 검색으로 바꿔 답하지 않는다 (정하지 않음)"))
    for call in (lambda: main.search("가상", None), lambda: main.ask("가상", None)):
        with pytest.raises(HTTPException) as e:
            call()
        assert e.value.status_code == 503 and "LLM_PROVIDER=off" in e.value.detail
    assert fake_openai.made == []
