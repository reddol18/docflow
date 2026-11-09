"""내부 데모 CA와 서버 인증서 (https://github.com/reddol18/docflow/issues/48). scripts/ca.ps1이 부른다 — 직접 부르지 않는다.

표준 입력 첫 줄: CA 키 암호 (ca.ps1이 DPAPI로 보관한 값을 넘긴다). 키·인증서 내용은 출력하지 않는다 — 경로·지문·만료만.

  ca.py init  <폴더>    CA 키(EC P-384, 암호화 PKCS8)·CA 인증서(5년). 이미 있으면 멈춘다 (덮어쓰지 않음)
  ca.py issue <폴더>    서비스마다 서버 키(EC P-256)·인증서(397일)를 <폴더>/issued/<이름>/tls.{crt,key}에 (있으면 새로 바꿈)
  ca.py show  <폴더>    CA·발급한 인증서의 지문·만료

CA 인증서에는 이름 제약(Name Constraints, critical)을 건다: 아래 SERVICES의 이름만 발급할 수 있고 IP 주소는 막는다.
이 CA를 Windows 신뢰 루트에 넣어도, 키가 새면 다른 사이트(예: 은행)를 흉내 내는 인증서를 브라우저가 받지 않게.
"""
import datetime as dt
import hashlib
import ipaddress
import os
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

NS = "docs"
# 서비스 → 인증서 이름(SAN). 첫 이름이 CN. 바꾸면 PERMITTED도 맞춘다
SERVICES = {
    "web": ["docflow.localhost"],  # 브라우저 주소 https://docflow.localhost:<포트> (*.localhost는 루프백으로 풀린다)
    "postgres": ["postgres", f"postgres.{NS}", f"postgres.{NS}.svc", f"postgres.{NS}.svc.cluster.local"],
    "qdrant": ["qdrant", f"qdrant.{NS}", f"qdrant.{NS}.svc", f"qdrant.{NS}.svc.cluster.local"],
}
# 이름 제약: dNSName은 그 이름과 하위 이름을 허용한다 (RFC 5280 4.2.1.10)
PERMITTED = ["docflow.localhost", "postgres", "qdrant", f"postgres.{NS}", f"qdrant.{NS}", f"postgres.{NS}.svc",
             f"qdrant.{NS}.svc", f"{NS}.svc.cluster.local"]
CA_DAYS, LEAF_DAYS = 5 * 365 + 1, 397  # 397일: 브라우저의 서버 인증서 최대 유효 기간 정책에 맞춘다
CA_NAME = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "docflow demo"),
                     x509.NameAttribute(NameOID.COMMON_NAME, "docflow internal demo CA")])


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _write(path, data, private=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)  # Windows는 폴더 ACL(ca.ps1)이 막는다
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def fingerprint(cert):
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest().upper()


def describe(label, path, cert):
    san = ""
    try:
        san = ", ".join(cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName))
    except x509.ExtensionNotFound:
        pass
    print(f"{label}: {path}")
    print(f"  SHA-256 {fingerprint(cert)}")
    print(f"  만료 {cert.not_valid_after_utc:%Y-%m-%d}" + (f", 이름 {san}" if san else ""))


def ca_cert_for(key, now):
    pub = key.public_key()
    return (x509.CertificateBuilder()
            .subject_name(CA_NAME).issuer_name(CA_NAME).public_key(pub)
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=CA_DAYS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                                         key_encipherment=False, data_encipherment=False, key_agreement=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.NameConstraints(
                permitted_subtrees=[x509.DNSName(n) for n in PERMITTED],
                excluded_subtrees=[x509.IPAddress(ipaddress.ip_network("0.0.0.0/0")), x509.IPAddress(ipaddress.ip_network("::/0"))]),
                critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(pub), critical=False)
            .sign(key, hashes.SHA384()))


def leaf_cert_for(names, leaf_key, ca_key, ca_cert, now):
    pub = leaf_key.public_key()
    return (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
            .issuer_name(ca_cert.subject).public_key(pub)
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=LEAF_DAYS))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=False, crl_sign=False, content_commitment=False,
                                         key_encipherment=False, data_encipherment=False, key_agreement=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(pub), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))


def init(d, passphrase):
    key_path, crt_path = d / "ca.key", d / "ca.crt"
    if key_path.exists() or crt_path.exists():
        sys.exit(f"이미 CA가 있음: {d} — 덮어쓰지 않는다 (새로 만들려면 신뢰 루트에서 지우고 폴더를 지운 뒤)")
    key = ec.generate_private_key(ec.SECP384R1())
    cert = ca_cert_for(key, _now())
    _write(key_path, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.BestAvailableEncryption(passphrase)), private=True)
    _write(crt_path, cert.public_bytes(serialization.Encoding.PEM))
    describe("CA 인증서", crt_path, cert)
    print(f"CA 키(암호화): {key_path}")


def load_ca(d, passphrase):
    try:
        key = serialization.load_pem_private_key((d / "ca.key").read_bytes(), passphrase)
        cert = x509.load_pem_x509_certificate((d / "ca.crt").read_bytes())
    except FileNotFoundError:
        sys.exit(f"CA가 없음: {d} — 먼저 .\\scripts\\ca.ps1 init")
    except (ValueError, TypeError):
        sys.exit("CA 키를 열지 못함 (암호가 맞지 않거나 파일이 깨짐)")
    return key, cert


def issue(d, passphrase):
    ca_key, ca_cert = load_ca(d, passphrase)
    now = _now()
    for name, names in SERVICES.items():
        leaf_key = ec.generate_private_key(ec.SECP256R1())
        cert = leaf_cert_for(names, leaf_key, ca_key, ca_cert, now)
        out = d / "issued" / name
        _write(out / "tls.key", leaf_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                       serialization.NoEncryption()), private=True)
        _write(out / "tls.crt", cert.public_bytes(serialization.Encoding.PEM))
        describe(f"서버 인증서 {name}", out / "tls.crt", cert)


def show(d):
    p = d / "ca.crt"
    if not p.exists():
        sys.exit(f"CA가 없음: {d}")
    describe("CA 인증서", p, x509.load_pem_x509_certificate(p.read_bytes()))
    for name in SERVICES:
        p = d / "issued" / name / "tls.crt"
        if p.exists():
            describe(f"서버 인증서 {name}", p, x509.load_pem_x509_certificate(p.read_bytes()))
        else:
            print(f"서버 인증서 {name}: 아직 없음 (.\\scripts\\ca.ps1 issue)")


def main(argv):
    sys.stdout.reconfigure(encoding="utf-8")  # ca.ps1이 콘솔을 UTF-8로 맞춘다 (-I라 PYTHONUTF8은 듣지 않음)
    if len(argv) != 2 or argv[0] not in ("init", "issue", "show"):
        sys.exit(__doc__)
    cmd, d = argv[0], Path(argv[1])
    if cmd == "show":
        return show(d)
    passphrase = sys.stdin.readline().strip().lstrip("﻿").encode()
    if len(passphrase) < 32:
        sys.exit("CA 키 암호가 없음 (ca.ps1로 부를 것)")
    (init if cmd == "init" else issue)(d, passphrase)


if __name__ == "__main__":
    main(sys.argv[1:])
