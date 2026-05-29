"""Persistent WebSocket session for live Trade Republic data subscriptions.

Usage::

    async with client.open_session(session_token) as session:
        async def on_price(data):
            print(data["last"]["price"])

        sub_id = await session.subscribe_ticker("US0378331005", on_price)
        await asyncio.sleep(60)
        await session.unsubscribe(sub_id)

Long-lived sessions
-------------------

Pass ``auto_reconnect=True`` (and optionally ``on_reconnect``) when opening
the session via :meth:`TRClient.open_session` to make the reader loop
reconnect on ``ConnectionClosed`` and replay live subscriptions on the
fresh socket. Set ``on_waf_expired`` / ``on_session_expired`` to plug in
the same refresh / notification hooks as :class:`TRClient`.
"""

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Union

import websockets

from ._classify import (
    classify_ws_connect_error,
    classify_ws_error_frame,
)
from .constants import FX_INSTRUMENTS, TR_WS_URL, WS_CONNECT_PAYLOAD
from .exceptions import (
    SessionExpired,
    TransientError,
    WafExpired,
)

logger = logging.getLogger(__name__)

_FRAME_RE = re.compile(r"^(\d+) ([AE]) ([\s\S]+)$")

WafHook = Callable[[], Union[str, None, Awaitable[Union[str, None]]]]
SessionHook = Callable[[], Union[str, None, Awaitable[Union[str, None]]]]
ReconnectHook = Callable[[], Union[None, Awaitable[None]]]
RefreshHook = Callable[[], Awaitable[Union[str, None]]]


