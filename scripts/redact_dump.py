"""Redact a smoke_fetch_all.py dump folder so it can be attached to a bug report.

Use after ``examples/smoke_fetch_all.py`` to anonymize the entire ``out/``
directory (or any other folder of JSON dumps). The script keeps all items
and preserves the data shape, but scrubs anything user-specific:

- ``sender`` / ``iban`` / ``holderName`` / ``beneficiary`` / ``email`` /
  ``phoneNumber`` field values are replaced wholesale
- ``title`` / ``subtitle`` are replaced when the same item has a
  ``sender`` (so a real name in the timeline header is not retained)
- IBANs, JWTs, emails, phone numbers, AWS WAF tokens are caught by regex
  inside any string value
- URL query strings are stripped (pre-signed S3 URLs leak temp credentials)
- TR cash account numbers are mapped to a deterministic placeholder
  (``9000000001``, ``9000000002``, …) — same input number → same output
  across files so the document stays internally consistent
- Any user-supplied strings (``--also-redact``) are replaced with
  ``[REDACTED]``

Usage::

    # Default: read examples/out/, write examples/out_redacted/
    python scripts/redact_dump.py

    # Redact extra strings the regexes can't catch (real name fragments,
    # account labels, etc.) — repeat the flag for each term
    python scripts/redact_dump.py \\
        --also-redact "Jane Doe" --also-redact "JANE-DOE"

    # Custom in/out paths
    python scripts/redact_dump.py --in /tmp/dump --out /tmp/dump_clean

Always eyeball the output for anything the script missed. Names embedded
in unexpected places (notes, descriptions, sub-payloads) are the classic
blind spot — use ``--also-redact`` to catch them once you've spotted any.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

DEFAULT_IN = Path("examples/out")
DEFAULT_OUT = Path("examples/out_redacted")

# ── Field-name rules (case-insensitive substring match on the key) ──────────

# Fields whose VALUE is replaced wholesale, regardless of content. The
# replacement string mirrors what a developer would expect to see in a
# bug-report JSON.
PII_FIELD_REPLACEMENTS = {
    "sender": "REDACTED_NAME",
    "beneficiary": "REDACTED_NAME",
    "holdername": "REDACTED_NAME",
    "holder_name": "REDACTED_NAME",
    "iban": "REDACTED_IBAN",
    "email": "redacted@example.com",
    "phonenumber": "+33600000000",
    "phone_number": "+33600000000",
    "phone": "+33600000000",
}

# When an item has a "sender" field (real-name bearing), its title / subtitle
# almost always mirror that name. Wipe them in that scope only.
HEADER_FIELDS_TO_CLEAR_NEAR_SENDER = {"title", "subtitle"}

# ── Regex rules (applied inside any string value) ───────────────────────────

IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}\s?[A-Z0-9\s]{11,30}\b")
JWT_RE = re.compile(r"eyJ[A-Za-z0-9_\-\.]{20,}")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RE = re.compile(r"\+\d{6,}")
# Long pure-digit runs (account numbers). Pure digits only, so ISINs
# (always 2 letters + 10 alphanumeric) are not matched.
LONG_DIGITS_RE = re.compile(r"\b\d{10,}\b")
URL_WITH_QUERY_RE = re.compile(r"(https?://[^\s?]+)\?[^\s]*")

REPLACEMENTS = {
    "iban_regex": "FR7600000000000000000000000",
    "jwt": "REDACTED_JWT",
    "email_regex": "redacted@example.com",
    "phone_regex": "+33600000000",
    "url_query": r"\1?REDACTED",
}


def _is_pii_field(key: str) -> str | None:
    """Return the replacement value if ``key`` matches a PII field, else None."""
    lk = key.lower()
    # Exact / suffix match against the normalized key.
    for needle, replacement in PII_FIELD_REPLACEMENTS.items():
        if lk == needle or lk.endswith("_" + needle) or lk.endswith(needle.title()):
            return replacement
    return None


# ── Cash-account-number mapping ─────────────────────────────────────────────


class CashAccountMapper:
    """Deterministic placeholder assignment for TR cash account numbers.

    Same input number always maps to the same placeholder, so cross-file
    references (e.g. ``cashAccountNumber`` in transactions matching
    ``cash_account_number`` in accounts) stay consistent in the redacted
    output.
    """

    def __init__(self, start: int = 9000000001):
        self._next = start
        self._map: dict[str, str] = {}

    def get(self, raw: str) -> str:
        if raw not in self._map:
            self._map[raw] = str(self._next)
            self._next += 1
        return self._map[raw]


# ── Main redaction walker ───────────────────────────────────────────────────


class Redactor:
    """Walk a JSON-shaped tree applying every redaction rule.

    Maintains a counter so the script can report what it changed.
    """

    # Fields whose values are TR cash account numbers (10-digit). These get
    # the deterministic mapping rather than the generic long-digits placeholder.
    CASH_ACCOUNT_FIELDS = {
        "cashaccountnumber",
        "cash_account_number",
        "securities_account_number",
        "securitiesaccountnumber",
        "tr_cash_account",
    }

    def __init__(self, cash_mapper: CashAccountMapper, also_redact: list[str]):
        self.cash_mapper = cash_mapper
        # Sort longest-first so "Jane Doe Smith" is replaced before "Doe"
        # — prevents partial matches from leaking the longer name. Match
        # case-insensitively because TR mixes "Jane Doe", "JANE DOE", and
        # "jane doe" across title / sender / description fields.
        terms = sorted({s for s in also_redact if s}, key=len, reverse=True)
        self.also_redact = terms
        self._also_redact_re = [
            (re.compile(re.escape(t), re.IGNORECASE), t) for t in terms
        ]
        self.stats: Counter[str] = Counter()

    def redact(self, obj, parent_dict=None):
        if isinstance(obj, dict):
            has_sender = any(
                k.lower() == "sender" and v for k, v in obj.items()
            )
            out = {}
            for k, v in obj.items():
                # Field-name rule first — wins over any value walk.
                replacement = _is_pii_field(k)
                if replacement is not None and v not in (None, "", 0):
                    self.stats[f"field:{k}"] += 1
                    out[k] = replacement
                    continue

                # Sender-adjacent header fields.
                if has_sender and k.lower() in HEADER_FIELDS_TO_CLEAR_NEAR_SENDER and isinstance(v, str) and v:
                    self.stats[f"header_near_sender:{k}"] += 1
                    out[k] = "REDACTED_HEADER"
                    continue

                # Cash account numbers — deterministic mapping.
                if k.lower() in self.CASH_ACCOUNT_FIELDS and isinstance(v, str) and v.isdigit():
                    self.stats["cash_account_number"] += 1
                    out[k] = self.cash_mapper.get(v)
                    continue

                out[k] = self.redact(v, parent_dict=obj)
            return out

        if isinstance(obj, list):
            return [self.redact(x, parent_dict=parent_dict) for x in obj]

        if isinstance(obj, str):
            return self._redact_string(obj)

        return obj

    def _redact_string(self, s: str) -> str:
        original = s

        # URL query strings (incl. AWS pre-signed S3).
        s = URL_WITH_QUERY_RE.sub(REPLACEMENTS["url_query"], s)

        # User-supplied literal terms (case-insensitive).
        for rgx, term in self._also_redact_re:
            if rgx.search(s):
                s, n = rgx.subn("[REDACTED]", s)
                self.stats[f"user_term:{term}"] += n

        # Regex pattern matches — JWT first (it can contain "@" via "Bearer").
        if JWT_RE.search(s):
            s = JWT_RE.sub(REPLACEMENTS["jwt"], s)
            self.stats["jwt"] += 1
        if EMAIL_RE.search(s):
            s = EMAIL_RE.sub(REPLACEMENTS["email_regex"], s)
            self.stats["email"] += 1
        if IBAN_RE.search(s):
            s = IBAN_RE.sub(REPLACEMENTS["iban_regex"], s)
            self.stats["iban_regex"] += 1
        if PHONE_RE.search(s):
            s = PHONE_RE.sub(REPLACEMENTS["phone_regex"], s)
            self.stats["phone"] += 1
        if LONG_DIGITS_RE.search(s):
            s = LONG_DIGITS_RE.sub("1234567890", s)
            self.stats["long_digits"] += 1

        if s != original and "url_query" not in self.stats:
            # url_query already counted above via the substitution.
            pass

        return s


# ── CLI ─────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--in",
        dest="in_dir",
        type=Path,
        default=DEFAULT_IN,
        help=f"Source directory of JSON dumps (default: {DEFAULT_IN}).",
    )
    parser.add_argument(
        "--out",
        dest="out_dir",
        type=Path,
        default=DEFAULT_OUT,
        help=f"Target directory; created if missing (default: {DEFAULT_OUT}).",
    )
    parser.add_argument(
        "--also-redact",
        dest="also_redact",
        action="append",
        default=[],
        metavar="STRING",
        help=(
            "Extra literal string to redact (matched case-insensitively). "
            "Repeat the flag for each term. Typical use: your real name "
            "and any name fragments the regex rules can't catch."
        ),
    )
    args = parser.parse_args()

    if not args.in_dir.is_dir():
        sys.exit(f"Input directory not found: {args.in_dir}")

    json_files = sorted(args.in_dir.glob("*.json"))
    if not json_files:
        sys.exit(f"No .json files in {args.in_dir}")

    args.out_dir.mkdir(parents=True, exist_ok=True)

    cash_mapper = CashAccountMapper()
    redactor = Redactor(cash_mapper=cash_mapper, also_redact=args.also_redact)

    for src in json_files:
        try:
            data = json.loads(src.read_text())
        except json.JSONDecodeError as e:
            print(f"  ! {src.name}: invalid JSON ({e}); copying verbatim", file=sys.stderr)
            (args.out_dir / src.name).write_text(src.read_text())
            continue

        cleaned = redactor.redact(data)
        dest = args.out_dir / src.name
        dest.write_text(json.dumps(cleaned, indent=2, ensure_ascii=False))
        print(f"  → {dest}")

    print("\nRedaction summary:")
    if not redactor.stats:
        print("  (nothing matched — double-check the input!)")
    else:
        for key, count in sorted(redactor.stats.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {count:5d}  {key}")

    if cash_mapper._map:
        print("\nCash account number mapping (kept consistent across files):")
        for raw, placeholder in cash_mapper._map.items():
            print(f"  {raw} → {placeholder}")

    print(
        "\nReview tip: open the redacted files and search for any real names, "
        "labels, or account aliases that the regex rules can't infer. Re-run "
        "with --also-redact for each one you find."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
