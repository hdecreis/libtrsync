"""Tests for the since / until / since_id filters on fetch_transactions.

These mock the WS at the ``websockets.connect`` boundary so we exercise
the real pagination + filter logic in ``fetch_transactions`` without
hitting the network.
"""

import json
from datetime import datetime, timezone

from traderepublic_sync import TRClient
from traderepublic_sync.client import (
    _coerce_datetime,
    _flatten_portfolio_positions,
    _parse_iso_timestamp,
)


# ── Helpers: minimal fake WS that replays a scripted page sequence ─────────


class _FakeWS:
    """Stand-in for a websockets connection used by fetch_transactions.

    Scripted with a sequence of timeline pages. Each page is a list of
    ``items`` (raw TR-shaped dicts). ``timelineDetailV2`` requests always
    reply with an empty detail (the test asserts on raw_items, not parsed
    fields). Records which detail IDs the code actually asked for so we
    can prove early-stop / skip behavior.
    """

    def __init__(self, pages):
        # Each page is {"items": [...], "cursors": {"after": "...next..."}}
        self._pages = list(pages)
        self._inbox: list[str] = []
        self.detail_fetched: list[str] = []
        self.tl_payloads: list[dict] = []
        self._next_page_idx = 0
        self._after_to_page = {
            page.get("_after"): i for i, page in enumerate(pages) if page.get("_after")
        }

    # The async context manager protocol — fetch_transactions does
    # `async with self._ws_session() as ws:`, which yields this object.

    async def send(self, msg: str):
        # Strip the "connect 34 {...}" handshake — no response is replayed
        # for it beyond what __aenter__ already pushed.
        if msg.startswith("connect "):
            return

        # Sub format: "sub <id> <json-payload>"
        if msg.startswith("sub "):
            _, sub_id, raw = msg.split(" ", 2)
            payload = json.loads(raw)
            ptype = payload.get("type")
            if ptype == "timelineTransactions":
                self.tl_payloads.append(payload)
                after = payload.get("after")
                if after is None:
                    page_idx = 0
                else:
                    page_idx = self._after_to_page.get(after)
                if page_idx is None or page_idx >= len(self._pages):
                    body = json.dumps({"items": []})
                else:
                    page = self._pages[page_idx]
                    body = json.dumps(
                        {
                            "items": page["items"],
                            "cursors": page.get("cursors", {}),
                        }
                    )
                self._inbox.append(f"{sub_id} A {body}")
            elif ptype == "timelineDetailV2":
                self.detail_fetched.append(payload.get("id"))
                self._inbox.append(f"{sub_id} A " + json.dumps({"sections": []}))
            else:
                self._inbox.append(f"{sub_id} A {{}}")
            return

        if msg.startswith("unsub "):
            sub_id = msg.split(" ", 1)[1]
            # TR doesn't necessarily reply to unsub; push an empty C frame
            # so the matching `ws.recv()` doesn't hang.
            self._inbox.append(f"{sub_id} C")
            return

    async def recv(self):
        if not self._inbox:
            # Should never happen in a well-formed test — surface loudly.
            raise RuntimeError("FakeWS recv() called with empty inbox")
        return self._inbox.pop(0)

    async def close(self):
        pass


def _install_fake_ws(monkeypatch, pages, initial_ack="connected"):
    """Patch the client's _ws_session() to yield a FakeWS preloaded with `pages`."""
    from contextlib import asynccontextmanager
    from traderepublic_sync import client as client_mod

    @asynccontextmanager
    async def fake_ws_session(self):
        ws = _FakeWS(pages)
        # The TRClient handshake calls send("connect 34 {...}") then recv().
        # Push the ack into the inbox so recv() returns "connected".
        ws._inbox.append(initial_ack)
        try:
            yield ws
        finally:
            await ws.close()

    monkeypatch.setattr(client_mod.TRClient, "_ws_session", fake_ws_session)
    return None


def _item(tid: str, ts: str, amount: float = -10.0):
    """Minimal raw TR timeline item shape."""
    return {
        "id": tid,
        "timestamp": ts,
        "title": f"item-{tid}",
        "subtitle": "",
        "amount": {"value": amount, "currency": "EUR"},
        "eventType": "TRADING_TRADE_EXECUTED",
        "icon": "",
    }


# ── Parser tests ────────────────────────────────────────────────────────────


def test_parse_iso_tz_offset_no_colon():
    dt = _parse_iso_timestamp("2026-05-18T14:26:40.491+0000")
    assert dt == datetime(2026, 5, 18, 14, 26, 40, 491000, tzinfo=timezone.utc)


def test_parse_iso_z_suffix():
    dt = _parse_iso_timestamp("2026-05-18T14:26:40.491Z")
    assert dt == datetime(2026, 5, 18, 14, 26, 40, 491000, tzinfo=timezone.utc)


