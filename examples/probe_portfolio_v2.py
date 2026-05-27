"""Diagnostic probe: dump the raw ``compactPortfolioByTypeV2`` response.

Reuses the session cached by ``smoke_fetch_all.py`` (so no 2FA), opens
a WebSocket, runs both ``accountPairs`` and ``compactPortfolioByTypeV2``,
and writes the verbatim subscription payloads to
``examples/out/portfolio_v2_raw.json``.

Purpose: figure out the real shape of the V2 response so
``_flatten_portfolio_positions`` in ``src/traderepublic_sync/client.py``
can be fixed. Inspect the dumped JSON's top-level keys (and the first
category's structure) and report back.
"""

import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import asdict
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import websockets  # noqa: E402

from traderepublic_sync import ConnectionState, TRAuthError, TRClient  # noqa: E402
from traderepublic_sync.constants import TR_WS_URL, WS_CONNECT_PAYLOAD  # noqa: E402


SESSION_FILE = Path(
    os.environ.get("LIBTRSYNC_SESSION")
    or Path.home() / ".config/libtrsync/session.json"
)
ACCOUNTS_PATH = Path(__file__).resolve().parent / "out" / "accounts.json"
OUT_PATH = Path(__file__).resolve().parent / "out" / "portfolio_v2_raw.json"


def _cached_securities_account() -> str | None:
    """Read securitiesAccountNumber from a previous smoke_fetch_all run."""
    if not ACCOUNTS_PATH.exists():
        return None
    try:
        for entry in json.loads(ACCOUNTS_PATH.read_text()):
            sec = entry.get("securities_account_number")
            if sec:
                return str(sec)
    except Exception:
        return None
    return None


def load_state() -> ConnectionState | None:
    if not SESSION_FILE.exists():
        return None
    try:
        return ConnectionState(**json.loads(SESSION_FILE.read_text()))
    except Exception:
        return None


async def probe(client: TRClient, session_token: str) -> dict:
    """Run accountPairs + compactPortfolioByTypeV2 and capture raw frames."""
    captured: dict = {}

    async with websockets.connect(TR_WS_URL) as ws:
        # Mirror the handshake the library uses (protocol 34).
        connect_payload = dict(WS_CONNECT_PAYLOAD)
        connect_payload["locale"] = client.locale
        await ws.send(f"connect 34 {json.dumps(connect_payload)}")
        await ws.recv()

        msg_id = 0

        async def sub_and_capture(payload: dict, label: str, timeout: float = 8.0):
            nonlocal msg_id
            msg_id += 1
            await ws.send(f"sub {msg_id} {json.dumps(payload)}")
            pattern_a = re.compile(rf"^{msg_id} A ([\s\S]+)$")
            pattern_e = re.compile(rf"^{msg_id} E ([\s\S]+)$")
            frames: list[dict] = []
            try:
                for _ in range(20):
                    frame = await asyncio.wait_for(ws.recv(), timeout=timeout)
                    ma = pattern_a.match(frame)
                    me = pattern_e.match(frame)
                    if ma:
                        frames.append({"code": "A", "body": json.loads(ma.group(1))})
                        break
                    if me:
                        frames.append({"code": "E", "body": me.group(1)})
                        break
            except asyncio.TimeoutError:
                frames.append({"code": "T", "body": "timeout"})
            finally:
                await ws.send(f"unsub {msg_id}")
                try:
                    await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
            captured[label] = frames

        # Prefer the cached securitiesAccountNumber from the last
        # smoke_fetch_all run — saves a round trip and avoids depending on
        # accountPairs (which seems to error/timeout for some users).
        sec_acc_no = _cached_securities_account()
        if not sec_acc_no:
            await sub_and_capture(
                {"type": "accountPairs", "token": session_token},
                "accountPairs",
            )
            pairs_body = (captured.get("accountPairs", [{}])[0] or {}).get("body")
            if isinstance(pairs_body, dict):
                accounts = pairs_body.get("accounts") or []
                if accounts and isinstance(accounts[0], dict):
                    sec_acc_no = accounts[0].get("securitiesAccountNumber") or ""

        captured["_sec_acc_no_used"] = sec_acc_no or "(none — sub will likely error)"

        await sub_and_capture(
            {
                "type": "compactPortfolioByTypeV2",
                "token": session_token,
                "secAccNo": sec_acc_no or "",
            },
            "compactPortfolioByTypeV2",
        )

        # As a sanity reference, also grab the legacy compactPortfolio output
        # — useful to compare positions across the two response shapes.
        await sub_and_capture(
            {"type": "compactPortfolio", "token": session_token, "secAccNo": sec_acc_no or ""},
            "compactPortfolio_legacy",
        )

        # Bitcoin-specific: confirm what homeInstrumentExchange + ticker
        # return for the BTC pseudo-ISIN on the one-shot path. trdump's
        # long-lived monitor reports BTC valuation as 0; we want to see
        # whether the underlying subs reply with a usable price or
        # something crypto-specific that the helpers don't recognize.
        btc_isin = "XF000BTC0017"
        await sub_and_capture(
            {"type": "homeInstrumentExchange", "id": btc_isin, "token": session_token},
            "homeInstrumentExchange_BTC",
        )

        # Try the ticker with whatever home exchange resolves to. If it
        # didn't resolve, also try a bare ISIN ticker as a fallback —
        # cryptos sometimes use a synthetic venue id.
        home_body = (captured.get("homeInstrumentExchange_BTC", [{}])[0] or {}).get("body")
        exchange_id = ""
        if isinstance(home_body, dict):
            exchange_id = home_body.get("id") or home_body.get("exchangeId") or ""
        captured["_btc_exchange_id"] = exchange_id or "(none resolved)"

        await sub_and_capture(
            {"type": "ticker", "id": f"{btc_isin}.{exchange_id}" if exchange_id else btc_isin, "token": session_token},
            "ticker_BTC",
        )

    return captured


