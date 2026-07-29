"""Pure parsing helpers for Trade Republic API responses.

These functions have no I/O and no third-party dependencies; they can be
imported and used standalone for testing or for processing pre-fetched
response payloads.
"""

import re

_CURRENCY_RE = re.compile(r"\b(EUR|USD|CAD|CHF|GBP)\b")

# A timeline event id is a plain UUID. TR uses ``timelineDetail`` actions to
# cross-link related events (e.g. a PEA cash top-up embeds a tappable row that
# points at the savings-plan execution it funds). The all-zeros UUID is a
# placeholder TR emits for inline ``infoPage`` sub-payloads — never a real
# event id, so exclude it.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)
_ZERO_UUID = "00000000-0000-0000-0000-000000000000"


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
    # Drop any residual currency letters (e.g. TR writes USD as "$US", leaving
    # "US" after the "$" is stripped) so float() doesn't choke.
    cleaned = re.sub(r"[A-Za-z]", "", cleaned)
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
        "related_event_ids": [],
    }

    # TR cross-links related events via ``timelineDetail`` actions buried in
    # the detail tree (often a ``listItem`` whose section is titled
    # "Transaction" but whose *item* title is empty, so the title-keyed loop
    # below never sees it). Walk the whole response for those links — they're
    # the only occurrence-level join between, e.g., a PEA cash top-up and the
    # savings-plan purchase it funds.
    self_id = detail_response.get("id")
    _collect_related_event_ids(detail_response, self_id, result["related_event_ids"])

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


def _collect_related_event_ids(node, self_id, out):
    """Recursively gather ``timelineDetail`` links to *other* timeline events.

    Collects each ``action.payload`` where ``action.type == "timelineDetail"``
    and the payload is a UUID that is neither the event's own id nor the
    all-zeros placeholder. Order-preserving and de-duplicated. ``out`` is
    mutated in place.
    """
    if isinstance(node, dict):
        if node.get("type") == "timelineDetail":
            payload = node.get("payload")
            if (
                isinstance(payload, str)
                and _UUID_RE.match(payload)
                and payload != _ZERO_UUID
                and payload != self_id
                and payload not in out
            ):
                out.append(payload)
        for value in node.values():
            _collect_related_event_ids(value, self_id, out)
    elif isinstance(node, list):
        for value in node:
            _collect_related_event_ids(value, self_id, out)


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
                sub_detail = sub.get("detail") if isinstance(sub.get("detail"), dict) else {}
                # Value may sit in detail.text or detail.displayValue.text
                # (bonds put "Valeur faciale" / "Quotation" in the latter).
                sub_text = (
                    sub_detail.get("text")
                    or (sub_detail.get("displayValue") or {}).get("text")
                    or ""
                )
                if sub_title in ("Actions", "Titres"):
                    result["quantity"] = parse_currency_amount(sub_text)
                elif sub_title in ("Prix du titre", "Cours du titre"):
                    result["unit_price"] = parse_currency_amount(sub_text)
                elif sub_title == "Valeur faciale":
                    # Bonds quote in % of par and have no share count: the face
                    # value (nominal, native ccy) is the quantity, held at unit
                    # price 1; the market quote is the "Quotation" row.
                    if result["quantity"] is None:
                        result["quantity"] = parse_currency_amount(sub_text)
                        if result["unit_price"] is None:
                            result["unit_price"] = 1.0
                elif sub_title == "Total":
                    if result["total"] is None:
                        result["total"] = parse_currency_amount(sub_text)

    # Fallback: parse "0,272717 × 91,67 EUR" or "0.004224 x 236,70 €"
    # (round-ups use a lowercase 'x' and a dot decimal) from displayValue / text.
    if result["quantity"] is None:
        display = detail.get("displayValue") or {}
        for cand in (display.get("prefix", ""), display.get("text", ""), detail.get("text", "")):
            m = re.search(r"([0-9][0-9.,\s]*?)\s*[×xX]\s*([0-9][0-9.,\s]*)", cand or "")
            if not m:
                continue
            qty = parse_currency_amount(m.group(1))
            if qty is not None:
                result["quantity"] = qty
                if result["unit_price"] is None:
                    result["unit_price"] = parse_currency_amount(m.group(2))
                break


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
