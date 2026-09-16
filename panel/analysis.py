"""Analysis over the panel.

Every function here returns a `Result`, and every `Result` carries the `Basis`
it was computed on: the tier, the lines, the regions actually present, the sail
window, the scrape dates, the granularity, and the row counts behind it.

Why the basis is not optional
-----------------------------
The two tiers are not two samples of the same population. weekly-full covers
Jan-Aug 2027 across five regions. daily-marker covers Oct-Dec 2026, and in that
window the Mediterranean season has ended and the ships have repositioned, so
its supply is effectively Caribbean-only on both NCL and Carnival. A number
computed on one tier therefore says nothing about the other, and a number
computed by pooling them describes no real population at all: it would read as
"Southern Europe pricing is moving" when the movement is Caribbean rows
entering the average.

So: no function pools tiers. `tier` is a required argument everywhere, a query
is scoped to exactly one tier, and combining two results requires
`combine_bases(..., allow_mixed_basis=True)`, which stamps a loud caveat onto
the output rather than letting the mixture pass silently. Anything rendered for
the pitch should print `Result.basis.label()` next to the number.

Cross-line functions additionally route through
`panel.sources.capabilities.require_granularity`, so a comparison can never run
at a resolution only one side publishes.
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import sqlite3
import statistics
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Sequence

from panel.sources.capabilities import (
    CAPABILITIES,
    comparison_column,
    require_granularity,
    shared_granularity,
)

TIERS = ("weekly-full", "daily-marker")

# What each tier is evidence *about*. Stated here so a caveat is attached even
# when a run happens to be missing rows for an unrelated reason.
TIER_SCOPE_NOTES: dict[str, str] = {
    "weekly-full": (
        "weekly-full is the Jan-Aug 2027 book across all configured regions; "
        "it is a forward-book cross-section, not a near-term depletion series."
    ),
    "daily-marker": (
        "daily-marker is the Oct-Dec 2026 near-term cohort, and its region mix "
        "is set by seasonal deployment, not by sampling choice. Measured from "
        "the near-term universe on 2026-09-15: Caribbean 104 eligible "
        "itineraries (85 NCL / 19 Carnival); Southern Europe 15, ALL NCL, all "
        "departing October-early November, 0 in December and 0 on Carnival; "
        "Alaska 0. So cross-line work in this tier is Caribbean-only, and its "
        "Southern Europe rows are a single-line October series that dies when "
        "the Mediterranean season ends."
    ),
}

# Regions a tier can genuinely support a *peer* comparison in. Anything else is
# single-line evidence and is labelled as such.
PEER_COMPARABLE_NOTE = (
    "PEER-COMPARABLE REGIONS in this basis: {ok}. Single-line only (no peer "
    "rows, so no cross-line claim can rest on them): {solo}."
)

# Price fields are not identically defined across sources; say so once.
PRICE_BASIS_NOTE = (
    "price_pppn is per person per night, taxes and fees excluded on every line. "
    "Vendor base fields differ (NCL combinedPrice, Carnival per-person fare), so "
    "levels are comparable in direction and spread more safely than in absolute cents."
)

LINE_BY_NAME: dict[str, str] = {c.line: k for k, c in CAPABILITIES.items()}


class BasisError(ValueError):
    """Raised when results resting on different evidence would be blended."""


# -- basis ------------------------------------------------------------------

@dataclass(frozen=True)
class Basis:
    tier: str
    lines: tuple[str, ...] = ()
    regions: tuple[str, ...] = ()
    sail_window: tuple[str | None, str | None] = (None, None)
    scrape_dates: tuple[str, ...] = ()
    granularity: str = "category"
    n_observations: int = 0
    n_sailings: int = 0
    caveats: tuple[str, ...] = ()

    def label(self) -> str:
        """One line naming exactly what this number rests on."""
        lo, hi = self.sail_window
        window = f"{lo or '?'}..{hi or '?'}"
        regions = ", ".join(self.regions) if self.regions else "no regions"
        dates = (f"{self.scrape_dates[0]}..{self.scrape_dates[-1]}"
                 if len(self.scrape_dates) > 1 else
                 (self.scrape_dates[0] if self.scrape_dates else "no scrapes"))
        return (f"[{self.tier}] sail {window} | regions: {regions} | "
                f"observed {dates} | {self.n_observations} obs / "
                f"{self.n_sailings} sailings | granularity: {self.granularity}")

    def with_caveat(self, *notes: str) -> "Basis":
        extra = tuple(n for n in notes if n and n not in self.caveats)
        return replace(self, caveats=self.caveats + extra)


@dataclass(frozen=True)
class Result:
    name: str
    basis: Basis
    rows: list[dict[str, Any]] = field(default_factory=list)
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "analysis": self.name,
            "basis": {
                "label": self.basis.label(),
                "tier": self.basis.tier,
                "lines": list(self.basis.lines),
                "regions": list(self.basis.regions),
                "sail_window": list(self.basis.sail_window),
                "scrape_dates": list(self.basis.scrape_dates),
                "granularity": self.basis.granularity,
                "n_observations": self.basis.n_observations,
                "n_sailings": self.basis.n_sailings,
                "caveats": list(self.basis.caveats),
            },
            "notes": list(self.notes),
            "rows": self.rows,
        }


def combine_bases(bases: Sequence[Basis], *,
                  allow_mixed_basis: bool = False) -> Basis:
    """Merge bases, refusing to blend tiers unless explicitly forced.

    The escape hatch exists because sometimes you genuinely do want both
    numbers on one slide. It does not make the mixture comparable, so it
    records that fact in the caveats instead of hiding it.
    """
    if not bases:
        raise BasisError("no bases to combine")
    tiers = sorted({b.tier for b in bases})
    if len(tiers) > 1 and not allow_mixed_basis:
        raise BasisError(
            f"refusing to combine results from different tiers {tiers}: these "
            "cover different sail windows and different regions, so a pooled "
            "figure describes no real population. Pass allow_mixed_basis=True "
            "if you intend to show them side by side."
        )
    merged = Basis(
        tier=tiers[0] if len(tiers) == 1 else "+".join(tiers),
        lines=tuple(sorted({l for b in bases for l in b.lines})),
        regions=tuple(sorted({r for b in bases for r in b.regions})),
        sail_window=(
            min((b.sail_window[0] for b in bases if b.sail_window[0]), default=None),
            max((b.sail_window[1] for b in bases if b.sail_window[1]), default=None),
        ),
        scrape_dates=tuple(sorted({d for b in bases for d in b.scrape_dates})),
        granularity=shared_or_coarsest([b.granularity for b in bases]),
        n_observations=sum(b.n_observations for b in bases),
        n_sailings=sum(b.n_sailings for b in bases),
        caveats=tuple(dict.fromkeys(c for b in bases for c in b.caveats)),
    )
    if len(tiers) > 1:
        merged = merged.with_caveat(
            "MIXED EVIDENTIARY BASIS: this figure spans tiers "
            f"{tiers}, which cover different sail windows and different region "
            "mixes. Do not read it as one population; report the components "
            "separately."
        )
    return merged


def shared_or_coarsest(levels: Iterable[str]) -> str:
    from panel.sources.capabilities import GRANULARITIES
    idx = [GRANULARITIES.index(l) for l in levels if l in GRANULARITIES]
    return GRANULARITIES[min(idx)] if idx else "category"


# -- query scaffolding ------------------------------------------------------

def _where(tier: str, *, lines: Sequence[str] | None = None,
           regions: Sequence[str] | None = None,
           scrape_date: str | None = None,
           scrape_dates: Sequence[str] | None = None,
           sail_from: str | None = None, sail_to: str | None = None,
           priced_only: bool = False,
           product: str | None = None) -> tuple[str, list[Any]]:
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r}; expected one of {TIERS}")
    sql = ["tier = ?"]
    args: list[Any] = [tier]
    if lines:
        sql.append("line IN (%s)" % ",".join("?" * len(lines)))
        args.extend(lines)
    if regions:
        sql.append("region IN (%s)" % ",".join("?" * len(regions)))
        args.extend(regions)
    if scrape_date:
        sql.append("scrape_date = ?")
        args.append(scrape_date)
    if scrape_dates:
        sql.append("scrape_date IN (%s)" % ",".join("?" * len(scrape_dates)))
        args.extend(scrape_dates)
    if sail_from:
        sql.append("sail_date >= ?")
        args.append(sail_from)
    if sail_to:
        sql.append("sail_date <= ?")
        args.append(sail_to)
    if priced_only:
        sql.append("price_pppn IS NOT NULL")
    if product == "cruise_only":
        # Strictly 0, not "not 1": is_package IS NULL means the product type was
        # never captured, and those are precisely the rows that may be packages.
        sql.append("is_package = 0")
    elif product == "package_only":
        sql.append("is_package = 1")
    elif product not in (None, "all"):
        raise ValueError(f"unknown product filter {product!r}")
    return " AND ".join(sql), args


def _basis(conn: sqlite3.Connection, tier: str, clause: str, args: list[Any],
           *, granularity: str = "category") -> Basis:
    row = conn.execute(
        f"""SELECT COUNT(*) n, COUNT(DISTINCT sailing_id) s,
                   MIN(sail_date) a, MAX(sail_date) b
            FROM observations WHERE {clause}""", args).fetchone()
    lines = [r[0] for r in conn.execute(
        f"SELECT DISTINCT line FROM observations WHERE {clause} ORDER BY line", args)]
    regions = [r[0] for r in conn.execute(
        f"SELECT DISTINCT region FROM observations WHERE {clause} "
        "AND region IS NOT NULL ORDER BY region", args)]
    dates = [r[0] for r in conn.execute(
        f"SELECT DISTINCT scrape_date FROM observations WHERE {clause} "
        "ORDER BY scrape_date", args)]
    basis = Basis(
        tier=tier, lines=tuple(lines), regions=tuple(regions),
        sail_window=(row["a"], row["b"]), scrape_dates=tuple(dates),
        granularity=granularity, n_observations=row["n"], n_sailings=row["s"],
    )
    note = TIER_SCOPE_NOTES.get(tier)
    if note:
        basis = basis.with_caveat(note)

    # Which regions actually carry more than one line, computed from the rows
    # in scope rather than assumed from the config.
    per_region = collections.defaultdict(set)
    for r in conn.execute(
            f"SELECT DISTINCT region, line FROM observations WHERE {clause} "
            "AND region IS NOT NULL", args):
        per_region[r["region"]].add(r["line"])
    ok = sorted(r for r, ls in per_region.items() if len(ls) > 1)
    solo = sorted(r for r, ls in per_region.items() if len(ls) == 1)
    if per_region and len(lines) > 1:
        basis = basis.with_caveat(PEER_COMPARABLE_NOTE.format(
            ok=", ".join(ok) or "none",
            solo=", ".join(f"{r} ({next(iter(per_region[r]))})" for r in solo) or "none"))

    if tier == "daily-marker" and "Southern Europe" not in regions:
        basis = basis.with_caveat(
            "COVERAGE: no Southern Europe rows in this tier. Southern Europe "
            "evidence must come from weekly-full and be reported separately.")
    if len(regions) == 1:
        basis = basis.with_caveat(
            f"SINGLE-REGION BASIS: every row here is {regions[0]}. Do not "
            "generalise this to the other regions in the panel.")
    return basis


def _product_filter_caveat(conn: sqlite3.Connection, tier: str, **filters: Any) -> str:
    """Say what a cruise-only filter removed, and what it could not classify.

    Land+cruise packages carry a package fare against cruise-segment nights, so
    including them inflates price_pppn and compares a bundled land tour with a
    peer's ship fare. Rows collected before the flag existed are NULL, and a
    NULL is not a negative -- they are excluded and counted here so the reader
    can see how much of the panel the comparison is actually standing on.
    """
    clause, args = _where(tier, **filters, priced_only=True)
    row = conn.execute(
        f"""SELECT SUM(is_package = 0) AS cruise, SUM(is_package = 1) AS pkg,
                   SUM(is_package IS NULL) AS unknown, COUNT(*) AS total
            FROM observations WHERE {clause}""", args).fetchone()
    total = row["total"] or 0
    unknown = row["unknown"] or 0
    parts = [f"PRODUCT FILTER: cruise-only. Of {total} priced rows in scope, "
             f"{row['cruise'] or 0} are cruise-only, {row['pkg'] or 0} are "
             f"land+cruise packages (excluded), {unknown} are unclassified."]
    if unknown:
        parts.append(
            f"The {unknown} unclassified rows were collected before is_package "
            "was captured and are EXCLUDED, since an unknown product is not a "
            "known cruise. Re-run the collector, or backfill with "
            "scripts/backfill_packages.py, to bring them back into scope.")
    return " ".join(parts)


def _stats(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"n": 0, "mean": None, "median": None, "p25": None, "p75": None,
                "min": None, "max": None}
    vs = sorted(values)
    return {
        "n": len(vs),
        "mean": round(statistics.fmean(vs), 2),
        "median": round(statistics.median(vs), 2),
        "p25": round(vs[max(0, int(len(vs) * 0.25) - (len(vs) % 4 == 0))], 2),
        "p75": round(vs[min(len(vs) - 1, int(len(vs) * 0.75))], 2),
        "min": round(vs[0], 2),
        "max": round(vs[-1], 2),
    }


# -- 1. availability snapshot ----------------------------------------------

def availability_snapshot(conn: sqlite3.Connection, *, tier: str,
                          scrape_date: str | None = None,
                          lines: Sequence[str] | None = None,
                          regions: Sequence[str] | None = None,
                          product: str | None = None,
                          group_by: Sequence[str] = ("line", "region", "cabin_category"),
                          ) -> Result:
    """Availability mix per group: the series no published dataset carries.

    A minimum-fare series cannot distinguish "price rose" from "the cheap
    cabins are gone". This is the denominator that separates them.
    """
    allowed = {"line", "region", "cabin_category", "ship", "sail_month", "nights"}
    bad = [g for g in group_by if g not in allowed]
    if bad:
        raise ValueError(f"cannot group availability by {bad}; allowed: {sorted(allowed)}")
    cols = ["substr(sail_date,1,7) AS sail_month" if g == "sail_month" else g
            for g in group_by]

    clause, args = _where(tier, lines=lines, regions=regions,
                          scrape_date=scrape_date, product=product)
    basis = _basis(conn, tier, clause, args)

    rows = conn.execute(
        f"""SELECT {', '.join(cols)},
                   COUNT(*) AS cells,
                   SUM(availability_status='available') AS available,
                   SUM(availability_status='limited')   AS limited,
                   SUM(availability_status='solo_only') AS solo_only,
                   SUM(availability_status='sold_out')  AS sold_out,
                   SUM(availability_status='unknown' OR availability_status IS NULL)
                       AS unknown,
                   SUM(price_pppn IS NOT NULL) AS priced,
                   AVG(price_pppn) AS mean_pppn,
                   MIN(price_pppn) AS min_pppn
            FROM observations WHERE {clause}
            GROUP BY {', '.join(str(i + 1) for i in range(len(cols)))}
            ORDER BY {', '.join(str(i + 1) for i in range(len(cols)))}""",
        args).fetchall()

    out = []
    for r in rows:
        d = {g: r[g if g != "sail_month" else "sail_month"] for g in group_by}
        cells = r["cells"] or 0
        solo = r["solo_only"] or 0
        # Solo-only cells were never open to a 2-pax booking, so they belong in
        # neither half of a depletion ratio. Excluding them from the denominator
        # is the difference between measuring scarcity and measuring how many
        # Studio cabins a ship has.
        bookable = cells - solo
        closed = (r["limited"] or 0) + (r["sold_out"] or 0)
        d.update({
            "cells": cells,
            "bookable_cells": bookable,
            "available": r["available"] or 0,
            "limited": r["limited"] or 0,
            "solo_only": solo,
            "sold_out": r["sold_out"] or 0,
            "unknown": r["unknown"] or 0,
            # The headline: share of 2-pax-bookable cells no longer freely open.
            "closed_share": round(closed / bookable, 4) if bookable else None,
            "sold_out_share": round((r["sold_out"] or 0) / bookable, 4)
                              if bookable else None,
            "priced_share": round((r["priced"] or 0) / bookable, 4)
                            if bookable else None,
            "mean_pppn": round(r["mean_pppn"], 2) if r["mean_pppn"] is not None else None,
            "min_pppn": round(r["min_pppn"], 2) if r["min_pppn"] is not None else None,
        })
        out.append(d)

    return Result(
        "availability_snapshot", basis, out,
        notes=(
            "closed_share = (limited + sold_out) / bookable cells, where "
            "bookable excludes solo_only.",
            "solo_only is NCL's SOLO_GUEST_ONLY on Studio cabins: single "
            "occupancy by design, so it is a product attribute and not "
            "depletion. It is reported but kept out of both sides of the ratio.",
            "Carnival publishes no 'limited' state (soldOut is a boolean), so its "
            "limited count is structurally 0 and its closed_share is a strict "
            "sold-out share. Compare closed_share within a line over time, and "
            "sold_out_share across lines.",
            "An unpriced cell is a cell the vendor declined to quote, usually "
            "because it is sold out; priced_share is therefore a second, "
            "independent read on depletion.",
        ))


# -- 2. peer gap ------------------------------------------------------------

def peer_gap(conn: sqlite3.Connection, *, tier: str,
             treatment_line: str = "Norwegian Cruise Line",
             peer_lines: Sequence[str] | None = None,
             granularity: str = "category",
             scrape_date: str | None = None,
             regions: Sequence[str] | None = None,
             nights_band: tuple[int, int] | None = None,
             product: str = "cruise_only",
             min_cells: int = 5) -> Result:
    """Treatment-vs-peer price gap at a granularity both sides genuinely publish.

    Runs per (region, cabin bucket) so the comparison is like-for-like on
    itinerary geography; pooling regions would make the gap a function of where
    each line happens to deploy rather than of how it prices.
    """
    peer_lines = list(peer_lines or [c.line for c in CAPABILITIES.values()
                                     if c.line != treatment_line])
    lines = [treatment_line, *peer_lines]
    keys = [LINE_BY_NAME[l] for l in lines if l in LINE_BY_NAME]
    unknown = [l for l in lines if l not in LINE_BY_NAME]
    if unknown:
        raise KeyError(f"no declared capability for {unknown}; "
                       f"known lines: {sorted(LINE_BY_NAME)}")
    require_granularity(keys, granularity)      # refuses over-fine comparisons
    col = comparison_column(granularity)

    clause, args = _where(tier, lines=lines, regions=regions,
                          scrape_date=scrape_date, priced_only=True,
                          product=product)
    if nights_band:
        clause += " AND nights BETWEEN ? AND ?"
        args = [*args, nights_band[0], nights_band[1]]
    basis = _basis(conn, tier, clause, args, granularity=granularity)

    rows = conn.execute(
        f"""SELECT line, region, {col} AS bucket, nights, price_pppn
            FROM observations WHERE {clause} AND {col} IS NOT NULL""",
        args).fetchall()

    grouped: dict[tuple[str, str], dict[str, list[float]]] = collections.defaultdict(
        lambda: collections.defaultdict(list))
    for r in rows:
        grouped[(r["region"], r["bucket"])][r["line"]].append(r["price_pppn"])

    out = []
    for (region, bucket), by_line in sorted(grouped.items(),
                                            key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
        t = by_line.get(treatment_line, [])
        peers = [v for l in peer_lines for v in by_line.get(l, [])]
        if len(t) < min_cells or len(peers) < min_cells:
            # Reported, not dropped: a thin cell is information about coverage.
            out.append({
                "region": region, granularity: bucket,
                "treatment_n": len(t), "peer_n": len(peers),
                "treatment_median_pppn": None, "peer_median_pppn": None,
                "gap_pppn": None, "gap_pct": None,
                "status": f"insufficient cells (need >= {min_cells} each side)",
            })
            continue
        tm = statistics.median(t)
        pm = statistics.median(peers)
        out.append({
            "region": region, granularity: bucket,
            "treatment_n": len(t), "peer_n": len(peers),
            "treatment_median_pppn": round(tm, 2),
            "peer_median_pppn": round(pm, 2),
            "treatment_p25_pppn": _stats(t)["p25"],
            "treatment_p75_pppn": _stats(t)["p75"],
            "peer_p25_pppn": _stats(peers)["p25"],
            "peer_p75_pppn": _stats(peers)["p75"],
            "gap_pppn": round(tm - pm, 2),
            "gap_pct": round((tm - pm) / pm * 100, 2) if pm else None,
            "status": "ok",
        })

    basis = basis.with_caveat(PRICE_BASIS_NOTE)
    if product == "cruise_only":
        basis = basis.with_caveat(_product_filter_caveat(
            conn, tier, lines=lines, regions=regions, scrape_date=scrape_date))
    if granularity != shared_granularity(keys):
        basis = basis.with_caveat(
            f"requested granularity {granularity!r} is coarser than the shared "
            f"maximum {shared_granularity(keys)!r}")
    return Result(
        "peer_gap", basis, out,
        notes=(
            f"treatment: {treatment_line}; peers: {', '.join(peer_lines)}.",
            f"compared at '{granularity}' -- the finest level "
            f"{sorted(keys)} all genuinely publish.",
            "Medians, not minima: the whole point of the panel is that the "
            "minimum is contaminated by inventory mix.",
            "gap_pct > 0 means the treatment line prices above the peer set.",
        ))


# -- 3. cohort index --------------------------------------------------------

def cohort_index(conn: sqlite3.Connection, *, tier: str,
                 lines: Sequence[str] | None = None,
                 regions: Sequence[str] | None = None,
                 base_scrape_date: str | None = None,
                 by: Sequence[str] = ("line", "region", "cabin_category"),
                 ) -> Result:
    """Price level per sail-month cohort, indexed to a base scrape date.

    Holding the cohort fixed is what stops a rising mean from being read as
    price strength when it is really the near-dated, cheaper sailings leaving
    the book.
    """
    clause, args = _where(tier, lines=lines, regions=regions, priced_only=True)
    basis = _basis(conn, tier, clause, args)
    if not basis.scrape_dates:
        return Result("cohort_index", basis, [],
                      notes=("no priced rows in scope",))
    base = base_scrape_date or basis.scrape_dates[0]

    group_cols = list(by)
    rows = conn.execute(
        f"""SELECT {', '.join(group_cols)}, substr(sail_date,1,7) AS cohort,
                   scrape_date, AVG(price_pppn) AS mean_pppn,
                   COUNT(*) AS cells
            FROM observations WHERE {clause}
            GROUP BY {', '.join(group_cols)}, cohort, scrape_date
            ORDER BY {', '.join(group_cols)}, cohort, scrape_date""",
        args).fetchall()

    baseline: dict[tuple, float] = {}
    for r in rows:
        if r["scrape_date"] == base:
            baseline[tuple(r[g] for g in group_cols) + (r["cohort"],)] = r["mean_pppn"]

    out = []
    for r in rows:
        key = tuple(r[g] for g in group_cols) + (r["cohort"],)
        b = baseline.get(key)
        d = {g: r[g] for g in group_cols}
        d.update({
            "cohort": r["cohort"], "scrape_date": r["scrape_date"],
            "cells": r["cells"],
            "mean_pppn": round(r["mean_pppn"], 2),
            "base_mean_pppn": round(b, 2) if b else None,
            "index": round(r["mean_pppn"] / b * 100, 2) if b else None,
        })
        out.append(d)

    single = len(basis.scrape_dates) == 1
    if single:
        basis = basis.with_caveat(
            "ONE SCRAPE DATE: every index is 100 by construction. A cohort "
            "index needs at least two collection dates to carry information.")
    return Result("cohort_index", basis, out,
                  notes=(f"base scrape date: {base}; index = 100 at base.",
                         "Cohort = sail month, so the same forward book is "
                         "compared with itself across collection dates."))


# -- 4. depletion rate ------------------------------------------------------

def depletion_rate(conn: sqlite3.Connection, *, tier: str,
                   lines: Sequence[str] | None = None,
                   regions: Sequence[str] | None = None,
                   by: Sequence[str] = ("line", "region", "cabin_category"),
                   min_dates: int = 2) -> Result:
    """Change in closed share per day, per group.

    Requires at least two collection dates in the tier. Returns an empty result
    with an explicit caveat rather than a fabricated slope when it has one.
    """
    clause, args = _where(tier, lines=lines, regions=regions)
    basis = _basis(conn, tier, clause, args)
    if len(basis.scrape_dates) < min_dates:
        return Result(
            "depletion_rate",
            basis.with_caveat(
                f"NOT COMPUTABLE: depletion needs >= {min_dates} collection "
                f"dates in '{tier}', found {len(basis.scrape_dates)}. A slope "
                "from one observation would be invented, not measured."),
            [], notes=("no slope computed",))

    group_cols = list(by)
    rows = conn.execute(
        f"""SELECT {', '.join(group_cols)}, scrape_date,
                   SUM(availability_status != 'solo_only'
                       OR availability_status IS NULL) AS cells,
                   SUM(availability_status IN ('limited','sold_out')) AS closed
            FROM observations WHERE {clause}
            GROUP BY {', '.join(group_cols)}, scrape_date
            ORDER BY {', '.join(group_cols)}, scrape_date""",
        args).fetchall()

    series: dict[tuple, list[tuple[str, float, int]]] = collections.defaultdict(list)
    for r in rows:
        if not r["cells"]:
            continue
        series[tuple(r[g] for g in group_cols)].append(
            (r["scrape_date"], (r["closed"] or 0) / r["cells"], r["cells"]))

    out = []
    for key, points in sorted(series.items(), key=lambda kv: tuple(map(str, kv[0]))):
        if len(points) < min_dates:
            continue
        (d0, s0, n0), (d1, s1, n1) = points[0], points[-1]
        days = (datetime.date.fromisoformat(d1) - datetime.date.fromisoformat(d0)).days
        d = dict(zip(group_cols, key))
        d.update({
            "first_date": d0, "last_date": d1, "days": days,
            "first_closed_share": round(s0, 4), "last_closed_share": round(s1, 4),
            "delta_closed_share": round(s1 - s0, 4),
            "closed_share_per_day": round((s1 - s0) / days, 5) if days else None,
            "observations": len(points), "first_cells": n0, "last_cells": n1,
        })
        out.append(d)

    return Result("depletion_rate", basis, out,
                  notes=("closed share = (limited + sold_out) / 2-pax-bookable "
                         "cells; solo_only cells are excluded from both sides.",
                         "Endpoint slope, not a fit: with few collection dates a "
                         "regression would overstate precision.",
                         "Cell counts are reported because a group whose offered "
                         "cells shrink is depleting in a way a share can mask."))


# -- 5. promo diff ----------------------------------------------------------

def promo_diff(conn: sqlite3.Connection, *, tier: str,
               lines: Sequence[str] | None = None,
               regions: Sequence[str] | None = None) -> Result:
    """Promo-hash churn per line across collection dates.

    Only lines whose capability declares `exposes_promo_detail` carry real
    signal here; the rest are reported as not-applicable rather than as zero.
    """
    clause, args = _where(tier, lines=lines, regions=regions)
    basis = _basis(conn, tier, clause, args)

    rows = conn.execute(
        f"""SELECT line, scrape_date,
                   COUNT(*) AS cells,
                   SUM(promo_hash IS NOT NULL) AS with_promo,
                   COUNT(DISTINCT promo_hash) AS distinct_promos
            FROM observations WHERE {clause}
            GROUP BY line, scrape_date ORDER BY line, scrape_date""",
        args).fetchall()

    out = []
    prev: dict[str, set[str]] = {}
    for r in rows:
        key = LINE_BY_NAME.get(r["line"])
        exposes = bool(key and CAPABILITIES[key].exposes_promo_detail)
        hashes = {h[0] for h in conn.execute(
            f"SELECT DISTINCT promo_hash FROM observations WHERE {clause} "
            "AND line = ? AND scrape_date = ? AND promo_hash IS NOT NULL",
            [*args, r["line"], r["scrape_date"]])}
        before = prev.get(r["line"])
        out.append({
            "line": r["line"], "scrape_date": r["scrape_date"],
            "cells": r["cells"],
            "promo_share": round((r["with_promo"] or 0) / r["cells"], 4)
                           if r["cells"] else None,
            "distinct_promos": r["distinct_promos"],
            "new_promos": len(hashes - before) if before is not None else None,
            "dropped_promos": len(before - hashes) if before is not None else None,
            "status": "ok" if exposes else "line does not expose promo detail",
        })
        prev[r["line"]] = hashes

    return Result("promo_diff", basis, out,
                  notes=("promo_hash is a stable hash of the sorted (code, title, "
                         "inclusion) triples, so churn means the offer really "
                         "changed, not that the payload was reordered.",
                         "Bodies are in the `promos` table; Store.promo_text(hash) "
                         "resolves one."))


# -- 6. earnings window compare --------------------------------------------

def earnings_window_compare(conn: sqlite3.Connection, *, tier: str,
                            event_date: str = "2026-11-04",
                            window_days: int = 14,
                            lines: Sequence[str] | None = None,
                            regions: Sequence[str] | None = None) -> Result:
    """Sailings departing inside the earnings window vs those outside it.

    The comparison is cross-sectional on sail date, not on collection date: it
    asks whether the book NCLH is about to report on looks different from the
    book on either side of it.
    """
    ev = datetime.date.fromisoformat(event_date)
    lo = (ev - datetime.timedelta(days=window_days)).isoformat()
    hi = (ev + datetime.timedelta(days=window_days)).isoformat()

    clause, args = _where(tier, lines=lines, regions=regions)
    basis = _basis(conn, tier, clause, args)

    rows = conn.execute(
        f"""SELECT line, region, cabin_category,
                   (sail_date >= ? AND sail_date <= ?) AS in_window,
                   SUM(availability_status != 'solo_only'
                       OR availability_status IS NULL) AS cells,
                   SUM(availability_status IN ('limited','sold_out')) AS closed,
                   AVG(price_pppn) AS mean_pppn
            FROM observations WHERE {clause}
            GROUP BY line, region, cabin_category, in_window""",
        [lo, hi, *args]).fetchall()

    agg: dict[tuple, dict[int, sqlite3.Row]] = collections.defaultdict(dict)
    for r in rows:
        agg[(r["line"], r["region"], r["cabin_category"])][int(r["in_window"])] = r

    out = []
    for key, sides in sorted(agg.items(), key=lambda kv: tuple(map(str, kv[0]))):
        inside, outside = sides.get(1), sides.get(0)
        d = dict(zip(("line", "region", "cabin_category"), key))
        d.update({
            "in_window_cells": inside["cells"] if inside else 0,
            "outside_cells": outside["cells"] if outside else 0,
            "in_window_mean_pppn": round(inside["mean_pppn"], 2)
                if inside and inside["mean_pppn"] is not None else None,
            "outside_mean_pppn": round(outside["mean_pppn"], 2)
                if outside and outside["mean_pppn"] is not None else None,
            "in_window_closed_share": round(inside["closed"] / inside["cells"], 4)
                if inside and inside["cells"] else None,
            "outside_closed_share": round(outside["closed"] / outside["cells"], 4)
                if outside and outside["cells"] else None,
        })
        out.append(d)

    covered = sum(r["in_window_cells"] for r in out)
    if not covered:
        basis = basis.with_caveat(
            f"NO COVERAGE: no sailing in '{tier}' departs between {lo} and {hi}, "
            "so this tier cannot speak to the earnings window at all.")
    return Result("earnings_window_compare", basis, out,
                  notes=(f"event {event_date}, window {lo}..{hi} (+/-{window_days}d).",
                         "Split is on sail date, not collection date."))


# -- rendering --------------------------------------------------------------

def render(result: Result, *, limit: int = 0) -> str:
    lines = [f"== {result.name} ==", f"   {result.basis.label()}"]
    for c in result.basis.caveats:
        lines.append(f"   ! {c}")
    for n in result.notes:
        lines.append(f"   - {n}")
    lines.append("")
    rows = result.rows[:limit] if limit else result.rows
    if not rows:
        lines.append("   (no rows)")
        return "\n".join(lines)
    cols = list(rows[0].keys())
    width = {c: max(len(str(c)), *(len(_fmt(r.get(c))) for r in rows)) for c in cols}
    lines.append("  " + "  ".join(str(c).ljust(width[c]) for c in cols))
    lines.append("  " + "  ".join("-" * width[c] for c in cols))
    for r in rows:
        lines.append("  " + "  ".join(_fmt(r.get(c)).ljust(width[c]) for c in cols))
    if limit and len(result.rows) > limit:
        lines.append(f"  ... {len(result.rows) - limit} more rows")
    return "\n".join(lines)


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:,.2f}"
    return str(v)


# -- CLI --------------------------------------------------------------------

ANALYSES = {
    "availability": availability_snapshot,
    "peer-gap": peer_gap,
    "cohort-index": cohort_index,
    "depletion": depletion_rate,
    "promo-diff": promo_diff,
    "earnings-window": earnings_window_compare,
}


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("analysis", choices=sorted(ANALYSES))
    ap.add_argument("--tier", required=True, choices=list(TIERS),
                    help="required: results are never pooled across tiers")
    ap.add_argument("--db", default=os.path.join("data", "panel.sqlite"))
    ap.add_argument("--scrape-date", default=None)
    ap.add_argument("--region", action="append", dest="regions")
    ap.add_argument("--line", action="append", dest="lines")
    ap.add_argument("--group-by", default=None,
                    help="comma-separated grouping columns")
    ap.add_argument("--product", default=None,
                    choices=["cruise_only", "package_only", "all"],
                    help="peer-gap only; default cruise_only (excludes land+cruise)")
    ap.add_argument("--min-cells", type=int, default=None,
                    help="peer-gap only; minimum cells per side (default 5)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json", dest="as_json", action="store_true")
    args = ap.parse_args(argv)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    fn = ANALYSES[args.analysis]

    kwargs: dict[str, Any] = {"tier": args.tier}
    if args.regions:
        kwargs["regions"] = args.regions
    if args.analysis == "peer-gap":
        if args.scrape_date:
            kwargs["scrape_date"] = args.scrape_date
        if args.product:
            kwargs["product"] = args.product
        if args.min_cells is not None:
            kwargs["min_cells"] = args.min_cells
    else:
        if args.lines:
            kwargs["lines"] = args.lines
        if args.analysis == "availability" and args.scrape_date:
            kwargs["scrape_date"] = args.scrape_date
    if args.group_by:
        key = "group_by" if args.analysis == "availability" else "by"
        kwargs[key] = tuple(g.strip() for g in args.group_by.split(","))

    result = fn(conn, **kwargs)
    if args.as_json:
        print(json.dumps(result.to_dict(), indent=1))
    else:
        print(render(result, limit=args.limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
