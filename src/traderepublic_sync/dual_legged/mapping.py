"""Map raw Trade Republic events to dual-legged transaction dicts.

Each transaction has explicit credit / debit / fee / tax legs. This module
turns a raw TR timeline item (plus its parsed detail from
:func:`traderepublic_sync.parsing.parse_detail_sections`) into a dict
matching that schema.

Generic users of the library can ignore this module entirely.
"""

from ..constants import TR_ASSETS_BASE
from ..parsing import extract_isin_from_icon, normalize_tr_id


EVENT_TYPE_MAP = {
    "TRADING_SAVINGSPLAN_EXECUTED":       "PURCHASE",
    "SAVINGS_PLAN_INVOICE_CREATED":       "PURCHASE",
    "PRIVATE_MARKET_FUND_TRADE_EXECUTED": "PURCHASE",
    "SPARE_CHANGE_AGGREGATE":             "PURCHASE",

    "SSP_CORPORATE_ACTION_CASH": "DIVIDEND",
    "INTEREST_PAYOUT":           "INTEREST",
    "INTEREST_PAYOUT_CREATED":   "INTEREST",

    "BANK_TRANSACTION_INCOMING":   "TRANSFER",
    "PAYMENT_INBOUND":             "TRANSFER",
    "PAYMENT_INBOUND_CREDIT_CARD": "TRANSFER",
    "PAYMENT_INBOUND_GOOGLE_PAY":  "TRANSFER",

    # PEA top-up events: cash arriving in the PEA cash account to fund a
    # purchase. These are NOT duplicates of the trade event — the trade
    # debits PEA cash for its full price (possibly > the pay-in amount when
    # PEA cash had a residual). See deduplicate_pea() for the historical
    # context behind this classification.
    "PEA_SAVINGS_PLAN_PAY_IN": "TRANSFER",
    "PEA_DEPOSIT_DEBIT":       "TRANSFER",

    "CARD_TRANSACTION": "EXPENSE",
    "CARD_ORDER_FEE":   "FEE",

    "TRADE_INVOICE":          None,
    "TRADING_TRADE_EXECUTED": None,
}

# Event types that represent a cash top-up INTO a TR sub-account (PEA cash)
# rather than a movement to/from an external bank. For these we know the
# direction even though the amount sign is the same user-centric "negative"
# as a regular outbound transfer.
_PEA_INBOUND_TRANSFER_EVENTS = {"PEA_SAVINGS_PLAN_PAY_IN", "PEA_DEPOSIT_DEBIT"}


def determine_account(main_item, parsed_detail):
    """Determine account name from TR data. Returns ``(account_name, account_type)``."""
    acct = parsed_detail.get("account") or parsed_detail.get("portfolio")
    if acct:
        if "PEA" in acct.upper() or "PLAN" in acct.upper():
            return "Trade Republic PEA", "BROKERAGE"
        elif "ORDINAIRE" in acct.upper() or "CTO" in acct.upper():
            return "Trade Republic CTO", "BROKERAGE"

    event_type = main_item.get("eventType") or ""
    if event_type.startswith("PEA_"):
        return "Trade Republic PEA", "BROKERAGE"

    cash_acct = main_item.get("cashAccountNumber")
    if cash_acct:
        return f"Trade Republic ({cash_acct})", "BROKERAGE"

    return "Trade Republic", "BROKERAGE"


def determine_tx_type(main_item, parsed_detail):
    """Determine transaction type from event type and detail context."""
    event_type = main_item.get("eventType", "")
    mapped = EVENT_TYPE_MAP.get(event_type)

    if mapped is not None:
        return mapped

    order_type = (parsed_detail.get("order_type") or "").lower()
    if "vente" in order_type or "sell" in order_type:
        return "SELL"
    if "achat" in order_type or "buy" in order_type or "plan" in order_type:
        return "PURCHASE"

    amount_val = (main_item.get("amount") or {}).get("value", 0)
    if amount_val < 0:
        return "PURCHASE"
    elif amount_val > 0:
        return "SELL"

    return "CUSTOM"


