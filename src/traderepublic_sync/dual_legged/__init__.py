"""Optional dual-legged transaction mapping.

The functions in this subpackage transform Trade Republic timeline events
into a double-entry transaction schema with explicit credit / debit / fee /
tax legs. They are kept separate from the core library so generic users can
ignore them.
"""

from .mapping import (
    EVENT_TYPE_MAP,
    build_dual_legged_transaction,
    deduplicate_pea,
    determine_account,
    determine_tx_type,
)

__all__ = [
    "EVENT_TYPE_MAP",
    "build_dual_legged_transaction",
    "deduplicate_pea",
    "determine_account",
    "determine_tx_type",
]
