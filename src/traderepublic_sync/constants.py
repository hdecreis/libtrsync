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