def build_dual_legged_transaction(main_item, parsed_detail):
    """Build a dual-legged transaction dict from TR main item + parsed detail."""
    event_type = main_item.get("eventType") or ""
    tx_type = determine_tx_type(main_item, parsed_detail)
    account_name, account_type = determine_account(main_item, parsed_detail)

    isin = parsed_detail.get("isin") or extract_isin_from_icon(main_item.get("icon"))
    asset_name = parsed_detail.get("asset_name") or main_item.get("title", "")
    currency = (main_item.get("amount") or {}).get("currency", "EUR")

    amount_val = abs((main_item.get("amount") or {}).get("value", 0))
    quantity = parsed_detail.get("quantity")
    unit_price = parsed_detail.get("unit_price")
    total = parsed_detail.get("total") or amount_val
    fees = parsed_detail.get("fees")
    taxes = parsed_detail.get("taxes")

    timestamp = main_item.get("timestamp", "")
    date = timestamp[:19] if len(timestamp) >= 19 else timestamp

    tx = {
        "date": date,
        "transaction_type": tx_type,
        "description": parsed_detail.get("event_description") or main_item.get("subtitle", ""),
        "account_name": account_name,
        "account_type": account_type,
        "currency": currency,
        "asset_isin": isin,
        "asset_name": asset_name,
    }

    asset_value = None
    if quantity and unit_price:
        asset_value = round(quantity * unit_price, 6)

    if tx_type == "PURCHASE":
        tx["credit_asset_code"] = isin or asset_name
        tx["credit_asset_name"] = asset_name
        tx["credit_amount"] = quantity
        if total:
            debit_total = total
        elif asset_value is not None:
            debit_total = asset_value + (fees or 0) + (taxes or 0)
        else:
            debit_total = amount_val
        tx["debit_asset_code"] = currency
        tx["debit_amount"] = round(debit_total, 2)
        tx["quantity"] = quantity
        tx["unit_price"] = unit_price

    elif tx_type == "SELL":
        if total:
            credit_total = total
        elif asset_value is not None:
            credit_total = asset_value - (fees or 0) - (taxes or 0)
        else:
            credit_total = amount_val
        tx["credit_asset_code"] = currency
        tx["credit_amount"] = round(credit_total, 2)
        tx["debit_asset_code"] = isin or asset_name
        tx["debit_asset_name"] = asset_name
        tx["debit_amount"] = quantity
        tx["quantity"] = quantity
        tx["unit_price"] = unit_price

    elif tx_type == "DIVIDEND":
        tx["credit_asset_code"] = currency
        tx["credit_amount"] = total
        tx["reference_asset_code"] = isin or asset_name
        tx["reference_asset_name"] = asset_name
        tx["quantity"] = quantity
        tx["dividend_per_share"] = parsed_detail.get("dividend_per_share")
        tx["dividend_currency"] = parsed_detail.get("dividend_currency") or currency

    elif tx_type == "INTEREST":
        tx["credit_asset_code"] = currency
        tx["credit_amount"] = total

    elif tx_type == "TRANSFER":
        if event_type in _PEA_INBOUND_TRANSFER_EVENTS:
            # PEA top-up: money arriving in the PEA cash account from the
            # user's external bank (or CTO). TR signs the amount negatively
            # (user-centric "cash spent on PEA") so we ignore the sign and
            # set both legs — the consumer routes ``credit_account`` to PEA
            # cash and ``debit_account`` to the external/CTO source.
            tx["credit_asset_code"] = currency
            tx["credit_amount"] = total
            tx["debit_asset_code"] = currency
            tx["debit_amount"] = total
        elif (main_item.get("amount") or {}).get("value", 0) >= 0:
            tx["credit_asset_code"] = currency
            tx["credit_amount"] = total
        else:
            tx["debit_asset_code"] = currency
            tx["debit_amount"] = total
        tx["sender"] = parsed_detail.get("sender")
        tx["iban"] = parsed_detail.get("iban")

    elif tx_type == "EXPENSE":
        tx["debit_asset_code"] = currency
        tx["debit_amount"] = total

    elif tx_type == "FEE":
        tx["debit_asset_code"] = currency
        tx["debit_amount"] = total

    if fees and fees > 0:
        tx["fee_amount"] = fees
        tx["fee_currency"] = parsed_detail.get("fees_currency") or currency
    if taxes and taxes > 0:
        tx["tax_amount"] = taxes
        tx["tax_currency"] = parsed_detail.get("taxes_currency") or currency

    doc_urls = parsed_detail.get("document_urls", [])
    if doc_urls:
        tx["document_urls"] = doc_urls

    icon_path = main_item.get("icon")
    if icon_path:
        tx["icon_url"] = f"{TR_ASSETS_BASE}/{icon_path}/dark.min.svg"

    tx["tr_id"] = normalize_tr_id(main_item.get("id"))
    tx["tr_event_type"] = event_type
    tx["tr_cash_account"] = main_item.get("cashAccountNumber")
    tx["tr_status"] = main_item.get("status")

    return tx


