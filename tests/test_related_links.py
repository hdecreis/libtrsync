"""Tests for related_tr_ids — TR's occurrence-level cross-links between events.

A PEA cash top-up embeds a ``timelineDetail`` row pointing at the savings-plan
purchase it funds. That embedded link is the only reliable join between the two
events: their amount, date and ISIN don't pair them (the top-up can be smaller
than the trade when PEA cash holds a residual). The parser surfaces every such
link as ``related_event_ids``; the mapping layer exposes them as
``related_tr_ids`` on the dual-legged transaction.
"""

import json
from pathlib import Path

import pytest

from traderepublic_sync import parse_detail_sections
from traderepublic_sync.dual_legged import build_dual_legged_transaction

FIXTURES = Path(__file__).parent / "fixtures"


def build(name: str) -> dict:
    item = json.loads((FIXTURES / f"{name}.json").read_text())
    parsed = parse_detail_sections(item["_detail_raw"])
    return build_dual_legged_transaction(item, parsed)


def test_pay_in_links_to_the_purchase_it_funds():
    """The PEA top-up's detail embeds a timelineDetail row pointing at the
    savings-plan purchase — surfaced as a single related_tr_ids entry."""
    tx = build("pea_pay_in_linked")
    assert tx["related_tr_ids"] == ["f15b1a0f-4ed3-46ba-bbdd-9ff6dd8c2f48"]
    # The link points at *another* event, never the top-up's own id.
    assert tx["tr_id"] not in tx["related_tr_ids"]


def test_pending_pay_in_is_a_transfer_not_a_purchase():
    """This event has eventType=null and order_type "Plan d'épargne", which
    used to misclassify it as PURCHASE. The "investissez … dans votre PEA"
    header marks it as the PEA funding transfer, with balanced cash legs."""
    tx = build("pea_pay_in_linked")
    assert tx["transaction_type"] == "TRANSFER"
    assert tx["account_name"] == "Trade Republic PEA"
    assert tx["credit_asset_code"] == "EUR"
    assert tx["debit_asset_code"] == "EUR"
    assert tx["credit_amount"] == pytest.approx(10.36)
    assert tx["debit_amount"] == pytest.approx(10.36)


def test_no_links_yields_empty_list():
    """Events with no cross-link get an empty list — never the event's own id,
    never the all-zeros infoPage placeholder buried in nested actions."""
    for name in ("pea_pay_in", "pea_purchase", "trade", "dividend", "interest"):
        tx = build(name)
        assert tx["related_tr_ids"] == [], name


def test_parse_detail_sections_surfaces_related_event_ids():
    """The parsing layer is the source of truth for the link; assert it
    directly so the field is covered independently of the mapping layer."""
    item = json.loads((FIXTURES / "pea_pay_in_linked.json").read_text())
    parsed = parse_detail_sections(item["_detail_raw"])
    assert parsed["related_event_ids"] == ["f15b1a0f-4ed3-46ba-bbdd-9ff6dd8c2f48"]
