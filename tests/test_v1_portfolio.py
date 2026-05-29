"""v1 Portfolio facade: EUR valuation, PE held/committed, sold assets, snapshot."""

import asyncio

from traderepublic_sync.v1 import Portfolio


# ── fake low-level client ────────────────────────────────────────────────────

_PAIRS = [
    {"securitiesAccountNumber": "SEC1", "cashAccountNumber": "CASH1",
     "productType": "DEFAULT", "currency": "EUR"},
    {"securitiesAccountNumber": "SEC2", "cashAccountNumber": "CASH2",
     "productType": "TAX_WRAPPER", "currency": "EUR"},
]

_ASSETS = {
    "SEC1": [
        {"isin": "US1", "asset_name": "US Stock", "quantity": 10, "currency": "USD",
         "average_buy_in": 90.0, "current_price": 116.35,
         "instrument_type": "stock", "category": "stocksAndETFs"},
        {"isin": "US2", "asset_name": "US Bond", "quantity": 1000, "currency": "USD",
         "average_buy_in": 0.95, "current_price": 116.35,
         "instrument_type": "bond", "category": "bonds"},
        {"isin": "NOPX", "asset_name": "No Price", "quantity": 3, "currency": "EUR",
         "average_buy_in": 10.0, "current_price": None,
         "instrument_type": "stock", "category": "stocksAndETFs"},
    ],
    "SEC2": [
        {"isin": "IE1", "asset_name": "ETF", "quantity": 5, "currency": "EUR",
         "average_buy_in": 200.0, "current_price": 210.0,
         "instrument_type": "fund", "category": "stocksAndETFs"},
        {"isin": "PE1", "asset_name": "Apollo", "quantity": 2, "currency": "EUR",
         "average_buy_in": 100.0, "current_price": 108.0,
         "instrument_type": "privateFund", "category": "privateMarkets"},
        {"isin": "XF1", "asset_name": "BTC", "quantity": 0.5, "currency": "EUR",
         "average_buy_in": 40000.0, "current_price": 50000.0,
         "instrument_type": "crypto", "category": "cryptos"},
    ],
}

_PM = {
    "SEC2": [
        {"instrumentId": "PE1",
         "pendingAmounts": [{"amount": {"value": 50.0, "currency": "EUR"},
                             "executionDate": "2026-12-01"}]},
    ],
}

_TXS = [
    {"transaction_type": "SELL", "asset_isin": "USSOLD", "asset_name": "Sold Stock",
     "credit_amount": 383.0, "debit_amount": 10},
    {"transaction_type": "PURCHASE", "asset_isin": "USSOLD", "asset_name": "Sold Stock",
     "debit_amount": 300.0, "credit_amount": 10},
    {"transaction_type": "SELL", "asset_isin": "XFSOLD", "asset_name": "Sold Crypto",
     "credit_amount": 1200.0},
    {"transaction_type": "PURCHASE", "asset_isin": "XFSOLD", "asset_name": "Sold Crypto",
     "debit_amount": 1000.0},
]

# taxes/pnl is equities/funds-only; crypto (XFSOLD) returns nothing → timeline.
_PNL = {
    "US1": [{"sec_acc_no": "SEC1", "instrument_id": "US1", "realized_pnl": None,
             "dividend_return": {"value": 4.81, "currency": "EUR"}, "last_updated": None}],
    "USSOLD": [{"sec_acc_no": "SEC1", "instrument_id": "USSOLD",
                "realized_pnl": {"value": 83.0, "currency": "EUR"},
                "dividend_return": None, "last_updated": None}],
}


class FakeClient:
    _session_token = "tok"

    async def fetch_account_pairs(self, token=None):
        return _PAIRS

    async def fetch_account_list(self, token=None):
        return [
            {"account_name": "Trade Republic CTO", "account_type": "BROKERAGE",
             "currency": "EUR", "securities_account_number": "SEC1",
             "cash_account_number": "CASH1"},
            {"account_name": "Trade Republic PEA", "account_type": "BROKERAGE",
             "currency": "EUR", "securities_account_number": "SEC2",
             "cash_account_number": "CASH2"},
        ]

    async def fetch_asset_list(self, token=None, sec_acc_no=None):
        return _ASSETS.get(sec_acc_no, [])

    async def fetch_fx_rates(self, currencies, token=None):
        # Mirror the real client: only USD/GBP/CHF/JPY, EUR skipped.
        out = {}
        for c in currencies:
            if (c or "").upper() == "USD":
                out["USD"] = {"rate": 1.1635, "bid": 1.163, "ask": 1.164}
        return out

    async def fetch_cash_balance(self, token=None, account_number=None):
        amounts = {"CASH1": 1000.0, "CASH2": 500.0}
        return [{"accountNumber": account_number,
                 "amount": amounts.get(account_number), "currencyId": "EUR"}]

    async def fetch_private_markets(self, sec_acc_no, token=None):
        return _PM.get(sec_acc_no, [])

    async def fetch_transactions(self, token=None, since=None, until=None):
        return {"transactions": _TXS, "raw_items": []}

    def fetch_realized_pnl(self, instrument_id, sec_acc_nos=None):
        return _PNL.get(instrument_id, [])