def deduplicate_pea(transactions):
    """Drop only true intra-page duplicates.

    History: an earlier version of this function merged ``PEA_SAVINGS_PLAN_PAY_IN``
    (or ``PEA_DEPOSIT_DEBIT``) into the matching ``TRADING_SAVINGSPLAN_EXECUTED``
    on the assumption that the pay-in was a redundant mirror of the trade.
    That was wrong: the pay-in is the **transfer** that funds the trade,
    and when the PEA cash account already holds a residual balance the
    pay-in amount can be smaller than the trade amount (e.g. 10.23 € paid
    in + 7.40 € residual → 17.63 € trade). Collapsing them silently lost
    that cash flow.

    With ``PEA_SAVINGS_PLAN_PAY_IN`` and ``PEA_DEPOSIT_DEBIT`` now mapped
    to ``TRANSFER`` (see ``EVENT_TYPE_MAP``), the two events are distinct
    transaction types and are kept side by side. This function is retained
    in the public API and now only collapses the rare case where TR emits
    two events of the **same** dual-legged type, on the same date+ISIN,
    with the same primary amount — usually a TR-side glitch. The richer
    one (with quantity/unit_price filled in) wins.
    """
    def _key(tx):
        return (
            tx.get("date", "")[:10],
            tx.get("asset_isin"),
            tx.get("transaction_type"),
            _primary_amount(tx),
        )

    by_key: dict[tuple, list[dict]] = {}
    out_order: list[tuple] = []
    standalone: list[dict] = []

    for tx in transactions:
        if not tx.get("asset_isin"):
            standalone.append(tx)
            continue
        k = _key(tx)
        if k not in by_key:
            by_key[k] = []
            out_order.append(k)
        by_key[k].append(tx)

    deduped: list[dict] = []
    for k in out_order:
        group = by_key[k]
        if len(group) == 1:
            deduped.append(group[0])
            continue
        # True duplicate: keep the entry with the most detail filled in.
        best = max(
            group,
            key=lambda t: (
                1 if t.get("quantity") else 0,
                1 if t.get("unit_price") else 0,
                1 if t.get("fee_amount") else 0,
                1 if t.get("tax_amount") else 0,
            ),
        )
        deduped.append(best)

    # Preserve original ordering: standalone (no ISIN) items pass through
    # in input order at the end, matching the previous behavior.
    return deduped + standalone


def _primary_amount(tx):
    """Return the side of the tx that represents the primary money flow.

    Used as part of the dedup key so two events with the same dual-legged
    type but different amounts (e.g. partial cash + savings plan top-up)
    are NOT merged.
    """
    for key in ("credit_amount", "debit_amount"):
        v = tx.get(key)
        if isinstance(v, (int, float)) and v:
            return round(float(v), 2)
    return None
