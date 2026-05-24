"""Live test: dump accounts, assets, and transactions as JSON.

Session state is persisted to .libtrsync (JSON) so that subsequent runs
reuse the existing WAF token + session token and skip login/2FA.
"""
import asyncio
import json
import logging
import os
import sys
from dataclasses import asdict

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from traderepublic_sync import ConnectionState, TRClient, TRAuthError

STATE_FILE = os.path.join(ROOT, ".libtrsync")
_config_path = os.path.join(ROOT, ".testconfig")
if not os.path.exists(_config_path):
    print(f"Missing {_config_path} — create it with {{\"phone\": \"+33...\", \"pin\": \"1234\"}}", file=sys.stderr)
    sys.exit(1)
with open(_config_path) as _f:
    _cfg = json.load(_f)
PHONE: str = _cfg["phone"]
PIN: str = _cfg["pin"]
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")


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


# ── Try to resume a saved session ────────────────────────────────────────────

saved = load_state()
session_token = None
client = None

if saved and saved.session_token:
    if not saved.is_waf_valid():
        print("WAF token expired, re-authenticating...", file=sys.stderr)
    else:
        print("Found saved session, trying to reuse...", file=sys.stderr)
        client = TRClient(waf_token=saved.waf_token, device_info=saved.device_info, locale=saved.locale)
        try:
            async def _validate():
                await asyncio.wait_for(client.fetch_account_list(saved.session_token), timeout=8.0)
            asyncio.run(_validate())
            session_token = saved.session_token
            print("Session still valid, skipping login.", file=sys.stderr)
        except (TRAuthError, asyncio.TimeoutError):
            print("Saved session expired, re-authenticating...", file=sys.stderr)
            client = None

# ── Full login flow if no valid session ───────────────────────────────────────

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

    if len(sys.argv) > 1:
        code = sys.argv[1].strip()
        print(f"Using 2FA code: {code}", file=sys.stderr)
    else:
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

# ── Fetch everything ──────────────────────────────────────────────────────────

accounts = asyncio.run(client.fetch_account_list(session_token))
print("Accounts fetched.", file=sys.stderr)

assets = asyncio.run(client.fetch_asset_list(session_token))
print("Assets fetched.", file=sys.stderr)

result = asyncio.run(client.fetch_transactions(session_token))
print("Transactions fetched.", file=sys.stderr)

# ── Dump JSON files ───────────────────────────────────────────────────────────

os.makedirs(OUT_DIR, exist_ok=True)

def dump(filename, data):
    path = os.path.join(OUT_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"Written {path} ({len(data)} items)", file=sys.stderr)

dump("accounts.json", accounts)
dump("assets.json", assets)
dump("transactions_raw.json", result["raw_items"])
dump("transactions_dl.json", result["transactions"])
