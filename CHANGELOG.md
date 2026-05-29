# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.1] - 2026-05-29

Made the snapshot call more efficient. It was lagging because:
- bad code fetching multiple times the same endpoint
- 1s timer wrongly placed on ticker calls
- listing of the fully-sold assets was searching through ALL transactions,
  now only from "SELL" events (for stocks&etfs), then fetch the pnl from tax
  informations. for crypto and bonds (no tax information), we still have to
  fetch the full transactions, but only on those assets.

### Changed
- **`fetch_transactions` (`client.py`)** - two new optional params, default None
  so existing callers (incl. trdump fetch) are byte-identical:
  - event_types → injects the types wire key
  - categories → injects the categoryIds wire key
- Optimization only: **Snapshot path (`portfolio.py`)**
  `realized_pnl` and `sold_assets` now replace the single unbounded n-item walk
  with two narrow filtered walks:
  1. event_types=["TRADING_TRADE_EXECUTED","PRIVATE_MARKET_FUND_TRADE_EXECUTED"]
     → sold-off detection across all classes (~37 items)
  2. categories=["CRYPTO","BOND"]
     → complete history for the classes TR 404s on taxes/pnl, so the timeline
       fallback keeps a full cost basis (~3 items)

## [0.5.0] - 2026-05-29

Pushes portfolio *computation* into the library: a new
`traderepublic_sync.v1` facade returns typed, EUR-correct figures, plus new
low-level endpoints for realized P&L and FX. Lets downstream consumers (e.g.
`trdump`) drop their hand-rolled valuation/FX/aggregation.

### Added
- **`traderepublic_sync.v1` facade** — `Portfolio` (one-time:
  `accounts` / `cash` / `positions` / `quote` / `transactions` / `fx_rates`
  / `realized_pnl` / `sold_assets` / `snapshot`) and `PortfolioStream`
  (live: `prices` / `positions` / `cash` / `transactions` / `fx`), with
  frozen typed models (`Position`, `Money`, `FxRate`, `RealizedPnl`,
  `SoldAsset`, `PortfolioSnapshot`, …). Positions are valued **in EUR**:
  bonds divided by 100 (percent-of-par) and FX-converted, `averageBuyIn`
  kept as EUR, unpriced positions falling back to cost basis. PE **held vs
  committed** are separate fields; `snapshot(include_committed=False)`
  controls only whether uncalled capital counts toward headline totals.
- **`TRClient.fetch_realized_pnl(instrument_id, sec_acc_nos=None)`** —
  server-computed realized P&L + dividend return via
  `GET /api/v2/taxes/pnl` (equities/funds; 404 → empty for crypto/bonds).
- **`TRClient.fetch_fx_rate` / `fetch_fx_rates`** + `fx_mid()` — EUR
  conversion rates from TR's own LSX `ticker` instruments (USD/GBP/CHF/JPY),
  mid of bid/ask, 6dp HALF_EVEN.
- **`TRClient.fetch_private_markets(sec_acc_no)`** — Private-Markets
  positions (incl. `pendingAmounts`) for committed-capital valuation.
- **`TRClient.from_state()`**, **`resume_session()`** — a generic
  session-resume policy (JWT reuse → WAF refresh → no-2FA refresh → full
  login) with filesystem/UI left to callbacks.
- **`ConnectionState.is_session_valid()` / `session_expiry_from_token()`** —
  the JWT-expiry twin of the existing WAF helpers.
- **`TRClient._rest_request()`** — authenticated REST plumbing reusing the
  cookie jar + WAF headers, with WAF-refresh and no-2FA session-refresh
  retries.
- **`scripts/probe_rest.py`** and **`docs/tr-undocumented-api.md`**.

### Changed
- **`TRSession.subscribe_cash`** now filters by `accountNumber` (not `id`),
  fixing TR's DEFAULT-scoping quirk that returned the primary account's
  cash for every subscription. New `TRSession.subscribe_fx`.
- **`TRClient.fetch_asset_list(sec_acc_no=...)`** — optional per-account
  parameter (default unchanged). Its raw metrics remain quote-currency;
  use the `v1` `Portfolio` for EUR-correct value/P&L.
- **`TRClient.fetch_cash_balance(account_number=...)`** — optional scoping.

## [0.4.1] - 2026-05-29

Adds a no-2FA session-refresh path so long-lived consumers stop hitting
2FA every time the ~5-minute `tr_session` JWT lapses.

