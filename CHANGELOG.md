# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/hdecreis/libtrsync/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/hdecreis/libtrsync/releases/tag/v0.2.0
[0.1.0]: https://github.com/hdecreis/libtrsync/releases/tag/v0.1.0
