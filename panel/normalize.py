"""Normalization rules shared by every line.

Two rules matter most and are enforced here rather than in the collectors:

1. Taxes and fees are NEVER folded into price. `price_total` / `price_pppn`
   are fare-only; taxes land in their own column.
2. Cabin labels are mapped from an explicit per-line dict. Nothing is fuzzy
   matched. An unknown label yields None and is logged for a human to resolve.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any, Iterable, Mapping, Sequence

STANDARD_CATEGORIES = ("inside", "oceanview", "balcony", "suite")

# availability_status vocabulary written to the DB.
#
# `solo_only` is deliberately NOT `limited`. NCL returns SOLO_GUEST_ONLY on its
# Studio cabins, which are single-occupancy staterooms: the restriction is a
# property of the product, not a sign that inventory is running down. In the
# 2026-09-15 panel every one of the 291 SOLO_GUEST_ONLY cells was a STUDIO and
# no STUDIO was ever AVAILABLE, which is what a structural attribute looks like
# rather than scarcity. Folding it into `limited` put 43% of NCL's Caribbean
# "inside" cells into a depletion bucket that contained no depletion at all.
AVAIL_AVAILABLE = "available"
AVAIL_LIMITED = "limited"       # genuinely scarce: vendor says few left
AVAIL_SOLO_ONLY = "solo_only"   # bookable, but single occupancy only
AVAIL_SOLD_OUT = "sold_out"
AVAIL_UNKNOWN = "unknown"

# States that mean "a two-person booking cannot freely be made here". Used by
# analysis to build a depletion share; solo_only is excluded from both sides of
# that ratio because it was never open to the panel's 2-pax basis.
AVAIL_CLOSED = (AVAIL_LIMITED, AVAIL_SOLD_OUT)


class UnmappedCabinLabel(Exception):
    """Raised only by strict callers; the collector logs instead."""


class CurrencyMismatch(Exception):
    """A priced row arrived in a currency the panel did not ask for.

    Deliberately fatal rather than skipped. Both sources resolve market
    server-side -- NCL from client IP at the Akamai edge, Carnival from a
    cache we do not control -- so a currency change means the egress or the
    upstream moved, and every row from that run is suspect. A panel that
    quietly mixes CAD and USD prices produces a ~35% phantom price gap that
    looks exactly like a pricing signal.
    """


def map_cabin_category(raw_label: str, mapping: Mapping[str, str]) -> str | None:
    """Map a raw vendor label to one of STANDARD_CATEGORIES.

    Exact, case-insensitive lookup against an explicit dict. No fuzzy matching:
    an unrecognised label returns None so the caller can log it rather than guess.
    """
    if raw_label is None:
        return None
    key = str(raw_label).strip().upper()
    if not key:
        return None
    value = mapping.get(key)
    if value is None:
        return None
    value = value.strip().lower()
    if value not in STANDARD_CATEGORIES:
        raise ValueError(
            f"cabin mapping for {raw_label!r} is {value!r}, "
            f"which is not one of {STANDARD_CATEGORIES}"
        )
    return value


def nights_between(sail_date: str | None, return_date: str | None) -> int | None:
    """Nights = whole days between embark and disembark."""
    if not sail_date or not return_date:
        return None
    try:
        a = date.fromisoformat(str(sail_date)[:10])
        b = date.fromisoformat(str(return_date)[:10])
    except ValueError:
        return None
    n = (b - a).days
    return n if n > 0 else None


def price_pppn(price_per_person: float | None, nights: int | None) -> float | None:
    """Per person per night, taxes and fees EXCLUDED.

    `price_per_person` is the published double-occupancy fare for one guest for
    the whole voyage, so dividing by nights gives per-person-per-night directly.
    The spec states this as (price_total for 2 pax) / 2 / nights, which is the
    same quantity since price_total == price_per_person * 2.
    """
    if price_per_person is None or not nights:
        return None
    try:
        return round(float(price_per_person) / int(nights), 4)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def price_total_double(price_per_person: float | None) -> float | None:
    """Total for 2 pax, taxes and fees EXCLUDED."""
    if price_per_person is None:
        return None
    try:
        return round(float(price_per_person) * 2.0, 4)
    except (TypeError, ValueError):
        return None


def promo_payload(offers: Sequence[Mapping[str, Any]]) -> tuple[str | None, str | None]:
    """Return (verbatim JSON of offers, stable hash).

    The text is stored verbatim so a re-parse can recover anything. The hash is
    built from sorted (code, title, inclusion) triples so that pure reordering
    by the vendor does not read as a promo change, while a genuine add/remove or
    retitle does.
    """
    if not offers:
        return None, None
    verbatim = json.dumps(list(offers), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
    signature = sorted(
        (str(o.get("code", "")), str(o.get("title", "")), str(o.get("inclusion", "")))
        for o in offers
    )
    digest = hashlib.sha256(
        json.dumps(signature, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return verbatim, digest


def region_for(destination_codes: Iterable[str], mapping: Mapping[str, str]) -> str | None:
    """First destination code with a configured region wins.

    Vendors attach marketing tags (NCL uses WEEKEND, HOLIDAY) alongside the real
    geography, so unmapped codes are skipped rather than treated as regions.
    """
    for code in destination_codes or ():
        region = mapping.get(str(code).strip().upper())
        if region:
            return region
    return None


def month_key(d: str | None) -> str | None:
    """'2027-03-19T00:00' -> '2027-03'. Used for cohort grouping."""
    if not d:
        return None
    s = str(d)[:7]
    return s if len(s) == 7 and s[4] == "-" else None


def window_side(sail_date: str | None, start: str | None,
                end: str | None) -> str | None:
    """Which side of the window a sail date falls on.

    Returns None when the date is inside, "before"/"after" when outside, and
    "undated" when there is no parseable date. Used to COUNT what the window
    drops: a sailing excluded by date must be reported, not vanish.
    """
    if not sail_date:
        return "undated"
    try:
        d = date.fromisoformat(str(sail_date)[:10])
    except ValueError:
        return "undated"
    if start and d < date.fromisoformat(start):
        return "before"
    if end and d > date.fromisoformat(end):
        return "after"
    return None


def count_window_drop(stats: dict | None, sail_date: str | None,
                      start: str | None, end: str | None) -> None:
    """Increment stats[side] for a date outside the window. No-op if inside."""
    if stats is None:
        return
    side = window_side(sail_date, start, end)
    if side:
        stats[side] = stats.get(side, 0) + 1


def in_window(sail_date: str | None, start: str | None, end: str | None) -> bool:
    """Inclusive YYYY-MM-DD window test against a sail date."""
    if not sail_date:
        return False
    try:
        d = date.fromisoformat(str(sail_date)[:10])
    except ValueError:
        return False
    if start and d < date.fromisoformat(start):
        return False
    if end and d > date.fromisoformat(end):
        return False
    return True
