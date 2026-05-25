"""traderepublic_sync — unofficial Python client for Trade Republic."""

from .client import TRClient
from .session import TRSession
from .constants import DEFAULT_HEADERS, TR_API_BASE, TR_WS_URL, WS_CONNECT_PAYLOAD
from .exceptions import TRAuthError
from .parsing import (
    extract_isin_from_icon,
    normalize_tr_id,
    parse_detail_sections,
    parse_currency_amount,
)
from .state import ConnectionState
from .waf import get_waf_token

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # Main client
    "TRClient",
    "TRSession",
    # State
    "ConnectionState",
    # WAF
    "get_waf_token",
    # Parsing utilities
    "parse_currency_amount",
    "parse_detail_sections",
    "extract_isin_from_icon",
    "normalize_tr_id",
    # Constants
    "TR_API_BASE",
    "TR_WS_URL",
    "DEFAULT_HEADERS",
    "WS_CONNECT_PAYLOAD",
    # Exceptions
    "TRAuthError",
]
