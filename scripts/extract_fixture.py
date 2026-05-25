"""Pull a single timeline item from a raw dump, sanitize PII, save as fixture.

Usage:
    python scripts/extract_fixture.py <event_type> <output_name> [--index N]

The extractor walks tests/out/transactions_raw.json, picks the Nth item
matching <event_type> (default: first), runs a sanitizer over every string
value, and writes the result to tests/fixtures/<output_name>.json.

The sanitizer is a starting point. Always eyeball the output for PII the
regex missed (real names in "Expéditeur" / "sender" fields are the
classic blind spot).
"""

import argparse
import json
import re
import sys
from pathlib import Path

RAW = Path("tests/out/transactions_raw.json")
FIXTURE_DIR = Path("tests/fixtures")

IBAN_RE = re.compile(r"[A-Z]{2}\d{2}\s?[A-Z0-9\s]{11,30}")
LONG_DIGITS_RE = re.compile(r"\b\d{10,}\b")
JWT_RE = re.compile(r"eyJ[A-Za-z0-9_\-\.]{20,}")
PHONE_RE = re.compile(r"\+\d{6,}")
# AWS access keys + the ``X-Amz-Credential`` URL parameter — secret
# scanning fires on either even when the rest of the URL has been redacted.
AWS_KEY_ID_RE = re.compile(r"\b(?:ASIA|AKIA)[A-Z0-9]{16,}\b")
X_AMZ_CRED_RE = re.compile(r"X-Amz-Credential=[^&\s\"']+", re.IGNORECASE)
# TR's masked tails: ``··7892`` (card last-4), ``..4118`` (IBAN tail).
# Preserve the masking chars and zero out the identifier.
MASKED_TAIL_RE = re.compile(r"[·•\.\*]{2,}([A-Z0-9]{2,6})\b")


def sanitize(obj):
    """Walk a dict/list, replace PII patterns inside strings with placeholders.

    Known blind spots — review by hand:
    - Sender/beneficiary names in "Expéditeur" fields
    - Account holder names embedded in event titles
    """
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize(x) for x in obj]
    if isinstance(obj, str):
        s = obj
        # Strip query strings from URLs (pre-signed S3 URLs contain temp AWS creds)
        if s.startswith("https://") and "?" in s:
            s = s.split("?", 1)[0] + "?REDACTED"
        s = IBAN_RE.sub("FR7600000000000000000000000", s)
        s = LONG_DIGITS_RE.sub("1234567890", s)
        s = JWT_RE.sub("REDACTED_JWT", s)
        s = PHONE_RE.sub("+33600000000", s)
        s = X_AMZ_CRED_RE.sub("X-Amz-Credential=REDACTED", s)
        s = AWS_KEY_ID_RE.sub("AKIAREDACTEDAWSKEY00", s)
        s = MASKED_TAIL_RE.sub(
            lambda m: m.group(0).replace(m.group(1), "0" * len(m.group(1))),
            s,
        )
        return s
    return obj


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("event_type", help="TR eventType (e.g. TRADING_TRADE_EXECUTED)")
    parser.add_argument("name", help="Fixture file name (without .json)")
    parser.add_argument("--index", type=int, default=0, help="Nth match (default: 0)")
    args = parser.parse_args()

    if not RAW.exists():
        sys.exit(f"Missing {RAW}. Run scripts/smoke_fetch_all.py first.")

    items = json.loads(RAW.read_text())
    matches = [i for i in items if i.get("eventType") == args.event_type]
    if not matches:
        sys.exit(f"No items with eventType={args.event_type!r}")
    if args.index >= len(matches):
        sys.exit(f"Only {len(matches)} matches; --index {args.index} out of range")

    raw = matches[args.index]
    clean = sanitize(raw)

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    out = FIXTURE_DIR / f"{args.name}.json"
    out.write_text(json.dumps(clean, indent=2, ensure_ascii=False))
    print(f"Wrote {out} (eventType={args.event_type}, index={args.index})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
