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
    "PEA_SAVINGS_PLAN_PAY_IN":            "PURCHASE",
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

    "CARD_TRANSACTION": "EXPENSE",
    "CARD_ORDER_FEE":   "FEE",

    "TRADE_INVOICE":          None,
    "TRADING_TRADE_EXECUTED": None,
    "PEA_DEPOSIT_DEBIT":      None,
}


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
        if (main_item.get("amount") or {}).get("value", 0) >= 0:
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
    """Deduplicate PEA mirror event pairs, keeping the one with most detail."""
    PEA_MIRROR_TYPES = {"PEA_SAVINGS_PLAN_PAY_IN", "PEA_DEPOSIT_DEBIT"}

    by_key = {}
    for tx in transactions:
        isin = tx.get("asset_isin")
        if not isin:
            by_key[id(tx)] = [tx]
            continue
        key = (tx.get("date", "")[:10], isin)
        if key not in by_key:
            by_key[key] = []
        by_key[key].append(tx)

    result = []
    for key, group in by_key.items():
        if len(group) == 1:
            result.append(group[0])
            continue

        has_pea_mirror = any(
            (t.get("tr_event_type") or "") in PEA_MIRROR_TYPES for t in group
        )
        if not has_pea_mirror:
            result.extend(group)
            continue

        best = max(
            group,
            key=lambda t: (
                1 if t.get("quantity") else 0,
                1 if t.get("unit_price") else 0,
                0 if (t.get("tr_event_type") or "") in PEA_MIRROR_TYPES else 1,
            ),
        )
        pea_tx = next(
            (t for t in group if (t.get("tr_event_type") or "") in PEA_MIRROR_TYPES),
            None,
        )
        if pea_tx and "PEA" in (pea_tx.get("account_name") or ""):
            best["account_name"] = pea_tx["account_name"]
            best["account_type"] = pea_tx["account_type"]
        result.append(best)
    return result
