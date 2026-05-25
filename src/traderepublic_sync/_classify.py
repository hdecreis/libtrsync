"""Internal helpers: map raw HTTP / WS errors to the typed exceptions.

These functions exist so the same vocabulary applies whether the call site
is a REST helper in :mod:`client` or the reader loop in :mod:`session`.
Keep the rules conservative — when in doubt, prefer ``TransientError`` over
``SessionExpired`` (a wrong choice forces unnecessary 2FA).
"""

from __future__ import annotations

import requests

from .exceptions import (
    SessionExpired,
    TRAuthError,
    TransientError,
    WafExpired,
)


# Body fragments that strongly suggest one cause over another.
# TR's error payloads are not documented; treat these as best-effort hints.
_WAF_HINTS = ("aws-waf", "awswaf", "waf-token", "x-aws-waf-token", "blocked by")
_SESSION_HINTS = ("session", "tr_session", "unauthorized", "auth", "expired")


def classify_http(resp: requests.Response, *, context: str = "") -> None:
    """Raise the right typed exception for a non-2xx HTTP response.

    Returns ``None`` for 2xx so callers can use this as a guard. The
    ``context`` string is included in exception messages — pass "login",
    "verify_2fa", etc. so the caller knows which operation failed.
    """
    code = resp.status_code
    if 200 <= code < 300:
        return

    body = (resp.text or "")[:500]  # cap so giant HTML error pages don't blow up logs
    body_lc = body.lower()
    label = f"{context} " if context else ""

    if code in (502, 503, 504) or code >= 500:
        raise TransientError(f"{label}HTTP {code} (server): {body}")

    if code == 403:
        # 403 is almost always WAF; check session hints just in case the API
        # ever returns a session-related 403.
        if any(h in body_lc for h in _SESSION_HINTS) and not any(h in body_lc for h in _WAF_HINTS):
            raise SessionExpired(f"{label}HTTP 403 (session): {body}")
        raise WafExpired(f"{label}HTTP 403 (WAF): {body}")

    if code == 401:
        if any(h in body_lc for h in _WAF_HINTS):
            raise WafExpired(f"{label}HTTP 401 (WAF): {body}")
        raise SessionExpired(f"{label}HTTP 401: {body}")

    if code == 429:
        raise TransientError(f"{label}HTTP 429 (rate-limited): {body}")

    raise TRAuthError(f"{label}HTTP {code}: {body}")


def classify_network_error(exc: Exception, *, context: str = "") -> TransientError:
    """Wrap a ``requests`` network-layer error in :class:`TransientError`."""
    label = f"{context} " if context else ""
    return TransientError(f"{label}network error: {exc!r}")


def classify_ws_connect_error(exc: Exception) -> Exception:
    """Map a ``websockets`` connect/handshake failure to a typed exception.

    A 403 from the WS HTTP-upgrade handshake is the WAF rejecting us.
    Any other connection-level failure is treated as transient.
    """
    # `websockets` exposes different exception classes across versions;
    # check by attribute rather than `isinstance` to stay version-agnostic.
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    if status == 403:
        return WafExpired(f"WS handshake rejected (HTTP 403, WAF): {exc!r}")
    if status in (401,):
        return SessionExpired(f"WS handshake rejected (HTTP 401): {exc!r}")
    return TransientError(f"WS connect failed: {exc!r}")


def classify_ws_error_frame(body: str) -> Exception | None:
    """Map the body of an ``<id> E <json>`` WS frame to a typed exception.

    Returns ``None`` if the payload doesn't look like an auth issue — the
    caller can then surface it as a generic error to the subscription
    callback rather than tearing down the session.
    """
    body_lc = body.lower()
    if "session" in body_lc and ("expired" in body_lc or "invalid" in body_lc):
        return SessionExpired(f"WS E frame: {body[:200]}")
    if "unauthorized" in body_lc or "auth" in body_lc:
        return SessionExpired(f"WS E frame: {body[:200]}")
    if "waf" in body_lc:
        return WafExpired(f"WS E frame: {body[:200]}")
    return None