def test_parse_iso_microseconds():
    dt = _parse_iso_timestamp("2026-05-18T14:26:40.491231Z")
    assert dt == datetime(2026, 5, 18, 14, 26, 40, 491231, tzinfo=timezone.utc)


def test_parse_iso_handles_garbage():
    assert _parse_iso_timestamp(None) is None
    assert _parse_iso_timestamp("") is None
    assert _parse_iso_timestamp("not a date") is None


def test_coerce_datetime_string_and_datetime():
    assert _coerce_datetime("2026-05-18T14:26:40Z") == datetime(
        2026, 5, 18, 14, 26, 40, tzinfo=timezone.utc
    )
    aware = datetime(2026, 5, 18, tzinfo=timezone.utc)
    assert _coerce_datetime(aware) is aware


def test_coerce_datetime_naive_assumed_utc():
    naive = datetime(2026, 5, 18, 12, 0, 0)
    out = _coerce_datetime(naive)
    assert out.tzinfo == timezone.utc
    assert out.replace(tzinfo=None) == naive


# ── _flatten_portfolio_positions (compactPortfolioByTypeV2) ────────────────


def test_flatten_portfolio_v2_normalizes_field_names():
    """V2 uses ``isin`` and an envelope ``averageBuyIn``; the helper maps
    both to the legacy field names so downstream code stays uniform."""
    response = {
        "categories": [
            {
                "categoryType": "stocksAndETFs",
                "positions": [
                    {
                        "isin": "US0382221051",
                        "averageBuyIn": {"value": 258.19, "currency": "EUR"},
                        "netSize": 2.0,
                        "name": "Applied Materials",
                        "instrumentType": "stock",
                    },
                ],
            },
        ]
    }
    out = _flatten_portfolio_positions(response)
    assert len(out) == 1
    pos = out[0]
    assert pos["instrumentId"] == "US0382221051"  # legacy alias
    assert pos["isin"] == "US0382221051"  # original V2 field kept
    assert pos["averageBuyIn"] == 258.19  # unwrapped from envelope
    assert pos["averageBuyInCurrency"] == "EUR"  # currency exposed separately
    assert pos["name"] == "Applied Materials"
    assert pos["instrumentType"] == "stock"
    assert pos["_category"] == "stocksAndETFs"


def test_flatten_portfolio_v2_real_shape_from_dump():
    """Smoke test on the exact V2 shape observed in a live response —
    one position per known category, including the Bitcoin crypto."""
    response = {
        "categories": [
            {
                "categoryType": "stocksAndETFs",
                "positions": [
                    {
                        "isin": "IE00B4ND3602",
                        "averageBuyIn": {"value": 67.18, "currency": "EUR"},
                        "netSize": 17.99,
                        "name": "Physical Gold USD (Acc)",
                        "instrumentType": "fund",
                    },
                ],
            },
            {
                "categoryType": "cryptos",
                "positions": [
                    {
                        "isin": "XF000BTC0017",
                        "averageBuyIn": {"value": 72039.57, "currency": "EUR"},
                        "netSize": 0.000152,
                        "name": "Bitcoin",
                        "instrumentType": "crypto",
                    },
                ],
            },
            {
                "categoryType": "privateMarkets",
                "positions": [
                    {
                        "isin": "LU3170240538",
                        "averageBuyIn": {"value": 101.98, "currency": "EUR"},
                        "netSize": 2.65,
                        "name": "Private Equity",
                        "instrumentType": "privateFund",
                    },
                ],
            },
        ]
    }
    out = _flatten_portfolio_positions(response)
    assert [p["instrumentId"] for p in out] == [
        "IE00B4ND3602",
        "XF000BTC0017",
        "LU3170240538",
    ]
    assert [p["_category"] for p in out] == [
        "stocksAndETFs",
        "cryptos",
        "privateMarkets",
    ]
    btc = next(p for p in out if p["isin"] == "XF000BTC0017")
    assert btc["instrumentType"] == "crypto"
    assert btc["name"] == "Bitcoin"
    assert btc["averageBuyIn"] == 72039.57


def test_flatten_portfolio_v2_uses_instruments_key_when_present():
    """Some TR app versions use ``instruments`` instead of ``positions``."""
    response = {
        "categories": [
            {
                "categoryType": "crypto",
                "instruments": [{"isin": "XF000BTC0017", "netSize": 1}],
            },
        ]
    }
    out = _flatten_portfolio_positions(response)
    assert len(out) == 1 and out[0]["instrumentId"] == "XF000BTC0017"
    assert out[0]["_category"] == "crypto"