def _pf():
    return Portfolio(FakeClient())


def _by_isin(positions):
    return {p.isin: p for p in positions}


# ── EUR valuation (the fetch_asset_list fix) ─────────────────────────────────


def test_usd_stock_converted_to_eur():
    pos = _by_isin(asyncio.run(_pf().positions()))
    us1 = pos["US1"]
    # 10 × (116.35 / 1) / 1.1635 = 1000.00 ; cost 10 × 90 = 900.
    assert round(us1.value_eur, 2) == 1000.0
    assert round(us1.cost_basis_eur, 2) == 900.0
    assert round(us1.unrealized_pnl_eur, 2) == 100.0
    assert us1.priced is True


def test_bond_divided_by_100_and_fx():
    pos = _by_isin(asyncio.run(_pf().positions()))
    bond = pos["US2"]
    # 1000 × (116.35 / 100) / 1.1635 = 1000.00 ; cost 1000 × 0.95 = 950.
    assert bond.price_scale == 100
    assert round(bond.value_eur, 2) == 1000.0
    assert round(bond.cost_basis_eur, 2) == 950.0


def test_unpriced_falls_back_to_cost():
    pos = _by_isin(asyncio.run(_pf().positions()))
    nopx = pos["NOPX"]
    assert nopx.priced is False
    assert nopx.value_eur == nopx.cost_basis_eur == 30.0


def test_multi_account_aggregation():
    pos = asyncio.run(_pf().positions())
    isins = {p.isin for p in pos}
    assert {"US1", "US2", "NOPX"} <= isins  # SEC1
    assert {"IE1", "PE1", "XF1"} <= isins   # SEC2
    secs = {p.sec_acc_no for p in pos}
    assert secs == {"SEC1", "SEC2"}


# ── PE held vs committed ─────────────────────────────────────────────────────


def test_pe_value_excludes_committed_but_exposes_it():
    pe = _by_isin(asyncio.run(_pf().positions()))["PE1"]
    assert pe.value_eur == 216.0          # 2 × 108 NAV, committed NOT folded in
    assert pe.cost_basis_eur == 200.0
    assert pe.committed_eur == 50.0
    assert len(pe.committed_schedule) == 1
    assert pe.committed_schedule[0].execution_date == "2026-12-01"


def test_snapshot_committed_flag_moves_only_totals():
    pf = _pf()
    off = asyncio.run(pf.snapshot(include_committed=False))
    on = asyncio.run(pf.snapshot(include_committed=True))

    assert round(on.total_value_eur - off.total_value_eur, 2) == 50.0
    assert round(on.total_cost_eur - off.total_cost_eur, 2) == 50.0
    # Unrealized P&L identical: committed nets out (added to both sides).
    assert round(on.total_unrealized_pnl_eur, 2) == round(off.total_unrealized_pnl_eur, 2)
    assert off.total_committed_eur == on.total_committed_eur == 50.0


# ── realized P&L + sold assets ───────────────────────────────────────────────


def test_realized_pnl_totals_mix_tr_and_timeline():
    rp = asyncio.run(_pf().realized_pnl())
    # USSOLD 83 (TR) + XFSOLD 200 (timeline: 1200 − 1000).
    assert round(rp["total_realized_eur"], 2) == 283.0
    assert round(rp["total_dividends_eur"], 2) == 4.81
    by_isin = {e.isin: e for e in rp["instruments"]}
    assert by_isin["XFSOLD"].source == "timeline"
    assert by_isin["USSOLD"].source == "tr"


def test_sold_assets_set_diff_and_fallback():
    sold = {s.isin: s for s in asyncio.run(_pf().sold_assets())}
    assert set(sold) == {"USSOLD", "XFSOLD"}  # held ISINs excluded
    assert sold["USSOLD"].realized_pnl.value == 83.0
    assert sold["USSOLD"].source == "tr"
    assert sold["XFSOLD"].realized_pnl.value == 200.0
    assert sold["XFSOLD"].source == "timeline"
    assert sold["XFSOLD"].name == "Sold Crypto"


# ── snapshot composition ─────────────────────────────────────────────────────


def test_snapshot_totals_and_unpriced_count():
    snap = asyncio.run(_pf().snapshot())
    assert round(snap.total_value_eur, 2) == 28296.0
    assert round(snap.total_cost_eur, 2) == 23080.0
    assert round(snap.total_unrealized_pnl_eur, 2) == 5216.0
    assert round(snap.total_cash_eur, 2) == 1500.0
    assert round(snap.total_realized_pnl_eur, 2) == 283.0
    assert snap.unpriced_count == 1
    assert "USD" in snap.fx_rates
    assert snap.includes_committed is False
