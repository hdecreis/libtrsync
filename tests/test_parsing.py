from traderepublic_sync import parse_currency_amount, normalize_tr_id, extract_isin_from_icon
from traderepublic_sync.parsing import _parse_transaction_nested


def test_parse_currency_amount_dollar_us():
    # TR writes USD as "$US"; the residual "US" must not break the parse.
    assert parse_currency_amount("12,54 $US") == 12.54
    assert parse_currency_amount("11,29\xa0$US") == 11.29


def test_transaction_nested_round_up_lowercase_x():
    # Round-ups format the line as "0.004224 x 236,70 €" (lowercase x, dot decimal).
    result = {"quantity": None, "unit_price": None, "total": None}
    detail = {"displayValue": {"text": "0.004224 x 236,70 €"}}
    _parse_transaction_nested(detail, result)
    assert result["quantity"] == 0.004224
    assert result["unit_price"] == 236.70


def test_transaction_nested_bond_valeur_faciale():
    # Bonds expose no share count — the nominal "Valeur faciale" is the quantity
    # (native ccy) at unit price 1; the market quote is "Quotation" (% of par).
    result = {"quantity": None, "unit_price": None, "total": None}
    detail = {
        "action": {"type": "infoPage", "payload": {"sections": [{"data": [
            {"title": "Valeur faciale", "detail": {"text": "12,54 $US"}},
            {"title": "Quotation", "detail": {"text": "91,26 %"}},
            {"title": "Total", "detail": {"text": "11,00 €"}},
        ]}]}}
    }
    _parse_transaction_nested(detail, result)
    assert result["quantity"] == 12.54
    assert result["unit_price"] == 1.0
    assert result["total"] == 11.0


def test_parse_currency_amount_french_format():
    assert parse_currency_amount("1 000,00 EUR") == 1000.0   # plain space thousands
    assert parse_currency_amount("1\xa0000,00 EUR") == 1000.0  # NBSP thousands
    assert parse_currency_amount("1 000,00 EUR") == 1000.0  # NNBSP thousands
    assert parse_currency_amount("15,635 EUR") == 15.635
    assert parse_currency_amount("+200,00 $") == 200.0
    assert parse_currency_amount("1 329,57") == 1329.57


def test_parse_currency_amount_eu_format():
    # Dot = thousands separator, comma = decimal
    assert parse_currency_amount("1.000,99") == 1000.99
    assert parse_currency_amount("1.023.999,01 EUR") == 1023999.01


def test_parse_currency_amount_us_format():
    assert parse_currency_amount("$1,023,999.01") == 1023999.01
    assert parse_currency_amount("1,000.00 USD") == 1000.0
    assert parse_currency_amount("691.4") == 691.4


def test_parse_currency_amount_iso_codes():
    assert parse_currency_amount("100,00 EUR") == 100.0
    assert parse_currency_amount("100,00 USD") == 100.0
    assert parse_currency_amount("100,00 CAD") == 100.0
    assert parse_currency_amount("100,00 CHF") == 100.0
    assert parse_currency_amount("100,00 GBP") == 100.0


def test_parse_currency_amount_handles_garbage():
    assert parse_currency_amount("") is None
    assert parse_currency_amount(None) is None
    assert parse_currency_amount("not a number") is None


def test_normalize_tr_id_collapses_zero_runs():
    raw = "109a10.00000000000000000026-4179-877d-b06690923902"
    assert normalize_tr_id(raw) == "109a10.0026-4179-877d-b06690923902"


def test_normalize_tr_id_leaves_short_runs_alone():
    assert normalize_tr_id("abc-00-def") == "abc-00-def"


def test_extract_isin_from_icon():
    assert extract_isin_from_icon("logos/FR0011550672/v2") == "FR0011550672"
    assert extract_isin_from_icon("logos/US0378331005/v2") == "US0378331005"
    assert extract_isin_from_icon(None) is None
    assert extract_isin_from_icon("no isin here") is None

