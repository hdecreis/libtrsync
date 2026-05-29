"""Session-JWT expiry helpers on ConnectionState (twin of the WAF helpers)."""

import base64
import json
import time

from traderepublic_sync import ConnectionState


def _jwt(exp: int | None) -> str:
    payload = {} if exp is None else {"exp": exp}
    b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{b64}.signature"


def test_no_token_is_invalid():
    assert ConnectionState("p", "x").is_session_valid() is False


def test_live_jwt_is_valid():
    state = ConnectionState("p", "x", session_token=_jwt(int(time.time()) + 600))
    assert state.is_session_valid() is True


def test_expired_jwt_is_invalid():
    state = ConnectionState("p", "x", session_token=_jwt(int(time.time()) - 600))
    assert state.is_session_valid() is False


def test_skew_makes_about_to_expire_invalid():
    # 5 s from now, with a 15 s skew → treated as already invalid.
    state = ConnectionState("p", "x", session_token=_jwt(int(time.time()) + 5))
    assert state.is_session_valid(skew_seconds=15) is False


def test_undecodable_token_is_treated_as_valid():
    # Can't parse locally → don't force a re-login; let the server decide.
    state = ConnectionState("p", "x", session_token="not-a-jwt")
    assert state.is_session_valid() is True


def test_session_expiry_from_token_round_trips():
    exp = int(time.time()) + 300
    iso = ConnectionState.session_expiry_from_token(_jwt(exp))
    assert iso is not None and iso.startswith("20")


def test_session_expiry_from_token_none_for_garbage():
    assert ConnectionState.session_expiry_from_token("garbage") is None
    assert ConnectionState.session_expiry_from_token(None) is None
