"""Connection state holder"""

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
