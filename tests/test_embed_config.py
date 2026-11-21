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
    ("bge-m3:latest", 1024, "documents_bge-m3-latest_1024"),
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
