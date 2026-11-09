"""Qdrant 연결 인자 (app/store.py qdrant_args, https://github.com/reddol18/docflow/issues/48) — 연결은 하지 않는다. 키 값은 지어낸 것."""
import ssl

import pytest

from app import store


def _self_signed():
    import datetime as dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    k = ec.generate_private_key(ec.SECP256R1())
    n = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "test ca")])
    now = dt.datetime.now(dt.timezone.utc)
    c = (x509.CertificateBuilder().subject_name(n).issuer_name(n).public_key(k.public_key()).serial_number(1)
         .not_valid_before(now).not_valid_after(now + dt.timedelta(days=1))
         .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(k, hashes.SHA256()))
    return c.public_bytes(serialization.Encoding.PEM)


def test_default_no_key():
    assert store.qdrant_args({}) == {"url": store.QDRANT_URL, "timeout": 30}


def test_key_and_ca(tmp_path):
    key = tmp_path / "api-key"
    key.write_text("fake-key-for-test\n", encoding="utf-8")
    env = {"QDRANT_URL": "https://qdrant:6333", "QDRANT_API_KEY_FILE": str(key)}
    a = store.qdrant_args(env)
    assert a["api_key"] == "fake-key-for-test" and a["url"] == "https://qdrant:6333" and "verify" not in a
    ca = tmp_path / "ca.crt"
    ca.write_bytes(_self_signed())
    v = store.qdrant_args({**env, "QDRANT_CA_FILE": str(ca)})["verify"]
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
