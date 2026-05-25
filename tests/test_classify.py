"""Unit tests for the typed-exception classifier and the WAF refresh hook.

These cover the auth state machine logic — that the right exception class
is raised for each kind of failure, and that ``on_waf_expired`` is called
on WafExpired and the operation is retried exactly once.
"""

from types import SimpleNamespace

import pytest

from traderepublic_sync._classify import (
    classify_http,
    classify_ws_connect_error,
    classify_ws_error_frame,
)
from traderepublic_sync import (
    SessionExpired,
    TRAuthError,
    TRClient,
    TRError,
    TransientError,
    WafExpired,
)


def _resp(status: int, body: str = ""):
    return SimpleNamespace(status_code=status, text=body)


# ── classify_http ───────────────────────────────────────────────────────────

def test_classify_http_2xx_is_noop():
    classify_http(_resp(200, "{}"))  # does not raise


def test_classify_http_403_default_is_waf():
    with pytest.raises(WafExpired):
        classify_http(_resp(403, "Request blocked by WAF"))


def test_classify_http_403_with_session_hint_is_session():
    with pytest.raises(SessionExpired):
        classify_http(_resp(403, "session has expired"))


def test_classify_http_401_default_is_session():
    with pytest.raises(SessionExpired):
        classify_http(_resp(401, "unauthorized"))


def test_classify_http_401_with_waf_hint_is_waf():
    with pytest.raises(WafExpired):
        classify_http(_resp(401, "x-aws-waf-token missing"))


def test_classify_http_500_is_transient():
    with pytest.raises(TransientError):
        classify_http(_resp(503, "service unavailable"))


def test_classify_http_429_is_transient():
    with pytest.raises(TransientError):
        classify_http(_resp(429, "slow down"))


def test_classify_http_400_is_generic_auth_error():
    # 400 / 422 typically mean bad PIN or wrong 2FA code — not WAF/session.
    with pytest.raises(TRAuthError) as excinfo:
        classify_http(_resp(400, "wrong PIN"))
    # Should be plain TRAuthError, not one of the lifecycle subclasses.
    assert not isinstance(excinfo.value, WafExpired)
    assert not isinstance(excinfo.value, SessionExpired)


def test_classify_http_context_in_message():
    with pytest.raises(WafExpired, match="login HTTP 403"):
        classify_http(_resp(403, "blocked"), context="login")


# ── classify_ws_connect_error ───────────────────────────────────────────────

def test_classify_ws_403_handshake_is_waf():
    exc = SimpleNamespace(status_code=403)
    typed = classify_ws_connect_error(exc)
    assert isinstance(typed, WafExpired)


def test_classify_ws_401_handshake_is_session():
    exc = SimpleNamespace(status_code=401)
    typed = classify_ws_connect_error(exc)
    assert isinstance(typed, SessionExpired)


def test_classify_ws_other_failure_is_transient():
    exc = OSError("connection refused")
    typed = classify_ws_connect_error(exc)
    assert isinstance(typed, TransientError)


# ── classify_ws_error_frame ─────────────────────────────────────────────────

def test_classify_ws_error_frame_session_expired():
    typed = classify_ws_error_frame('{"errorMessage": "session expired"}')
    assert isinstance(typed, SessionExpired)


def test_classify_ws_error_frame_unauthorized():
    typed = classify_ws_error_frame('{"errorMessage": "UNAUTHORIZED"}')
    assert isinstance(typed, SessionExpired)


def test_classify_ws_error_frame_unrecognized_returns_none():
    assert classify_ws_error_frame('{"errorMessage": "rate limit"}') is None


# ── Exception hierarchy ─────────────────────────────────────────────────────

def test_typed_exceptions_inherit_from_TRError():
    assert issubclass(WafExpired, TRError)
    assert issubclass(SessionExpired, TRError)
    assert issubclass(TransientError, TRError)
    assert issubclass(TRAuthError, TRError)


def test_waf_and_session_are_subclasses_of_TRAuthError():
    # Lets consumers keep their existing `except TRAuthError` while still
    # being able to handle the new subclasses where they care to.
    assert issubclass(WafExpired, TRAuthError)
    assert issubclass(SessionExpired, TRAuthError)


# ── WAF refresh hook on TRClient ────────────────────────────────────────────

