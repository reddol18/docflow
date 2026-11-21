"""임베딩 모델·차원 → Qdrant 모음 (https://github.com/reddol18/docflow/issues/81, docs/design.md '임베딩 비교').
기본 설정(openai text-embedding-3-small, 1536)은 지금 모음 'documents'를 그대로 쓰고, 다른 모델은 이름에 모델·차원이 든
새 모음을 쓴다. 있는 모음의 차원이 설정과 다르면 시작할 때 멈춘다. Qdrant는 메모리 모드 (DB 없음)."""
import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from app import store


@pytest.mark.parametrize("env", [{}, {"LLM_PROVIDER": "openai"}, {"LLM_PROVIDER": "off"},
                                 {"EMBED_MODEL": "text-embedding-3-small"},
                                 {"EMBED_MODEL": "text-embedding-3-small", "EMBED_DIM": "1536"}])
def test_default_keeps_current_collection(env):
    """지금 배포(환경변수 없음)는 설정을 바꾸지 않아도 예전과 같은 모음 'documents', 1536차원"""
    assert store.embed_config(env) == ("text-embedding-3-small", 1536, "documents")


def test_module_default_is_documents():
    """conftest가 EMBED_* 를 비운 상태로 불러온 app.store = 운영 기본값"""
    assert (store.EMBED_MODEL, store.EMBED_DIM, store.COLLECTION) == ("text-embedding-3-small", 1536, "documents")


def test_local_bge_m3_gets_own_collection():
    env = {"LLM_PROVIDER": "local", "EMBED_MODEL": "bge-m3", "EMBED_DIM": "1024"}
    assert store.embed_config(env) == ("bge-m3", 1024, "documents_bge-m3_1024")


@pytest.mark.parametrize("model,dim,name", [
    ("bge-m3:latest", 1024, "documents_bge-m3_1024"),  # :latest는 떼어 같은 모델·같은 모음
    (" bge-m3 ", 1024, "documents_bge-m3_1024"),
    ("qwen3-embedding:0.6b", 1024, "documents_qwen3-embedding-0-6b_1024"),
    ("org/model.v2", 768, "documents_org-model-v2_768"),
    ("text-embedding-3-small", 512, "documents_text-embedding-3-small_512"),  # 같은 모델이라도 차원이 다르면 다른 모음
    ("text-embedding-3-large", 3072, "documents_text-embedding-3-large_3072"),
])
def test_collection_name(model, dim, name):
    assert store.collection_name(model, dim) == name


@pytest.mark.parametrize("env,msg", [
    ({"LLM_PROVIDER": "local"}, "EMBED_MODEL 필요"),
    ({"LLM_PROVIDER": "local", "EMBED_MODEL": "bge-m3"}, "EMBED_DIM"),  # 모르는 차원을 1536으로 짐작하지 않는다
    ({"EMBED_MODEL": "text-embedding-3-large"}, "EMBED_DIM"),
    ({"EMBED_DIM": "abc"}, "양의 정수"),
    ({"EMBED_DIM": "0"}, "양의 정수"),
    ({"EMBED_DIM": "-5"}, "양의 정수"),
    ({"EMBED_DIM": "512"}, "1536차원만"),  # 기본 모델은 1536만 (dimensions=를 보내지 않는다)
    ({"EMBED_MODEL": "text-embedding-3-small", "EMBED_DIM": "3072"}, "1536차원만"),
])
def test_bad_config_stops(env, msg):
    with pytest.raises(store.EmbedConfigError, match=msg):
        store.embed_config(env)


def test_ensure_collection_creates_with_dim():
    q = QdrantClient(":memory:")
    assert store.ensure_collection(q, "documents_bge-m3_1024", 1024) is True
    assert store.collection_dim(q, "documents_bge-m3_1024") == 1024
    assert store.ensure_collection(q, "documents_bge-m3_1024", 1024) is False  # 두 번째 시작: 그대로 쓴다


