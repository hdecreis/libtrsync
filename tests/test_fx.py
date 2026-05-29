"""FX mid computation and EUR conversion."""

from traderepublic_sync import fx_mid
from traderepublic_sync.v1 import FxRate
from traderepublic_sync.v1 import fx


# ── fx_mid ───────────────────────────────────────────────────────────────────


def test_fx_mid_is_average_of_bid_ask():
    # Doc sample: LS000IUSD006.LSX bid 1.163 / ask 1.164 → mid 1.1635.
    assert fx_mid(1.163, 1.164) == 1.1635


def test_fx_mid_six_dp_half_even():
    # (1.0000005 + 1.0000005) / 2 = 1.0000005 → HALF_EVEN rounds to 1.000000.
    assert fx_mid(1.0000005, 1.0000005) == 1.0


def test_fx_mid_falls_back_to_bid_when_ask_missing():
    assert fx_mid(1.23, None) == 1.23


def test_fx_mid_falls_back_to_ask_when_bid_missing():
    assert fx_mid(None, 1.42) == 1.42


def test_fx_mid_none_when_both_missing():
    assert fx_mid(None, None) is None


def test_fx_mid_handles_string_inputs():
    assert fx_mid("1.163", "1.164") == 1.1635


# ── convert_to_eur / rate ────────────────────────────────────────────────────


def test_convert_usd_to_eur_direction():
    # rate is USD-per-EUR, so 116.35 USD / 1.1635 = 100.00 EUR.
    rates = {"USD": FxRate("USD", 1.1635)}
    assert round(fx.convert_to_eur(116.35, "USD", rates), 4) == 100.0


def test_convert_eur_is_identity():
    assert fx.convert_to_eur(42.0, "EUR", None) == 42.0


def test_convert_unknown_currency_returns_none():
    assert fx.convert_to_eur(10.0, "SEK", {"USD": FxRate("USD", 1.16)}) is None


def test_rate_eur_is_one():
    assert fx.rate("EUR", None) == 1.0


def test_rate_accepts_plain_float_map():
    assert fx.rate("USD", {"USD": 1.1635}) == 1.1635


def test_rates_from_raw_builds_fxrate_objects():
    raw = {"usd": {"rate": 1.1635, "bid": 1.163, "ask": 1.164}}
    built = fx.rates_from_raw(raw)
    assert built["USD"] == FxRate("USD", 1.1635, 1.163, 1.164)
