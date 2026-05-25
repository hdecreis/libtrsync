# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/hdecreis/libtrsync/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/hdecreis/libtrsync/releases/tag/v0.3.1
[0.3.0]: https://github.com/hdecreis/libtrsync/releases/tag/v0.3.0
[0.2.0]: https://github.com/hdecreis/libtrsync/releases/tag/v0.2.0
[0.1.0]: https://github.com/hdecreis/libtrsync/releases/tag/v0.1.0