class _MockResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, status_code, body=""):
        self.status_code = status_code
        self.text = body
        self.headers = {}

    def json(self):
        return {"processId": "abc-123", "countdownInSeconds": 60}


def test_login_retries_once_on_waf_expired(monkeypatch):
    """First call gets 403 (WAF), hook returns new token, retry succeeds."""
    calls = {"login": 0, "refresh": 0}

    def fake_post(*args, **kwargs):
        calls["login"] += 1
        # First call: WAF rejected. Second call: success.
        if calls["login"] == 1:
            return _MockResponse(403, "blocked by WAF")
        return _MockResponse(200)

    def on_waf_expired():
        calls["refresh"] += 1
        return "fresh-waf-token"

    import requests as _requests
    monkeypatch.setattr(_requests, "post", fake_post)

    client = TRClient(waf_token="stale", on_waf_expired=on_waf_expired)
    result = client.login("+33612345678", "1234")

    assert result["process_id"] == "abc-123"
    assert calls["login"] == 2
    assert calls["refresh"] == 1
    assert client.waf_token == "fresh-waf-token"


def test_login_raises_wafexpired_when_no_hook(monkeypatch):
    """Without a hook, WafExpired propagates unchanged — no silent retry."""
    def fake_post(*args, **kwargs):
        return _MockResponse(403, "blocked by WAF")

    import requests as _requests
    monkeypatch.setattr(_requests, "post", fake_post)

    client = TRClient(waf_token="stale")
    with pytest.raises(WafExpired):
        client.login("+33612345678", "1234")


def test_login_does_not_retry_on_session_or_other(monkeypatch):
    """Non-WAF errors do NOT call the hook and are NOT retried."""
    calls = {"login": 0, "refresh": 0}

    def fake_post(*args, **kwargs):
        calls["login"] += 1
        return _MockResponse(400, "bad PIN")

    def on_waf_expired():
        calls["refresh"] += 1
        return "should-not-be-used"

    import requests as _requests
    monkeypatch.setattr(_requests, "post", fake_post)

    client = TRClient(waf_token="ok", on_waf_expired=on_waf_expired)
    with pytest.raises(TRAuthError):
        client.login("+33612345678", "1234")

    assert calls["login"] == 1
    assert calls["refresh"] == 0


def test_login_refresh_hook_returning_none_does_not_retry(monkeypatch):
    """Hook that returns None and doesn't mutate token → re-raise."""
    calls = {"login": 0}

    def fake_post(*args, **kwargs):
        calls["login"] += 1
        return _MockResponse(403, "blocked by WAF")

    def on_waf_expired():
        return None  # acquisition failed

    import requests as _requests
    monkeypatch.setattr(_requests, "post", fake_post)

    client = TRClient(waf_token="", on_waf_expired=on_waf_expired)
    with pytest.raises(WafExpired):
        client.login("+33612345678", "1234")

    assert calls["login"] == 1


def test_login_async_hook_on_sync_path_raises_clearly(monkeypatch):
    """A coroutine-returning hook used on the sync REST path must error out
    with a clear message, not deadlock or silently succeed."""
    async def async_hook():
        return "tok"

    def fake_post(*args, **kwargs):
        return _MockResponse(403, "blocked by WAF")

    import requests as _requests
    monkeypatch.setattr(_requests, "post", fake_post)

    client = TRClient(waf_token="stale", on_waf_expired=async_hook)
    with pytest.raises(TRError, match="coroutine"):
        client.login("+33612345678", "1234")


# ── E-frame detection in _parse_ws_json ─────────────────────────────────────

def test_parse_ws_json_raises_on_session_error_frame():
    from traderepublic_sync.client import _parse_ws_json

    with pytest.raises(SessionExpired):
        _parse_ws_json('1 E {"errorMessage": "session expired"}')


def test_parse_ws_json_raises_transient_on_unknown_error_frame():
    from traderepublic_sync.client import _parse_ws_json

    with pytest.raises(TransientError):
        _parse_ws_json('1 E {"errorMessage": "internal"}')


def test_parse_ws_json_normal_frame_still_parses():
    from traderepublic_sync.client import _parse_ws_json

    assert _parse_ws_json('1 A {"foo": "bar"}') == {"foo": "bar"}