def summarize(captured: dict) -> str:
    """Compact one-line summary of each sub for the console."""
    lines = []
    for label, value in captured.items():
        if label.startswith("_"):
            lines.append(f"  {label}: {value}")
            continue
        frames = value
        if not frames:
            lines.append(f"  {label}: no frame")
            continue
        body = frames[0].get("body")
        code = frames[0].get("code")
        if isinstance(body, dict):
            keys = sorted(body.keys())
            shape = ", ".join(keys[:8]) + ("…" if len(keys) > 8 else "")
            lines.append(f"  {label}: {code}-frame, top-level keys [{shape}]")
        elif isinstance(body, list):
            lines.append(f"  {label}: {code}-frame, list of {len(body)} items")
        elif isinstance(body, str):
            # Truncated raw body — useful for E frames.
            preview = body[:120].replace("\n", " ")
            lines.append(f"  {label}: {code}-frame, str body: {preview!r}")
        else:
            lines.append(f"  {label}: {code}-frame, body={type(body).__name__}")
    return "\n".join(lines)


async def probe_long_lived_session(client: TRClient, session_token: str) -> dict:
    """Mirror what trdump's monitor does: open a TRSession and try to
    subscribe to BTC's ticker through ``subscribe_ticker``.

    Captures the first frame(s) so we can see whether the long-lived
    path resolves the home exchange and starts receiving prices the same
    way the one-shot path does in ``fetch_asset_list``.
    """
    captured: dict = {}
    btc_isin = "XF000BTC0017"

    async with client.open_session(session_token) as session:
        # 1. Manual home exchange lookup via session.request — same call
        # that subscribe_ticker makes internally.
        home = await session.request("homeInstrumentExchange", {"id": btc_isin}, timeout=8.0)
        captured["session.request(homeInstrumentExchange BTC)"] = home

        # 2. Full subscribe_ticker — collect frames for 6s.
        frames: list[dict] = []

        def on_tick(data):
            frames.append(data)

        try:
            sub_id = await session.subscribe_ticker(btc_isin, on_tick)
            await asyncio.sleep(6.0)
            await session.unsubscribe(sub_id)
            captured["session.subscribe_ticker(BTC)"] = {
                "frames_received": len(frames),
                "first_frame": frames[0] if frames else None,
                "first_frame_keys": (
                    sorted(frames[0].keys()) if frames and isinstance(frames[0], dict) else None
                ),
            }
        except Exception as e:
            captured["session.subscribe_ticker(BTC)"] = {"error": f"{type(e).__name__}: {e}"}

    return captured


def main() -> int:
    saved = load_state()
    if not saved or not saved.session_token:
        sys.exit(
            f"No saved session at {SESSION_FILE}. "
            "Run examples/smoke_fetch_all.py first to authenticate."
        )

    client = TRClient(
        waf_token=saved.waf_token,
        device_info=saved.device_info,
        locale=saved.locale,
    )

    try:
        captured = asyncio.run(probe(client, saved.session_token))
        captured["_long_lived_session"] = asyncio.run(
            probe_long_lived_session(client, saved.session_token)
        )
    except TRAuthError as e:
        sys.exit(f"Auth failed — session probably expired. Re-run smoke_fetch_all.py. ({e})")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(captured, indent=2, ensure_ascii=False, default=str))
    print(f"Wrote {OUT_PATH}\n")
    print("Summary:")
    print(summarize(captured))
    print("\nNext: paste the path or the JSON content back so the shape can be inspected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
