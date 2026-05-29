"""Tests for the no-2FA session-refresh path (Option A).

Covers ``WebRefreshAuth`` (``GET /api/v1/auth/web/session``), the cookie-jar
persistence that powers it, the ``DeviceKeyAuth`` plugin stub, and the
``TRSession`` proactive-refresh helpers.
"""

import asyncio

import pytest
import requests

from traderepublic_sync import (
    AuthStrategy,
    ConnectionState,
    DeviceKeyAuth,
    SessionExpired,
    TRClient,
    TRSession,
    WebRefreshAuth,
)


class _MockResponse:
    def __init__(self, status_code, body=""):
        self.status_code = status_code
        self.text = body
        self.headers = {}


# ── WebRefreshAuth / refresh_session ────────────────────────────────────────

def test_default_auth_is_web_refresh():
    assert isinstance(TRClient().auth, WebRefreshAuth)
    assert isinstance(TRClient().auth, AuthStrategy)


def test_refresh_session_rolls_new_token(monkeypatch):
    def fake_get(self, *a, **k):
        self.cookies.set("tr_session", "rolled-token", domain="api.traderepublic.com")
        return _MockResponse(200)

    monkeypatch.setattr(requests.Session, "get", fake_get)

    client = TRClient(waf_token="ok")
    token = client.refresh_session()

    assert token == "rolled-token"
    assert client._session_token == "rolled-token"


def test_refresh_session_no_cookie_raises_session_expired(monkeypatch):
    # Endpoint succeeds but rolls no tr_session → refresh cookie is dead.
    monkeypatch.setattr(requests.Session, "get", lambda self, *a, **k: _MockResponse(200))

    client = TRClient(waf_token="ok")
    with pytest.raises(SessionExpired):
        client.refresh_session()


def test_refresh_session_retries_once_on_waf(monkeypatch):
    calls = {"n": 0}

    def fake_get(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return _MockResponse(403, "blocked by WAF")
        self.cookies.set("tr_session", "after-waf", domain="api.traderepublic.com")
        return _MockResponse(200)

    monkeypatch.setattr(requests.Session, "get", fake_get)

    client = TRClient(waf_token="stale", on_waf_expired=lambda: "fresh-waf")
    assert client.refresh_session() == "after-waf"
    assert calls["n"] == 2
    assert client.waf_token == "fresh-waf"


# ── cookie-jar persistence ──────────────────────────────────────────────────

def test_cookie_dump_load_roundtrip():
    client = TRClient()
    client._http.cookies.set("tr_session", "abc", domain="api.traderepublic.com", path="/")
    client._http.cookies.set("tr_refresh", "xyz", domain="api.traderepublic.com", path="/")

    dumped = client.dump_cookies()
    names = {c["name"] for c in dumped}
    assert {"tr_session", "tr_refresh"} <= names

    restored = TRClient(session_cookies=dumped)
    assert restored._token_from_jar() == "abc"


def test_connection_state_carries_cookies():
    st = ConnectionState(phone_number="+33600000000", pin="0000")
    assert st.session_cookies == []
    st.session_cookies = [{"name": "tr_session", "value": "v", "domain": "d", "path": "/"}]
    assert st.session_cookies[0]["name"] == "tr_session"


# ── DeviceKeyAuth stub ──────────────────────────────────────────────────────

def test_device_key_auth_is_not_implemented():
    with pytest.raises(NotImplementedError):
        DeviceKeyAuth()


# ── TRSession proactive refresh ─────────────────────────────────────────────

def test_session_do_refresh_adopts_new_token():
    async def refresher():
        return "new-tok"

    sess = TRSession(token="old", session_refresher=refresher)
    assert asyncio.run(sess._do_refresh()) is True
    assert sess._token == "new-tok"


def test_session_do_refresh_without_refresher_is_noop():
    sess = TRSession(token="old")
    assert asyncio.run(sess._do_refresh()) is False
    assert sess._token == "old"


def test_session_do_refresh_propagates_session_expired():
    async def refresher():
        raise SessionExpired("refresh cookie dead")

    sess = TRSession(token="old", session_refresher=refresher)
    with pytest.raises(SessionExpired):
        asyncio.run(sess._do_refresh())
