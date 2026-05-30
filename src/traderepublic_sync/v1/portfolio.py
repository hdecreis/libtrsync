"""One-time portfolio facade + a live streaming facade.

:class:`Portfolio` wraps a :class:`~traderepublic_sync.client.TRClient` and
returns typed, EUR-normalised models: positions valued correctly (bonds
÷100 + FX, ``averageBuyIn`` already EUR), realized P&L from sales and
dividends, and fully-sold assets. :class:`PortfolioStream` wraps a
:class:`~traderepublic_sync.session.TRSession` for live subscriptions.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from ..client import TRClient, _brokerage_cash_account_number
from . import fx
from .models import (
    Account,
    CashBalance,
    CommittedAmount,
    FxRate,
    InterestEarned,
    Money,
    Position,
    PortfolioSnapshot,
    Quote,
    RealizedPnl,
    SoldAsset,
)

_CAT_BONDS = "bonds"
_CAT_PRIVATE = "privateMarkets"

# Realized-P&L fetch tuning (see ``realized_pnl``). The timeline is consulted
# for two things, each served by a narrow server-side filtered walk instead of
# the full history:
#   1. Discovering instruments with a SELL leg (the "sold-off" set). Sells ride
#      on trade-execution events; ``TRADING_TRADE_EXECUTED`` carries both buys
#      and sells, so this set finds every disposed ISIN across all classes.
#   2. Reconstructing realized P&L for the asset classes TR 404s on its
#      ``taxes/pnl`` REST endpoint (crypto / bonds). Those are pulled *whole*
#      by ``categoryIds`` (cheap — few items) so the timeline cost basis keeps
#      every purchase, including savings-plan ones, that an event-type filter
#      would otherwise drop. Extend if TR starts 404-ing another class.
_REALIZED_TRADE_EVENT_TYPES = [
    "TRADING_TRADE_EXECUTED",
    "PRIVATE_MARKET_FUND_TRADE_EXECUTED",
]
_REALIZED_FALLBACK_CATEGORIES = ["CRYPTO", "BOND"]


# ── cash frame parsing (TR returns list-or-dict; guards the DEFAULT echo) ────


def _cash_frame_account(data) -> str | None:
    """The ``accountNumber`` an availableCash frame reports, if any."""
    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict) and entry.get("accountNumber"):
                return entry["accountNumber"]
        return None
    if isinstance(data, dict):
        return data.get("accountNumber")
    return None


def _extract_cash(data, default_currency: str) -> tuple[float | None, str]:
    """Pull ``(amount, currency)`` out of an availableCash frame (list or dict)."""

    def _amount(entry: dict) -> float | None:
        v = entry.get("amount")
        if v is None:
            v = entry.get("value")
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    if isinstance(data, list):
        chosen = None
        for entry in data:
            if not isinstance(entry, dict):
                continue
            cur = entry.get("currencyId") or entry.get("currency")
            if cur == default_currency:
                chosen = entry
                break
        if chosen is None:
            chosen = next((e for e in data if isinstance(e, dict)), None)
        if chosen is None:
            return None, default_currency
        currency = (
            chosen.get("currencyId") or chosen.get("currency") or default_currency
        )
        return _amount(chosen), currency
    if isinstance(data, dict):
        currency = data.get("currencyId") or data.get("currency") or default_currency
        return _amount(data), currency
    return None, default_currency


def _price_scale(asset: dict) -> int:
    """100 for bonds (percent-of-par price), 1 otherwise."""
    if asset.get("instrument_type") == "bond" or asset.get("category") == _CAT_BONDS:
        return 100
    return 1


class Portfolio:
    """One-time portfolio reads over a :class:`TRClient`.

    All methods are async (the underlying data is WebSocket-sourced; realized
    P&L is REST and runs off-loop). Construct via the client::

        client = TRClient(session_token=token)
        pf = Portfolio(client)
        snap = await pf.snapshot()
    """

    def __init__(self, client: TRClient, session_token: str | None = None) -> None:
        self._client = client
        self._token = session_token or client._session_token
        self._pairs: list[dict] | None = None

    # ── account plumbing ──────────────────────────────────────────────────

    async def _account_pairs(self) -> list[dict]:
        if self._pairs is None:
            self._pairs = await self._client.fetch_account_pairs(self._token)
        return self._pairs

    def _sec_acc_nos(self, pairs: list[dict]) -> list[str]:
        return [
            p["securitiesAccountNumber"]
            for p in pairs
            if p.get("securitiesAccountNumber")
        ]

    async def accounts(self) -> list[Account]:
        raw = await self._client.fetch_account_list(self._token)
        return [
            Account(
                name=a["account_name"],
                type=a["account_type"],
                currency=a["currency"],
                securities_account_number=a["securities_account_number"],
                cash_account_number=a["cash_account_number"],
            )
            for a in raw
        ]

    async def cash(self) -> list[CashBalance]:
        """Available cash per account, scoped by ``accountNumber``."""
        pairs = await self._account_pairs()
        seen: set[str] = set()
        out: list[CashBalance] = []
        for pair in pairs:
            cash_no = pair.get("cashAccountNumber")
            if not cash_no or cash_no in seen:
                continue
            seen.add(cash_no)
            currency = pair.get("currency", "EUR")
            data = await self._client.fetch_cash_balance(
                self._token, account_number=cash_no
            )
            # Guard TR's DEFAULT-scoping quirk: if the frame reports a
            # different account, the filter was ignored — don't trust it.
            got = _cash_frame_account(data)
            if got and got != cash_no:
                out.append(
                    CashBalance(account_number=cash_no, amount=None, currency=currency)
                )
                continue
            amount, ccy = _extract_cash(data, currency)
            out.append(CashBalance(account_number=cash_no, amount=amount, currency=ccy))
        return out

    async def quote(self, isin: str) -> Quote:
        q = await self._client.fetch_ticker(isin, self._token)
        return Quote(
            isin=q["isin"],
            name=q.get("asset_name"),
            exchange_id=q.get("exchange_id"),
            currency=q.get("currency") or "EUR",
            last=q.get("current_price"),
            prev_close=q.get("previous_close"),
            bid=q.get("bid"),
            ask=q.get("ask"),
            open=q.get("open"),
        )

    async def fx_rates(self, currencies) -> dict[str, FxRate]:
        raw = await self._client.fetch_fx_rates(list(currencies), self._token)
        return fx.rates_from_raw(raw)

    async def transactions(
        self, since=None, until=None, *, event_types=None, categories=None
    ) -> list[dict]:
        result = await self._client.fetch_transactions(
            self._token,
            since=since,
            until=until,
            event_types=event_types,
            categories=categories,
        )
        return result["transactions"]

    # ── positions (EUR-correct, held vs committed) ────────────────────────

    async def positions(self) -> list[Position]:
        positions, _ = await self._positions_with_rates()
        return positions

    async def _positions_with_rates(self) -> tuple[list[Position], dict[str, FxRate]]:
        pairs = await self._account_pairs()
        sec_accs = self._sec_acc_nos(pairs)

        raw_by_acc: list[tuple[str, list[dict]]] = []
        for sec in sec_accs:
            assets = await self._client.fetch_asset_list(self._token, sec_acc_no=sec)
            raw_by_acc.append((sec, assets))

        # Resolve FX for every foreign currency seen across positions.
        currencies = {
            a.get("currency")
            for _sec, assets in raw_by_acc
            for a in assets
            if a.get("currency")
        }
        rates = await self.fx_rates(currencies)

        # Private-Markets pending (committed) capital, per account that has PE.
        pending_by_isin: dict[str, list[CommittedAmount]] = {}
        for sec, assets in raw_by_acc:
            if not any(a.get("category") == _CAT_PRIVATE for a in assets):
                continue
            pm = await self._client.fetch_private_markets(sec, self._token)
            for pos in pm:
                isin = pos.get("instrumentId") or pos.get("isin")
                if not isin:
                    continue
                schedule = []
                for pa in pos.get("pendingAmounts") or []:
                    amt = pa.get("amount") or {}
                    val = amt.get("value")
                    try:
                        val = float(val) if val is not None else None
                    except (TypeError, ValueError):
                        val = None
                    if val is None:
                        continue
                    schedule.append(
                        CommittedAmount(
                            amount=Money(val, amt.get("currency") or "EUR"),
                            execution_date=pa.get("executionDate"),
                        )
                    )
                if schedule:
                    pending_by_isin[isin] = schedule

        positions: list[Position] = []
        for sec, assets in raw_by_acc:
            for a in assets:
                positions.append(self._build_position(a, sec, rates, pending_by_isin))
        return positions, rates

    @staticmethod
    def _build_position(
        asset: dict,
        sec_acc_no: str,
        rates: dict[str, FxRate],
        pending_by_isin: dict[str, list[CommittedAmount]],
    ) -> Position:
        isin = asset.get("isin") or asset.get("instrumentId") or ""
        quantity = asset.get("quantity") or 0.0
        currency = asset.get("currency") or "EUR"
        avg = asset.get("average_buy_in")
        current_price = asset.get("current_price")
        scale = _price_scale(asset)

        cost_basis_eur = quantity * (avg or 0.0)  # averageBuyIn already EUR

        px_eur = None
        if current_price is not None:
            px_eur = fx.convert_to_eur(current_price / scale, currency, rates)
        if px_eur is None:
            priced = False
            value_eur = cost_basis_eur  # fall back to cost basis
        else:
            priced = True
            value_eur = quantity * px_eur

        schedule = pending_by_isin.get(isin, [])
        committed_eur = 0.0
        for c in schedule:
            conv = fx.convert_to_eur(c.amount.value, c.amount.currency, rates)
            committed_eur += conv if conv is not None else c.amount.value

        return Position(
            isin=isin,
            name=asset.get("asset_name") or isin,
            sec_acc_no=sec_acc_no,
            category=asset.get("category"),
            instrument_type=asset.get("instrument_type"),
            quantity=quantity,
            currency=currency,
            average_buy_in=avg,
            current_price=current_price,
            price_scale=scale,
            value_eur=value_eur,
            cost_basis_eur=cost_basis_eur,
            unrealized_pnl_eur=value_eur - cost_basis_eur,
            priced=priced,
            committed_eur=committed_eur,
            committed_schedule=schedule,
        )

    # ── realized P&L + sold assets ────────────────────────────────────────

    async def realized_pnl(self, positions: list[Position] | None = None) -> dict:
        """Realized P&L + dividends across held and previously-sold instruments.

        Returns ``{"total_realized_eur", "total_dividends_eur", "instruments":
        list[RealizedPnl]}``. Uses TR's server figure (``taxes/pnl``) where
        available (equities/funds) and a timeline fallback for crypto/bonds
        (which TR 404s).

        Rather than walking the entire timeline, this issues two narrow
        server-side filtered fetches (see ``_REALIZED_*`` module constants):
        a trade-event walk to find every sold-off instrument, and a
        ``categoryIds`` walk over the 404-prone classes to reconstruct their
        P&L with a complete cost basis.

        ``positions`` is only used for the held-ISIN set; pass an already
        fetched list (as ``snapshot`` does) to avoid re-running the position
        walk.
        """
        pairs = await self._account_pairs()
        sec_accs = self._sec_acc_nos(pairs)
        if positions is None:
            positions = await self.positions()

        # (1) Trade events → the set of instruments that were (partly) sold.
        trade_txs = await self.transactions(event_types=_REALIZED_TRADE_EVENT_TYPES)
        # (2) Classes TR 404s on taxes/pnl, fetched whole → accurate fallback.
        fallback_txs = await self.transactions(categories=_REALIZED_FALLBACK_CATEGORIES)

        held = {p.isin for p in positions if p.isin}
        # Sold-off detection unions both walks (a crypto sell may surface only
        # in the category walk); membership-only, so duplicates are harmless.
        sold_index = _index_transactions(trade_txs + fallback_txs)
        sold = {isin for isin, agg in sold_index.items() if agg["sells"]}
        # The timeline fallback reads cost basis only from the category walk,
        # which holds the complete (deduplicated) history for those classes.
        fallback_index = _index_transactions(fallback_txs)

        entries: list[RealizedPnl] = []
        for isin in sorted(held | sold):
            entries.append(await self._realized_for(isin, sec_accs, fallback_index))

        total_realized = sum(e.realized_pnl.value for e in entries if e.realized_pnl)
        total_dividends = sum(
            e.dividend_return.value for e in entries if e.dividend_return
        )
        return {
            "total_realized_eur": total_realized,
            "total_dividends_eur": total_dividends,
            "instruments": entries,
        }

    async def _realized_for(
        self, isin: str, sec_accs: list[str], tx_index: dict
    ) -> RealizedPnl:
        rows = await asyncio.to_thread(self._client.fetch_realized_pnl, isin, sec_accs)
        if rows:
            # Sum across securities accounts into one figure for the instrument.
            realized = _sum_money([r["realized_pnl"] for r in rows])
            dividend = _sum_money([r["dividend_return"] for r in rows])
            return RealizedPnl(
                isin=isin,
                sec_acc_no=rows[0].get("sec_acc_no"),
                realized_pnl=realized,
                dividend_return=dividend,
                source="tr",
                last_updated=rows[0].get("last_updated"),
            )
        # 404 (crypto/bonds/savings) → reconstruct from the timeline.
        realized, dividend = _timeline_realized(tx_index.get(isin))
        return RealizedPnl(
            isin=isin,
            sec_acc_no=None,
            realized_pnl=realized,
            dividend_return=dividend,
            source="timeline",
        )

    async def sold_assets(self) -> list[SoldAsset]:
        """Instruments with a SELL leg that are no longer held.

        Uses the same two narrow filtered walks as ``realized_pnl`` (trade
        events for sold-off detection, ``categoryIds`` for the 404-prone
        classes' cost basis) instead of the full timeline.
        """
        pairs = await self._account_pairs()
        sec_accs = self._sec_acc_nos(pairs)
        positions = await self.positions()

        trade_txs = await self.transactions(event_types=_REALIZED_TRADE_EVENT_TYPES)
        fallback_txs = await self.transactions(categories=_REALIZED_FALLBACK_CATEGORIES)

        held = {p.isin for p in positions if p.isin}
        sold_index = _index_transactions(trade_txs + fallback_txs)
        names = _isin_names(trade_txs + fallback_txs)
        fallback_index = _index_transactions(fallback_txs)
        fully_sold = sorted(
            isin
            for isin, agg in sold_index.items()
            if agg["sells"] and isin not in held
        )

        out: list[SoldAsset] = []
        for isin in fully_sold:
            r = await self._realized_for(isin, sec_accs, fallback_index)
            out.append(
                SoldAsset(
                    isin=isin,
                    name=names.get(isin, isin),
                    realized_pnl=r.realized_pnl,
                    dividend_return=r.dividend_return,
                    source=r.source,
                )
            )
        return out

    # ── cash interest ─────────────────────────────────────────────────────

    async def interest(self) -> InterestEarned:
        """Cash interest on the brokerage account (lifetime + pending).

        Resolves the ``DEFAULT`` (CTO) pair's ``cashAccountNumber`` and calls
        TR's interest details endpoint (REST, run off-loop). Returns an
        :class:`InterestEarned` with ``None`` fields when interest isn't
        activated or no brokerage cash account exists. ``earned`` is the
        lifetime "Total Earned"; ``pending`` is accrued-but-unpaid interest.
        """
        pairs = await self._account_pairs()
        core = _brokerage_cash_account_number(pairs)
        if not core:
            return InterestEarned(earned=None, pending=None)
        raw = await asyncio.to_thread(self._client.interest_summary, core)
        return _interest_from_raw(raw)

    # ── snapshot ──────────────────────────────────────────────────────────

    async def snapshot(self, include_committed: bool = False) -> PortfolioSnapshot:
        """Compose a full portfolio snapshot.

        ``include_committed=False`` (default): headline totals are the invested
        book only. ``True``: PE uncalled capital (``committed_eur``) is added to
        *both* value and cost, so it shows in the value total but nets to 0 in
        unrealized P&L. ``total_committed_eur`` is always reported separately.
        """
        accounts = await self.accounts()
        positions, rates = await self._positions_with_rates()
        cash = await self.cash()
        # Reuse the positions just fetched — realized_pnl only needs them for
        # the held-ISIN set, so this avoids a second full position walk.
        realized = await self.realized_pnl(positions=positions)
        interest = await self.interest()

        total_value = sum(p.value_eur for p in positions)
        total_cost = sum(p.cost_basis_eur for p in positions)
        total_committed = sum(p.committed_eur for p in positions)
        if include_committed:
            total_value += total_committed
            total_cost += total_committed

        total_cash = 0.0
        for c in cash:
            if c.amount is None:
                continue
            conv = fx.convert_to_eur(c.amount, c.currency, rates)
            total_cash += conv if conv is not None else c.amount

        # Lifetime cash interest counts as realized income; pending does not.
        total_realized = realized["total_realized_eur"]
        if interest.earned is not None:
            total_realized += interest.earned

        return PortfolioSnapshot(
            accounts=accounts,
            positions=positions,
            cash=cash,
            fx_rates=rates,
            total_value_eur=total_value,
            total_cost_eur=total_cost,
            total_unrealized_pnl_eur=total_value - total_cost,
            total_committed_eur=total_committed,
            total_cash_eur=total_cash,
            total_realized_pnl_eur=total_realized,
            total_dividends_eur=realized["total_dividends_eur"],
            unpriced_count=sum(1 for p in positions if not p.priced),
            includes_committed=include_committed,
            fetched_at=datetime.now(timezone.utc).isoformat(),
            interest=interest,
        )

    # ── live streaming ────────────────────────────────────────────────────

    @asynccontextmanager
    async def stream(self, **open_session_kwargs):
        """Async context manager yielding a :class:`PortfolioStream`."""
        async with self._client.open_session(
            self._token, **open_session_kwargs
        ) as session:
            yield PortfolioStream(session)


class PortfolioStream:
    """Thin typed wrapper over :class:`TRSession` live subscriptions."""

    def __init__(self, session) -> None:
        self._session = session

    async def prices(self, isin: str, callback) -> int:
        return await self._session.subscribe_ticker(isin, callback)

    async def positions(self, sec_acc_no: str, callback) -> int:
        return await self._session.subscribe_portfolio(sec_acc_no, callback)

    async def cash(self, cash_acc_no: str, callback) -> int:
        return await self._session.subscribe_cash(cash_acc_no, callback)

    async def transactions(self, cash_acc_no: str, callback) -> int:
        return await self._session.subscribe_transactions(cash_acc_no, callback)

    async def fx(self, currency: str, callback) -> int:
        return await self._session.subscribe_fx(currency, callback)

    async def unsubscribe(self, sub_id: int) -> None:
        await self._session.unsubscribe(sub_id)


# ── transaction-ledger helpers (timeline fallback) ──────────────────────────


def _index_transactions(txs: list[dict]) -> dict[str, dict]:
    """Group dual-legged transactions by ISIN into sells/purchases/dividends."""
    index: dict[str, dict] = {}
    for tx in txs:
        isin = tx.get("asset_isin")
        if not isin:
            continue
        agg = index.setdefault(isin, {"sells": [], "purchases": [], "dividends": []})
        ttype = tx.get("transaction_type")
        if ttype == "SELL":
            agg["sells"].append(tx)
        elif ttype == "PURCHASE":
            agg["purchases"].append(tx)
        elif ttype == "DIVIDEND":
            agg["dividends"].append(tx)
    return index


def _timeline_realized(agg: dict | None) -> tuple[Money | None, Money | None]:
    """Approximate realized P&L + dividends for one ISIN from the timeline.

    Realized = Σ SELL proceeds − Σ PURCHASE cost. Exact for a fully-sold
    instrument; an approximation while partially held (no lot matching). Only
    returned when there's at least one sell. Dividends = Σ DIVIDEND credits.
    """
    if not agg:
        return None, None
    realized = None
    if agg["sells"]:
        proceeds = sum(_num(t.get("credit_amount")) for t in agg["sells"])
        cost = sum(_num(t.get("debit_amount")) for t in agg["purchases"])
        realized = Money(round(proceeds - cost, 2), "EUR")
    dividend = None
    if agg["dividends"]:
        total = sum(_num(t.get("credit_amount")) for t in agg["dividends"])
        dividend = Money(round(total, 2), "EUR")
    return realized, dividend


def _isin_names(txs: list[dict]) -> dict[str, str]:
    names: dict[str, str] = {}
    for tx in txs:
        isin = tx.get("asset_isin")
        name = tx.get("asset_name")
        if isin and name and isin not in names:
            names[isin] = name
    return names


def _sum_money(items: list[dict | None]) -> Money | None:
    """Sum a list of ``{value, currency}`` legs (skipping None) into one Money."""
    present = [m for m in items if m]
    if not present:
        return None
    currency = present[0].get("currency") or "EUR"
    return Money(round(sum(m["value"] for m in present), 2), currency)


def _num(value) -> float:
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _money_value(node) -> float | None:
    """Pull the float ``value`` out of a TR ``{value, currency}`` MoneyAmount."""
    if not isinstance(node, dict):
        return None
    value = node.get("value")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _interest_from_raw(raw: dict | None) -> InterestEarned:
    """Shape TR's interest ``details-data`` payload into an :class:`InterestEarned`."""
    if not isinstance(raw, dict):
        return InterestEarned(earned=None, pending=None)
    earned_node = raw.get("interestEarned")
    pending_node = raw.get("pendingInterestEarned")
    currency = "EUR"
    for node in (earned_node, pending_node):
        if isinstance(node, dict) and node.get("currency"):
            currency = node["currency"]
            break
    return InterestEarned(
        earned=_money_value(earned_node),
        pending=_money_value(pending_node),
        currency=currency,
    )
