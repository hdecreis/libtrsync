"""Typed models for the :mod:`traderepublic_sync.v1` portfolio facade.

Frozen dataclasses with EUR-normalised, computed figures. The low-level
``TRClient`` returns loosely-typed dicts in the instrument's quote currency;
these models carry the *computed* EUR values (bonds divided by 100 and
FX-converted, ``averageBuyIn`` already EUR) so consumers don't reimplement
that math.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional


@dataclass(frozen=True)
class Money:
    """An amount in a currency."""

    value: float
    currency: str = "EUR"


@dataclass(frozen=True)
class FxRate:
    """EUR conversion rate: ``rate`` = units of ``currency`` per 1 EUR."""

    currency: str
    rate: float
    bid: Optional[float] = None
    ask: Optional[float] = None

    def to_eur(self, amount: float) -> float:
        """Convert an amount in ``self.currency`` to EUR."""
        return amount / self.rate


@dataclass(frozen=True)
class CommittedAmount:
    """One uncalled Private-Markets capital commitment (a future capital call)."""

    amount: Money
    execution_date: Optional[str] = None


@dataclass(frozen=True)
class Account:
    name: str
    type: str
    currency: str
    securities_account_number: Optional[str]
    cash_account_number: Optional[str]


@dataclass(frozen=True)
class CashBalance:
    account_number: Optional[str]
    amount: Optional[float]
    currency: str = "EUR"


@dataclass(frozen=True)
class Quote:
    isin: str
    name: Optional[str]
    exchange_id: Optional[str]
    currency: str
    last: Optional[float]
    prev_close: Optional[float]
    bid: Optional[float] = None
    ask: Optional[float] = None
    open: Optional[float] = None


@dataclass(frozen=True)
class Position:
    """A held position with EUR-normalised value and P&L.

    Held and committed are kept strictly separate: ``value_eur`` /
    ``cost_basis_eur`` / ``unrealized_pnl_eur`` are always the *invested* book
    and are never moved by any flag. ``committed_eur`` /
    ``committed_schedule`` carry Private-Markets uncalled capital (``0.0`` /
    ``[]`` for everything else). Whether committed counts toward a headline
    total is the caller's choice (``snapshot(include_committed=...)``).
    """

    isin: str
    name: str
    sec_acc_no: Optional[str]
    category: Optional[str]
    instrument_type: Optional[str]
    quantity: float
    currency: str
    average_buy_in: Optional[float]   # cost per unit, already EUR
    current_price: Optional[float]    # quote-currency, percent-of-par for bonds
    price_scale: int                  # 100 for bonds, else 1

    value_eur: float                  # HELD only
    cost_basis_eur: float             # HELD only
    unrealized_pnl_eur: float         # value_eur − cost_basis_eur
    priced: bool                      # False → value_eur fell back to cost basis

    committed_eur: float = 0.0
    committed_schedule: list[CommittedAmount] = field(default_factory=list)

    @property
    def unrealized_pnl_pct(self) -> Optional[float]:
        if not self.cost_basis_eur:
            return None
        return self.unrealized_pnl_eur / self.cost_basis_eur * 100


@dataclass(frozen=True)
class RealizedPnl:
    """Server-computed realized P&L + dividend return for one instrument."""

    isin: str
    sec_acc_no: Optional[str]
    realized_pnl: Optional[Money]
    dividend_return: Optional[Money]
    source: Literal["tr", "timeline"] = "tr"
    last_updated: Optional[str] = None


@dataclass(frozen=True)
class SoldAsset:
    """An instrument that's been sold (no longer held), with its realized P&L."""

    isin: str
    name: str
    realized_pnl: Optional[Money]
    dividend_return: Optional[Money]
    source: Literal["tr", "timeline"] = "tr"


@dataclass(frozen=True)
class PortfolioSnapshot:
    accounts: list[Account]
    positions: list[Position]
    cash: list[CashBalance]
    fx_rates: dict[str, FxRate]
    total_value_eur: float
    total_cost_eur: float
    total_unrealized_pnl_eur: float
    total_committed_eur: float
    total_cash_eur: float
    total_realized_pnl_eur: float
    total_dividends_eur: float
    unpriced_count: int
    includes_committed: bool
    fetched_at: str
