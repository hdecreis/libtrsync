"""Live test: search for an asset by name/ISIN and stream its ticker.

Usage:
    python test_live_track_asset.py <query> [type]

    type: crypto | stock | etf | bond | derivative | fund

Session state is persisted to ~/.config/libtrsync/session.json
and test configuration to ~/.config/libtrsync/testconfig.json
so that subsequent runs reuse the existing WAF token + session
token and skip login/2FA.

Examples:
    python test_live_track_asset.py bitcoin crypto
    python test_live_track_asset.py apple stock
    python test_live_track_asset.py "MSCI World" etf
    python test_live_track_asset.py XF000BTC0017
"""
import asyncio
import json
import logging
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from traderepublic_sync import ConnectionState, TRClient, TRAuthError

config_path = Path(
    os.environ.get("LIBTRSYNC_TESTCONFIG")
    or Path.home() / ".config/libtrsync/testconfig.json"
)

session_path = Path(
    os.environ.get("LIBTRSYNC_SESSION")
    or Path.home() / ".config/libtrsync/session.json"
)


STATE_FILE = session_path
if not os.path.exists(config_path):
    print(f"Missing {config_path} — create it with {{\"phone\": \"+33...\", \"pin\": \"1234\"}}", file=sys.stderr)
    sys.exit(1)
with open(config_path) as _f:
    _cfg = json.load(_f)
PHONE: str = _cfg["phone"]
PIN: str = _cfg["pin"]

if len(sys.argv) < 2:
    print("Usage: python test_live_track_asset.py <query> [type]", file=sys.stderr)
    sys.exit(1)

QUERY = sys.argv[1]
INSTRUMENT_TYPE = sys.argv[2] if len(sys.argv) > 2 else None


def load_state() -> ConnectionState | None:
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE) as f:
            return ConnectionState(**json.load(f))
    except Exception:
        return None


def save_state(state: ConnectionState) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(asdict(state), f, indent=2)
    print(f"Session saved to {STATE_FILE}", file=sys.stderr)


# ── Auth ──────────────────────────────────────────────────────────────────────

saved = load_state()
session_token = None
client = None

if saved and saved.session_token and saved.is_waf_valid():
    print("Trying saved session...", file=sys.stderr)
    client = TRClient(waf_token=saved.waf_token, device_info=saved.device_info, locale=saved.locale)
    try:
        async def _validate():
            await asyncio.wait_for(client.fetch_account_list(saved.session_token), timeout=8.0)
        asyncio.run(_validate())
        session_token = saved.session_token
        print("Session reused.", file=sys.stderr)
    except (TRAuthError, asyncio.TimeoutError):
        print("Saved session expired, re-authenticating...", file=sys.stderr)
        client = None

if session_token is None:
    client = TRClient(
        device_info=saved.device_info if saved else None,
        locale=saved.locale if saved else "fr",
    )
    print("Acquiring WAF token...", file=sys.stderr)
    client.acquire_waf_token("playwright")

    print("Logging in...", file=sys.stderr)
    login = client.login(phone_number=PHONE, pin=PIN)
    print(f"2FA requested. Countdown: {login['countdown']}s", file=sys.stderr)

    code = input("Enter 2FA code: ").strip()
    session_token = client.verify_2fa(login["process_id"], code)
    print("Authenticated.", file=sys.stderr)

    save_state(ConnectionState(
        phone_number=PHONE,
        pin=PIN,
        locale=client.locale,
        waf_token=client.waf_token,
        waf_expires_at=ConnectionState.waf_expiry_from_token(client.waf_token),
        device_info=client.device_info,
        session_token=session_token,
        auth_status="authenticated",
    ))

# ── Subscribe ─────────────────────────────────────────────────────────────────

async def run():
    async with client.open_session(session_token) as session:
        print(f'\nSearching for "{QUERY}"...', file=sys.stderr)
        results = await session.search_instrument(QUERY, instrument_type=INSTRUMENT_TYPE)
        if not results:
            print(f"No results found for {QUERY!r}.", file=sys.stderr)
            return

        # Pick the first result and show alternatives
        hit = results[0]
        isin = hit.get("isin") or hit.get("instrumentId")
        name = hit.get("name") or hit.get("shortName") or isin
        print(f'Using: {name} ({isin})', file=sys.stderr)
        if len(results) > 1:
            others = ", ".join(
                f"{r.get('name', '?')} ({r.get('isin', '?')})" for r in results[1:4]
            )
            print(f"Other matches: {others}", file=sys.stderr)

        def on_tick(data):
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            last = (data.get("last") or {}).get("price", "—")
            bid  = (data.get("bid")  or {}).get("price", "—")
            ask  = (data.get("ask")  or {}).get("price", "—")
            pre  = (data.get("pre")  or {}).get("price", "—")
            if last != "—" and pre not in ("—", None) and pre != 0:
                pct = (float(last) - float(pre)) / float(pre) * 100
                trend = f"{pct:+.2f}%"
            else:
                trend = "—"
            print(f"[{ts}] {name}  last={last}  bid={bid}  ask={ask}  prev_close={pre}  ({trend})")

        print(f"Subscribing to ticker...\n", file=sys.stderr)
        sub_id = await session.subscribe_ticker(isin, on_tick)
        print(f"Subscribed (sub_id={sub_id}). Press Ctrl+C to stop.\n", file=sys.stderr)
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            pass
        finally:
            await session.unsubscribe(sub_id)

try:
    asyncio.run(run())
except KeyboardInterrupt:
    print("\nStopped.", file=sys.stderr)
