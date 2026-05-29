"""Generic session-resume policy.

Consumers persist a :class:`ConnectionState` between runs and want to come
back online doing the *least* work: reuse the cached token if it's still
live, mint a fresh one from the refresh cookie if it isn't, refresh only the
WAF if that's all that expired, and fall back to a full 2FA login only when
nothing else works.

That decision tree is generic. What's *not* generic — where the state lives
on disk, how credentials are prompted, how a 2FA code is collected — is left
to the consumer via callbacks. This keeps the library free of filesystem and
UI assumptions while still owning the policy.

Example::

    from traderepublic_sync import resume_session

    client, token = resume_session(
        load_state(),
        on_waf=lambda: acquire_waf_via_playwright(),   # → fresh WAF token
        on_2fa=do_full_login,                          # → (client, token)
        persist=lambda c, t: save_state(c, t),
    )
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Optional

from .client import TRClient
from .exceptions import SessionExpired, TransientError, WafExpired
from .state import ConnectionState

logger = logging.getLogger(__name__)

# on_waf() → a fresh WAF token string (or None after mutating the client).
WafProvider = Callable[[], Optional[str]]
# on_2fa() → a freshly authenticated (client, session_token) pair. Owns the
# full login + 2FA flow (phone/pin/code collection) entirely.
FullLogin = Callable[[], "tuple[TRClient, str]"]
# persist(client, session_token) → None. Save the refreshed state.
Persist = Callable[[TRClient, str], None]


def resume_session(
    state: ConnectionState | None,
    *,
    on_waf: WafProvider | None = None,
    on_2fa: FullLogin | None = None,
    persist: Persist | None = None,
    validate: bool = True,
) -> tuple[TRClient, str]:
    """Return an authenticated ``(client, session_token)`` from ``state``.

    Resolution order:

    1. No state / no session token → full login (``on_2fa``).
    2. JWT dead **and** no refresh cookie → full login.
    3. WAF expired → refresh it (``on_waf``, else
       ``client.acquire_waf_token("playwright")``) and persist.
    4. JWT live → reuse it (validated against TR unless ``validate=False``).
    5. JWT dead or validation failed, but a refresh cookie is cached →
       ``client.refresh_session()`` (no 2FA), persist, reuse.
    6. Otherwise → full login.

    ``on_2fa`` is required for any path that reaches a full login; without it
    those paths raise :class:`SessionExpired`.
    """
    if state is None or not state.session_token:
        logger.info("resume: no cached session token — full login")
        return _full_login(on_2fa)

    sess_live = state.is_session_valid()
    can_refresh = bool(state.session_cookies)
    if not sess_live and not can_refresh:
        logger.info("resume: token expired and no refresh cookie — full login")
        return _full_login(on_2fa)

    client = TRClient.from_state(state)

    # A valid WAF is needed both to validate and to call refresh_session.
    if not state.is_waf_valid():
        logger.info("resume: WAF expired — refreshing")
        try:
            waf = on_waf() if on_waf else client.acquire_waf_token("playwright")
        except Exception as e:  # noqa: BLE001 — any WAF failure → fall back
            logger.warning("resume: WAF refresh failed (%r) — full login", e)
            return _full_login(on_2fa)
        if isinstance(waf, str) and waf:
            client.waf_token = waf
        if persist:
            persist(client, state.session_token)

    if sess_live:
        if not validate:
            return client, state.session_token
        err = _validate(client, state.session_token)
        if err is None:
            logger.info("resume: cached token valid — reusing")
            return client, state.session_token
        if isinstance(err, (TransientError, WafExpired)) and not can_refresh:
            # Don't burn a re-login on a transient hiccup when we can't refresh.
            raise err
        logger.info("resume: validation failed (%s) — trying refresh", type(err).__name__)

    if can_refresh:
        try:
            new_token = client.refresh_session()
        except SessionExpired:
            logger.info("resume: refresh cookie dead — full login")
            return _full_login(on_2fa)
        if persist:
            persist(client, new_token)
        logger.info("resume: minted fresh token via refresh cookie — reusing")
        return client, new_token

    return _full_login(on_2fa)


def _validate(client: TRClient, session_token: str) -> Exception | None:
    """Cheapest live check that the session works. Returns the error, or None."""
    async def _check() -> None:
        await client.fetch_account_list(session_token)

    try:
        asyncio.run(_check())
        return None
    except Exception as e:  # noqa: BLE001 — caller branches on the type
        return e


def _full_login(on_2fa: FullLogin | None) -> tuple[TRClient, str]:
    if on_2fa is None:
        raise SessionExpired(
            "session cannot be resumed without 2FA and no on_2fa callback was "
            "provided — supply one that runs login() + verify_2fa()."
        )
    return on_2fa()
