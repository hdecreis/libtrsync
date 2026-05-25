"""Pure parsing helpers for Trade Republic API responses.

These functions have no I/O and no third-party dependencies; they can be
imported and used standalone for testing or for processing pre-fetched
response payloads.
"""

import re

_CURRENCY_RE = re.compile(r"\b(EUR|USD|CAD|CHF|GBP)\b")


def parse_currency_amount(text):
    """Parse a TR-formatted amount string into a float.

    Handles:
    - Currency symbols: € $ £
    - ISO currency codes: EUR USD CAD CHF GBP
    - French thousand separators (space-separated groups)
    - Leading +/-
    - Regular spaces, NO-BREAK SPACE (U+00A0), NARROW NO-BREAK SPACE (U+202F)
    """
    if not text or not isinstance(text, str):
        return None
    cleaned = _CURRENCY_RE.sub("", text)
    cleaned = (
        cleaned
        .replace("€", "")
        .replace("$", "")
        .replace("£", "")
        .replace("\xa0", "")   # NO-BREAK SPACE (U+00A0)
        .replace(" ", "") # NARROW NO-BREAK SPACE (U+202F)
        .replace(" ", "")      # plain space (thousand separator)
        .replace("+", "")
        .strip()
    )
    # Detect decimal separator by which comes last.
    # Both present: last one is decimal (e.g. "1,023,999.01" → US; "1.000,99" → FR)
    # Only comma:   FR decimal (e.g. "15,635")
    # Only dot or neither: standard decimal
    has_comma = "," in cleaned
    has_dot = "." in cleaned
    if has_comma and has_dot:
        if cleaned.rfind(".") > cleaned.rfind(","):
            # US format: commas are thousands separators
            cleaned = cleaned.replace(",", "")
        else:
            # FR/EU format: dots are thousands separators
            cleaned = cleaned.replace(".", "").replace(",", ".")
    elif has_comma:
        cleaned = cleaned.replace(",", ".")

    try:
        return float(cleaned)
    except ValueError:
        return None


def _extract_currency_symbol(text):
    """Extract currency code from a text containing a currency symbol or ISO code."""
    if not text:
        return None
    if "$" in text:
        return "USD"
    if "£" in text:
        return "GBP"
    if "CHF" in text:
        return "CHF"
    if "CAD" in text:
        return "CAD"
    if "USD" in text:
        return "USD"
    if "GBP" in text:
        return "GBP"
    if "€" in text or "EUR" in text:
        return "EUR"
    return None


