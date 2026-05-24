"""Persistent WebSocket session for live Trade Republic data subscriptions.

Usage::

    async with client.open_session(session_token) as session:
        async def on_price(data):
            print(data["last"]["price"])

        sub_id = await session.subscribe_ticker("US0378331005", on_price)
        await asyncio.sleep(60)
        await session.unsubscribe(sub_id)
"""

import asyncio
import json
import logging
import re
from collections.abc import Callable

import websockets

from .constants import TR_WS_URL, WS_CONNECT_PAYLOAD

logger = logging.getLogger(__name__)

_FRAME_RE = re.compile(r"^(\d+) ([AE]) ([\s\S]+)$")


class TRSession:
    """Long-lived WebSocket session with callback-based subscription dispatch.

    Open via :meth:`TRClient.open_session` — do not instantiate directly.
    """

    def __init__(self, token: str, locale: str = "fr"):
        self._token = token
        self._locale = locale
        self._ws = None
        self._msg_id = 0
        self._subs: dict[int, Callable] = {}
        self._reader_task: asyncio.Task | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def __aenter__(self) -> "TRSession":
        connect_payload = dict(WS_CONNECT_PAYLOAD)
        connect_payload["locale"] = self._locale
        self._ws = await websockets.connect(TR_WS_URL)
        await self._ws.send(f"connect 31 {json.dumps(connect_payload)}")
        ack = await self._ws.recv()
        if ack != "connected":
            raise RuntimeError(f"Unexpected connect ack: {ack!r}")
        self._reader_task = asyncio.create_task(self._reader_loop())
        logger.info("TRSession connected")
        return self

    async def __aexit__(self, *_) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()
        logger.info("TRSession closed")

    # ── Reader loop ────────────────────────────────────────────────────────

    async def _reader_loop(self) -> None:
        try:
            async for frame in self._ws:
                m = _FRAME_RE.match(frame)
                if not m:
                    continue
                sub_id = int(m.group(1))
                is_error = m.group(2) == "E"
                try:
                    data = json.loads(m.group(3))
                except json.JSONDecodeError:
                    continue

                cb = self._subs.get(sub_id)
                if cb is None:
                    continue

                try:
                    if is_error:
                        logger.warning("Sub %d error: %s", sub_id, data)
                        continue
                    result = cb(data)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    logger.exception("Exception in callback for sub %d", sub_id)

        except asyncio.CancelledError:
            pass
        except websockets.ConnectionClosed:
            logger.warning("TRSession WebSocket closed unexpectedly")

    # ── Core subscribe / unsubscribe ───────────────────────────────────────

    async def subscribe(self, sub_type: str, params: dict, callback: Callable) -> int:
        """Subscribe to a topic. ``callback(data: dict)`` is called for every
        incoming frame. Returns the subscription ID.

        ``callback`` may be a plain function or a coroutine function.
        """
        self._msg_id += 1
        sub_id = self._msg_id
        self._subs[sub_id] = callback
        payload = {"type": sub_type, "token": self._token, **params}
        await self._ws.send(f"sub {sub_id} {json.dumps(payload)}")
        logger.debug("Subscribed %d → %s", sub_id, sub_type)
        return sub_id

    async def unsubscribe(self, sub_id: int) -> None:
        """Unsubscribe and stop dispatching to the associated callback."""
        self._subs.pop(sub_id, None)
        await self._ws.send(f"unsub {sub_id}")
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

        ``callback(data)`` receives frames with a ``positions`` list.
        """
        return await self.subscribe("compactPortfolio", {"secAccNo": sec_acc_no}, callback)

    async def subscribe_cash(self, cash_acc_no: str, callback: Callable) -> int:
        """Subscribe to live available cash updates.

        ``callback(data)`` receives frames with cash balance info.
        """
        return await self.subscribe("availableCash", {"id": cash_acc_no}, callback)

    async def subscribe_transactions(self, cash_acc_no: str, callback: Callable) -> int:
        """Subscribe to live transaction timeline updates."""
        return await self.subscribe("timelineTransactions", {"id": cash_acc_no}, callback)

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