### Added
- **Pluggable auth strategies** (`auth.py`): `AuthStrategy` ABC with
  `WebRefreshAuth` (the default) and a `DeviceKeyAuth` stub. Select via
  `TRClient(auth=...)`.
- **`TRClient.refresh_session()`** — mints a fresh `tr_session` with no
  2FA via `GET /api/v1/auth/web/session`, for as long as TR's refresh
  cookie is valid. Raises `SessionExpired` when a no-2FA refresh is no
  longer possible (refresh cookie expired/revoked → re-run
  `login()` + `verify_2fa()`).
- **Cookie-jar retention**: `TRClient` now keeps a `requests.Session`, so
  the refresh cookie set at `verify_2fa` time survives. `dump_cookies()` /
  `load_cookies()` and a new `ConnectionState.session_cookies` field
  persist the jar across processes alongside `session_token`.
- **`TRSession` proactive refresh**: `open_session(..., auto_refresh=True,
  refresh_interval=270.0)` runs a background task that refreshes the token
  just under its expiry and feeds it to live subscriptions — the session
  stays valid indefinitely without 2FA. The reader loop also attempts a
  refresh before tearing down on a `SessionExpired` frame.
- 11 tests covering refresh, cookie round-trip, the `DeviceKeyAuth` stub,
  and the `TRSession` refresh helpers.

### Changed
- `TRClient` REST calls (`login`, `request_sms`, `verify_2fa`) now go
  through the shared `requests.Session` so cookies accumulate across the
  login flow. Tests that monkeypatched module-level `requests.post` now
  patch `requests.Session.post`.

### Notes for consumers
- `DeviceKeyAuth` (the durable device-key/ECDSA flow) is a documented
  plugin point, not yet implemented — it would log you out of your phone
  (TR allows one paired device at a time), so `WebRefreshAuth` is the
  default and recommended path.

## [0.4.0] — 2026-05-27

Adopts TR's V2 portfolio subscription (`compactPortfolioByTypeV2`) — now
the only sub that surfaces crypto positions — and bumps the WebSocket
handshake to protocol 34, which V2-class subs require. Includes one
observable callback shape change for consumers of `subscribe_portfolio`.

### Changed
- **BREAKING:** `TRSession.subscribe_portfolio` now subscribes to
  `compactPortfolioByTypeV2` instead of the legacy `compactPortfolio`.
  Callbacks receive the V2 envelope
  (`{"categories": [{"categoryType": ..., "positions": [...]}, ...]}`)
  instead of the legacy flat shape (`{"positions": [...]}`). Migration:
  pass each frame through the new
  `traderepublic_sync.client._flatten_portfolio_positions(data)` helper
  for a normalized flat list, or index `data["categories"]` directly.
- **Behaviour:** `TRClient.fetch_asset_list` now uses
  `compactPortfolioByTypeV2` internally too. The returned dicts have
  the same keys as before, but `asset_name` may differ for some
  instruments because V2 carries an inline `name` (the same string the
  TR mobile app shows) and we use it instead of issuing the per-position
  `instrument` round trip. Examples observed in the wild:
  `"ISHARES PHYSICAL GOLD"` → `"Physical Gold USD (Acc)"`,
  `"iShares VII plc - iShares Core S&P 500 UCITS ETF USD (Acc)"` →
  `"Core S&P 500 USD (Acc)"`,
  `"TAIWAN SEMICON.MANU.ADR/5"` → `"TSMC (ADR)"`. Field name, type, and
  position in the dict are unchanged.
- **Internal:** every WebSocket handshake in `client.py` and
  `session.py` is now `connect 34` (was `connect 31`). V2-class subs
  require protocol 34; protocol 31 silently drops them. `fetch_account_pairs`
  was already on 34. Fully transparent unless a consumer was monkey-patching
  the connect frame.

### Added
- `_flatten_portfolio_positions(portfolio)` — private helper that
  normalises both the V2 (`categories[].positions`) and legacy
  (`positions`) responses into a flat list of dicts with stable keys.
  Each V2 position is normalised so `instrumentId` aliases `isin`,
  `averageBuyIn` is the unwrapped scalar (with `averageBuyInCurrency`
  exposed separately), and `_category` tags the source bucket.
- Two new keys on every `fetch_asset_list` asset dict:
  `instrument_type` (V2: `stock` / `fund` / `crypto` / `privateFund`)
  and `category` (V2: `stocksAndETFs` / `cryptos` / `privateMarkets`).
  Both are `None` when the legacy sub is used.