def test_existing_default_collection_is_reused():
    """지금 배포의 모음(documents, 1536)은 기본 설정에서 그대로 쓰인다 — 새로 만들거나 지우지 않는다"""
    q = QdrantClient(":memory:")
    q.create_collection("documents", vectors_config=VectorParams(size=1536, distance=Distance.COSINE))
    q.upsert("documents", points=[PointStruct(id=1, vector=[0.1] * 1536, payload={"vol": "v", "doc_id": "d"})])
    assert store.ensure_collection(q, "documents", 1536) is False
    assert q.count("documents", exact=True).count == 1


def test_dimension_mismatch_stops():
    q = QdrantClient(":memory:")
    q.create_collection("documents", vectors_config=VectorParams(size=1536, distance=Distance.COSINE))
    with pytest.raises(store.EmbedConfigError, match="1536차원"):
        store.ensure_collection(q, "documents", 1024)


def test_new_model_leaves_legacy_collection(capsys):
    """모델을 바꾸면 새 모음을 만들고 예전 'documents'는 남긴다(되돌리기용) — 알림을 남긴다"""
    q = QdrantClient(":memory:")
    q.create_collection("documents", vectors_config=VectorParams(size=1536, distance=Distance.COSINE))
    assert store.ensure_collection(q, "documents_bge-m3_1024", 1024) is True
    assert q.collection_exists("documents")
    assert "예전 모음 'documents'" in capsys.readouterr().out


def test_named_vectors_collection_stops():
    q = QdrantClient(":memory:")
    q.create_collection("documents", vectors_config={"a": VectorParams(size=4, distance=Distance.COSINE)})
    with pytest.raises(store.EmbedConfigError, match="벡터 설정"):
        store.ensure_collection(q, "documents", 4)


def test_offline_settings_import(tmp_path):
    """k8s/offline 값(LLM_PROVIDER=local·bge-m3 1024·LLM_TIMEOUT_SECONDS)으로 app.llm·app.store를 새 프로세스에서 불러온다 —
    불러올 때(모듈 맨 위 load_provider·embed_config) 멈추지 않는지. 2026-11-21 WSL 시험에서 local_timeout이 load_provider보다
    뒤에 정의돼 NameError로 파드가 뜨지 않은 것을 막는다. 호출·연결 없음"""
    import os
    import subprocess
    import sys
    from pathlib import Path
    env = {**os.environ, "LLM_PROVIDER": "local", "LLM_BASE_URL": "http://ollama.llm.svc.cluster.local:11434/v1",
           "EXTRACT_MODEL": "qwen2.5:7b", "CHAT_MODEL": "qwen2.5:7b", "CELL_MODEL": "qwen2.5vl:7b",
           "EMBED_MODEL": "bge-m3", "EMBED_DIM": "1024", "LLM_TIMEOUT_SECONDS": "900", "DATA_DIR": str(tmp_path)}
    code = ("from app import llm, store; "
            "print(llm.PROVIDER.name, llm.local_timeout(), store.COLLECTION, store.EMBED_DIM)")
    r = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1], env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    assert r.stdout.split() == ["local", "900.0", "documents_bge-m3_1024", "1024"]


def test_latest_tag_is_same_model():
    a = store.embed_config({"LLM_PROVIDER": "local", "EMBED_MODEL": "bge-m3:latest", "EMBED_DIM": "1024"})
    b = store.embed_config({"LLM_PROVIDER": "local", "EMBED_MODEL": "bge-m3", "EMBED_DIM": "1024"})
    assert a == b == ("bge-m3", 1024, "documents_bge-m3_1024")


def test_new_collection_records_model():
    q = QdrantClient(":memory:")
    store.ensure_collection(q, "documents_bge-m3_1024", 1024, "bge-m3:latest")
    assert store.collection_model(q, "documents_bge-m3_1024") == "bge-m3"
    assert q.get_collection("documents_bge-m3_1024").config.metadata["embed_dim"] == 1024
    assert store.ensure_collection(q, "documents_bge-m3_1024", 1024, "bge-m3") is False  # 같은 모델(정규화)이면 그대로


