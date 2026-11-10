"""안쪽 통신 설정 (app/store.py qdrant_args·check_transport, https://github.com/reddol18/docflow/issues/48) — 연결은 하지 않는다.
키 값은 지어낸 것."""
import importlib.util
import ssl
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app import store

_spec = importlib.util.spec_from_file_location("ca", Path(__file__).parents[1] / "scripts" / "ca.py")
ca = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ca)


def _ca_pem():
    """scripts/ca.py가 만드는 것과 같은 CA 인증서 (이름 제약·serverAuth 포함)"""
    return ca.ca_cert_for(ec.generate_private_key(ec.SECP384R1()), ca._now()).public_bytes(serialization.Encoding.PEM)


def test_default_no_key():
    assert store.qdrant_args({}) == {"url": store.QDRANT_URL, "timeout": 30}


def test_key_and_ca(tmp_path):
    key = tmp_path / "api-key"
    key.write_text("fake-key-for-test\n", encoding="utf-8")
    env = {"QDRANT_URL": "https://qdrant.docs.svc.cluster.local:6333", "QDRANT_API_KEY_FILE": str(key)}
    a = store.qdrant_args(env)
    assert a["api_key"] == "fake-key-for-test" and a["url"] == env["QDRANT_URL"] and "verify" not in a
    cafile = tmp_path / "ca.crt"
    cafile.write_bytes(_ca_pem())
    v = store.qdrant_args({**env, "QDRANT_CA_FILE": str(cafile)})["verify"]
    assert isinstance(v, ssl.SSLContext) and v.verify_mode == ssl.CERT_REQUIRED and v.check_hostname
    bad = tmp_path / "bad.crt"
    bad.write_text("not a certificate", encoding="utf-8")
    with pytest.raises(RuntimeError):
        store.qdrant_args({**env, "QDRANT_CA_FILE": str(bad)})
    with pytest.raises(RuntimeError):
        store.qdrant_args({**env, "QDRANT_CA_FILE": str(tmp_path / "none.crt")})


@pytest.mark.parametrize("content,url", [(None, "https://qdrant:6333"), ("  \n", "https://qdrant:6333"),
                                         ("fake-key-for-test", "http://qdrant:6333")])
def test_key_problems_stop_start(tmp_path, content, url):
    """키 파일을 줬는데 없음·빈 파일·http 주소(키가 평문으로 흐름) → 시작할 때 멈춤"""
    key = tmp_path / "api-key"
    if content is not None:
        key.write_text(content, encoding="utf-8")
    with pytest.raises(RuntimeError) as e:
        store.qdrant_args({"QDRANT_URL": url, "QDRANT_API_KEY_FILE": str(key)})
    assert "fake-key-for-test" not in str(e.value)  # 오류 문구에 키를 싣지 않는다


SAFE = {"DOCFLOW_REQUIRE_TLS": "1",
        "PG_URL": "postgresql://docflow@postgres.docs.svc.cluster.local:5432/docflow?sslmode=verify-full&sslrootcert=/x/ca.crt",
        "QDRANT_URL": "https://qdrant.docs.svc.cluster.local:6333", "QDRANT_API_KEY_FILE": "/etc/qdrant/api-key"}


def test_check_transport_deployed():
    store.check_transport(SAFE)
    store.check_transport({**SAFE, "PG_URL": "postgresql://docflow@h/db?sslmode=require"})
    store.check_transport({**SAFE, "PG_URL": "postgresql://docflow@h/db", "PGSSLMODE": "verify-ca"})
    bads = [{"PG_URL": "postgresql://docflow@h/db"},  # sslmode 없음 (libpq 기본 prefer = 평문으로 떨어질 수 있음)
            {"PG_URL": "postgresql://docflow@h/db?sslmode=prefer"},
            {"PG_URL": "postgresql://docflow:fake-pw@h/db?sslmode=disable"},
            {"PG_URL": "not a url ==="},
            {"PG_URL": "postgresql://docflow@%2Fvar%2Frun%2Fpostgresql/docflow?sslmode=verify-full"},  # 유닉스 소켓
            {"PG_URL": "postgresql:///docflow?host=/var/run/postgresql&sslmode=require"},
            {"PG_URL": "postgresql:///docflow?sslmode=require"},  # 호스트 없음 = 기본 소켓
            {"QDRANT_URL": "http://qdrant:6333"},
            {"QDRANT_API_KEY_FILE": ""}]
    for b in bads:
        with pytest.raises(RuntimeError) as e:
            store.check_transport({**SAFE, **b})
        assert "fake-pw" not in str(e.value)


def test_check_transport_off_without_marker():
    """표시가 없으면(이 PC 시험 PGlite·개발 서버) 평문 기본값 그대로 — 시험이 깨지지 않게"""
    store.check_transport({"PG_URL": "postgresql://postgres@127.0.0.1:55432/postgres?sslmode=disable"})
    store.check_transport({})
