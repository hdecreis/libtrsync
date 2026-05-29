# traderepublic-sync

[![CI](https://github.com/hdecreis/libtrsync/actions/workflows/ci.yml/badge.svg)](https://github.com/hdecreis/libtrsync/actions/workflows/ci.yml)

Unofficial Python client for **Trade Republic**. Handles AWS WAF token
acquisition, phone+PIN login with 2FA, WebSocket data fetching, and parsing
of the timeline-detail responses into structured Python dicts.

> ⚠️ **Unofficial.** Trade Republic does not publish an API. This library
> reverse-engineers the web app's WebSocket protocol; it can break at any
> time and is not endorsed by Trade Republic. Use at your own risk.

## Install

```bash
# Base install (websockets + requests only)
pip install -e .

# With Playwright for WAF token acquisition (recommended)
pip install -e .[playwright]
playwright install chromium

# Or with Selenium
pip install -e .[selenium]
```

Python ≥ 3.11.

## Quickstart

```python
import asyncio
from traderepublic_sync import TRClient

client = TRClient(locale="fr")

# 1. WAF token (uses headless browser)
client.acquire_waf_token("playwright")

# 2. Login - Trade Republic will push a 2FA prompt to your phone
login = client.login(phone_number="+33612121212", pin="1234")
print(f"2FA code requested. You have {login['countdown']}s.")

# Optional: ask for SMS instead of the in-app push
# client.request_sms(login["process_id"])

# 3. Verify 2FA (read the code from wherever the user enters it)
code = input("2FA code: ")
session_token = client.verify_2fa(login["process_id"], code)

# 4. Fetch data
result = asyncio.run(client.fetch_transactions(session_token))
print(f"{len(result['transactions'])} transactions, "
      f"{len(result['raw_items'])} raw items")

balance = asyncio.run(client.fetch_cash_balance(session_token))
print("Cash:", balance)
```

## Persisting session state

`ConnectionState` is a plain dataclass - pickle it, JSON-encode it, or store
it in your own DB. The WAF token + session token can be reused across
processes until they expire (typically a few hours).

```python
from dataclasses import asdict
import json
from traderepublic_sync import ConnectionState, TRClient

# Save after a successful login
state = ConnectionState(
    phone_number="+33612121212",
    pin="1234",
    waf_token=client.waf_token,
    device_info=client.device_info,
    session_token=session_token,
    auth_status="authenticated",
)
with open("tr_state.json", "w") as f:
    json.dump(asdict(state), f)

# Restore later
with open("tr_state.json") as f:
    state = ConnectionState(**json.load(f))

client = TRClient(waf_token=state.waf_token, device_info=state.device_info)
asyncio.run(client.fetch_transactions(state.session_token))
```

## Portfolio API (`traderepublic_sync.v1`)

A versioned facade that returns **typed, EUR-correct** portfolio data — the
computation layer most consumers would otherwise hand-roll. It wraps
`TRClient` and adds: valuation in EUR (bonds divided by 100 + FX,
`averageBuyIn` kept as EUR), unrealized **and** realized P&L (sales +
dividends via `GET /api/v2/taxes/pnl`, with a timeline fallback for
crypto/bonds), fully-sold-asset discovery, and an FX source backed by TR's
own LSX `ticker` rates.

```python
import asyncio
from traderepublic_sync import TRClient
from traderepublic_sync.v1 import Portfolio

async def main():
    client = TRClient(session_token=token)   # or TRClient.from_state(state)
    pf = Portfolio(client)

    snap = await pf.snapshot()               # include_committed=False by default
    print(snap.total_value_eur, snap.total_unrealized_pnl_eur)
    print(snap.total_realized_pnl_eur, snap.total_dividends_eur)

    for s in await pf.sold_assets():
        print(s.isin, s.realized_pnl, s.source)   # "tr" or "timeline"

asyncio.run(main())
```

> The low-level `fetch_asset_list` returns metrics in the instrument's
> **quote currency** (no FX, no bond ÷100) — fine as raw data, but use the
> `v1` `Position` for EUR-correct value/P&L.

**Private Markets:** held and committed are separate. `Position.value_eur`
is the invested book only; `committed_eur` / `committed_schedule` carry
uncalled capital. `snapshot(include_committed=True)` adds committed to both
value and cost (so it shows in the value total but nets to 0 in P&L).

Live equivalents are on `Portfolio.stream()` (`prices` / `positions` /
`cash` / `transactions` / `fx`). See `docs/tr-undocumented-api.md` for the
underlying REST/WS endpoints and `scripts/probe_rest.py` to probe them.

## Dual-legged transactions (optional)

The mapping layer shapes TR events into a double-entry transaction schema
(PURCHASE / SELL / DIVIDEND / TRANSFER / …) with explicit credit / debit /
fee / tax legs. It lives in a separate submodule so generic users can ignore it:

```python
from traderepublic_sync.dual_legged import (
    build_dual_legged_transaction,
    deduplicate_pea,
    EVENT_TYPE_MAP,
)

# Given a raw TR item + its parsed detail (use parse_detail_sections from
# the main package), produce a dual-legged transaction dict:
tx = build_dual_legged_transaction(raw_item, parsed_detail)
```

`fetch_transactions()` already applies this mapping and returns both forms
under `"transactions"` (dual-legged) and `"raw_items"` (raw TR items with the
parsed detail attached as `_detail` / `_detail_raw`).

## Live subscriptions (TRSession)

`TRClient` exposes one-shot helpers (`fetch_transactions`, `fetch_cash_balance`,
`fetch_ticker`, …) that open a WebSocket, send a single request, and close.
For **streaming** use cases — live ticker, live portfolio updates, instrument
search — open a long-lived session via `client.open_session()`.

```python
import asyncio
from traderepublic_sync import TRClient

client = TRClient(waf_token=..., device_info=...)
# ...login + verify_2fa as in the quickstart...

async def watch_apple():
    async with client.open_session(session_token) as session:
        def on_tick(data):
            last = (data.get("last") or {}).get("price")
            print(f"AAPL = {last}")

        sub_id = await session.subscribe_ticker("US0378331005", on_tick)
        await asyncio.sleep(60)            # stream for a minute
        await session.unsubscribe(sub_id)  # optional — __aexit__ also cleans up

asyncio.run(watch_apple())
```

### Convenience subscriptions

| Method | What it streams |
|---|---|
| `subscribe_ticker(isin, cb)` | Live price (`last`, `bid`, `ask`, `open`, `pre`) — resolves the home exchange for you |
| `subscribe_portfolio(sec_acc_no, cb)` | Live positions list (quantity + cost basis) |
| `subscribe_cash(cash_acc_no, cb)` | Available cash balance |
| `subscribe_transactions(cash_acc_no, cb)` | Timeline transactions as they appear |

All callbacks may be plain functions or coroutines. They receive the parsed
JSON payload of each incoming frame; errors in the callback are logged and
swallowed so one bad frame doesn't kill the stream.

### Searching for instruments (securities)

`search_instrument(query, instrument_type=None, limit=20)` queries TR's
`neonSearch` endpoint — the same one the web app uses for the asset
picker. It accepts a free-text query (name, ticker, ISIN fragment) and
returns the raw result list.

```python
async with client.open_session(session_token) as session:
    # By asset class
    btc     = await session.search_instrument("bitcoin", instrument_type="crypto")
    apple   = await session.search_instrument("apple",   instrument_type="stock")
    msci    = await session.search_instrument("MSCI World", instrument_type="etf")

    # Without a type filter, results span all asset classes
    mixed   = await session.search_instrument("tesla", limit=5)

    # By ISIN (or fragment)
    by_isin = await session.search_instrument("US0378331005")
```

**Parameters**

| Name | Type | Notes |
|---|---|---|
| `query` | `str` | Free-text search — name, ticker, partial ISIN |
| `instrument_type` | `str \| None` | One of `"crypto"`, `"stock"`, `"etf"`, `"bond"`, `"derivative"`, `"fund"` — or `None` to search everything |
| `limit` | `int` | Max results (default `20`) |

**Result shape** — each item is a raw TR dict, typically including:

```python
{
    "isin": "XF000BTC0017",         # ISIN or pseudo-ISIN for crypto
    "name": "Bitcoin",
    "type": "crypto",
    "exchanges": [{"slug": "BTC", "name": "Bitcoin"}],
    # ...additional fields vary by asset class
}
```

Use the returned `isin` to feed `subscribe_ticker()`, `fetch_ticker()`,
or any other instrument-keyed API on the client.

### Lower-level primitives

If a TR subscription type isn't covered by the convenience helpers, use
`subscribe()` / `request()` directly:

```python
async with client.open_session(session_token) as session:
    # One-shot: subscribe, take the first frame, unsubscribe.
    data = await session.request("availableCash", {"id": cash_acc_no})

    # Streaming: keep receiving until you unsubscribe.
    sub_id = await session.subscribe(
        "compactPortfolio",
        {"secAccNo": sec_acc_no},
        callback=lambda d: print(d["positions"]),
    )
```

The WebSocket token is injected for you; you don't need to pass it in
`params`.

## Downloading files listed in transactions

The key points:
  - Cookie: tr_session=<session_token> — this is how TR authenticates document downloads (same mechanism as login).
  - Headers: reuse client._headers() for x-aws-waf-token and x-tr-device-info — TR's WAF will reject requests without a valid token.
  - The document URLs are absolute https:// URLs already, no base URL manipulation needed.

Examples:

  ```python
  import requests

  def download_tr_documents(client, session_token: str, transactions: list, output_dir: str = "."):
      """Download all PDF documents from a list of dual-legged transactions."""
      import os

      headers = client._headers()  # includes x-aws-waf-token + x-tr-device-info
      cookies = {"tr_session": session_token}

      for tx in transactions:
          for doc in tx.get("document_urls", []):
              url = doc["url"]
              title = doc["title"].replace("/", "-")
              tr_id = tx.get("tr_id", "unknown")
              filename = f"{tx['date'][:10]}_{tr_id}_{title}.pdf"
              filepath = os.path.join(output_dir, filename)

              resp = requests.get(url, headers=headers, cookies=cookies)
              resp.raise_for_status()

              with open(filepath, "wb") as f:
                  f.write(resp.content)
              print(f"Saved: {filepath}")
  
  result = asyncio.run(client.fetch_transactions(session_token))
  download_tr_documents(client, session_token, result["transactions"], output_dir="/tmp/tr_docs")
  ```

  Or if you want to pull from raw_items instead (same document URLs, accessible before the dual-legged mapping):
  ```
  for item in result["raw_items"]:
      for doc in item["_detail"].get("document_urls", []):
          ...
  ```

## Pure parsing utilities

These are dependency-free helpers you can use without authenticating:

```python
from traderepublic_sync import (
    parse_currency_amount,                # "1 000,00 EUR" → 1000.0
    parse_detail_sections,     # timelineDetailV2 dict → structured dict
    extract_isin_from_icon,    # "logos/FR0011550672/v2" → "FR0011550672"
)
```

## Layout

```
src/traderepublic_sync/
├── client.py       # TRClient (login, 2FA, one-shot websocket fetches)
├── session.py      # TRSession (long-lived ws, callback-based subscriptions)
├── waf.py          # AWS WAF token via Playwright or Selenium
├── parsing.py      # parse_currency_amount, parse_detail_sections, ISIN extraction
├── state.py        # ConnectionState dataclass
├── constants.py    # API URLs, default headers, WS connect payload
├── exceptions.py   # TRAuthError
└── dual_legged/    # Optional dual-legged transaction mapping
    └── mapping.py
```

## Reporting bugs

The TR API is undocumented and locale-sensitive — the most useful thing
you can attach to a bug report is a redacted copy of your own dump, so we
can reproduce the parsing path that misbehaved. Two scripts make this
safe:

1. **Dump everything** with `examples/smoke_fetch_all.py` — writes
   `accounts.json`, `assets.json`, `transactions_raw.json` and
   `transactions_dl.json` under `examples/out/`. The raw file contains
   the full TR responses (`_detail` + `_detail_raw`) which is what we
   need for parser fixes.
2. **Anonymize** the whole folder with `scripts/redact_dump.py` before
   sharing. It keeps every item and preserves the data shape, but:
   - replaces ``sender`` / ``iban`` / ``holderName`` / ``email`` /
     ``phone`` field values wholesale,
   - regex-scrubs IBANs, JWTs, emails, phone numbers, AWS pre-signed
     URL query strings, and any 10+ digit run,
   - maps TR cash account numbers to consistent placeholders
     (``9000000001``, ``9000000002``, …) so cross-file references still
     match,
   - takes ``--also-redact "<string>"`` for anything the regexes can't
     infer (your real name and its variants, account labels, etc.).
     Matched case-insensitively. Repeat the flag per term.

   ```bash
   # Default in/out: examples/out/ → examples/out_redacted/
   python scripts/redact_dump.py \
       --also-redact "Jane Doe" \
       --also-redact "Jane-Doe" \
       --also-redact "DOE-J"
   ```

   The script prints a per-rule hit count and the cash-account-number
   mapping at the end. **Open the redacted JSONs and search for any
   remaining real names, IBAN fragments, or labels** — name variants
   (truncations, capitalizations, hyphenations) are the classic blind
   spot. Re-run with extra ``--also-redact`` flags until clean.

3. **Open an issue** on
   [github.com/hdecreis/libtrsync](https://github.com/hdecreis/libtrsync/issues)
   describing what you expected vs. what happened, and attach the
   relevant file(s) from `examples/out_redacted/`. A single offending
   `eventType` is often enough — pulling one item with
   `scripts/extract_fixture.py` (which also sanitizes) lets us turn it
   straight into a regression test.

## License

MIT. See [LICENSE](LICENSE).