def test_flatten_portfolio_legacy_flat_shape_passes_through_unchanged():
    """Old ``compactPortfolio`` flat shape uses ``instrumentId`` + string
    ``averageBuyIn`` already, and is returned untouched."""
    response = {
        "positions": [
            {"instrumentId": "AAA", "netSize": "1.0", "averageBuyIn": "100.0"},
            {"instrumentId": "BBB", "netSize": "2.0", "averageBuyIn": "200.0"},
        ]
    }
    out = _flatten_portfolio_positions(response)
    assert [p["instrumentId"] for p in out] == ["AAA", "BBB"]
    assert "_category" not in out[0]
    # Legacy shape isn't normalized — averageBuyIn stays a string, callers
    # already coerce via _to_float.
    assert out[0]["averageBuyIn"] == "100.0"


def test_flatten_portfolio_empty_and_garbage():
    assert _flatten_portfolio_positions({}) == []
    assert _flatten_portfolio_positions({"categories": []}) == []
    assert _flatten_portfolio_positions(None) == []
    # categories that aren't dicts get skipped, not raised
    assert _flatten_portfolio_positions({"categories": ["nope", None]}) == []


# ── End-to-end filter tests ─────────────────────────────────────────────────

# Newest-first by date. Two pages, 3 items each.
PAGES = [
    {
        "_after": None,
        "items": [
            _item("id-2026-05-20", "2026-05-20T10:00:00Z"),
            _item("id-2026-05-15", "2026-05-15T10:00:00Z"),
            _item("id-2026-05-10", "2026-05-10T10:00:00Z"),
        ],
        "cursors": {"after": "cursor-page-2"},
    },
    {
        "_after": "cursor-page-2",
        "items": [
            _item("id-2026-04-20", "2026-04-20T10:00:00Z"),
            _item("id-2026-04-10", "2026-04-10T10:00:00Z"),
            _item("id-2026-03-01", "2026-03-01T10:00:00Z"),
        ],
        "cursors": {},  # no further pages
    },
]


def _fetch(client, **kwargs):
    import asyncio

    return asyncio.run(client.fetch_transactions(session_token="tok", **kwargs))


def test_no_filter_walks_everything(monkeypatch):
    _install_fake_ws(monkeypatch, PAGES)
    client = TRClient(waf_token="w", session_token="tok")
    result = _fetch(client)
    ids = [item["id"] for item in result["raw_items"]]
    assert ids == [
        "id-2026-05-20",
        "id-2026-05-15",
        "id-2026-05-10",
        "id-2026-04-20",
        "id-2026-04-10",
        "id-2026-03-01",
    ]


def test_since_stops_walk_when_older_than_bound(monkeypatch):
    _install_fake_ws(monkeypatch, PAGES)
    client = TRClient(waf_token="w", session_token="tok")
    # Only April 15 onwards → should include all of page 1 and part of page 2,
    # then early-stop at id-2026-04-10.
    result = _fetch(client, since=datetime(2026, 4, 15, tzinfo=timezone.utc))
    ids = [item["id"] for item in result["raw_items"]]
    assert ids == [
        "id-2026-05-20",
        "id-2026-05-15",
        "id-2026-05-10",
        "id-2026-04-20",
    ]


def test_since_accepts_iso_string(monkeypatch):
    _install_fake_ws(monkeypatch, PAGES)
    client = TRClient(waf_token="w", session_token="tok")
    result = _fetch(client, since="2026-05-12T00:00:00Z")
    ids = [item["id"] for item in result["raw_items"]]
    assert ids == ["id-2026-05-20", "id-2026-05-15"]


def test_until_skips_newer_but_keeps_walking(monkeypatch):
    fake_pages = PAGES
    _install_fake_ws(monkeypatch, fake_pages)
    client = TRClient(waf_token="w", session_token="tok")
    # Only items <= 2026-05-16: skip the first two newest, then keep going
    # through both pages.
    result = _fetch(client, until="2026-05-16T00:00:00Z")
    ids = [item["id"] for item in result["raw_items"]]
    assert ids == [
        "id-2026-05-15",
        "id-2026-05-10",
        "id-2026-04-20",
        "id-2026-04-10",
        "id-2026-03-01",
    ]


def test_until_skips_detail_fetch_for_excluded_items(monkeypatch):
    """The whole point: excluded items must NOT trigger timelineDetailV2."""
    _install_fake_ws(monkeypatch, PAGES)
    # Capture the FakeWS instance the patched _ws_session creates.
    captured = {}
    from contextlib import asynccontextmanager
    from traderepublic_sync import client as client_mod

    @asynccontextmanager
    async def capturing(self):
        ws = _FakeWS(PAGES)
        ws._inbox.append("connected")
        captured["ws"] = ws
        try:
            yield ws
        finally:
            await ws.close()

    monkeypatch.setattr(client_mod.TRClient, "_ws_session", capturing)

    client = TRClient(waf_token="w", session_token="tok")
    _fetch(client, until="2026-05-16T00:00:00Z")
    fetched = captured["ws"].detail_fetched
    assert "id-2026-05-20" not in fetched  # skipped (newer than `until`)
    assert "id-2026-05-15" in fetched
    assert "id-2026-04-20" in fetched