def parse_detail_sections(detail_response):
    """Extract structured data from a timelineDetailV2 response."""
    result = {
        "isin": None,
        "asset_name": None,
        "account": None,
        "portfolio": None,
        "order_type": None,
        "quantity": None,
        "unit_price": None,
        "total": None,
        "fees": None,
        "fees_currency": None,
        "taxes": None,
        "taxes_currency": None,
        "dividend_per_share": None,
        "dividend_currency": None,
        "sender": None,
        "iban": None,
        "event_description": None,
        "currency": None,
        "document_urls": [],
    }

    sections = detail_response.get("sections", [])

    for section in sections:
        stype = section.get("type", "")

        if stype == "header":
            result["event_description"] = section.get("title", "")
            action = section.get("action")
            if isinstance(action, dict):
                payload = action.get("payload")
                if isinstance(payload, str) and re.match(r"^[A-Z]{2}[A-Z0-9]{10}$", payload):
                    result["isin"] = payload

        elif stype == "table" and isinstance(section.get("data"), list):
            for item in section["data"]:
                title = item.get("title", "")
                detail = item.get("detail", {})
                if not isinstance(detail, dict):
                    continue
                text = detail.get("text", "")

                if title == "Compte":
                    result["account"] = text
                elif title == "Portefeuille":
                    result["portfolio"] = text
                elif title in ("Actif", "Actifs"):
                    result["asset_name"] = text
                elif title == "Type d'ordre":
                    result["order_type"] = text
                elif title == "Frais":
                    val = parse_currency_amount(text)
                    if val is not None and val > 0:
                        result["fees"] = val
                        result["fees_currency"] = _extract_currency_symbol(text)
                elif title in ("Impôts", "Taxes"):  # Impôts
                    val = parse_currency_amount(text)
                    if val is not None:
                        result["taxes"] = abs(val)
                        result["taxes_currency"] = _extract_currency_symbol(text)
                elif title == "Total":
                    result["total"] = parse_currency_amount(text)
                elif title == "Dividende par action":
                    result["dividend_per_share"] = parse_currency_amount(text)
                    result["dividend_currency"] = _extract_currency_symbol(text)
                elif title == "Expéditeur":  # Expéditeur
                    result["sender"] = text
                elif title == "IBAN":
                    result["iban"] = text
                elif title == "Transaction":
                    _parse_transaction_nested(detail, result)
                elif title in ("Actions", "Titres"):
                    result["quantity"] = parse_currency_amount(text)
                elif title in ("Prix du titre", "Cours du titre"):
                    result["unit_price"] = parse_currency_amount(text)
                elif title == "Événement":  # Événement
                    if not result["order_type"]:
                        result["order_type"] = text

        elif stype == "documents" and isinstance(section.get("data"), list):
            for item in section["data"]:
                action = item.get("action")
                if isinstance(action, dict):
                    url = action.get("payload")
                    if isinstance(url, str) and url.startswith("http"):
                        doc_title = item.get("title", "Document")
                        result["document_urls"].append({"title": doc_title, "url": url})

    return result


def _parse_transaction_nested(detail, result):
    """Parse nested transaction detail (quantity, unit_price, total) from infoPage action."""
    action = detail.get("action", {})
    if isinstance(action, dict) and action.get("type") == "infoPage":
        payload = action.get("payload", {})
        for sec in payload.get("sections", []):
            if not isinstance(sec.get("data"), list):
                continue
            for sub in sec["data"]:
                sub_title = sub.get("title", "")
                sub_text = (
                    sub.get("detail", {}).get("text", "")
                    if isinstance(sub.get("detail"), dict)
                    else ""
                )
                if sub_title in ("Actions", "Titres"):
                    result["quantity"] = parse_currency_amount(sub_text)
                elif sub_title in ("Prix du titre", "Cours du titre"):
                    result["unit_price"] = parse_currency_amount(sub_text)
                elif sub_title == "Total":
                    if result["total"] is None:
                        result["total"] = parse_currency_amount(sub_text)

    # Fallback: parse "3 x 17,41 EUR" from displayValue
    if result["quantity"] is None:
        display = detail.get("displayValue", {})
        prefix = display.get("prefix", "")
        text = display.get("text", "")
        if prefix and "×" in prefix:  # ×
            qty_str = (
                prefix.split("×")[0]
                .strip()
                .replace(",", ".")
                .replace("\xa0", "")
                .replace(" ", "")
                .replace(" ", "")
            )
            try:
                result["quantity"] = float(qty_str)
            except ValueError:
                pass
            result["unit_price"] = parse_currency_amount(text)


def extract_isin_from_icon(icon_path):
    """Extract ISIN from icon paths like 'logos/FR0011550672/v2'."""
    if not icon_path:
        return None
    m = re.search(r"/([A-Z]{2}[A-Z0-9]{10})/", icon_path)
    return m.group(1) if m else None


def normalize_tr_id(raw_id):
    """Normalize TR event IDs that contain absurd zero-padding.

    e.g. ``'109a10.00000000...00026-4179-877d-b06690923902'``
    becomes ``'109a10.0026-4179-877d-b06690923902'``.
    """
    if not raw_id or not isinstance(raw_id, str):
        return raw_id
    # Collapse any run of 3+ consecutive zeros down to '00'
    return re.sub(r"0{3,}", "00", raw_id)
