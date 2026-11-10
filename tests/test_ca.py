"""내부 데모 CA (scripts/ca.py, https://github.com/reddol18/docflow/issues/48) — 임시 폴더에만 만든다.
발급한 서버 인증서가 CA로 확인되고, CA 이름 제약 밖의 이름·IP 인증서는 (CA 키로 서명해도) 거절되는지."""
import datetime as dt
import importlib.util
import io
import ipaddress
import sys
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509 import verification

_spec = importlib.util.spec_from_file_location("ca", Path(__file__).parents[1] / "scripts" / "ca.py")
ca = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ca)
PASS = "x" * 44


def run(monkeypatch, *argv, stdin=PASS):
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin + "\n"))
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="utf-8"))
    ca.main(list(argv))


def load(p):
    return x509.load_pem_x509_certificate(Path(p).read_bytes())


def verifier(root, name):
    store = verification.Store([root])
    return verification.PolicyBuilder().store(store).time(dt.datetime.now(dt.timezone.utc)).build_server_verifier(name)


@pytest.fixture
def made(tmp_path, monkeypatch):
    run(monkeypatch, "init", str(tmp_path))
    run(monkeypatch, "issue", str(tmp_path))
    return tmp_path


def test_issued_certs_verify(made):
    root = load(made / "ca.crt")
    for svc, names in ca.SERVICES.items():
        leaf = load(made / "issued" / svc / "tls.crt")
        for n in names:
            verifier(root, x509.DNSName(n)).verify(leaf, [])  # 실패하면 예외
        assert (leaf.not_valid_after_utc - leaf.not_valid_before_utc).days <= 398
        key = serialization.load_pem_private_key((made / "issued" / svc / "tls.key").read_bytes(), None)
        assert key.public_key() == leaf.public_key()
    assert b"ENCRYPTED PRIVATE KEY" in (made / "ca.key").read_bytes()  # CA 키는 암호화
    with pytest.raises(verification.VerificationError):
        verifier(root, x509.DNSName("docflow.localhost")).verify(load(made / "issued" / "postgres" / "tls.crt"), [])  # 서비스마다 다른 이름


def _forge(made, san):
    """CA 키로 이름 제약 밖 인증서를 직접 서명 — 키가 새도 브라우저·라이브러리가 받지 않아야 한다"""
    ca_key = serialization.load_pem_private_key((made / "ca.key").read_bytes(), PASS.encode())
    root = load(made / "ca.crt")
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    b = (x509.CertificateBuilder().subject_name(x509.Name([])).issuer_name(root.subject).public_key(leaf_key.public_key())
         .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=1))
         .not_valid_after(now + dt.timedelta(days=30))
         .add_extension(x509.SubjectAlternativeName(san if isinstance(san, list) else [san]), critical=True)
         .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
         .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
         .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False))
    return root, b.sign(ca_key, hashes.SHA256())


@pytest.mark.parametrize("san,name", [
    (x509.DNSName("bank.example.com"), x509.DNSName("bank.example.com")),
    (x509.DNSName("localhost"), x509.DNSName("localhost")),  # 개발 서버 이름도 밖
    (x509.DNSName("postgres"), x509.DNSName("postgres")),  # 짧은 이름도 밖 (클라이언트는 전체 이름을 쓴다)
    (x509.DNSName("evil.docs.svc.cluster.local"), x509.DNSName("evil.docs.svc.cluster.local")),  # 같은 네임스페이스 다른 서비스
    (x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))),
])
def test_name_constraints_block_other_names(made, san, name):
    root, leaf = _forge(made, san)
    with pytest.raises(verification.VerificationError):
        verifier(root, name).verify(leaf, [])


def test_init_does_not_overwrite_and_wrong_pass(made, monkeypatch):
    before = (made / "ca.key").read_bytes()
    with pytest.raises(SystemExit):
        run(monkeypatch, "init", str(made))
    assert (made / "ca.key").read_bytes() == before
    with pytest.raises(SystemExit):
        run(monkeypatch, "issue", str(made), stdin="y" * 44)  # 틀린 암호
    with pytest.raises(SystemExit):
        run(monkeypatch, "issue", str(made), stdin="")  # 암호 없음


@pytest.mark.parametrize("san", [x509.RFC822Name("a@bank.example.com"), x509.UniformResourceIdentifier("https://bank.example.com/")])
def test_name_constraints_block_mail_and_uri(made, san):
    """메일·URI 이름은 쓸 수 없는 도메인(.invalid)만 허용 — CA 키로 서명해도 이름 확인 전에 제약에서 거절"""
    ok = x509.DNSName("docflow.localhost")
    root, leaf = _forge(made, [ok])
    verifier(root, ok).verify(leaf, [])  # 대조군: 허용 이름만이면 통과
    root, leaf = _forge(made, [ok, san])  # 허용 이름 + 메일·URI → 제약 위반
    with pytest.raises(verification.VerificationError):
        verifier(root, ok).verify(leaf, [])


def test_ca_extensions(made):
    root = load(made / "ca.crt")
    eku = root.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(eku) == [x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]
    nc = root.extensions.get_extension_for_class(x509.NameConstraints)
    assert nc.critical
    dns = [n.value for n in nc.value.permitted_subtrees if isinstance(n, x509.DNSName)]
    assert dns == ["docflow.localhost", "postgres.docs.svc.cluster.local", "qdrant.docs.svc.cluster.local"]
