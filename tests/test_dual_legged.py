"""Fixture-based tests for build_dual_legged_transaction.

These tests exercise the mapping layer that turns a raw TR timeline item +
parsed detail into a double-entry transaction dict. The shape of the
output depends on transaction_type, so each test focuses on the fields
that matter for that specific type.
"""

import json
from pathlib import Path

import pytest

from traderepublic_sync import parse_detail_sections
from traderepublic_sync.dual_legged import (
    EVENT_TYPE_MAP,
    build_dual_legged_transaction,
    deduplicate_pea,
    determine_account,
    determine_tx_type,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load_item(name: str) -> dict:
    """Return the full raw timeline item (with _detail_raw attached)."""
    return json.loads((FIXTURES / f"{name}.json").read_text())


def build(name: str) -> dict:
    """Convenience: load + parse + build, return the dual-legged tx."""
    item = load_item(name)
    parsed = parse_detail_sections(item["_detail_raw"])
    return build_dual_legged_transaction(item, parsed)


# ── Per-type transaction shape ──────────────────────────────────────────────

def test_purchase_savingsplan_legs():
    tx = build("purchase_savingsplan")
    assert tx["transaction_type"] == "PURCHASE"
    assert tx["credit_asset_code"] == "IE00BG0SKF03"      # ISIN bought
    assert tx["credit_amount"] == pytest.approx(0.305922)  # quantity
    assert tx["debit_asset_code"] == "EUR"                  # cash spent
    assert tx["debit_amount"] == pytest.approx(25.0)
    assert tx["unit_price"] == pytest.approx(81.72)
    assert tx["asset_name"] == "Edge MSCI EM Value USD (Acc)"


def test_trade_legs_with_fees_and_taxes():
    tx = build("trade")
    assert tx["transaction_type"] == "PURCHASE"
    assert tx["credit_asset_code"] == "FR0000052292"  # ISIN from icon
    assert tx["credit_amount"] == pytest.approx(1.0)
    assert tx["debit_asset_code"] == "EUR"
    assert tx["debit_amount"] == pytest.approx(1603.38)  # includes fees+taxes
    assert tx["fee_amount"] == pytest.approx(1.0)
    assert tx["tax_amount"] == pytest.approx(6.38)
    # PEA portfolio detection
    assert tx["account_name"] == "Trade Republic PEA"
    assert tx["account_type"] == "BROKERAGE"


def test_dividend_legs():
    tx = build("dividend")
    assert tx["transaction_type"] == "DIVIDEND"
    assert tx["credit_asset_code"] == "EUR"               # cash received
    assert tx["credit_amount"] == pytest.approx(7.4)
    assert tx["reference_asset_code"] == "FR0000120073"   # asset that paid
    assert tx["reference_asset_name"] == "Air Liquide"
    assert tx["quantity"] == pytest.approx(2.0)
    assert tx["dividend_per_share"] == pytest.approx(3.7)
    assert tx["dividend_currency"] == "EUR"
    # 0,00 taxes: parser sets fees=None and taxes=0 — tx mapping treats
    # taxes>0 as the gating condition, so tax_amount should NOT be set
    assert "tax_amount" not in tx


def test_interest_legs():
    tx = build("interest")
    assert tx["transaction_type"] == "INTEREST"
    assert tx["credit_asset_code"] == "EUR"
    assert tx["credit_amount"] == pytest.approx(0.62)
    # Tax leg present (interest is taxable)
    assert tx["tax_amount"] == pytest.approx(0.29)


def test_transfer_in_legs():
    tx = build("transfer_in")
    assert tx["transaction_type"] == "TRANSFER"
    # Incoming: credit side populated, no debit
    assert tx["credit_asset_code"] == "EUR"
    assert tx["credit_amount"] == pytest.approx(200.0)
    assert "debit_amount" not in tx
    assert tx["sender"] == "M JOHN DOE"
    assert tx["iban"] == "..0000"


def test_card_expense_legs():
    tx = build("card_expense")
    assert tx["transaction_type"] == "EXPENSE"
    assert tx["debit_asset_code"] == "EUR"
    assert tx["debit_amount"] == pytest.approx(4.9)
    assert "credit_amount" not in tx


# ── Cross-cutting metadata fields ───────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "purchase_savingsplan", "trade", "dividend",
    "interest", "transfer_in", "card_expense",
])
def test_every_tx_has_metadata(name):
    tx = build(name)
    assert tx["date"]                # ISO timestamp prefix
    assert tx["currency"] == "EUR"
    assert tx["tr_id"]               # normalized event ID
    assert tx["tr_event_type"]       # raw TR eventType


