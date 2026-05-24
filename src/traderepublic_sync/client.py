"""Trade Republic API client.

Handles authentication (login + 2FA), WebSocket subscription, and
transaction fetching. Returns both raw TR items and dual-legged transaction
dicts.
"""

import asyncio  # noqa: F401  (kept for callers that want `asyncio.run`)
import base64
import hashlib
import json
import logging
import re
import uuid

import requests
import websockets

from .constants import DEFAULT_HEADERS, TR_API_BASE, TR_WS_URL, WS_CONNECT_PAYLOAD
from .exceptions import TRAuthError
from .parsing import parse_detail_sections
from .dual_legged.mapping import build_dual_legged_transaction, deduplicate_pea
from .session import TRSession
from .waf import get_waf_token

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

    def __init__(self, waf_token=None, device_info=None, locale="fr"):
        self.waf_token = waf_token or ""
        self.device_info = device_info or self._generate_device_info()
        self.locale = locale
        self._session_token = None

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

    # ── WAF ────────────────────────────────────────────────────────────

    def acquire_waf_token(self, method: str = "playwright") -> str:
        """Acquire a WAF token using the specified method. Stores it on self."""
        self.waf_token = get_waf_token(method)
        return self.waf_token

    # ── Auth ───────────────────────────────────────────────────────────

    def login(self, phone_number: str, pin: str) -> dict:
        """Initiate login. Returns dict with process_id and countdown."""
        resp = requests.post(
            f"{TR_API_BASE}/api/v1/auth/web/login",
            json={"phoneNumber": phone_number, "pin": pin},
            headers=self._headers(),
        )
        if resp.status_code != 200:
            raise TRAuthError(f"Login failed (HTTP {resp.status_code}): {resp.text}")

        data = resp.json()
        process_id = data.get("processId")
        if not process_id:
            raise TRAuthError("Login response missing processId")

        return {
            "process_id": process_id,
            "countdown": data.get("countdownInSeconds", 60),
        }

    def request_sms(self, process_id: str) -> bool:
        """Request 2FA code via SMS instead of push notification."""
        resp = requests.post(
            f"{TR_API_BASE}/api/v1/auth/web/login/{process_id}/resend",
            headers=self._headers(),
        )
        return resp.status_code == 200

    def verify_2fa(self, process_id: str, code: str) -> str:
        """Verify 2FA code. Returns session token on success."""
        resp = requests.post(
            f"{TR_API_BASE}/api/v1/auth/web/login/{process_id}/{code}",
            headers=self._headers(),
        )
        if resp.status_code != 200:
            raise TRAuthError(f"2FA verification failed (HTTP {resp.status_code}): {resp.text}")

        # Extract session token from Set-Cookie header
        session_token = None
        for header, value in resp.headers.items():
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

    async def fetch_transactions(self, session_token: str | None = None) -> dict:
        """Fetch all transactions + details via WebSocket.

        Returns a dict with:
            - ``transactions``: list of dual-legged transaction dicts (deduped).
            - ``raw_items``: list of raw TR items, each with ``_detail`` and
              ``_detail_raw`` attached for downstream re-processing.
        """
        token = session_token or self._session_token
        if not token:
            raise TRAuthError("No session token. Call login() + verify_2fa() first.")

        raw_items = []
        dual_legged_transactions = []
        message_id = 0

        async with websockets.connect(TR_WS_URL) as ws:
            connect_payload = dict(WS_CONNECT_PAYLOAD)
            connect_payload["locale"] = self.locale
            await ws.send(f"connect 31 {json.dumps(connect_payload)}")
            await ws.recv()
            logger.info("WebSocket connected")

            after_cursor = None
            page = 0
            while True:
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
                    transaction_id = item.get("id")
                    detail_raw = {}
                    if transaction_id:
                        detail_payload = {
                            "type": "timelineDetailV2",
                            "id": transaction_id,
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

        async with websockets.connect(TR_WS_URL) as ws:
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

        Uses ``compactPortfolio`` (quantity + cost basis), ``homeInstrumentExchange``
        (exchange resolution), and ``ticker`` (live price + previous close) in a
        single WebSocket session. All derived metrics are computed client-side:

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

        async with websockets.connect(TR_WS_URL) as ws:
            connect_payload = dict(WS_CONNECT_PAYLOAD)
            connect_payload["locale"] = self.locale
            await ws.send(f"connect 31 {json.dumps(connect_payload)}")
            await ws.recv()

            msg_id = 0

            async def sub_recv(payload: dict, timeout: float = 5.0) -> dict:
                nonlocal msg_id
                msg_id += 1
                return await _ws_sub(ws, msg_id, payload, timeout)

            portfolio = await sub_recv({"type": "compactPortfolio", "token": token, "secAccNo": sec_acc_no})
            positions = portfolio.get("positions", [])
            logger.info("compactPortfolio: %d positions", len(positions))

            assets = []
            for pos in positions:
                isin = pos.get("instrumentId")
                if not isin:
                    continue

                quantity     = _to_float(pos.get("netSize", 0))
                virtual_size = _to_float(pos.get("virtualSize"))
                avg_buy_in   = _to_float(pos.get("averageBuyIn"))

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

        async with websockets.connect(TR_WS_URL) as ws:
            connect_payload = dict(WS_CONNECT_PAYLOAD)
            connect_payload["locale"] = self.locale
            await ws.send(f"connect 31 {json.dumps(connect_payload)}")
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

    def open_session(self, session_token: str | None = None) -> TRSession:
        """Return a :class:`TRSession` async context manager for live subscriptions.

        Usage::

            async with client.open_session(session_token) as session:
                sub_id = await session.subscribe_ticker("US0378331005", on_price)
                await asyncio.sleep(60)
        """
        token = session_token or self._session_token
        if not token:
            raise TRAuthError("No session token.")
        return TRSession(token=token, locale=self.locale)

    async def fetch_cash_balance(self, session_token: str | None = None):
        """Fetch available cash balance via WebSocket."""
        token = session_token or self._session_token
        if not token:
            raise TRAuthError("No session token.")

        async with websockets.connect(TR_WS_URL) as ws:
            connect_payload = dict(WS_CONNECT_PAYLOAD)
            connect_payload["locale"] = self.locale
            await ws.send(f"connect 31 {json.dumps(connect_payload)}")
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
    """Send ``sub <msg_id> <payload>``, wait for the ``A`` data frame, unsub."""
    await ws.send(f"sub {msg_id} {json.dumps(payload)}")
    pattern = re.compile(rf"^{msg_id} A ([\s\S]+)$")
    result: dict = {}
    try:
        for _ in range(20):
            frame = await asyncio.wait_for(ws.recv(), timeout=timeout)
            m = pattern.match(frame)
            if m:
                result = json.loads(m.group(1))
                break
    except asyncio.TimeoutError:
        pass
    finally:
        await ws.send(f"unsub {msg_id}")
        try:
            await asyncio.wait_for(ws.recv(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
    return result


def _to_float(value) -> float | None:
    """Coerce a value that may be int, float, or string to float, or None."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_ws_json(response):
    """Extract JSON object from a WebSocket response string."""
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
