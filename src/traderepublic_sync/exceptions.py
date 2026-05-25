"""Exceptions raised by traderepublic_sync.

The library distinguishes four recovery classes so consumers don't have to
guess from a raw HTTP status or a WebSocket close code:

- ``WafExpired``       — re-acquire the AWS WAF token, retry.
- ``SessionExpired``   — full login + 2FA flow; the user has to act.
- ``TransientError``   — network blip; retry the same call.
- ``TRAuthError``      — auth failure that isn't one of the above
                         (bad PIN, wrong 2FA code, etc.). User-fixable.

All four inherit from :class:`TRError` for catch-all handling.
"""


class TRError(Exception):
    """Base class for every exception raised by this library."""


class TRAuthError(TRError):
    """Authentication failure that is *not* a token/session lifecycle issue.

    Typical cause: wrong PIN, wrong 2FA code, banned account. The consumer
    has to surface this to a human — no automated recovery is possible.
    """


class WafExpired(TRAuthError):
    """The AWS WAF token was rejected (or missing).

    Recovery: call :meth:`TRClient.acquire_waf_token` (or the
    consumer-supplied ``on_waf_expired`` hook) and retry the same operation.
    The ``tr_session`` cookie is unaffected — no 2FA needed.
    """


class SessionExpired(TRAuthError):
    """The ``tr_session`` cookie is invalid or has expired.

    Recovery: full ``login()`` + ``verify_2fa()`` flow. ``device_info``
    should be reused so the device stays trusted; ``waf_token`` may be
    reused if still valid.
    """


class TransientError(TRError):
    """Network blip, WS dropped, request timeout, 5xx response.

    Recovery: retry the same call (typically with exponential backoff).
    Nothing about credentials has changed.
    """
