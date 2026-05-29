#!/usr/bin/env python3
"""Probe Trade Republic's authenticated REST endpoints.

The interesting figures TR computes server-side (realized P&L, dividend
return, lot cost basis, income analytics) live on plain HTTPS endpoints under
``https://api.traderepublic.com`` — *not* the WebSocket. See
``docs/tr-undocumented-api.md`` for the catalogue. This script fires one
authenticated request and pretty-prints the JSON so those endpoints can be
confirmed against a live session before anyone depends on them.

It is the REST counterpart to ``trdump probe`` (which speaks the WebSocket).
For the FX rates — which *are* on the WebSocket, as ``ticker`` subs against the
synthetic LSX instruments — use trdump instead, e.g.::

    trdump probe ticker '{"id": "LS000IUSD006.LSX"}' --protocol 34

Auth is reused from trdump (``~/.config/trdump/session.json`` + credentials),
so this must run somewhere both ``trdump`` and ``traderepublic_sync`` import —
the simplest being trdump's own venv::

    python /path/to/libtrsync/scripts/probe_rest.py pnl   --account 1 --instrument US0378331005
    python .../probe_rest.py positions --account 1 --instrument US0378331005
    python .../probe_rest.py income-events   --account 1 --instrument US0378331005
    python .../probe_rest.py income-returns  --account 1 --instrument US0378331005 --shares 10 --amount 1000
    python .../probe_rest.py get  "/api/v2/taxes/calculations"            # arbitrary GET
    python .../probe_rest.py get  "/api/v2/taxes/pnl" -q secAccNo=ABC -q instrumentId=US0378331005
    python .../probe_rest.py post "/api/v1/.../returns" --body '{"size":"10","amount":{...}}'

``--account N`` is 1-indexed against ``accountPairs`` order and auto-fills the
securities account number; pass ``--sec-acc-no`` to set it explicitly.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys


def _authenticate(locale: str):
    """Return an authenticated ``(TRClient, session_token)``.

    Prefer trdump's ``authenticate()`` — it owns the credential prompt, the
    cached ``session.json``, the WAF refresh and the no-2FA session refresh.
    """
    try:
        from trdump.auth import authenticate
    except ImportError as e:
        sys.exit(
            f"Could not import trdump.auth ({e}).\n"
            "Run this from an environment where both 'trdump' and "
            "'traderepublic_sync' are importable (trdump's venv is easiest), "
            "since auth/session handling is reused from trdump."
        )
    return authenticate(locale=locale)


def _resolve_sec_acc_no(client, session_token: str, account_idx: int) -> str:
    """Map a 1-indexed account number to its securitiesAccountNumber."""
    pairs = asyncio.run(client.fetch_account_pairs(session_token))
    if not pairs:
        sys.exit("accountPairs returned no accounts — can't resolve --account.")
    if not (1 <= account_idx <= len(pairs)):
        sys.exit(f"--account {account_idx}: only {len(pairs)} account(s) available.")
    sec = pairs[account_idx - 1].get("securitiesAccountNumber")
    print(f"  → secAccNo auto-filled: {sec}", file=sys.stderr)
    return sec


def _request(client, method: str, path: str, params, body, timeout: float) -> tuple[int, object]:
    """Fire one authenticated request via the TRClient's cookie jar + headers."""
    from traderepublic_sync import TR_API_BASE

    url = TR_API_BASE + path if path.startswith("/") else f"{TR_API_BASE}/{path}"
    print(f"{method} {url}  params={params or {}}", file=sys.stderr)
    resp = client._http.request(
        method,
        url,
        headers=client._headers(),     # x-aws-waf-token + x-tr-device-info + defaults
        params=params or None,         # cookie jar (tr_session) rides on the session
        json=body if body is not None else None,
        timeout=timeout,
    )
    try:
        parsed = resp.json()
    except ValueError:
        parsed = {"_raw": resp.text}
    return resp.status_code, parsed


# ── preset endpoint builders ────────────────────────────────────────────────
#
# Each returns (method, path, query-params dict, json-body or None).

