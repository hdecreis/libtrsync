"""traderepublic_sync — unofficial Python client for Trade Republic."""

import logging
from importlib.metadata import PackageNotFoundError, version as _pkg_version

from .client import TRClient
from .session import TRSession
from .constants import DEFAULT_HEADERS, TR_API_BASE, TR_WS_URL, WS_CONNECT_PAYLOAD
from .exceptions import (
    SessionExpired,
    TRAuthError,
    TRError,
    TransientError,
    WafExpired,
)
from .parsing import (
    extract_isin_from_icon,
    normalize_tr_id,
    parse_detail_sections,
    parse_currency_amount,
)
from .state import ConnectionState
from .waf import get_waf_token

# Library convention: attach a NullHandler so consumers that haven't
# configured logging don't see "No handler found" warnings.
logging.getLogger(__name__).addHandler(logging.NullHandler())

try:
    __version__ = _pkg_version("traderepublic-sync")
except PackageNotFoundError:
    # Package not installed (e.g. running from a source checkout without `pip install`).
    __version__ = "0.0.0+unknown"

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
    "TRError",
    "TRAuthError",
    "WafExpired",
    "SessionExpired",
    "TransientError",
]
