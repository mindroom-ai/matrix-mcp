"""TLS helpers for Matrix clients."""

from __future__ import annotations

import ssl

import certifi


def default_ssl_context() -> ssl.SSLContext:
    """Return a client SSL context that always has CA certificates loaded.

    Python builds without a usable system trust store (for example
    uv-managed CPython on NixOS) create default contexts with zero CA
    certificates, so every HTTPS request fails verification. Fall back to
    certifi's bundle in that case; explicit ``SSL_CERT_FILE``/``SSL_CERT_DIR``
    overrides keep working because ``ssl.create_default_context`` honors
    them before the fallback triggers.
    """
    context = ssl.create_default_context()
    if context.cert_store_stats()["x509_ca"] == 0:
        context = ssl.create_default_context(cafile=certifi.where())
    return context