def test_slug_collision_other_model_stops():
    """'org/model.v2'와 'org-model-v2'는 같은 모음 이름이 된다 — 적어 둔 모델 이름이 달라 멈춘다"""
    q = QdrantClient(":memory:")
    name = store.collection_name("org/model.v2", 768)
    assert name == store.collection_name("org-model-v2", 768)
    store.ensure_collection(q, name, 768, "org/model.v2")
    with pytest.raises(store.EmbedConfigError, match="다른 모델"):
        store.ensure_collection(q, name, 768, "org-model-v2")


def test_legacy_documents_without_metadata_still_works(capsys):
    """예전 운영 모음 'documents'(메타데이터 없음)는 기본 설정에서 차원만 보고 그대로 쓴다 — 경고도 없다"""
    q = QdrantClient(":memory:")
    q.create_collection("documents", vectors_config=VectorParams(size=1536, distance=Distance.COSINE))
    assert store.collection_model(q, "documents") is None
    assert store.ensure_collection(q, "documents", 1536, "text-embedding-3-small") is False
    assert "메타데이터가 없다" not in capsys.readouterr().out


def test_non_default_without_metadata_warns(capsys):
    q = QdrantClient(":memory:")
    q.create_collection("documents_bge-m3_1024", vectors_config=VectorParams(size=1024, distance=Distance.COSINE))
    assert store.ensure_collection(q, "documents_bge-m3_1024", 1024, "bge-m3") is False
    assert "메타데이터가 없다" in capsys.readouterr().out


def test_off_mode_warns_about_other_collections():
    q = QdrantClient(":memory:")
    q.create_collection("documents", vectors_config=VectorParams(size=1536, distance=Distance.COSINE))
    assert store.off_mode_warning(q, {"LLM_PROVIDER": "off"}) is None  # 기본 아닌 모음 없음
    q.create_collection("documents_bge-m3_1024", vectors_config=VectorParams(size=1024, distance=Distance.COSINE))
    w = store.off_mode_warning(q, {"LLM_PROVIDER": "off"})
    assert w and "documents_bge-m3_1024" in w
    assert store.off_mode_warning(q, {"LLM_PROVIDER": "openai"}) is None
    assert store.off_mode_warning(q, {"LLM_PROVIDER": "off", "EMBED_MODEL": "bge-m3", "EMBED_DIM": "1024"}) is None


def test_embedding_length_must_match(monkeypatch):
    """모델이 돌려준 벡터 길이가 EMBED_DIM과 다르면 멈춘다 (예: 설정 1536인데 모델은 1024)"""
    from types import SimpleNamespace

    from app import llm
    monkeypatch.setattr(llm, "EMBED_DIM_CHECK", True)
    monkeypatch.setattr(store, "EMBED_DIM", 4)
    monkeypatch.setattr(store, "add_usage", lambda *a, **k: None)
    monkeypatch.setattr(llm, "PROVIDER", llm.load_provider({
        "LLM_PROVIDER": "local", "LLM_BASE_URL": "http://127.0.0.1:11434/v1", "EMBED_MODEL": "가상-임베딩"}))

    def client(n):
        return SimpleNamespace(embeddings=SimpleNamespace(create=lambda **a: SimpleNamespace(
            usage=None, data=[SimpleNamespace(embedding=[0.0] * n) for _ in a["input"]])))
    monkeypatch.setattr(llm, "client", lambda: client(4))
    assert llm.embed("v", ["가"]) == [[0.0] * 4]
    monkeypatch.setattr(llm, "client", lambda: client(3))
    with pytest.raises(store.EmbedConfigError, match=r"\[3\]차원"):
        llm.embed("v", ["가"])
