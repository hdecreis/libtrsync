"""HTTP / WebSocket constants for the Trade Republic API."""

TR_API_BASE = "https://api.traderepublic.com"
TR_WS_URL = "wss://api.traderepublic.com"
TR_ASSETS_BASE = "https://assets.traderepublic.com/img"

DEFAULT_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "fr",
    "Cache-Control": "no-cache",
    "Content-Type": "application/json",
    "Pragma": "no-cache",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "x-tr-app-version": "13.40.5",
    "x-tr-platform": "web",
}

WS_CONNECT_PAYLOAD = {
    "locale": "fr",
    "platformId": "webtrading",
    "platformVersion": "safari - 18.3.0",
    "clientId": "app.traderepublic.com",
    "clientVersion": "3.151.3",
}

# REST path for server-computed realized P&L + dividend return (per instrument,
# per securities account). See ``TRClient.fetch_realized_pnl``.
TR_API_PNL_PATH = "/api/v2/taxes/pnl"

# Synthetic LSX instruments TR's own app subscribes to via the ``ticker`` topic
# to read EUR conversion rates (``subscribeToConversionRateFromEUR``). The mid
# of bid/ask is *units of foreign currency per 1 EUR* (e.g. USD per EUR — the
# classic EUR/USD orientation). Only these four currencies are wired up in the
# app build this was derived from; other currencies have no FX topic.
FX_INSTRUMENTS = {
    "USD": "LS000IUSD006.LSX",
    "GBP": "LS000IGBP005.LSX",
    "CHF": "LS000ICHF002.LSX",
    "JPY": "LS000IJPY001.LSX",
}
