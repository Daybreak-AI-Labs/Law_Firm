"""Proactive expiry-horizon warnings in `maverick doctor`: gRPC TLS
server certs that are about to lapse."""
from __future__ import annotations

import pytest


def _rows(monkeypatch):
    """Capture _row(marker, label, detail, fix) calls from a check."""
    import maverick.health as health
    captured = []
    monkeypatch.setattr(health, "_row",
                        lambda marker, label, detail="", fix="": captured.append(
                            (label, detail)))
    return health, captured


def test_tls_cert_expiry_warns(tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    import datetime as dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "t")])
    not_after = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=5)  # within 30d
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(issuer)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1))
        .not_valid_after(not_after)
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "server.crt"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    health, captured = _rows(monkeypatch)
    monkeypatch.setattr(
        "maverick.config.load_config",
        lambda: {"grpc": {"tls": True, "tls_cert": str(cert_path)}})
    monkeypatch.setattr("maverick.grpc_tls.tls_enabled",
                        lambda section, cfg=None: section == "grpc")
    health._check_tls_cert_expiry()
    msgs = [(label, d) for (label, d) in captured]
    assert any(label == "tls:grpc" and "expires in" in d for (label, d) in msgs)