- `examples/probe_portfolio_v2.py` — diagnostic script that reuses the
  cached session, captures the raw V2 portfolio response, the legacy
  response, and a side-by-side check of the BTC home-exchange + ticker
  chain on both one-shot and long-lived `TRSession` paths. Useful for
  reverse-engineering future TR response-shape changes without needing
  another 2FA round.
- 5 new tests covering the V2 / legacy normalisation, including a smoke
  test on the exact V2 shape observed live and the alternate `instruments`
  key some TR app versions use.

### Performance
- `fetch_asset_list` skips the per-position `instrument` round trip when
  V2 already carries `name`. For an 8-position portfolio that's 8 fewer
  WS sub/unsub cycles — a meaningful saving when the call is wired into
  a polling loop.

### Notes for consumers
- Consumers of `subscribe_portfolio` are the only ones affected. If you
  read `data["positions"]` inside your callback, you now get `None`.
  Wrap with `_flatten_portfolio_positions(data)` to keep the previous
  shape, or migrate to the categorised view.
- The auth state of `compactPortfolioByTypeV2` is occasionally rejected
  by TR with `AUTHENTICATION_ERROR / source=MAPPER` even on a valid
  session token (other subs on the same WS work fine). The library
  surfaces this as the usual `{"_error": True, "data": {...}}` envelope
  on the callback — make sure your `on_*` callbacks check for `_error`
  before processing.

## [0.3.1] — 2026-05-25

Patch release: scrub an AWS pre-signed URL that slipped into one of
the new PEA fixtures shipped in 0.3.0 (GitHub secret-scanning flagged
the `X-Amz-Credential` parameter), and harden both extraction scripts
so the same class of leak cannot recur.

### Fixed
- `tests/fixtures/pea_purchase.json` had a real AWS pre-signed S3 URL
  (with `X-Amz-Security-Token`, `X-Amz-Credential` and `X-Amz-Signature`)
  in a `document_urls` entry. Query string stripped in place; no other
  fixture is affected.
- `tests/fixtures/card_expense.json` carried a real payment-card last-4
  (`··7892`, Google Pay row) inherited from the original 0.2.0 fixture
  set. Card last-4 isn't PCI-sensitive on its own, but a public fixture
  should be impersonal — rewritten to `··0000`.

### Changed
- `scripts/redact_dump.py` and `scripts/extract_fixture.py` now apply
  three additional regex scrubs in every string they touch:
  - `\b(?:ASIA|AKIA)[A-Z0-9]{16,}\b` — STS-temporary and IAM-static
    AWS access key IDs, replaced with `AKIAREDACTEDAWSKEY00`.
  - `X-Amz-Credential=…` parameter (case-insensitive), replaced with
    `X-Amz-Credential=REDACTED`.
  - `[·•\.\*]{2,}[A-Z0-9]{2,6}` — TR-style masked tails such as
    `··7892` (card last-4) or `..4118` (IBAN tail). The leading
    masking chars are preserved; the trailing identifier is replaced
    with zeros (so the value still ``looks`` like what the parser
    would see).

  The AWS rules are belt-and-braces on top of the existing
  URL-query-string rule — they catch the same identifiers even when
  the surrounding URL has been URL-decoded, partially redacted, or
  only appears in a log line / error message that secret scanning
  would still alert on.
- `.gitleaks.toml` allowlists path-scope the `aws-access-token` rule
  for `scripts/redact_dump.py`, `scripts/extract_fixture.py` and
  `CHANGELOG.md` (the regex patterns and the `AKIAREDACTEDAWSKEY00`
  placeholder are intentional, not real credentials). A defensive
  global allowlist pins the placeholder string in case gitleaks ever
  renames the rule.

## [0.3.0] — 2026-05-25

Hardens the auth lifecycle for long-running consumers, makes
`fetch_transactions` cheap to run incrementally, fixes a silent
data-loss bug in the PEA dedup path, and ships a redaction tool so
users can attach safe dumps to bug reports.

### Added
- **Typed exception hierarchy** so consumers don't have to parse HTTP
  status codes or WS frame bodies to decide what to do:
  - `TRError` (new root) → `TRAuthError` → `WafExpired`, `SessionExpired`
  - `TRError` → `TransientError`
  - `TRAuthError` is kept so existing `except TRAuthError:` blocks still
    catch WAF/session lifecycle errors.
- **`on_waf_expired` / `on_session_expired` hooks** on `TRClient` (and
  forwarded to `TRSession` via `open_session`). The WAF hook may be
  sync or async, returns a fresh token (or `None`), and the library
  retries the failing operation exactly once. The session hook is
  notification-only — re-acquiring a session requires 2FA which the
  library cannot drive on its own.
