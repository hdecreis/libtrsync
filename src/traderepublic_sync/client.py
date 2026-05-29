"""Trade Republic API client.

Handles authentication (login + 2FA), WebSocket subscription, and
transaction fetching. Returns both raw TR items and dual-legged transaction
dicts.
"""

import asyncio
import base64
import hashlib
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Union

import requests
import websockets

from ._classify import (
    classify_http,
    classify_network_error,
    classify_ws_connect_error,
    classify_ws_error_frame,
)
from .constants import DEFAULT_HEADERS, TR_API_BASE, TR_WS_URL, WS_CONNECT_PAYLOAD
from .exceptions import (
    SessionExpired,
    TRAuthError,
    TRError,
    TransientError,
    WafExpired,
)
from .auth import AuthStrategy, WebRefreshAuth
from .parsing import normalize_tr_id, parse_detail_sections
from .dual_legged.mapping import build_dual_legged_transaction, deduplicate_pea
from .session import TRSession
from .waf import get_waf_token

# A hook may be a plain function or an async coroutine function. It may
# return a fresh token string (which we adopt) or ``None`` (in which case
# we assume the hook has already mutated ``self.waf_token`` /
# ``self._session_token``).
WafHook = Callable[[], Union[str, None, Awaitable[Union[str, None]]]]
SessionHook = Callable[[], Union[str, None, Awaitable[Union[str, None]]]]

logger = logging.getLogger(__name__)


