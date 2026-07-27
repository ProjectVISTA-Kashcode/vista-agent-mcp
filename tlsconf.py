"""
tlsconf — outbound TLS trust policy for VISTA-MCP.
==================================================

Every outbound call this server makes (the injected log fetch, the LogV AgentAssist
forward, the ORB ask) verifies TLS by default. Some deployments have to talk to internal
Fortinet hosts whose certificate a public trust store won't accept — an internal CA, or a
staging host serving a certificate whose SAN doesn't cover the name we dial
(`sa-staging.corp.fortinet.com` is the known case: `curl` fails there but `curl -k` works).
Python/httpx behaves exactly like `curl` without `-k` and raises `httpx.ConnectError`
("CERTIFICATE_VERIFY_FAILED") in ~0.1s, before any HTTP request goes out.

Two ways to accommodate that, in order of preference:

  MCP_FETCH_CA_BUNDLE=/etc/ssl/corp-ca.pem
      Trust an extra CA. This is the *correct* fix when the host's certificate is simply
      signed by an internal CA. It does NOT help a hostname/SAN mismatch — the name still
      has to match. (`SSL_CERT_FILE` / `SSL_CERT_DIR` are honoured too, via httpx trust_env.)

  MCP_FETCH_INSECURE_TLS_HOSTS=sa-staging.corp.fortinet.com,other.corp.fortinet.com
      Skip verification, but ONLY for these exact hostnames — the `curl -k` equivalent,
      scoped. This is what fixes a SAN mismatch. Every other host stays fully verified.

  MCP_FETCH_INSECURE_TLS=1
      Skip verification for *every* host. Blunt; prefer the host list.

Relaxing TLS for a host also constrains redirects: a relaxed request may only be redirected
to another relaxed host, so an unverified hop can't quietly hand us off to an arbitrary
target (see `_guard_request` in server.py).
"""
from __future__ import annotations

import os
import ssl
from functools import lru_cache
from urllib.parse import urlparse

import httpx

import vlog


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


CA_BUNDLE = os.getenv("MCP_FETCH_CA_BUNDLE", "").strip()
INSECURE_TLS = _flag("MCP_FETCH_INSECURE_TLS")
INSECURE_TLS_HOSTS = frozenset(
    h.strip().lower().rstrip(".")
    for h in os.getenv("MCP_FETCH_INSECURE_TLS_HOSTS", "").split(",")
    if h.strip()
)

# Fail fast and loudly on a mistyped bundle path rather than at the first fetch, where it
# would surface as a generic "could not fetch" hours later.
if CA_BUNDLE and not os.path.isfile(CA_BUNDLE):
    raise SystemExit(
        f"VISTA-MCP: MCP_FETCH_CA_BUNDLE points at '{CA_BUNDLE}', which is not a readable "
        f"file. Give it a PEM bundle path (mount it into the container), or unset it."
    )


def host_of(url: str) -> str:
    """Lowercased hostname of `url` ('' if unparseable)."""
    try:
        return (urlparse(url).hostname or "").lower().rstrip(".")
    except Exception:  # noqa: BLE001
        return ""


def is_relaxed(url: str) -> bool:
    """True if TLS verification is intentionally disabled for this URL's host."""
    return INSECURE_TLS or host_of(url) in INSECURE_TLS_HOSTS


@lru_cache(maxsize=2)
def _context(relaxed: bool) -> ssl.SSLContext:
    """Build (once) the SSL context for verified / relaxed outbound calls."""
    if relaxed:
        return httpx.create_ssl_context(verify=False)
    return httpx.create_ssl_context(verify=CA_BUNDLE or True)


def verify_for(url: str) -> ssl.SSLContext:
    """The `verify=` value to hand `httpx.AsyncClient` for a call to `url`."""
    relaxed = is_relaxed(url)
    if relaxed:
        vlog.log(
            f"TLS: verification DISABLED for host '{host_of(url) or '?'}' "
            f"({'MCP_FETCH_INSECURE_TLS' if INSECURE_TLS else 'MCP_FETCH_INSECURE_TLS_HOSTS'})",
            vlog.WARNING,
        )
    return _context(relaxed)


def is_cert_error(exc: BaseException) -> bool:
    """True if `exc` (or anything it wraps) is a TLS trust/verification failure.

    httpx surfaces these as a plain `ConnectError`, so the type alone doesn't say whether
    the host was unreachable or its certificate was rejected — walk the chain and look.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, ssl.SSLError):  # covers SSLCertVerificationError
            return True
        if "CERTIFICATE_VERIFY_FAILED" in str(cur) or "certificate verify failed" in str(cur):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def cert_error_hint(url: str) -> str:
    """Operator-facing remediation line for a rejected certificate on `url`'s host."""
    host = host_of(url) or "the host"
    return (
        f"TLS certificate rejected by '{host}' (same failure as `curl` without `-k`). "
        f"Fix one of: point MCP_FETCH_CA_BUNDLE at the CA that signed it (right fix for an "
        f"internal CA), or — for a hostname/SAN mismatch, which no CA bundle can fix — add "
        f"'{host}' to MCP_FETCH_INSECURE_TLS_HOSTS."
    )


def describe() -> str:
    """One-line summary for the startup banner."""
    if INSECURE_TLS:
        return "⚠️ DISABLED for ALL hosts (MCP_FETCH_INSECURE_TLS)"
    bits = [f"CA bundle {CA_BUNDLE}" if CA_BUNDLE else "system/certifi trust store"]
    if INSECURE_TLS_HOSTS:
        bits.append(f"⚠️ unverified for: {', '.join(sorted(INSECURE_TLS_HOSTS))}")
    return "verified (" + "; ".join(bits) + ")"
