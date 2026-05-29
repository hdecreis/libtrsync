"""EUR conversion helpers for the v1 facade.

A thin, dependency-free layer over the rates :meth:`TRClient.fetch_fx_rates`
returns. The surface mirrors trdump's ``fx.py`` (``rate(ccy) -> foreign per
EUR | None``, ``EUR -> 1.0``) so it's a near drop-in — but the rates come from
TR's own LSX ``ticker`` mid rather than the ECB daily feed.
"""

from __future__ import annotations

from ..client import fx_mid  # re-export
from ..constants import FX_INSTRUMENTS  # re-export
from .models import FxRate

__all__ = ["FX_INSTRUMENTS", "fx_mid", "rate", "convert_to_eur", "rates_from_raw"]


def rate(currency: str | None, rates: dict[str, FxRate] | dict[str, float] | None) -> float | None:
    """Units of ``currency`` per 1 EUR, or ``None`` if unavailable.

    ``EUR`` is always ``1.0``. ``rates`` may be the ``{CCY: FxRate}`` map from
    :meth:`Portfolio.fx_rates` or a plain ``{CCY: float}`` map. An unknown
    currency returns ``None`` so callers can fall back (e.g. to cost basis)
    rather than guess.
    """
    if not currency:
        return None
    cur = currency.upper()
    if cur == "EUR":
        return 1.0
    if not rates:
        return None
    entry = rates.get(cur)
    if entry is None:
        return None
    return entry.rate if isinstance(entry, FxRate) else float(entry)


def convert_to_eur(
    amount: float | None,
    currency: str | None,
    rates: dict[str, FxRate] | dict[str, float] | None,
) -> float | None:
    """Convert ``amount`` in ``currency`` to EUR, or ``None`` if not possible."""
    if amount is None:
        return None
    r = rate(currency, rates)
    if r is None or r == 0:
        return None
    return amount / r


def rates_from_raw(raw: dict[str, dict]) -> dict[str, FxRate]:
    """Build a ``{CCY: FxRate}`` map from :meth:`TRClient.fetch_fx_rates` output."""
    out: dict[str, FxRate] = {}
    for cur, entry in (raw or {}).items():
        out[cur.upper()] = FxRate(
            currency=cur.upper(),
            rate=entry["rate"],
            bid=entry.get("bid"),
            ask=entry.get("ask"),
        )
    return out
