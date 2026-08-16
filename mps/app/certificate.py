from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding, pkcs12


class CertificateError(Exception):
    pass


@dataclass(frozen=True)
class CertificateMaterial:
    private_key_pem: bytes
    certificate_pem: bytes
    subject: str
    oib: str
    not_valid_after: datetime
    chain_pem: tuple[bytes, ...]

    @property
    def is_valid_now(self) -> bool:
        return self.not_valid_after > datetime.now(timezone.utc)

    @property
    def certificate_der_b64(self) -> str:
        cert = x509.load_pem_x509_certificate(self.certificate_pem)
        import base64

        return base64.b64encode(cert.public_bytes(Encoding.DER)).decode('ascii')


def _extract_oib(cert: x509.Certificate) -> str:
    for attr in cert.subject:
        oid = attr.oid.dotted_string
        if oid == '2.5.4.97' and str(attr.value).startswith('HR'):
            return str(attr.value)[2:]
        if attr.oid == x509.NameOID.SERIAL_NUMBER and str(attr.value).startswith('HR'):
            return str(attr.value)[2:]
    for attr in cert.subject.get_attributes_for_oid(x509.NameOID.ORGANIZATIONAL_UNIT_NAME):
        value = str(attr.value)
        if value.isdigit() and len(value) == 11:
            return value
    return ''


def load_p12(path: str | Path, password: str) -> CertificateMaterial:
    p12_path = Path(path)
    if not p12_path.is_file():
        raise CertificateError(f'Certifikat nije pronađen: {p12_path}')

    try:
        key, cert, chain = pkcs12.load_key_and_certificates(
            p12_path.read_bytes(),
            password.encode('utf-8'),
        )
    except Exception as exc:
        raise CertificateError(f'Ne mogu učitati .p12: {exc}') from exc

    if key is None or cert is None:
        raise CertificateError('PKCS#12 ne sadrži privatni ključ ili certifikat.')

    not_valid_after = cert.not_valid_after_utc
    if not_valid_after <= datetime.now(timezone.utc):
        raise CertificateError(f'Certifikat istekao: {not_valid_after.isoformat()}')

    from cryptography.hazmat.primitives.serialization import NoEncryption, PrivateFormat

    chain_pem = tuple(c.public_bytes(Encoding.PEM) for c in (chain or []))
    return CertificateMaterial(
        private_key_pem=key.private_bytes(
            encoding=Encoding.PEM,
            format=PrivateFormat.PKCS8,
            encryption_algorithm=NoEncryption(),
        ),
        certificate_pem=cert.public_bytes(Encoding.PEM),
        subject=cert.subject.rfc4514_string(),
        oib=_extract_oib(cert),
        not_valid_after=not_valid_after,
        chain_pem=chain_pem,
    )