class TRSession:
    """Long-lived WebSocket session with callback-based subscription dispatch.

    Open via :meth:`TRClient.open_session` — do not instantiate directly.
    """

    def __init__(
        self,
        token: str,
        locale: str = "fr",
        *,
        auto_reconnect: bool = False,
        on_waf_expired: WafHook | None = None,
        on_session_expired: SessionHook | None = None,
        on_reconnect: ReconnectHook | None = None,
        session_refresher: RefreshHook | None = None,
        refresh_interval: float = 270.0,
        reconnect_backoff: float = 2.0,
        reconnect_max_backoff: float = 60.0,
    ):
        self._token = token
        self._locale = locale
        self._ws = None
        self._msg_id = 0
        # sub_id -> (sub_type, params, callback) — params include the
        # session ``token`` so we can replay on reconnect.
        self._subs: dict[int, tuple[str, dict, Callable]] = {}
        self._reader_task: asyncio.Task | None = None
        self._refresh_task: asyncio.Task | None = None
        self._auto_reconnect = auto_reconnect
        self._on_waf_expired = on_waf_expired
        self._on_session_expired = on_session_expired
        self._on_reconnect = on_reconnect
        self._session_refresher = session_refresher
        self._refresh_interval = refresh_interval
        self._reconnect_backoff = reconnect_backoff
        self._reconnect_max_backoff = reconnect_max_backoff
        self._closing = False

    # ── Hook helpers ──────────────────────────────────────────────────────

    async def _refresh_waf(self) -> bool:
        if not self._on_waf_expired:
            return False
        result = self._on_waf_expired()
        if asyncio.iscoroutine(result):
            result = await result
        # The hook may return a new WAF token, but TRSession doesn't
        # actually need to carry it — the WAF cookie is what matters for
        # the WS handshake and that's owned by the consumer's environment.
        # The non-empty return is treated as "go ahead, retry".
        return bool(result) or True  # if hook ran at all, treat as "retry"

    async def _notify_session_expired(self) -> str | None:
        if not self._on_session_expired:
            return None
        result = self._on_session_expired()
        if asyncio.iscoroutine(result):
            result = await result
        if isinstance(result, str) and result:
            self._token = result
            return result
        return None

    async def _maybe_reconnect_hook(self) -> None:
        if not self._on_reconnect:
            return
        result = self._on_reconnect()
        if asyncio.iscoroutine(result):
            await result

    async def _do_refresh(self) -> bool:
        """Invoke the refresher and adopt a fresh token. Returns True on success.

        Re-raises :class:`SessionExpired` so callers can distinguish "can't
        refresh, give up" from a transient refresh hiccup.
        """
        if not self._session_refresher:
            return False
        result = await self._session_refresher()
        if isinstance(result, str) and result:
            self._token = result
            logger.debug("TRSession token refreshed")
            return True
        return False

    async def _refresh_loop(self) -> None:
        """Proactively refresh the session token just under its expiry."""
        while not self._closing:
            try:
                await asyncio.sleep(self._refresh_interval)
            except asyncio.CancelledError:
                return
            if self._closing:
                return
            try:
                await self._do_refresh()
            except SessionExpired:
                logger.warning("Proactive refresh failed; session expired")
                await self._notify_session_expired()
                return
            except Exception:
                # Transient refresh error (network/WAF) — keep the loop alive
                # and try again next tick rather than killing the session.
                logger.exception("Session refresh error; will retry next interval")

    # ── Connection management ─────────────────────────────────────────────

    async def _connect_once(self):
        """Open the WS and send the initial ``connect`` frame."""
        try:
            ws = await websockets.connect(TR_WS_URL)
        except Exception as e:
            raise classify_ws_connect_error(e) from e

        connect_payload = dict(WS_CONNECT_PAYLOAD)
        connect_payload["locale"] = self._locale
        await ws.send(f"connect 34 {json.dumps(connect_payload)}")
        ack = await ws.recv()
        if ack != "connected":
            await ws.close()
            raise TransientError(f"Unexpected connect ack: {ack!r}")
        return ws

    async def _open(self) -> None:
        """Open the WS, with one WAF-retry if a refresh hook is set."""
        try:
            self._ws = await self._connect_once()
        except WafExpired:
            if not await self._refresh_waf():
                raise
            self._ws = await self._connect_once()

    async def _replay_subscriptions(self) -> None:
        """After a reconnect, resend ``sub`` frames for every live subscription."""
        for sub_id, (sub_type, params, _cb) in list(self._subs.items()):
            payload = {"type": sub_type, "token": self._token, **params}
            try:
                await self._ws.send(f"sub {sub_id} {json.dumps(payload)}")
                logger.debug("Replayed sub %d → %s", sub_id, sub_type)
            except Exception:
                logger.exception("Failed to replay sub %d", sub_id)

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def __aenter__(self) -> "TRSession":
        await self._open()
        self._reader_task = asyncio.create_task(self._reader_loop())
        if self._session_refresher:
            self._refresh_task = asyncio.create_task(self._refresh_loop())
        logger.info("TRSession connected")
        return self

    async def __aexit__(self, *_) -> None:
        self._closing = True
        for task in (self._reader_task, self._refresh_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        logger.info("TRSession closed")

    # ── Reader loop ────────────────────────────────────────────────────────

    async def _reader_loop(self) -> None:
        backoff = self._reconnect_backoff
        while True:
            try:
                async for frame in self._ws:
                    m = _FRAME_RE.match(frame)
                    if not m:
                        continue
                    sub_id = int(m.group(1))
                    is_error = m.group(2) == "E"
                    body = m.group(3)

                    if is_error:
                        # Surface auth-relevant errors as typed exceptions so
                        # the consumer hook gets a chance to react. Other
                        # errors stay scoped to the callback.
                        typed = classify_ws_error_frame(body)
                        if isinstance(typed, SessionExpired):
                            logger.warning("Sub %d session expired", sub_id)
                            # Try a refresh first — if it yields a fresh
                            # token, drop the socket so the reconnect path
                            # replays subs with it. Only give up if we can't.
                            try:
                                if await self._do_refresh():
                                    break
                            except SessionExpired:
                                pass
                            await self._notify_session_expired()
                            # Without a valid token there's no point staying
                            # connected — tear down and exit the loop.
                            return
                        if isinstance(typed, WafExpired):
                            logger.warning("Sub %d WAF rejected; refreshing", sub_id)
                            if await self._refresh_waf():
                                # Drop current connection so the reconnect
                                # path picks up the refreshed WAF cookie.
                                break
                            return
                        # Non-auth error: dispatch to the callback as an
                        # error payload so the consumer can decide.
                        cb_entry = self._subs.get(sub_id)
                        if cb_entry:
                            cb = cb_entry[2]
                            try:
                                data = json.loads(body)
                            except json.JSONDecodeError:
                                data = {"_raw": body}
                            try:
                                result = cb({"_error": True, "data": data})
                                if asyncio.iscoroutine(result):
                                    await result
                            except Exception:
                                logger.exception("Exception in callback for sub %d", sub_id)
                        continue

                    cb_entry = self._subs.get(sub_id)
                    if cb_entry is None:
                        continue
                    cb = cb_entry[2]

                    try:
                        data = json.loads(body)
                    except json.JSONDecodeError:
                        continue

                    try:
                        result = cb(data)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        logger.exception("Exception in callback for sub %d", sub_id)

                # ``async for`` returns when the WS closes cleanly.
                if self._closing:
                    return

            except asyncio.CancelledError:
                return
            except websockets.ConnectionClosed:
                if self._closing:
                    return
                logger.warning("TRSession WebSocket closed unexpectedly")

            if not self._auto_reconnect or self._closing:
                logger.info("TRSession reader exiting (auto_reconnect=%s)", self._auto_reconnect)
                return

            # Reconnect with exponential backoff.
            try:
                await asyncio.sleep(backoff)
                await self._open()
                await self._maybe_reconnect_hook()
                await self._replay_subscriptions()
                backoff = self._reconnect_backoff  # reset on success
                logger.info("TRSession reconnected; %d subs replayed", len(self._subs))
            except (TransientError, WafExpired, OSError) as e:
                logger.warning("Reconnect failed: %s; backing off %.1fs", e, backoff)
                backoff = min(backoff * 2, self._reconnect_max_backoff)
                continue
            except SessionExpired:
                logger.warning("Session expired during reconnect")
                await self._notify_session_expired()
                return

    # ── Core subscribe / unsubscribe ───────────────────────────────────────

    async def subscribe(self, sub_type: str, params: dict, callback: Callable) -> int:
        """Subscribe to a topic. ``callback(data: dict)`` is called for every
        incoming frame. Returns the subscription ID.

        ``callback`` may be a plain function or a coroutine function.

        When ``auto_reconnect=True``, the (``sub_type``, ``params``,
        ``callback``) triple is retained so the sub is replayed after a
        reconnect.
        """
        self._msg_id += 1
        sub_id = self._msg_id
        self._subs[sub_id] = (sub_type, dict(params), callback)
        payload = {"type": sub_type, "token": self._token, **params}
        await self._ws.send(f"sub {sub_id} {json.dumps(payload)}")
        logger.debug("Subscribed %d → %s", sub_id, sub_type)
        return sub_id

    async def unsubscribe(self, sub_id: int) -> None:
        """Unsubscribe and stop dispatching to the associated callback."""
        self._subs.pop(sub_id, None)
        try:
            await self._ws.send(f"unsub {sub_id}")
        except Exception:
            # WS may be down mid-reconnect; the sub is already forgotten
            # locally, which is all that matters.
            pass
        logger.debug("Unsubscribed %d", sub_id)

    # ── One-shot helper ────────────────────────────────────────────────────

    async def request(self, sub_type: str, params: dict, timeout: float = 5.0) -> dict:
        """Subscribe, receive the first data frame, unsubscribe, return data."""
        fut: asyncio.Future = asyncio.get_event_loop().create_future()

        def on_data(data):
            if not fut.done():
                fut.set_result(data)

        sub_id = await self.subscribe(sub_type, params, on_data)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("request timeout for %s %s", sub_type, params)
            return {}
        finally:
            await self.unsubscribe(sub_id)

    # ── Convenience subscriptions ──────────────────────────────────────────

    async def subscribe_ticker(self, isin: str, callback: Callable) -> int:
        """Subscribe to live ticker updates for an ISIN.

        Resolves the home exchange internally, then subscribes to ``ticker``.
        ``callback(data)`` receives frames with ``last``, ``pre``, ``bid``,
        ``ask``, ``open`` price objects.
        Returns the ticker subscription ID.
        """
        home = await self.request("homeInstrumentExchange", {"id": isin})
        exchange_id = home.get("id") or home.get("exchangeId")
        if not exchange_id:
            raise ValueError(f"Could not resolve home exchange for {isin}")
        return await self.subscribe("ticker", {"id": f"{isin}.{exchange_id}"}, callback)

    async def subscribe_portfolio(self, sec_acc_no: str, callback: Callable) -> int:
        """Subscribe to live portfolio position updates.

        Uses ``compactPortfolioByTypeV2`` — the response groups positions
        under per-instrument-type ``categories[]``. Pass the raw frame
        through :func:`traderepublic_sync.client._flatten_portfolio_positions`
        if you want a flat list.
        """
        return await self.subscribe("compactPortfolioByTypeV2", {"secAccNo": sec_acc_no}, callback)

    async def subscribe_cash(self, cash_acc_no: str, callback: Callable) -> int:
        """Subscribe to live available cash updates for a specific account.

        ``callback(data)`` receives frames with cash balance info.

        The filter param is ``accountNumber``, **not** ``id``: TR treats an
        ``id``-scoped ``availableCash`` sub as DEFAULT-scoped and returns the
        *primary* account's balance for every subscription (so a second
        account would show the first account's cash). ``accountNumber`` is
        what TR's own web client sends.
        """
        return await self.subscribe(
            "availableCash", {"accountNumber": cash_acc_no}, callback
        )

    async def subscribe_transactions(self, cash_acc_no: str, callback: Callable) -> int:
        """Subscribe to live transaction timeline updates.

        Note: ``timelineTransactions`` is account-global (the one-shot
        ``fetch_transactions`` sends no account filter at all), so the
        ``cash_acc_no`` is passed through as TR has historically accepted it.
        """
        return await self.subscribe("timelineTransactions", {"id": cash_acc_no}, callback)

    async def subscribe_fx(self, currency: str, callback: Callable) -> int:
        """Subscribe to the live EUR conversion rate for ``currency``.

        Uses the same ``ticker`` topic as instruments, against TR's synthetic
        LSX currency instruments. ``callback(data)`` receives frames with
        ``bid`` / ``ask`` price objects; the mid (``(bid+ask)/2``) is *units
        of foreign currency per 1 EUR*. Only USD / GBP / CHF / JPY are wired
        up by TR.
        """
        instrument = FX_INSTRUMENTS.get(currency.upper())
        if not instrument:
            raise ValueError(
                f"No FX ticker instrument for {currency!r}; "
                f"supported: {', '.join(sorted(FX_INSTRUMENTS))}"
            )
        return await self.subscribe("ticker", {"id": instrument}, callback)

    async def search_instrument(
        self,
        query: str,
        instrument_type: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Search for instruments by name or symbol via ``neonSearch``.

        ``instrument_type`` filters by asset class: ``"crypto"``, ``"stock"``,
        ``"etf"``, ``"bond"``, ``"derivative"``, ``"fund"``.

        Returns the raw list of result items from the search response.
        Each item typically contains ``isin``, ``name``, ``type``, and
        ``exchanges``.

        Example — find Bitcoin's pseudo-ISIN::

            results = await session.search_instrument("bitcoin", instrument_type="crypto")
            isin = results[0]["isin"]  # "XF000BTC0017"
        """
        filters = [{"key": "type", "value": instrument_type}] if instrument_type else []
        data = await self.request(
            "neonSearch",
            {"data": {"q": query, "filter": filters, "page": 1, "pageSize": limit}},
        )
        return data.get("results") or data.get("items") or []
