"""Make sure Python's own TLS has certificate authorities to check against.

Most of what the client downloads goes through `requests`, which brings its
own CA bundle (certifi). But the check that decides whether a connect
succeeded — "does the tunnel carry real traffic", dns_health.http_probe — is
an HTTPS request made with the standard library, and the standard library
looks for certificate authorities where the Python it was built with keeps
them.

On Windows that is the Windows certificate store, and it works. On Linux it is
the distribution's store, and it works. On macOS it is a path inside the
Python installation — and in a packaged app that is the installation on the
BUILD machine (/Library/Frameworks/Python.framework/…/etc/openssl), which a
user's Mac does not have. Python on macOS never reads the system keychain.
So in the packaged macOS app every such request failed certificate
verification, the traffic check failed with it, and the client reported "the
VPN server isn't passing traffic" for any server at all.

ensure_ca_bundle() points the standard library at the bundled certifi list
where it would otherwise have nothing. It works through the SSL_CERT_FILE
environment variable, which OpenSSL reads whenever a default context is made,
so it must run before the first connection — main.py calls it first thing.
"""
from __future__ import annotations

import os
import ssl
import sys


def ensure_ca_bundle() -> str:
    """Returns what the standard library's TLS will use: "system" (the
    platform's own store), "env" (the user set SSL_CERT_FILE / SSL_CERT_DIR
    and is left alone), "certifi" (set here) or "none" (nothing to set)."""
    if sys.platform == "win32":
        return "system"
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        return "env"
    if sys.platform != "darwin":
        # Linux and the rest: use the system store when there is one — it may
        # hold a company's own CA that certifi does not know.
        found = ssl.get_default_verify_paths()
        if found.cafile or found.capath:
            return "system"
    try:
        import certifi
        bundle = certifi.where()
    except Exception:
        return "none"
    if not os.path.isfile(bundle):
        return "none"
    os.environ["SSL_CERT_FILE"] = bundle
    return "certifi"