- **Auto-reconnect for `TRSession`** (`auto_reconnect=True`). The reader
  loop reconnects on `ConnectionClosed`, replays every live
  subscription on the fresh socket, and applies exponential backoff
  capped at `reconnect_max_backoff` (default 60s). Optional
  `on_reconnect` callback fires after a successful reconnect.
- **`fetch_transactions(since=, until=, since_id=)`** for lean
  incremental sync. TR's newest-first cursor pagination is leveraged
  to early-stop the walk and skip per-item `timelineDetailV2` round
  trips for items outside the window. Typical daily sync now touches
  one or two pages instead of the whole history. Accepts `datetime`
  (naive treated as UTC) or ISO-8601 strings; `since_id` is matched
  via `normalize_tr_id` so either the raw or normalized form works.
- **`scripts/redact_dump.py`** — anonymizes a `examples/out/` folder
  (or any directory of JSON dumps) so users can attach the result to
  bug reports. Field-name and regex rules cover sender / IBAN /
  email / phone / JWT / AWS pre-signed URL parameters; TR cash account
  numbers get a deterministic placeholder mapping that stays
  consistent across files; `--also-redact STRING` (repeatable,
  case-insensitive) handles real names and any variant the regexes
  cannot infer.
- New "Reporting bugs" section in the README documenting the
  smoke-fetch → redact → attach workflow.
- New private `_classify.py` module: `classify_http`,
  `classify_ws_connect_error`, `classify_ws_error_frame` — shared
  vocabulary used by both `TRClient` and `TRSession`.
- `session_token` keyword argument to `TRClient.__init__` so a
  previously-acquired token can be supplied at construction time
  instead of via every fetch call.
- Tolerant ISO-8601 timestamp parser handling TR's three observed
  variants (`+0000`, `Z`, microsecond `Z`).
- Two new fixtures (`pea_pay_in.json`, `pea_purchase.json`) extracted
  from real data and used to lock the PEA fix in place.
- 42 new tests (`test_classify.py`, `test_fetch_filters.py`, plus
  expanded `test_dual_legged.py`); suite is now 79 tests, all green.

### Changed
- **PEA event reclassification** — `PEA_SAVINGS_PLAN_PAY_IN` is now
  `TRANSFER` (was `PURCHASE`) and `PEA_DEPOSIT_DEBIT` is now
  `TRANSFER` (was `None` / skipped). These events represent the cash
  top-up from the user's external bank or CTO into PEA cash that
  funds an imminent trade, not the trade itself. **Output shape
  change** for consumers that import PEA savings plan / manual PEA
  trades: where they previously saw one merged event per day+ISIN,
  they will now see two — the inbound TRANSFER and the actual
  PURCHASE/SELL. The TRANSFER carries both `credit_amount` and
  `debit_amount` so consumers can route credit → PEA cash and
  debit → external/CTO without further guessing.
- **`deduplicate_pea` rewritten** — the previous "merge richer event"
  rule silently dropped real cash movements when a pay-in was
  smaller than the trade (e.g. 10.23 € pay-in + 7.40 € PEA residual
  → 17.63 € trade). The new implementation only collapses entries
  with the same `transaction_type` AND same primary amount on the
  same date+ISIN — the rare TR-side glitch case where two events
  are genuinely identical.
- REST helpers (`login`, `request_sms`, `verify_2fa`) now raise
  `WafExpired` / `SessionExpired` / `TransientError` / `TRAuthError`
  instead of a bare `TRAuthError` with HTTP details in the message.
- Every WS-fetching method now goes through a `_ws_session()` async
  context manager that handles WAF-aware connect retry uniformly.
- `_parse_ws_json` and `_ws_sub` detect TR error frames
  (`<id> E <body>`) and raise the right typed exception rather than
  silently returning `{}` (which previously made session-expired
  responses look like "no data").
- `TRClient.open_session()` accepts `auto_reconnect=` and
  `on_reconnect=` and forwards the client's `on_waf_expired` /
  `on_session_expired` hooks to the new session.

### Fixed
- PEA savings-plan and manual PEA trades no longer lose the inbound
  cash leg. See "Changed" above for the data-shape implication.
- Session-expired WS frames during `fetch_transactions` are now
  raised as `SessionExpired` instead of being silently swallowed and
  reported as "no transactions".
- A coroutine-returning `on_waf_expired` used on the sync REST path
  no longer deadlocks — it raises a clear `TRError` instructing the
  caller to provide a sync callback.

