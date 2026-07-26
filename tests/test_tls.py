from __future__ import annotations

import ssl

import certifi
import pytest

from matrix_mcp.tls import default_ssl_context


def test_default_ssl_context_has_ca_certificates() -> None:
    context = default_ssl_context()

    assert isinstance(context, ssl.SSLContext)
    assert context.cert_store_stats()["x509_ca"] > 0


def test_default_ssl_context_falls_back_to_certifi(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str | None] = []
    real_create = ssl.create_default_context

    def fake_create(*, cafile: str | None = None) -> ssl.SSLContext:
        calls.append(cafile)
        if cafile is None:
            return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        return real_create(cafile=cafile)

    monkeypatch.setattr(ssl, "create_default_context", fake_create)

    context = default_ssl_context()

    assert calls == [None, certifi.where()]
    assert context.cert_store_stats()["x509_ca"] > 0
