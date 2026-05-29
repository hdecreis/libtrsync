"""Pluggable session-refresh strategies.

The web-login flow (``login`` + ``verify_2fa``) yields a short-lived
``tr_session`` cookie *and* a longer-lived refresh cookie, both set on the
HTTP cookie jar. :class:`WebRefreshAuth` trades that jar for a fresh
``tr_session`` via ``GET /api/v1/auth/web/session`` with no 2FA, for as
long as the refresh cookie is valid (it eventually expires, and is revoked
when you log in on the phone — TR allows one device at a time).

Consumers select a strategy when constructing :class:`TRClient`::

    client = TRClient(auth=WebRefreshAuth())            # default
    client = TRClient(auth=DeviceKeyAuth(keyfile=...))  # not yet implemented
"""

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

import requests

from ._classify import classify_http, classify_network_error
from .constants import TR_API_BASE
from .exceptions import SessionExpired, WafExpired

if TYPE_CHECKING:
    from .client import TRClient

logger = logging.getLogger(__name__)


class AuthStrategy(ABC):
    """How a client re-acquires a valid ``tr_session`` without user 2FA."""

    @abstractmethod
    def refresh_session(self, client: "TRClient") -> str:
        """Mint a fresh ``tr_session`` and return it.

        Implementations should also update ``client._session_token``.

        Raises :class:`SessionExpired` if a no-2FA refresh isn't possible
        (e.g. the refresh credential itself expired) — the consumer then
        has to re-run ``login()`` + ``verify_2fa()``.
        """
        raise NotImplementedError


class WebRefreshAuth(AuthStrategy):
    """Refresh the web session via ``GET /api/v1/auth/web/session``.

    Relies on the cookie jar captured at ``verify_2fa`` time (it holds TR's
    refresh cookie alongside ``tr_session``). The endpoint rolls a new
    ``tr_session`` onto the jar; we read it back out. Works until the
    refresh cookie expires or is revoked.
    """

    def refresh_session(self, client: "TRClient") -> str:
        def _do() -> requests.Response:
            try:
                resp = client._http.get(
                    f"{TR_API_BASE}/api/v1/auth/web/session",
                    headers=client._headers(),
                )
            except requests.RequestException as e:
                raise classify_network_error(e, context="refresh_session") from e
            classify_http(resp, context="refresh_session")
            return resp

        try:
            _do()
        except WafExpired:
            if not client._refresh_waf_sync():
                raise
            _do()

        # The fresh tr_session is rolled onto the shared cookie jar.
        token = client._token_from_jar()
        if not token:
            raise SessionExpired(
                "web session refresh returned no tr_session cookie — "
                "the refresh cookie is likely expired; re-run login() + verify_2fa()."
            )
        client._session_token = token
        return token


class DeviceKeyAuth(AuthStrategy):
    """Device-key (ECDSA) pairing strategy — **not yet implemented**.

    Plugin point for the durable app-flow auth: generate an EC keypair,
    pair once via 2FA, persist the private key, then sign each login
    challenge so sessions can be minted with no 2FA. Trade-off: TR allows
    only one paired device at a time, so pairing logs you out of the phone.
    """

    def __init__(self, *_args, **_kwargs):
        raise NotImplementedError(
            "DeviceKeyAuth is a planned plugin point and not yet implemented. "
            "Use WebRefreshAuth (the default)."
        )

    def refresh_session(self, client: "TRClient") -> str:  # pragma: no cover
        raise NotImplementedError
