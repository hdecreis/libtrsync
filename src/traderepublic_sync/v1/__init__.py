"""Versioned portfolio facade for traderepublic_sync.

Typed, EUR-normalised reads over :class:`~traderepublic_sync.client.TRClient`::

    from traderepublic_sync import TRClient
    from traderepublic_sync.v1 import Portfolio

    client = TRClient(session_token=token)
    snap = await Portfolio(client).snapshot()
    print(snap.total_value_eur, snap.total_realized_pnl_eur)
"""

from . import fx
from .models import (
    Account,
    CashBalance,
    CommittedAmount,
    FxRate,
    Money,
    Position,
    PortfolioSnapshot,
    Quote,
    RealizedPnl,
    SoldAsset,
)
from .portfolio import Portfolio, PortfolioStream

__all__ = [
    "Portfolio",
    "PortfolioStream",
    "fx",
    # models
    "Account",
    "CashBalance",
    "CommittedAmount",
    "FxRate",
    "Money",
    "Position",
    "PortfolioSnapshot",
    "Quote",
    "RealizedPnl",
    "SoldAsset",
]
