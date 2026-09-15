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

# availability_status vocabulary written to the DB
AVAIL_AVAILABLE = "available"
AVAIL_LIMITED = "limited"
AVAIL_SOLD_OUT = "sold_out"
AVAIL_UNKNOWN = "unknown"


class UnmappedCabinLabel(Exception):
    """Raised only by strict callers; the collector logs instead."""


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
