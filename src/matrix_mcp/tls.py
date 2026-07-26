"""TLS helpers for Matrix clients."""

from __future__ import annotations

import os
import ssl

import certifi


def default_ssl_context() -> ssl.SSLContext:
    """Return a client SSL context that always has CA certificates loaded.

    Python builds without a usable system trust store (for example
    uv-managed CPython on NixOS) create default contexts with zero CA
    certificates, so every HTTPS request fails verification. Fall back to
    certifi's bundle in that case. Preserve explicit ``SSL_CERT_FILE`` and
    ``SSL_CERT_DIR`` overrides even when the initial certificate count is
    zero because CA directories can load certificates lazily.
    """
    context = ssl.create_default_context()
    explicit_ca_source = "SSL_CERT_FILE" in os.environ or "SSL_CERT_DIR" in os.environ
    if context.cert_store_stats()["x509_ca"] == 0 and not explicit_ca_source:
        context = ssl.create_default_context(cafile=certifi.where())
    return context
