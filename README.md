# traderepublic-sync

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
    parse_euro,                # "1 000,00 EUR" → 1000.0
    parse_detail_sections,     # timelineDetailV2 dict → structured dict
    extract_isin_from_icon,    # "logos/FR0011550672/v2" → "FR0011550672"
)
```

## Layout

```
src/traderepublic_sync/
├── client.py       # TRClient (login, 2FA, websocket fetch)
├── waf.py          # AWS WAF token via Playwright or Selenium
├── parsing.py      # parse_euro, parse_detail_sections, ISIN extraction
├── state.py        # ConnectionState dataclass
├── constants.py    # API URLs, default headers, WS connect payload
├── exceptions.py   # TRAuthError
└── dual_legged/    # Optional dual-legged transaction mapping
    └── mapping.py
```

## License

MIT. See [LICENSE](LICENSE).
