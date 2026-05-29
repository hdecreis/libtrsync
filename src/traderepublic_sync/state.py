"""Connection state holder"""

import base64
import binascii
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import parse_qs, unquote


@dataclass
class ConnectionState:
    """Mutable container for one user's Trade Republic session.

    Persist this however you like (pickle, JSON, DB). Pass the relevant
    fields into ``TRClient`` to resume an existing session.
    """

    phone_number: str
    pin: str
    locale: str = "fr"
    waf_token: Optional[str] = None
    waf_expires_at: Optional[str] = None  # ISO-8601 UTC datetime
    device_info: Optional[str] = None
    process_id: Optional[str] = None
    session_token: Optional[str] = None
    auth_status: str = "new"  # new | pending_2fa | authenticated | expired
    # Full HTTP cookie jar captured at verify_2fa time, as a list of
    # {name, value, domain, path} dicts. Holds TR's refresh cookie, which
    # is what lets ``TRClient.refresh_session`` mint a fresh ``session_token``
    # without 2FA. Reuse across processes alongside ``session_token``.
    # Appended last to keep the existing positional field order stable.
    session_cookies: list = field(default_factory=list)

    def is_authenticated(self) -> bool:
        return self.auth_status == "authenticated" and bool(self.session_token)

    def is_waf_valid(self) -> bool:
        """Return True if the WAF token exists and has not yet expired."""
        if not self.waf_token or not self.waf_expires_at:
            return False
        try:
            expires = datetime.fromisoformat(self.waf_expires_at)
            return datetime.now(timezone.utc) < expires
        except ValueError:
            return False

    def is_session_valid(self, skew_seconds: float = 15.0) -> bool:
        """True if the cached ``session_token`` JWT is present and not expiring.

        The twin of :meth:`is_waf_valid` for the ``tr_session`` cookie. The
        token *is* a JWT carrying a short (~5 min) ``exp``; we decode it
        locally — no signature check — purely to decide whether a no-2FA reuse
        is worth attempting before spending a network round-trip.

        Returns ``False`` when there is no token. When the token is present but
        its ``exp`` can't be decoded we return ``True`` ("can't tell locally —
        let the server decide") rather than forcing an unnecessary re-login.
        """
        if not self.session_token:
            return False
        exp = self.session_expiry_from_token(self.session_token)
        if exp is None:
            return True  # undecodable — don't reject a token we merely can't parse
        try:
            expires = datetime.fromisoformat(exp)
        except ValueError:
            return True
        return datetime.now(timezone.utc) < (expires - timedelta(seconds=skew_seconds))

    @staticmethod
    def session_expiry_from_token(token: str | None) -> Optional[str]:
        """Return the ``tr_session`` JWT ``exp`` as an ISO-8601 UTC string.

        ``None`` if the token is missing or its payload can't be decoded. No
        signature verification — this is only for local expiry bookkeeping.
        """
        if not token or token.count(".") < 2:
            return None
        try:
            payload_b64 = token.split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            exp = payload.get("exp")
            if exp is None:
                return None
            return datetime.fromtimestamp(int(exp), tz=timezone.utc).isoformat()
        except (ValueError, binascii.Error, TypeError, json.JSONDecodeError):
            return None

    @staticmethod
    def waf_expiry_from_token(token: str) -> str:
        """Parse X-Amz-Date + X-Amz-Expires from the WAF token string.

        Falls back to now + 900 s if the fields are not present.
        Returns an ISO-8601 UTC datetime string.
        """
        try:
            params = parse_qs(unquote(token))
            date_str = (params.get("X-Amz-Date") or [""])[0]
            expires_s = int((params.get("X-Amz-Expires") or ["900"])[0])
            if date_str:
                issued = datetime.strptime(date_str, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                return (issued + timedelta(seconds=expires_s)).isoformat()
        except Exception:
            pass
        return (datetime.now(timezone.utc) + timedelta(seconds=900)).isoformat()
