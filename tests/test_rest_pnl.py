"""Authenticated REST plumbing + realized-P&L parsing (taxes/pnl)."""

import pytest
import requests

from traderepublic_sync import TRClient


class _MockResponse:
    def __init__(self, status_code, json_body=None, text=""):
        self.status_code = status_code
        self._json = json_body
        self.text = text
        self.headers = {}

    def json(self):
        return self._json


_PNL_ONE = [
    {
        "secAccNo": "0254693503",
        "instrumentId": "NL0010273215",
        "realizedPnL": {"absolute": {"value": "302.00000000", "currency": "EUR"}},
        "dividendReturn": {"absolute": {"value": "10.80000000", "currency": "EUR"}},
        "lastUpdatedTimestamp": "2026-05-05T08:54:22.808790Z",
    }
]


def test_pnl_parsed_absolute_values(monkeypatch):
    monkeypatch.setattr(
        requests.Session, "request", lambda self, *a, **k: _MockResponse(200, _PNL_ONE)
    )
    client = TRClient(waf_token="ok", session_token="t")
    rows = client.fetch_realized_pnl("NL0010273215", sec_acc_nos=["0254693503"])

    assert len(rows) == 1
    r = rows[0]
    assert r["instrument_id"] == "NL0010273215"
    assert r["realized_pnl"] == {"value": 302.0, "currency": "EUR"}
    assert r["dividend_return"] == {"value": 10.8, "currency": "EUR"}
    assert r["last_updated"].startswith("2026-05-05")


def test_pnl_nullable_legs(monkeypatch):
    body = [
        {
            "secAccNo": "S1",
            "instrumentId": "US0378331005",
            "realizedPnL": None,
            "dividendReturn": {"absolute": {"value": "4.81", "currency": "EUR"}},
        }
    ]
    monkeypatch.setattr(
        requests.Session, "request", lambda self, *a, **k: _MockResponse(200, body)
    )
    client = TRClient(waf_token="ok", session_token="t")
    (r,) = client.fetch_realized_pnl("US0378331005", sec_acc_nos=["S1"])
    assert r["realized_pnl"] is None
    assert r["dividend_return"] == {"value": 4.81, "currency": "EUR"}


def test_pnl_one_entry_per_sec_acc(monkeypatch):
    body = [
        {"secAccNo": "S1", "instrumentId": "X", "realizedPnL": {"absolute": {"value": "1", "currency": "EUR"}}},
        {"secAccNo": "S2", "instrumentId": "X", "realizedPnL": {"absolute": {"value": "2", "currency": "EUR"}}},
    ]
    monkeypatch.setattr(
        requests.Session, "request", lambda self, *a, **k: _MockResponse(200, body)
    )
    client = TRClient(waf_token="ok", session_token="t")
    rows = client.fetch_realized_pnl("X", sec_acc_nos=["S1", "S2"])
    assert [r["sec_acc_no"] for r in rows] == ["S1", "S2"]


def test_pnl_404_returns_empty(monkeypatch):
    # crypto / bonds / savings → TR 404s; surfaced as an empty list.
    monkeypatch.setattr(
        requests.Session, "request", lambda self, *a, **k: _MockResponse(404)
    )
    client = TRClient(waf_token="ok", session_token="t")
    assert client.fetch_realized_pnl("XF000BTC0017", sec_acc_nos=["S1"]) == []


def test_rest_refreshes_session_on_401_then_retries(monkeypatch):
    calls = {"request": 0, "get": 0}

    def fake_request(self, *a, **k):
        calls["request"] += 1
        if calls["request"] == 1:
            return _MockResponse(401, text="unauthorized")  # → SessionExpired
        return _MockResponse(200, _PNL_ONE)

    def fake_get(self, *a, **k):
        # WebRefreshAuth.refresh_session() rolls a fresh tr_session onto the jar.
        calls["get"] += 1
        self.cookies.set("tr_session", "fresh", domain="api.traderepublic.com")
        return _MockResponse(200)

    monkeypatch.setattr(requests.Session, "request", fake_request)
    monkeypatch.setattr(requests.Session, "get", fake_get)

    client = TRClient(waf_token="ok", session_token="stale")
    rows = client.fetch_realized_pnl("NL0010273215", sec_acc_nos=["0254693503"])

    assert calls["request"] == 2  # failed, then retried
    assert calls["get"] == 1      # refresh happened
    assert client._session_token == "fresh"
    assert rows[0]["realized_pnl"]["value"] == 302.0


def test_rest_propagates_session_expired_when_refresh_fails(monkeypatch):
    from traderepublic_sync import SessionExpired

    monkeypatch.setattr(
        requests.Session, "request", lambda self, *a, **k: _MockResponse(401, text="nope")
    )
    # Refresh endpoint succeeds but rolls no cookie → refresh cookie is dead.
    monkeypatch.setattr(
        requests.Session, "get", lambda self, *a, **k: _MockResponse(200)
    )
    client = TRClient(waf_token="ok", session_token="stale")
    with pytest.raises(SessionExpired):
        client.fetch_realized_pnl("X", sec_acc_nos=["S1"])