class TRClient:
    """Trade Republic API client.

    Usage flow::

        client = TRClient()
        client.acquire_waf_token("playwright")
        login_result = client.login(phone_number, pin)
        # User provides 2FA code
        session_token = client.verify_2fa(login_result["process_id"], code_2fa)
        result = asyncio.run(client.fetch_transactions(session_token))
    """

    def __init__(
        self,
        waf_token: str | None = None,
        device_info: str | None = None,
        locale: str = "fr",
        session_token: str | None = None,
        on_waf_expired: WafHook | None = None,
        on_session_expired: SessionHook | None = None,
        session_cookies: list[dict] | None = None,
        auth: AuthStrategy | None = None,
    ):
        """Construct a TR client.

        Args:
            waf_token: pre-acquired AWS WAF token (e.g. from a previous run).
            device_info: opaque base64 device fingerprint — reuse to keep the
                device "trusted" and avoid extra friction on re-auth.
            locale: ``fr``, ``de``, ``en``, …
            session_token: pre-acquired ``tr_session`` cookie value. If
                provided, all WS/REST calls can omit the explicit ``session_token``
                argument.
            session_cookies: full cookie jar from a previous session (as
                produced by :meth:`dump_cookies` / stored on
                :class:`~traderepublic_sync.state.ConnectionState`). Holds
                TR's refresh cookie, so :meth:`refresh_session` can mint a
                fresh ``session_token`` without 2FA.
            auth: session-refresh strategy. Defaults to
                :class:`~traderepublic_sync.auth.WebRefreshAuth`, which
                refreshes via ``GET /api/v1/auth/web/session``.
            on_waf_expired: optional callback fired when a request fails with
                :class:`WafExpired`. May be sync or async. Should return a
                fresh WAF token string, or ``None`` after mutating
                ``self.waf_token`` itself. The library retries the failed
                call once after the hook returns successfully; if the hook
                is absent or returns falsy, :class:`WafExpired` propagates.
            on_session_expired: optional callback fired when a request fails
                with :class:`SessionExpired`. Notification-only by default;
                if the callback returns a new session token string the
                library adopts it and retries once.
        """
        self.waf_token = waf_token or ""
        self.device_info = device_info or self._generate_device_info()
        self.locale = locale
        self._session_token = session_token
        self.auth = auth if auth is not None else WebRefreshAuth()
        self.on_waf_expired = on_waf_expired
        self.on_session_expired = on_session_expired
        # Shared HTTP session so the cookie jar (incl. TR's refresh cookie)
        # accumulates across login → verify_2fa → refresh_session.
        self._http = requests.Session()
        if session_cookies:
            self.load_cookies(session_cookies)

    @staticmethod
    def _generate_device_info() -> str:
        device_id = hashlib.sha512(uuid.uuid4().bytes).hexdigest()
        device_info = {"stableDeviceId": device_id}
        return base64.b64encode(json.dumps(device_info).encode()).decode()

    def _headers(self) -> dict:
        h = dict(DEFAULT_HEADERS)
        h["x-aws-waf-token"] = self.waf_token
        h["x-tr-device-info"] = self.device_info
        return h

    # ── Cookie jar ──────────────────────────────────────────────────────

    def _token_from_jar(self) -> str | None:
        """Read the current ``tr_session`` value off the shared cookie jar."""
        return self._http.cookies.get("tr_session")

    def dump_cookies(self) -> list[dict]:
        """Serialise the cookie jar for persistence (e.g. onto ``ConnectionState``).

        Returns a list of ``{name, value, domain, path}`` dicts — enough to
        rebuild a jar that TR will accept, including the refresh cookie that
        powers :meth:`refresh_session`.
        """
        return [
            {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
            for c in self._http.cookies
        ]

    def load_cookies(self, cookies: list[dict]) -> None:
        """Restore a cookie jar previously produced by :meth:`dump_cookies`."""
        for ck in cookies or []:
            self._http.cookies.set(
                ck["name"],
                ck["value"],
                domain=ck.get("domain", ""),
                path=ck.get("path", "/"),
            )

    def refresh_session(self) -> str:
        """Mint a fresh ``tr_session`` via the configured :class:`AuthStrategy`.

        No 2FA. Updates the stored session token and returns it. Raises
        :class:`SessionExpired` if the strategy can't refresh (the consumer
        then has to re-run ``login()`` + ``verify_2fa()``).
        """
        if self.auth is None:
            raise SessionExpired("no auth strategy configured to refresh the session")
        return self.auth.refresh_session(self)

    # ── Refresh hooks ──────────────────────────────────────────────────

    def _refresh_waf_sync(self) -> bool:
        """Invoke ``on_waf_expired`` (sync path). Returns True if WAF was renewed."""
        if not self.on_waf_expired:
            return False
        result = self.on_waf_expired()
        if asyncio.iscoroutine(result):
            # Caller is on the sync REST path — can't await. Tell the user.
            try:
                result.close()  # best-effort cleanup
            except Exception:
                pass
            raise TRError(
                "on_waf_expired returned a coroutine but the failing call is "
                "synchronous. Provide a sync callback (or wrap your async one)."
            )
        if isinstance(result, str) and result:
            self.waf_token = result
        return bool(self.waf_token)

    async def _refresh_waf_async(self) -> bool:
        """Invoke ``on_waf_expired`` (async-aware). Returns True if WAF was renewed."""
        if not self.on_waf_expired:
            return False
        result = self.on_waf_expired()
        if asyncio.iscoroutine(result):
            result = await result
        if isinstance(result, str) and result:
            self.waf_token = result
        return bool(self.waf_token)

    def _notify_session_expired_sync(self) -> bool:
        """Invoke ``on_session_expired`` (sync path). Returns True if session was renewed."""
        if not self.on_session_expired:
            return False
        result = self.on_session_expired()
        if asyncio.iscoroutine(result):
            try:
                result.close()
            except Exception:
                pass
            return False
        if isinstance(result, str) and result:
            self._session_token = result
            return True
        return False

    async def _notify_session_expired_async(self) -> bool:
        if not self.on_session_expired:
            return False
        result = self.on_session_expired()
        if asyncio.iscoroutine(result):
            result = await result
        if isinstance(result, str) and result:
            self._session_token = result
            return True
        return False

    # ── WS connect helper ──────────────────────────────────────────────

    @asynccontextmanager
    async def _ws_session(self):
        """Open the TR WebSocket with WAF-aware connect retry.

        Drop-in replacement for ``async with websockets.connect(TR_WS_URL) as ws:``.
        On a 403 (WAF reject) during the WS upgrade, calls ``on_waf_expired``
        and retries the connect once. Any other connection-level failure is
        re-raised as :class:`TransientError`.
        """
        async def _connect():
            try:
                return await websockets.connect(TR_WS_URL)
            except WafExpired:
                raise
            except Exception as e:
                raise classify_ws_connect_error(e) from e

        try:
            ws = await _connect()
        except WafExpired:
            if not await self._refresh_waf_async():
                raise
            ws = await _connect()

        try:
            try:
                yield ws
            except SessionExpired:
                # Notify the consumer so they can flag the connection as
                # ``pending_2fa`` immediately. Re-acquiring the session
                # requires 2FA, which the library cannot drive on its own,
                # so we always re-raise — the consumer's call site retries
                # after the user re-authenticates.
                await self._notify_session_expired_async()
                raise
        finally:
            try:
                await ws.close()
            except Exception:
                pass

    # ── WAF ────────────────────────────────────────────────────────────

    def acquire_waf_token(self, method: str = "playwright") -> str:
        """Acquire a WAF token using the specified method. Stores it on self."""
        self.waf_token = get_waf_token(method)
        return self.waf_token

    # ── Auth ───────────────────────────────────────────────────────────

    def login(self, phone_number: str, pin: str) -> dict:
        """Initiate login. Returns dict with process_id and countdown.

        On :class:`WafExpired`, the ``on_waf_expired`` hook (if set) is
        invoked and the request is retried once.
        """
        def _do():
            try:
                resp = self._http.post(
                    f"{TR_API_BASE}/api/v1/auth/web/login",
                    json={"phoneNumber": phone_number, "pin": pin},
                    headers=self._headers(),
                )
            except requests.RequestException as e:
                raise classify_network_error(e, context="login") from e
            classify_http(resp, context="login")

            data = resp.json()
            process_id = data.get("processId")
            if not process_id:
                raise TRAuthError("Login response missing processId")

            return {
                "process_id": process_id,
                "countdown": data.get("countdownInSeconds", 60),
            }

        try:
            return _do()
        except WafExpired:
            if not self._refresh_waf_sync():
                raise
            return _do()

    def request_sms(self, process_id: str) -> bool:
        """Request 2FA code via SMS instead of push notification."""
        def _do():
            try:
                resp = self._http.post(
                    f"{TR_API_BASE}/api/v1/auth/web/login/{process_id}/resend",
                    headers=self._headers(),
                )
            except requests.RequestException as e:
                raise classify_network_error(e, context="request_sms") from e
            if resp.status_code in (401, 403, 429) or resp.status_code >= 500:
                classify_http(resp, context="request_sms")  # raises typed exception
            return resp.status_code == 200

        try:
            return _do()
        except WafExpired:
            if not self._refresh_waf_sync():
                raise
            return _do()

    def verify_2fa(self, process_id: str, code: str) -> str:
        """Verify 2FA code. Returns session token on success."""
        def _do():
            try:
                resp = self._http.post(
                    f"{TR_API_BASE}/api/v1/auth/web/login/{process_id}/{code}",
                    headers=self._headers(),
                )
            except requests.RequestException as e:
                raise classify_network_error(e, context="verify_2fa") from e
            classify_http(resp, context="verify_2fa")
            return resp

        try:
            resp = _do()
        except WafExpired:
            if not self._refresh_waf_sync():
                raise
            resp = _do()

        # The shared cookie jar now holds tr_session *and* TR's refresh
        # cookie — keeping both is what lets refresh_session() work later.
        session_token = self._token_from_jar()

        # Fallback: parse the Set-Cookie header directly (jar empty if a
        # caller swapped in a non-cookie-persisting transport).
        for header, value in resp.headers.items():
            if session_token:
                break
            if header.lower() == "set-cookie" and "tr_session" in value:
                for part in value.split(";"):
                    part = part.strip()
                    if part.startswith("tr_session="):
                        session_token = part.split("=", 1)[1]
                        break
                break

        if not session_token:
            # Fallback: parse with the original header parsing
            parsed = _headers_to_dict(resp)
            session_token = parsed.get("Set-Cookie", {}).get("tr_session")

        if not session_token:
            raise TRAuthError("Session token not found in response headers")

        self._session_token = session_token
        return session_token

    # ── WebSocket data fetching ────────────────────────────────────────

    async def fetch_transactions(
        self,
        session_token: str | None = None,
        *,
        since: datetime | str | None = None,
        until: datetime | str | None = None,
        since_id: str | None = None,
    ) -> dict:
        """Fetch transactions + details via WebSocket, optionally bounded.

        TR returns the timeline newest-first with cursor pagination. The
        filters below leverage that ordering to early-stop the walk and skip
        the per-item ``timelineDetailV2`` round trip for anything we'd
        throw away — so a daily incremental sync typically touches one or
        two pages instead of the whole history.

        Args:
            session_token: ``tr_session`` cookie. Falls back to the one
                stored on the client.
            since: lower bound (inclusive) on the transaction timestamp.
                Items older than this stop the walk. Accepts a ``datetime``
                (naive ones are assumed UTC) or an ISO-8601 string.
            until: upper bound (inclusive) on the transaction timestamp.
                Items newer than this are skipped but the walk continues.
            since_id: stop the walk as soon as this raw TR ``id`` is seen.
                The boundary item itself is **not** included. IDs are
                compared after :func:`normalize_tr_id` so callers can pass
                either the raw or normalized form.

        Returns a dict with:
            - ``transactions``: list of dual-legged transaction dicts (deduped).
            - ``raw_items``: list of raw TR items, each with ``_detail`` and
              ``_detail_raw`` attached for downstream re-processing.
        """
        token = session_token or self._session_token
        if not token:
            raise TRAuthError("No session token. Call login() + verify_2fa() first.")

        since_dt = _coerce_datetime(since)
        until_dt = _coerce_datetime(until)
        since_id_norm = normalize_tr_id(since_id) if since_id else None

        raw_items = []
        dual_legged_transactions = []
        message_id = 0
        stop_walk = False

        async with self._ws_session() as ws:
            connect_payload = dict(WS_CONNECT_PAYLOAD)
            connect_payload["locale"] = self.locale
            await ws.send(f"connect 34 {json.dumps(connect_payload)}")
            await ws.recv()
            logger.info("WebSocket connected")

            after_cursor = None
            page = 0
            while not stop_walk:
                payload = {"type": "timelineTransactions", "token": token}
                if after_cursor:
                    payload["after"] = after_cursor

                message_id += 1
                await ws.send(f"sub {message_id} {json.dumps(payload)}")
                response = await ws.recv()
                await ws.send(f"unsub {message_id}")
                await ws.recv()

                data = _parse_ws_json(response)
                if not data.get("items"):
                    break

                page += 1
                items = data["items"]
                logger.info("Page %d: %d transactions", page, len(items))

                for item in items:
                    # Cheap filters first — they may save a detail round trip.
                    raw_id = item.get("id")
                    if since_id_norm and normalize_tr_id(raw_id) == since_id_norm:
                        stop_walk = True
                        break

                    item_dt = _parse_iso_timestamp(item.get("timestamp"))

                    if since_dt and item_dt and item_dt < since_dt:
                        # Items are sorted newest-first; nothing after this
                        # will be inside the window.
                        stop_walk = True
                        break

                    if until_dt and item_dt and item_dt > until_dt:
                        # Skip but keep walking — we're still above the window.
                        continue

                    detail_raw = {}
                    if raw_id:
                        detail_payload = {
                            "type": "timelineDetailV2",
                            "id": raw_id,
                            "token": token,
                        }
                        message_id += 1
                        await ws.send(f"sub {message_id} {json.dumps(detail_payload)}")
                        detail_response = await ws.recv()
                        await ws.send(f"unsub {message_id}")
                        await ws.recv()
                        detail_raw = _parse_ws_json(detail_response)

                    parsed = parse_detail_sections(detail_raw)
                    dual_legged_tx = build_dual_legged_transaction(item, parsed)

                    item["_detail"] = parsed
                    item["_detail_raw"] = detail_raw
                    raw_items.append(item)

                    if dual_legged_tx:
                        dual_legged_transactions.append(dual_legged_tx)

                if stop_walk:
                    break

                after_cursor = data.get("cursors", {}).get("after")
                if not after_cursor:
                    break

        dual_legged_transactions = deduplicate_pea(dual_legged_transactions)
        logger.info("%d raw items -> %d dual-legged transactions", len(raw_items), len(dual_legged_transactions))

        return {
            "transactions": dual_legged_transactions,
            "raw_items": raw_items,
        }

    async def fetch_account_pairs(self, session_token: str | None = None) -> list[dict]:
        """Fetch raw account pairs via the ``accountPairs`` WebSocket subscription.

        Returns the ``accounts`` list from the response, each entry containing
        ``securitiesAccountNumber``, ``cashAccountNumber``, ``productType``, and
        ``currency``.
        """
        token = session_token or self._session_token
        if not token:
            raise TRAuthError("No session token.")

        async with self._ws_session() as ws:
            connect_payload = dict(WS_CONNECT_PAYLOAD)
            connect_payload["locale"] = self.locale
            await ws.send(f"connect 34 {json.dumps(connect_payload)}")
            await ws.recv()

            payload = {"type": "accountPairs", "token": token}
            await ws.send(f"sub 1 {json.dumps(payload)}")

            pattern = re.compile(r"^1 A (\{[\s\S]*\})$")
            data: dict = {}
            for _ in range(10):
                frame = await ws.recv()
                m = pattern.match(frame)
                if m:
                    data = json.loads(m.group(1))
                    break

        pairs = data.get("accounts", [])
        logger.info("Fetched %d account pairs", len(pairs))
        return pairs

    async def fetch_account_list(self, session_token: str | None = None) -> list[dict]:
        """Return named accounts derived from ``accountPairs``.

        Each entry has ``account_name``, ``account_type``, ``currency``,
        ``securities_account_number``, and ``cash_account_number``.
        """
        pairs = await self.fetch_account_pairs(session_token)
        return _pairs_to_accounts(pairs)

    async def fetch_asset_list(self, session_token: str | None = None) -> list[dict]:
        """Fetch current portfolio positions with live prices.

        Uses ``compactPortfolioByTypeV2`` (quantity + cost basis),
        ``homeInstrumentExchange`` (exchange resolution), and ``ticker``
        (live price + previous close) in a single WebSocket session. All
        derived metrics are computed client-side:

        - ``current_value``   = quantity × current_price
        - ``daily_trend_pct`` = (current_price − previous_close) / previous_close × 100
        - ``daily_trend_eur`` = (current_price − previous_close) × quantity
        - ``since_buy_pct``   = (current_price − average_buy_in) / average_buy_in × 100
        - ``since_buy_eur``   = (current_price − average_buy_in) × quantity
        """
        token = session_token or self._session_token
        if not token:
            raise TRAuthError("No session token.")

        pairs = await self.fetch_account_pairs(token)
        sec_acc_no = pairs[0]["securitiesAccountNumber"] if pairs else ""

        async with self._ws_session() as ws:
            connect_payload = dict(WS_CONNECT_PAYLOAD)
            connect_payload["locale"] = self.locale
            await ws.send(f"connect 34 {json.dumps(connect_payload)}")
            await ws.recv()

            msg_id = 0

            async def sub_recv(payload: dict, timeout: float = 5.0) -> dict:
                nonlocal msg_id
                msg_id += 1
                return await _ws_sub(ws, msg_id, payload, timeout)

            portfolio = await sub_recv({"type": "compactPortfolioByTypeV2", "token": token, "secAccNo": sec_acc_no})
            positions = _flatten_portfolio_positions(portfolio)
            logger.info("compactPortfolioByTypeV2: %d positions", len(positions))

            assets = []
            for pos in positions:
                isin = pos.get("instrumentId")
                if not isin:
                    continue

                quantity     = _to_float(pos.get("netSize", 0))
                virtual_size = _to_float(pos.get("virtualSize"))
                avg_buy_in   = _to_float(pos.get("averageBuyIn"))

                # V2 carries the display name in the portfolio payload —
                # skip the per-position ``instrument`` round-trip when it's
                # already there. Falls back to the legacy lookup for the
                # old compactPortfolio shape (no ``name`` field).
                name = pos.get("name")
                if not name:
                    instrument = await sub_recv({"type": "instrument", "id": isin, "jurisdiction": "FR", "token": token})
                    name = instrument.get("name", isin)

                home = await sub_recv({"type": "homeInstrumentExchange", "id": isin, "token": token})
                exchange_id = home.get("id") or home.get("exchangeId")
                # currency may arrive as {"id": "EUR", "name": "..."} or plain string
                raw_currency = home.get("currency", "EUR")
                currency = raw_currency.get("id") if isinstance(raw_currency, dict) else raw_currency

                last_price = prev_close = bid = ask = open_price = None
                if exchange_id:
                    ticker = await sub_recv({"type": "ticker", "id": f"{isin}.{exchange_id}", "token": token})
                    last_price  = _to_float((ticker.get("last") or {}).get("price"))
                    prev_close  = _to_float((ticker.get("pre")  or {}).get("price"))
                    bid         = _to_float((ticker.get("bid")  or {}).get("price"))
                    ask         = _to_float((ticker.get("ask")  or {}).get("price"))
                    open_price  = _to_float((ticker.get("open") or {}).get("price"))

                asset: dict = {
                    "isin": isin,
                    "asset_name": name,
                    "quantity": quantity,
                    "virtual_size": virtual_size,
                    "average_buy_in": avg_buy_in,
                    "currency": currency,
                    "exchange_id": exchange_id,
                    "current_price": last_price,
                    "previous_close": prev_close,
                    "bid": bid,
                    "ask": ask,
                    "open": open_price,
                    # V2 enrichments (None if the legacy sub was used).
                    "instrument_type": pos.get("instrumentType"),
                    "category": pos.get("_category"),
                }

                if last_price is not None and quantity:
                    asset["current_value"] = round(quantity * last_price, 2)
                if last_price is not None and prev_close:
                    asset["daily_trend_pct"] = round((last_price - prev_close) / prev_close * 100, 4)
                    asset["daily_trend_eur"] = round((last_price - prev_close) * quantity, 2)
                if last_price is not None and avg_buy_in:
                    asset["since_buy_pct"] = round((last_price - avg_buy_in) / avg_buy_in * 100, 4)
                    asset["since_buy_eur"] = round((last_price - avg_buy_in) * quantity, 2)

                assets.append(asset)

        logger.info("Fetched %d positions with live prices", len(assets))
        return assets

    @staticmethod
    def derive_account_list(fetch_result: dict) -> list[dict]:
        """Derive unique accounts from a pre-fetched :func:`fetch_transactions` result.

        Returns a list of ``{"account_name": ..., "account_type": ...}`` dicts.
        """
        seen: dict[str, dict] = {}
        for tx in fetch_result["transactions"]:
            name = tx.get("account_name") or "Trade Republic"
            if name not in seen:
                seen[name] = {
                    "account_name": name,
                    "account_type": tx.get("account_type", "BROKERAGE"),
                }
        accounts = list(seen.values())
        logger.info("Derived %d accounts from transactions", len(accounts))
        return accounts

    @staticmethod
    def derive_asset_list(fetch_result: dict) -> list[dict]:
        """Derive current holdings from a pre-fetched :func:`fetch_transactions` result.

        For each ISIN computes net quantity (PURCHASE adds, SELL subtracts) and
        tracks the most recent known unit price and asset name.

        Returns a list of dicts with keys:
            ``isin``, ``asset_name``, ``quantity``, ``last_unit_price``,
            ``estimated_value``, ``last_date``, ``account_name``.
        Only positions with a net quantity > 0 are returned.
        """
        holdings: dict[str, dict] = {}

        for tx in sorted(fetch_result["transactions"], key=lambda t: t.get("date", "")):
            isin = tx.get("asset_isin")
            if not isin:
                continue
            tx_type = tx.get("transaction_type")
            if tx_type not in ("PURCHASE", "SELL"):
                continue

            if isin not in holdings:
                holdings[isin] = {
                    "isin": isin,
                    "asset_name": tx.get("asset_name", ""),
                    "quantity": 0.0,
                    "last_unit_price": None,
                    "estimated_value": None,
                    "last_date": None,
                    "account_name": tx.get("account_name", ""),
                }

            h = holdings[isin]
            qty = tx.get("quantity") or 0.0
            if tx_type == "PURCHASE":
                h["quantity"] = round(h["quantity"] + qty, 10)
            elif tx_type == "SELL":
                h["quantity"] = round(h["quantity"] - qty, 10)

            if tx.get("unit_price"):
                h["last_unit_price"] = tx["unit_price"]
                h["last_date"] = tx.get("date")
            if tx.get("asset_name"):
                h["asset_name"] = tx["asset_name"]

        for h in holdings.values():
            if h["last_unit_price"] and h["quantity"] > 0:
                h["estimated_value"] = round(h["quantity"] * h["last_unit_price"], 2)

        positions = [h for h in holdings.values() if h["quantity"] > 1e-9]
        logger.info("Derived %d open positions from transactions", len(positions))
        return positions

    async def fetch_ticker(self, isin: str, session_token: str | None = None) -> dict:
        """Fetch the live quote for any ISIN via WebSocket.

        Resolves the home exchange via ``homeInstrumentExchange``, then
        subscribes to ``ticker`` for one frame of price data.

        Returns a dict with ``isin``, ``exchange_id``, ``currency``,
        ``current_price``, ``previous_close``, ``bid``, ``ask``, ``open``,
        and optionally ``asset_name``.
        """
        token = session_token or self._session_token
        if not token:
            raise TRAuthError("No session token.")

        async with self._ws_session() as ws:
            connect_payload = dict(WS_CONNECT_PAYLOAD)
            connect_payload["locale"] = self.locale
            await ws.send(f"connect 34 {json.dumps(connect_payload)}")
            await ws.recv()

            msg_id = 0

            async def sub_recv(payload: dict, timeout: float = 5.0) -> dict:
                nonlocal msg_id
                msg_id += 1
                return await _ws_sub(ws, msg_id, payload, timeout)

            instrument = await sub_recv({"type": "instrument", "id": isin, "jurisdiction": "FR", "token": token})
            name = instrument.get("name", isin)

            home = await sub_recv({"type": "homeInstrumentExchange", "id": isin, "token": token})
            exchange_id = home.get("id") or home.get("exchangeId")
            raw_currency = home.get("currency", "EUR")
            currency = raw_currency.get("id") if isinstance(raw_currency, dict) else raw_currency

            result: dict = {
                "isin": isin,
                "asset_name": name,
                "exchange_id": exchange_id,
                "currency": currency,
                "current_price": None,
                "previous_close": None,
                "bid": None,
                "ask": None,
                "open": None,
            }

            if exchange_id:
                ticker = await sub_recv({"type": "ticker", "id": f"{isin}.{exchange_id}", "token": token})
                result["current_price"] = _to_float((ticker.get("last") or {}).get("price"))
                result["previous_close"] = _to_float((ticker.get("pre")  or {}).get("price"))
                result["bid"]            = _to_float((ticker.get("bid")  or {}).get("price"))
                result["ask"]            = _to_float((ticker.get("ask")  or {}).get("price"))
                result["open"]           = _to_float((ticker.get("open") or {}).get("price"))

        return result

    def open_session(
        self,
        session_token: str | None = None,
        *,
        auto_reconnect: bool = False,
        on_reconnect=None,
        auto_refresh: bool = True,
        refresh_interval: float = 270.0,
    ) -> TRSession:
        """Return a :class:`TRSession` async context manager for live subscriptions.

        Usage::

            async with client.open_session(session_token) as session:
                sub_id = await session.subscribe_ticker("US0378331005", on_price)
                await asyncio.sleep(60)

        ``auto_reconnect=True`` keeps the session alive across transient
        WS drops, replaying live subscriptions on the new socket. The
        ``on_waf_expired`` / ``on_session_expired`` hooks configured on this
        client are forwarded to the session.

        ``auto_refresh=True`` (the default) runs a background task that
        calls :meth:`refresh_session` every ``refresh_interval`` seconds
        (default 270s, just under TR's ~5 min token lifetime) and feeds the
        fresh token to the live subscriptions — so the session stays valid
        indefinitely without 2FA, until the refresh cookie itself expires.
        Disabled automatically when no auth strategy is configured.
        """
        token = session_token or self._session_token
        if not token:
            raise TRAuthError("No session token.")

        refresher = None
        if auto_refresh and self.auth is not None:
            async def refresher() -> str:
                # refresh_session() is sync (requests) — keep it off the
                # event loop so the WS reader isn't blocked.
                return await asyncio.to_thread(self.refresh_session)

        return TRSession(
            token=token,
            locale=self.locale,
            auto_reconnect=auto_reconnect,
            on_waf_expired=self.on_waf_expired,
            on_session_expired=self.on_session_expired,
            on_reconnect=on_reconnect,
            session_refresher=refresher,
            refresh_interval=refresh_interval,
        )

    async def fetch_cash_balance(self, session_token: str | None = None):
        """Fetch available cash balance via WebSocket."""
        token = session_token or self._session_token
        if not token:
            raise TRAuthError("No session token.")

        async with self._ws_session() as ws:
            connect_payload = dict(WS_CONNECT_PAYLOAD)
            connect_payload["locale"] = self.locale
            await ws.send(f"connect 34 {json.dumps(connect_payload)}")
            await ws.recv()

            payload = {"type": "availableCash", "token": token}
            await ws.send(f"sub 1 {json.dumps(payload)}")
            response = await ws.recv()

            start = response.find("[")
            end = response.rfind("]")
            if start != -1 and end != -1:
                return json.loads(response[start : end + 1])
            return _parse_ws_json(response)


# ── Helpers ────────────────────────────────────────────────────────────────

_PRODUCT_TYPE_MAP = {
    "DEFAULT":     ("Trade Republic CTO", "BROKERAGE"),
    "TAX_WRAPPER": ("Trade Republic PEA", "BROKERAGE"),
}


def _pairs_to_accounts(pairs: list[dict]) -> list[dict]:
    """Map raw accountPairs entries to named account dicts."""
    accounts = []
    seen_cash: set[str] = set()
    for pair in pairs:
        product_type = pair.get("productType", "DEFAULT")
        name, acct_type = _PRODUCT_TYPE_MAP.get(product_type, ("Trade Republic", "BROKERAGE"))
        accounts.append({
            "account_name": name,
            "account_type": acct_type,
            "currency": pair.get("currency", "EUR"),
            "securities_account_number": pair.get("securitiesAccountNumber"),
            "cash_account_number": pair.get("cashAccountNumber"),
        })
        cash_num = pair.get("cashAccountNumber")
        if cash_num and cash_num not in seen_cash:
            seen_cash.add(cash_num)
            accounts.append({
                "account_name": "Trade Republic",
                "account_type": "CASH",
                "currency": pair.get("currency", "EUR"),
                "securities_account_number": None,
                "cash_account_number": cash_num,
            })
    return accounts


async def _ws_sub(ws, msg_id: int, payload: dict, timeout: float = 5.0) -> dict:
    """Send ``sub <msg_id> <payload>``, wait for the ``A`` data frame, unsub.

    If the server replies with ``<msg_id> E …`` and the body looks like an
    auth/session issue, raises the appropriate typed exception so callers
    can react instead of seeing an empty dict.
    """
    await ws.send(f"sub {msg_id} {json.dumps(payload)}")
    a_pattern = re.compile(rf"^{msg_id} A ([\s\S]+)$")
    e_pattern = re.compile(rf"^{msg_id} E ([\s\S]+)$")
    result: dict = {}
    try:
        for _ in range(20):
            frame = await asyncio.wait_for(ws.recv(), timeout=timeout)
            m = a_pattern.match(frame)
            if m:
                result = json.loads(m.group(1))
                break
            em = e_pattern.match(frame)
            if em:
                typed = classify_ws_error_frame(em.group(1))
                if typed:
                    raise typed
                # Unrecognized error frame — surface so caller can log/decide.
                raise TransientError(f"WS error frame: {em.group(1)[:200]}")
    except asyncio.TimeoutError:
        pass
    finally:
        try:
            await ws.send(f"unsub {msg_id}")
            try:
                await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
        except Exception:
            # Connection may already be dead; nothing more we can do here.
            pass
    return result


def _flatten_portfolio_positions(portfolio: dict) -> list[dict]:
    """Normalize ``compactPortfolioByTypeV2`` (or legacy ``compactPortfolio``)
    into a flat list of positions with a stable field shape.

    Both subscriptions are supported and produce dicts with the same keys
    regardless of source, so callers don't have to branch:

    - ``instrumentId`` — ISIN (mirrored as ``isin`` for V2 consumers)
    - ``netSize`` / ``virtualSize`` — quantities (float-coercible)
    - ``averageBuyIn`` — cost basis as a plain number (unwrapped from the
      V2 ``{value, currency}`` envelope when present)
    - ``averageBuyInCurrency`` — populated from V2's envelope when present
    - ``name``, ``instrumentType``, ``_category`` — V2-only enrichments

    The V2 response groups under ``categories[].positions`` (also seen as
    ``categories[].instruments`` across TR app versions). The legacy
    response is a top-level ``positions`` list.
    """
    if not isinstance(portfolio, dict):
        return []

    # Legacy flat shape — passes through unchanged for back-compat.
    flat = portfolio.get("positions")
    if isinstance(flat, list) and flat:
        return list(flat)

    out: list[dict] = []
    for group in portfolio.get("categories") or []:
        if not isinstance(group, dict):
            continue
        category = group.get("categoryType") or group.get("type")
        for raw in (group.get("positions") or group.get("instruments") or []):
            if not isinstance(raw, dict):
                continue
            out.append(_normalize_v2_position(raw, category))
    return out


def _normalize_v2_position(pos: dict, category: str | None) -> dict:
    """Map a V2 position dict to the shape downstream code expects.

    Keeps every original field intact and adds the legacy-style aliases
    (``instrumentId``, scalar ``averageBuyIn``) plus the ``_category``
    tag.
    """
    isin = pos.get("isin") or pos.get("instrumentId")

    avg = pos.get("averageBuyIn")
    if isinstance(avg, dict):
        avg_scalar = avg.get("value")
        avg_currency = avg.get("currency")
    else:
        avg_scalar = avg
        avg_currency = None

    normalized = {**pos, "instrumentId": isin, "isin": isin}
    if avg_scalar is not None:
        normalized["averageBuyIn"] = avg_scalar
    if avg_currency:
        normalized["averageBuyInCurrency"] = avg_currency
    if category and "_category" not in normalized:
        normalized["_category"] = category
    return normalized


def _coerce_datetime(value):
    """Accept None / ``datetime`` / ISO-8601 string. Return aware ``datetime`` or None.

    Naive datetimes are assumed UTC (matching how TR serializes timestamps).
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        return _parse_iso_timestamp(value)
    raise TypeError(f"Expected None, datetime, or ISO string; got {type(value).__name__}")


_ISO_TRAILING_TZ_RE = re.compile(r"([+-]\d{2})(\d{2})$")


def _parse_iso_timestamp(text):
    """Tolerant ISO-8601 parser for TR timestamps.

    Handles the three variants TR is known to emit:
      - ``2026-05-18T14:26:40.491+0000`` (no colon in offset)
      - ``2026-05-18T14:26:40.491Z``
      - ``2026-05-18T14:26:40.491231Z`` (microsecond precision)

    Returns an aware UTC ``datetime`` or ``None`` if the input is missing
    or unparseable.
    """
    if not text or not isinstance(text, str):
        return None
    s = text.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # Insert the missing colon in offsets like "+0000" → "+00:00".
    s = _ISO_TRAILING_TZ_RE.sub(r"\1:\2", s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _to_float(value) -> float | None:
    """Coerce a value that may be int, float, or string to float, or None."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_ws_json(response):
    """Extract JSON object from a WebSocket response string.

    Raises a typed exception if the frame is an error frame (``<id> E …``)
    so session/WAF problems surface instead of being silently treated as
    empty results.
    """
    parts = response.split(" ", 2)
    if len(parts) >= 2 and parts[1] == "E":
        body = parts[2] if len(parts) > 2 else ""
        typed = classify_ws_error_frame(body)
        if typed:
            raise typed
        raise TransientError(f"WS error frame: {body[:200]}")

    start = response.find("{")
    end = response.rfind("}")
    if start != -1 and end != -1:
        return json.loads(response[start : end + 1])
    return {}


def _headers_to_dict(response):
    """Parse response headers into a nested dict."""
    extracted = {}
    for header, value in response.headers.items():
        parsed_dict = {}
        entries = value.split(", ")
        for entry in entries:
            key_value = entry.split(";")[0]
            if "=" in key_value:
                k, v = key_value.split("=", 1)
                parsed_dict[k.strip()] = v.strip()
        extracted[header] = parsed_dict if parsed_dict else value
    return extracted