### Notes for libtrsync consumers
- Downstream code that catches `TRAuthError` continues to work
  unchanged (both new lifecycle exceptions inherit from it). Code
  that wants to distinguish the recovery paths should catch the
  specific subclasses.
- Consumers of `dual_legged` who relied on the old PEA-merged output
  may need to update their import / dedup logic to handle the
  TRANSFER row that now accompanies each PEA savings-plan PURCHASE.

## [0.2.0] — 2026-05-25

First useful release. Pulls the initial 0.1.0 tag forward with a real
test suite, CI, documentation, and the polish needed to be PyPI-ready.

### Added
- Sanitized fixture-based test suite (37 tests covering parsing +
  dual-legged mapping). Pure-logic helpers and the mapping layer are
  now regression-protected.
- GitHub Actions CI: `ruff check` + `pytest` on Python 3.11 / 3.12 / 3.13.
- Dependabot config for GitHub Actions and pip updates.
- `.pre-commit-config.yaml` running gitleaks, with a scoped `.gitleaks.toml`
  allowlist for TR chat-flow identifiers in fixtures.
- `scripts/extract_fixture.py` — one-shot tool for capturing sanitized
  fixtures from a real TR dump.
- Live `TRSession` documentation in README (TRSession itself shipped in
  0.1.0 but was undocumented).
- `Source` and `Changelog` URLs in PyPI project metadata.
- Python 3.13 classifier.
- `CHANGELOG.md`.
- `logging.NullHandler()` attached to the package logger so consumers
  without configured logging stay quiet.

### Changed
- **BREAKING:** `parse_euro` renamed to `parse_currency_amount` and
  broadened to handle US-format numbers, ISO codes
  (EUR / USD / CAD / CHF / GBP), and additional whitespace characters.
- `__version__` now reads from package metadata via
  `importlib.metadata.version` instead of being hardcoded — single
  source of truth lives in `pyproject.toml`.
- License metadata migrated to PEP 639:
  `license = "MIT"` (SPDX expression) + explicit
  `license-files = ["LICENSE"]`. Build now requires
  `setuptools>=77` (was `>=68`).
- Removed legacy `License :: OSI Approved :: MIT License` classifier
  (PEP 639 disallows combining SPDX license with the classifier form).

### Removed
- `requirements.txt` — `pyproject.toml` is now the single source of
  truth for library dependencies.

## [0.1.0] — 2026-05-24

Initial alpha release (tagged, never published to PyPI).

### Added
- `TRClient` — REST login + 2FA, one-shot WebSocket fetches:
  `fetch_transactions`, `fetch_cash_balance`, `fetch_account_list`,
  `fetch_asset_list`, `fetch_ticker`.
- `TRSession` — long-lived WebSocket with callback-based subscriptions:
  `subscribe_ticker`, `subscribe_portfolio`, `subscribe_cash`,
  `subscribe_transactions`, `search_instrument`.
- AWS WAF token acquisition via Playwright or Selenium (`waf.py`).
- `ConnectionState` dataclass for persisting session across processes,
  with WAF token expiry tracking.
- Pure parsing utilities (`parse_euro`, `parse_detail_sections`,
  `extract_isin_from_icon`, `normalize_tr_id`).
- Optional `dual_legged` submodule mapping TR events to a double-entry
  transaction schema (PURCHASE / SELL / DIVIDEND / TRANSFER / INTEREST /
  EXPENSE / FEE / CUSTOM) with explicit credit / debit / fee / tax legs.
- `deduplicate_pea` helper for collapsing TR's PEA mirror event pairs.
- Type information (`py.typed` marker shipped in the wheel).

[Unreleased]: https://github.com/hdecreis/libtrsync/compare/v0.5.1...HEAD
[0.5.1]: https://github.com/hdecreis/libtrsync/releases/tag/v0.5.1
[0.5.0]: https://github.com/hdecreis/libtrsync/releases/tag/v0.5.0
[0.4.1]: https://github.com/hdecreis/libtrsync/releases/tag/v0.4.1
[0.4.0]: https://github.com/hdecreis/libtrsync/releases/tag/v0.4.0
[0.3.1]: https://github.com/hdecreis/libtrsync/releases/tag/v0.3.1
[0.3.0]: https://github.com/hdecreis/libtrsync/releases/tag/v0.3.0
[0.2.0]: https://github.com/hdecreis/libtrsync/releases/tag/v0.2.0
[0.1.0]: https://github.com/hdecreis/libtrsync/releases/tag/v0.1.0