def test_since_id_stops_at_boundary_excluding_it(monkeypatch):
    _install_fake_ws(monkeypatch, PAGES)
    client = TRClient(waf_token="w", session_token="tok")
    result = _fetch(client, since_id="id-2026-05-10")
    ids = [item["id"] for item in result["raw_items"]]
    assert ids == ["id-2026-05-20", "id-2026-05-15"]


def test_since_id_normalizes_zero_runs(monkeypatch):
    # Build a custom page where the id has TR's absurd zero-padding.
    padded_id = "ce0c429b-00000000000000-328f-a304-96b69dd62702"
    pages = [
        {
            "_after": None,
            "items": [
                _item("newer-1", "2026-05-20T10:00:00Z"),
                _item("newer-2", "2026-05-15T10:00:00Z"),
                _item(padded_id, "2026-05-10T10:00:00Z"),
                _item("older", "2026-05-01T10:00:00Z"),
            ],
            "cursors": {},
        }
    ]
    _install_fake_ws(monkeypatch, pages)
    client = TRClient(waf_token="w", session_token="tok")

    # Caller passes the *normalized* form (collapsed to '00').
    result = _fetch(client, since_id="ce0c429b-00-328f-a304-96b69dd62702")
    ids = [item["id"] for item in result["raw_items"]]
    assert ids == ["newer-1", "newer-2"]


def test_since_id_stops_in_first_page_skips_page_2(monkeypatch):
    captured = {}
    from contextlib import asynccontextmanager
    from traderepublic_sync import client as client_mod

    @asynccontextmanager
    async def capturing(self):
        ws = _FakeWS(PAGES)
        ws._inbox.append("connected")
        captured["ws"] = ws
        try:
            yield ws
        finally:
            await ws.close()

    monkeypatch.setattr(client_mod.TRClient, "_ws_session", capturing)
    client = TRClient(waf_token="w", session_token="tok")
    _fetch(client, since_id="id-2026-05-15")
    # The second page (April + March items) must never have been requested.
    fetched = captured["ws"].detail_fetched
    assert all(
        not f.startswith("id-2026-04") and not f.startswith("id-2026-03")
        for f in fetched
    ), fetched


def test_combined_until_and_since(monkeypatch):
    _install_fake_ws(monkeypatch, PAGES)
    client = TRClient(waf_token="w", session_token="tok")
    result = _fetch(
        client,
        until="2026-05-16T00:00:00Z",
        since="2026-04-15T00:00:00Z",
    )
    ids = [item["id"] for item in result["raw_items"]]
    assert ids == ["id-2026-05-15", "id-2026-05-10", "id-2026-04-20"]


def _capture_ws(monkeypatch, pages):
    """Patch _ws_session to yield a captured FakeWS, returned to the caller."""
    from contextlib import asynccontextmanager
    from traderepublic_sync import client as client_mod

    captured = {}

    @asynccontextmanager
    async def capturing(self):
        ws = _FakeWS(pages)
        ws._inbox.append("connected")
        captured["ws"] = ws
        try:
            yield ws
        finally:
            await ws.close()

    monkeypatch.setattr(client_mod.TRClient, "_ws_session", capturing)
    return captured


def test_event_types_filter_injected_as_types(monkeypatch):
    captured = _capture_ws(monkeypatch, PAGES)
    client = TRClient(waf_token="w", session_token="tok")
    _fetch(client, event_types=["TRADING_TRADE_EXECUTED", "INTEREST_PAYOUT"])
    payloads = captured["ws"].tl_payloads
    assert payloads, "no timelineTransactions sub was sent"
    # Every page payload carries the server-side `types` filter (and no
    # `categoryIds`), under the TR wire key — not `eventTypes`.
    for p in payloads:
        assert p["types"] == ["TRADING_TRADE_EXECUTED", "INTEREST_PAYOUT"]
        assert "categoryIds" not in p
        assert "eventTypes" not in p


def test_categories_filter_injected_as_categoryids(monkeypatch):
    captured = _capture_ws(monkeypatch, PAGES)
    client = TRClient(waf_token="w", session_token="tok")
    _fetch(client, categories=["CRYPTO", "BOND"])
    payloads = captured["ws"].tl_payloads
    assert payloads
    for p in payloads:
        assert p["categoryIds"] == ["CRYPTO", "BOND"]
        assert "types" not in p


def test_no_filters_means_no_filter_keys(monkeypatch):
    captured = _capture_ws(monkeypatch, PAGES)
    client = TRClient(waf_token="w", session_token="tok")
    _fetch(client)
    for p in captured["ws"].tl_payloads:
        assert "types" not in p and "categoryIds" not in p