def _build_preset(name: str, args, sec_acc_no: str | None):
    if name == "pnl":
        _need(sec_acc_no, args.instrument, name)
        return "GET", "/api/v2/taxes/pnl", {
            "secAccNo": [sec_acc_no], "instrumentId": args.instrument,
        }, None

    if name == "positions":
        _need(sec_acc_no, args.instrument, name)
        return "GET", "/api/v2/taxes/positions", {
            "secAccNo": sec_acc_no, "instrumentId": args.instrument,
            "pageNumber": args.page, "pageSize": args.page_size,
        }, None

    if name == "income-events":
        _need(sec_acc_no, args.instrument, name)
        path = f"/api/v1/income-analytics/incomes/{sec_acc_no}/instrument/{args.instrument}/events-screen"
        return "GET", path, {"size": args.size}, None

    if name == "income-returns":
        _need(sec_acc_no, args.instrument, name)
        if args.shares is None or args.amount is None:
            sys.exit("income-returns needs --shares and --amount.")
        path = f"/api/v1/income-analytics/incomes/{sec_acc_no}/instruments/{args.instrument}/returns"
        body = {
            "size": str(args.shares),
            "amount": {"value": str(args.amount), "currency": args.currency},
            "totalPrice": {"value": str(args.amount), "currency": args.currency},
        }
        return "POST", path, None, body

    sys.exit(f"Unknown preset {name!r}.")


def _need(sec_acc_no, instrument, name):
    if not sec_acc_no:
        sys.exit(f"{name}: needs a securities account — pass --account N or --sec-acc-no.")
    if not instrument:
        sys.exit(f"{name}: needs --instrument <ISIN>.")


def _parse_q(pairs: list[str] | None) -> dict:
    """Parse repeated ``-q key=value`` into a dict (repeated keys → list)."""
    out: dict = {}
    for item in pairs or []:
        if "=" not in item:
            sys.exit(f"--query must be key=value, got {item!r}")
        k, v = item.split("=", 1)
        if k in out:
            out[k] = (out[k] if isinstance(out[k], list) else [out[k]]) + [v]
        else:
            out[k] = v
    return out


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "endpoint",
        help="preset (pnl | positions | income-events | income-returns) "
             "or a raw verb (get | post)",
    )
    p.add_argument("path", nargs="?", help="raw path for get/post (e.g. /api/v2/taxes/calculations)")

    p.add_argument("--account", type=int, help="1-indexed account; auto-fills secAccNo")
    p.add_argument("--sec-acc-no", help="explicit securities account number")
    p.add_argument("--instrument", help="instrument ISIN (e.g. US0378331005)")

    p.add_argument("--page", type=int, default=0, help="positions: pageNumber (default 0)")
    p.add_argument("--page-size", type=int, default=20, help="positions: pageSize (default 20)")
    p.add_argument("--size", default="1", help="income-events: size (default 1)")
    p.add_argument("--shares", help="income-returns: position size")
    p.add_argument("--amount", help="income-returns: invested amount")
    p.add_argument("--currency", default="EUR", help="income-returns: currency (default EUR)")

    p.add_argument("-q", "--query", action="append", help="raw mode: repeatable key=value")
    p.add_argument("--body", help="raw post: JSON body string")

    p.add_argument("--locale", default="fr")
    p.add_argument("--timeout", type=float, default=15.0)
    args = p.parse_args()

    client, session_token = _authenticate(args.locale)

    sec_acc_no = args.sec_acc_no
    if sec_acc_no is None and args.account is not None:
        sec_acc_no = _resolve_sec_acc_no(client, session_token, args.account)

    verb = args.endpoint.lower()
    if verb in ("get", "post"):
        if not args.path:
            sys.exit(f"{verb}: a raw path is required (e.g. {verb} /api/v2/taxes/calculations)")
        body = json.loads(args.body) if args.body else None
        method, path, params, body = verb.upper(), args.path, _parse_q(args.query), body
    else:
        method, path, params, body = _build_preset(verb, args, sec_acc_no)

    status, parsed = _request(client, method, path, params, body, args.timeout)
    print(f"HTTP {status}", file=sys.stderr)
    json.dump(parsed, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0 if 200 <= status < 300 else 2


if __name__ == "__main__":
    raise SystemExit(main())
