"""Probe the authenticated REST endpoints documented in docs/tr-undocumented-api.md.

Reuses a cached session (no 2FA) and exercises:
  - ``GET /api/v2/taxes/pnl`` for an ISIN (realized P&L + dividend return), and
  - the LSX ``ticker`` FX rate for a currency (default USD).

Usage::

    python scripts/probe_rest.py US0378331005            # pnl across all accounts + USD FX
    python scripts/probe_rest.py US0378331005 --account 0
    python scripts/probe_rest.py NL0010273215 --currency GBP

The session file defaults to ``~/.config/libtrsync/session.json`` (override
with ``LIBTRSYNC_SESSION``) — run ``examples/smoke_fetch_all.py`` first to
authenticate.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from traderepublic_sync import ConnectionState, TRAuthError, TRClient  # noqa: E402

SESSION_FILE = Path(
    os.environ.get("LIBTRSYNC_SESSION") or Path.home() / ".config/libtrsync/session.json"
)


def load_state() -> ConnectionState | None:
    if not SESSION_FILE.exists():
        return None
    try:
        return ConnectionState(**json.loads(SESSION_FILE.read_text()))
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("isin", help="Bare ISIN, e.g. US0378331005")
    parser.add_argument(
        "--account", type=int, default=None,
        help="0-indexed securities account (default: all accounts).",
    )
    parser.add_argument("--currency", default="USD", help="FX currency to probe (default USD).")
    args = parser.parse_args()

    saved = load_state()
    if not saved or not saved.session_token:
        sys.exit(
            f"No saved session at {SESSION_FILE}. "
            "Run examples/smoke_fetch_all.py first to authenticate."
        )

    client = TRClient.from_state(saved)

    try:
        pairs = asyncio.run(client.fetch_account_pairs(saved.session_token))
        sec_accs = [p["securitiesAccountNumber"] for p in pairs if p.get("securitiesAccountNumber")]
        if args.account is not None:
            if not (0 <= args.account < len(sec_accs)):
                sys.exit(f"--account {args.account}: only {len(sec_accs)} account(s).")
            sec_accs = [sec_accs[args.account]]

        print(f"== taxes/pnl  instrumentId={args.isin}  secAccNo={sec_accs} ==")
        pnl = client.fetch_realized_pnl(args.isin, sec_acc_nos=sec_accs)
        print(json.dumps(pnl, indent=2, ensure_ascii=False) if pnl else "(empty — 404; equities/funds only)")

        print(f"\n== FX  {args.currency} (LSX ticker mid) ==")
        fx = asyncio.run(client.fetch_fx_rate(args.currency, saved.session_token))
        print(json.dumps(fx, indent=2) if fx else f"(no rate for {args.currency}; USD/GBP/CHF/JPY only)")
    except TRAuthError as e:
        sys.exit(f"Auth failed — session probably expired. Re-run smoke_fetch_all.py. ({e})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
