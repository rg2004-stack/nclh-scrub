"""What each source actually resolves.

Sources do not expose the same resolution. Carnival publishes a vendor
sub-category (`8A`, `GS`) and a rate code per cell; NCL publishes neither.
Those columns are therefore NULL for NCL and populated for Carnival.

The risk this module exists to prevent: a cross-line comparison silently run
at a granularity only one side has. Comparing NCL against Carnival at
sub-category level would not be a like-for-like comparison — it would be
Carnival's real sub-categories against NCL's NULLs. `shared_granularity()`
computes the finest level every named line genuinely supports, and
`require_granularity()` refuses anything finer.

Any cross-line function (peer_gap in particular) must route through these.
"""
from __future__ import annotations

from dataclasses import dataclass

# Coarse -> fine. Index order is the comparison lattice.
GRANULARITIES = ("category", "subcategory", "rate_code")


@dataclass(frozen=True)
class SourceCapability:
    key: str
    line: str
    # finest granularity this source genuinely resolves
    granularity: str
    # availability vocabulary the source can actually emit
    availability_states: tuple[str, ...]
    exposes_units_remaining: bool
    exposes_tax_amount: bool
    exposes_promo_detail: bool
    notes: str = ""


CAPABILITIES: dict[str, SourceCapability] = {
    "ncl": SourceCapability(
        key="ncl",
        line="Norwegian Cruise Line",
        granularity="category",
        availability_states=("available", "limited", "sold_out"),
        exposes_units_remaining=False,
        # Verified absent: `taxesAndFees` is on 0 of 1,276 archived pricing rows
        # and on no NCL endpoint we can reach, in USD or CAD. NCL's own
        # disclaimers say taxes "are additional", so the published fare is
        # tax-exclusive and taxes_fees is NULL by design, not by market.
        exposes_tax_amount=False,
        exposes_promo_detail=True,
        notes=("cabin_subcategory is the stateroom type (INSIDE/BALCONY/...), "
               "which is category-level. No vendor sub-category or rate code."),
    ),
    "carnival": SourceCapability(
        key="carnival",
        line="Carnival Cruise Line",
        granularity="rate_code",
        availability_states=("available", "sold_out"),
        exposes_units_remaining=False,
        # Field exists and is USD-stamped but returns 0.0 in every sampled cell.
        exposes_tax_amount=False,
        exposes_promo_detail=False,
        notes=("vendor_category_code (8A/GS/6K) and rate_code (OB7/PSV/OTR) are "
               "genuinely published per cell. No 'limited' state: availability "
               "is a soldOut boolean."),
    ),
}


class GranularityError(ValueError):
    """Raised when a comparison is attempted at a resolution a source lacks."""


def capability(key: str) -> SourceCapability:
    if key not in CAPABILITIES:
        raise KeyError(f"no capability declared for source {key!r}; "
                       f"known: {sorted(CAPABILITIES)}")
    return CAPABILITIES[key]


def shared_granularity(keys: list[str] | tuple[str, ...]) -> str:
    """Finest granularity every named source supports.

    Comparing NCL (category) with Carnival (rate_code) yields 'category'.
    """
    if not keys:
        raise GranularityError("no sources given")
    levels = [GRANULARITIES.index(capability(k).granularity) for k in keys]
    return GRANULARITIES[min(levels)]


def require_granularity(keys: list[str] | tuple[str, ...], wanted: str) -> str:
    """Assert `wanted` is supported by every named source, else raise.

    Cross-line analysis calls this before grouping, so an over-fine comparison
    fails loudly rather than quietly returning a result built on NULLs.
    """
    if wanted not in GRANULARITIES:
        raise GranularityError(
            f"unknown granularity {wanted!r}; expected one of {GRANULARITIES}")
    shared = shared_granularity(keys)
    if GRANULARITIES.index(wanted) > GRANULARITIES.index(shared):
        lacking = [k for k in keys
                   if GRANULARITIES.index(capability(k).granularity)
                   < GRANULARITIES.index(wanted)]
        raise GranularityError(
            f"cannot compare {list(keys)} at {wanted!r}: "
            f"{lacking} only resolve to {shared!r}. "
            f"Cross-line comparison must use {shared!r}."
        )
    return wanted


def comparison_column(granularity: str) -> str:
    """Map a granularity to the observations column that carries it."""
    return {
        "category": "cabin_category",
        "subcategory": "vendor_category_code",
        "rate_code": "rate_code",
    }[granularity]