# ── EVENT_TYPE_MAP registry ─────────────────────────────────────────────────

def test_event_type_map_covers_known_types():
    """The mapping registry should cover the event types our fixtures use."""
    expected = {
        "TRADING_SAVINGSPLAN_EXECUTED": "PURCHASE",
        "SSP_CORPORATE_ACTION_CASH": "DIVIDEND",
        "INTEREST_PAYOUT": "INTEREST",
        "BANK_TRANSACTION_INCOMING": "TRANSFER",
        "CARD_TRANSACTION": "EXPENSE",
    }
    for event_type, expected_tx_type in expected.items():
        assert EVENT_TYPE_MAP[event_type] == expected_tx_type


def test_determine_tx_type_falls_back_to_order_type():
    """For unmapped event types, parser uses order_type heuristic."""
    item = {"eventType": "UNKNOWN_TYPE", "amount": {"value": -100}}
    parsed = {"order_type": "Ordre d'achat"}
    assert determine_tx_type(item, parsed) == "PURCHASE"

    parsed = {"order_type": "Ordre de vente"}
    assert determine_tx_type(item, parsed) == "SELL"


def test_determine_account_pea_detection():
    """Portfolio name containing 'PEA' should route to the PEA account."""
    item = {"eventType": "TRADING_TRADE_EXECUTED"}
    parsed = {"portfolio": "PEA", "account": None}
    name, typ = determine_account(item, parsed)
    assert name == "Trade Republic PEA"
    assert typ == "BROKERAGE"


# ── deduplicate_pea ─────────────────────────────────────────────────────────

def test_deduplicate_pea_keeps_richer_event():
    """When a PEA mirror pair appears for the same date+ISIN, the one with
    quantity/unit_price wins, but the PEA account name is preserved."""
    rich = {
        "date": "2026-05-18",
        "asset_isin": "FR0000120073",
        "quantity": 2.0,
        "unit_price": 100.0,
        "tr_event_type": "SAVINGS_PLAN_INVOICE_CREATED",
        "account_name": "Trade Republic CTO",
        "account_type": "BROKERAGE",
    }
    pea_mirror = {
        "date": "2026-05-18",
        "asset_isin": "FR0000120073",
        "quantity": None,
        "unit_price": None,
        "tr_event_type": "PEA_SAVINGS_PLAN_PAY_IN",
        "account_name": "Trade Republic PEA",
        "account_type": "BROKERAGE",
    }
    result = deduplicate_pea([rich, pea_mirror])
    assert len(result) == 1
    assert result[0]["quantity"] == 2.0           # richer event kept
    assert result[0]["account_name"] == "Trade Republic PEA"  # PEA name promoted


def test_deduplicate_pea_no_op_without_mirror():
    """If no PEA mirror is present, the function should not collapse."""
    txs = [
        {"date": "2026-05-18", "asset_isin": "X", "tr_event_type": "TRADING_TRADE_EXECUTED"},
        {"date": "2026-05-18", "asset_isin": "X", "tr_event_type": "TRADING_TRADE_EXECUTED"},
    ]
    assert len(deduplicate_pea(txs)) == 2
