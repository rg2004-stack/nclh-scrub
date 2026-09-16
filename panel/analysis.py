"""Analysis over the panel.

Every function here returns a `Result`, and every `Result` carries the `Basis`
it was computed on: the tier, the lines, the regions actually present, the sail
window, the scrape dates, the granularity, and the row counts behind it.

Why the basis is not optional
-----------------------------
The two tiers are not two samples of the same population. weekly-full is the
broad snapshot: every configured region and line, swept once a week.
daily-marker is a narrow, curated subset of near-term sailings read every day.
Their sail windows overlap, but their breadth and cadence do not, so the same
region can carry hundreds of weekly rows and a handful of daily ones.

A number computed on one tier therefore says nothing about the other, and a
number computed by pooling them describes no real population at all: it would
read as "Southern Europe pricing is moving" when the movement is Caribbean rows
entering the average, or as a depletion slope when it is really the weekly
sweep adding itineraries the daily tier never tracked.

Note that no window is hardcoded here. `Basis.label()` reports the sail window
actually present in the rows, because a window written into a docstring is a
claim that goes stale the moment the config changes -- which is exactly how the
"immutable" export path came to overwrite history.

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

from panel.normalize import AVAIL_CLOSED, AVAIL_SOLO_ONLY
from panel.sources.capabilities import (
    CAPABILITIES,
    comparison_column,
    peer_keys,
    require_granularity,
    shared_granularity,
)

TIERS = ("weekly-full", "daily-marker")

# What each tier is evidence *about*. Stated here so a caveat is attached even
# when a run happens to be missing rows for an unrelated reason.
TIER_SCOPE_NOTES: dict[str, str] = {
    "weekly-full": (
        "weekly-full is the broad weekly sweep: every configured region and "
        "line. It is a cross-section of the book, sampled once a week, not a "
        "high-frequency series -- read levels and spreads from it, and take "
        "day-to-day movement from daily-marker. The sail window it covers is "
        "whatever the config asks for; this basis reports the window actually "
        "present in the rows."
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

# Sample-size bands, reported PER ROW rather than as a footnote. A caveat that
# lives at the bottom of the output gets separated from the number the moment
# anyone copies a row into a deck, and these numbers are going into a deck.
THIN_CELLS = 30        # below this, a median is indicative at best
VERY_THIN_CELLS = 10   # below this, treat it as an anecdote

# Where a product filter removed most of a region's rows, the surviving sample
# is not just small, it is a different population from the headline row count.
HEAVY_EXCLUSION_PCT = 40.0

# Below this span, a cohort index has points but no direction. Fares move on
# booking-curve and promotional timescales, so a few days of history is a
# starting value, not a trend.
MIN_TREND_DAYS = 14


def sample_flag(treatment_n: int, peer_n: int, min_cells: int) -> str:
    """One short verdict on whether this row can carry weight."""
    low = min(treatment_n, peer_n)
    if low < min_cells:
        return f"INSUFFICIENT (n={low} < {min_cells})"
    if low < VERY_THIN_CELLS:
        return f"VERY THIN (n={low})"
    if low < THIN_CELLS:
        return f"THIN (n={low})"
    return "ok"


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

    # Rows the product filter removed, keyed the same way as the output, so the
    # exclusion travels in the row instead of sitting in a footnote.
    excluded: dict[tuple, int] = {}
    if product == "cruise_only":
        ex_clause, ex_args = _where(tier, lines=lines, regions=regions,
                                    scrape_date=scrape_date,
                                    product="package_only")
        for r in conn.execute(
                f"""SELECT {', '.join(cols)}, COUNT(*) n
                    FROM observations WHERE {ex_clause}
                    GROUP BY {', '.join(str(i + 1) for i in range(len(cols)))}""",
                ex_args):
            key = tuple(r[g if g != "sail_month" else "sail_month"] for g in group_by)
            excluded[key] = r["n"]

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
        if product == "cruise_only":
            key = tuple(d[g] for g in group_by)
            n_ex = excluded.get(key, 0)
            d["pkg_excluded"] = n_ex
            d["pkg_excluded_pct"] = (round(100.0 * n_ex / (cells + n_ex), 1)
                                     if (cells + n_ex) else 0.0)
        # Graded per row: a closed_share computed on 9 cells is an anecdote,
        # and it should say so next to itself rather than in the notes.
        d["sample"] = ("VERY THIN" if bookable < VERY_THIN_CELLS
                       else "THIN" if bookable < THIN_CELLS
                       else "ok") + f" (n={bookable})"
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
            f"`sample` grades each row on its bookable cell count: THIN below "
            f"{THIN_CELLS}, VERY THIN below {VERY_THIN_CELLS}.",
            "`pkg_excluded` is the land+cruise rows the cruise-only filter "
            "removed from that group; where it dominates, the row describes a "
            "much smaller product set than the region's raw size suggests.",
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
    # Controls (Royal: floor only) are reference points, not fare peers. They
    # are excluded by role here rather than left to require_granularity, which
    # would otherwise drag every cross-line comparison down to the floor rung.
    peer_lines = list(peer_lines or [CAPABILITIES[k].line for k in peer_keys()
                                     if CAPABILITIES[k].line != treatment_line])
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

    # How many rows the product filter removed, per cell, so the reader can see
    # the difference between "this region is small" and "this region is mostly
    # a product we excluded". In Alaska 71% of NCL's priced rows are cruisetours.
    excl_clause, excl_args = _where(tier, lines=lines, regions=regions,
                                    scrape_date=scrape_date, priced_only=True,
                                    product="package_only")
    if nights_band:
        excl_clause += " AND nights BETWEEN ? AND ?"
        excl_args = [*excl_args, nights_band[0], nights_band[1]]
    excluded: dict[tuple, int] = {}
    if product == "cruise_only":
        for r in conn.execute(
                f"""SELECT line, region, {col} AS bucket, COUNT(*) n
                    FROM observations WHERE {excl_clause} AND {col} IS NOT NULL
                    GROUP BY line, region, bucket""", excl_args):
            excluded[(r["line"], r["region"], r["bucket"])] = r["n"]

    grouped: dict[tuple[str, str], dict[str, list[float]]] = collections.defaultdict(
        lambda: collections.defaultdict(list))
    for r in rows:
        grouped[(r["region"], r["bucket"])][r["line"]].append(r["price_pppn"])

    out = []
    for (region, bucket), by_line in sorted(grouped.items(),
                                            key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
        t = by_line.get(treatment_line, [])
        peers = [v for l in peer_lines for v in by_line.get(l, [])]
        t_excl = excluded.get((treatment_line, region, bucket), 0)
        p_excl = sum(excluded.get((l, region, bucket), 0) for l in peer_lines)
        t_pct = round(100.0 * t_excl / (len(t) + t_excl), 1) if (len(t) + t_excl) else 0.0

        if len(t) < min_cells or len(peers) < min_cells:
            # Reported, not dropped: a thin cell is information about coverage.
            out.append({
                "region": region, granularity: bucket,
                "treatment_n": len(t), "peer_n": len(peers),
                "treatment_median_pppn": None, "peer_median_pppn": None,
                "gap_pppn": None, "gap_pct": None,
                "pkg_excluded_t": t_excl, "pkg_excluded_pct_t": t_pct,
                "sample": sample_flag(len(t), len(peers), min_cells),
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
            "pkg_excluded_t": t_excl, "pkg_excluded_pct_t": t_pct,
            "sample": sample_flag(len(t), len(peers), min_cells),
            "status": ("ok" if t_pct < HEAVY_EXCLUSION_PCT
                       else f"ok; {t_pct}% of treatment rows were packages"),
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
            f"`sample` grades the smaller side of each row: THIN below "
            f"{THIN_CELLS} cells, VERY THIN below {VERY_THIN_CELLS}. It is a "
            f"per-row column, not a footnote, so it travels with the number.",
            "`pkg_excluded_t` / `pkg_excluded_pct_t` are the treatment rows the "
            "cruise-only filter removed from that cell. A high percentage means "
            "the surviving sample is a different product mix from the region's "
            "headline row count.",
        ))


# -- 3. cohort index --------------------------------------------------------

def _history_note(dates: Sequence[str]) -> str:
    """State exactly how much history exists, in the output, every time.

    The whole point of this panel is a series nobody else has, which means for
    a while it is a series almost nobody has YET. An index printed without its
    own history length invites the reader to assume a run of observations
    behind it. There is not one, and this sentence is how the output says so.
    """
    n = len(dates)
    if n == 0:
        return "NO HISTORY: no priced rows in scope, so there is no series."
    if n == 1:
        return (
            f"SINCE INCEPTION = ONE DAY. The panel holds a single collection "
            f"date ({dates[0]}), so every index below is 100 by construction "
            "and measures nothing. This is the first observation of a series, "
            "not a series. It becomes informative from the second scheduled "
            "run onward."
        )
    span = (datetime.date.fromisoformat(dates[-1])
            - datetime.date.fromisoformat(dates[0])).days
    head = (f"HISTORY IS {n} COLLECTION DATES ({dates[0]}..{dates[-1]}, "
            f"{span} day{'s' if span != 1 else ''} end to end, "
            f"{n - 1} interval{'s' if n - 1 != 1 else ''}).")
    if span < MIN_TREND_DAYS:
        return (
            f"{head} That is too short a window to read a trend from: fares "
            "move on booking-curve and promotional timescales measured in "
            "weeks. Read these as the first points of a series, not as a "
            "direction of travel. The window lengthens by one point per "
            "scheduled run."
        )
    return (f"{head} Still early: treat direction as provisional until several "
            "weeks of scheduled runs have accumulated.")


def cohort_index(conn: sqlite3.Connection, *, tier: str,
                 lines: Sequence[str] | None = None,
                 regions: Sequence[str] | None = None,
                 base_scrape_date: str | None = None,
                 product: str | None = "cruise_only",
                 by: Sequence[str] = ("line", "region", "cabin_category"),
                 min_matched: int = 5) -> Result:
    """Since-inception price index on a MATCHED BASKET of cabins.

    The index is built the way an index has to be built if it is going to mean
    anything: fix the basket at the base date, then reprice that same basket.
    A cell is one (line, sailing_id, cabin_subcategory, market) -- one physical
    cabin grade on one departure -- and only cells priced on BOTH the base date
    and the comparison date enter the ratio.

    That constraint is the entire point, for two reasons.

    First, it is the panel's own thesis turned on itself. A mean over whatever
    happens to be priced today rises when the cheap cabins sell out, which is
    exactly the artefact the minimum-fare series suffers from and exactly what
    this panel exists to avoid publishing. `index_naive` below is that
    contaminated number, reported beside the matched one so the difference can
    be read off directly: `mix_effect_pp` is how many index points of the naive
    move are composition rather than price.

    Second, it makes the index immune to the collector's own scope changing.
    Sailings that were not in the book at the base date cannot enter a fixed
    basket, so widening the sail window cannot move `index_matched`. Cells
    LEAVING the basket still matter and are reported as `attrition_pct` --
    that is inventory depletion, which is signal, not contamination.

    The base is per (group, cohort): its inception, meaning the first date this
    panel ever saw that cell group priced. Coverage started at different times
    for different regions, so a single global base would silently discard
    everything that entered later. `base_date` and `days_since_base` are on
    every row because rows indexed to different bases are not comparable to
    each other in levels. Pass `base_scrape_date` to pin a common base instead.
    """
    clause, args = _where(tier, lines=lines, regions=regions,
                          priced_only=True, product=product)
    basis = _basis(conn, tier, clause, args)
    if product == "cruise_only":
        basis = basis.with_caveat(_product_filter_caveat(
            conn, tier, lines=lines, regions=regions, product="cruise_only"))

    group_cols = list(by)
    rows = conn.execute(
        f"""SELECT {', '.join(group_cols)}, substr(sail_date, 1, 7) AS cohort,
                   scrape_date, line AS _line, sailing_id AS _sid,
                   cabin_subcategory AS _sub, market AS _mkt, price_pppn
            FROM observations WHERE {clause}""", args).fetchall()

    history = _history_note(list(basis.scrape_dates))
    basis = basis.with_caveat(history)
    if not rows:
        return Result("cohort_index", basis, [], notes=("no priced rows in scope",))

    # (group, cohort) -> scrape_date -> {cell key: price}
    book: dict[tuple, dict[str, dict[tuple, float]]] = collections.defaultdict(
        lambda: collections.defaultdict(dict))
    for r in rows:
        key = tuple(r[g] for g in group_cols) + (r["cohort"],)
        book[key][r["scrape_date"]][
            (r["_line"], r["_sid"], r["_sub"], r["_mkt"])] = r["price_pppn"]

    out: list[dict[str, Any]] = []
    for key in sorted(book, key=lambda k: tuple(map(str, k))):
        dates = sorted(book[key])
        base = base_scrape_date if base_scrape_date in book[key] else dates[0]
        base_prices = book[key][base]
        base_d = datetime.date.fromisoformat(base)
        base_naive = statistics.fmean(base_prices.values()) if base_prices else None

        for date in dates:
            now = book[key][date]
            matched = set(base_prices) & set(now)
            basket_base = sum(base_prices[k] for k in matched)
            basket_now = sum(now[k] for k in matched)
            relatives = sorted((now[k] / base_prices[k] - 1) * 100
                               for k in matched if base_prices[k])
            naive_now = statistics.fmean(now.values()) if now else None
            idx_matched = (round(100 * basket_now / basket_base, 2)
                           if basket_base else None)
            idx_naive = (round(100 * naive_now / base_naive, 2)
                         if base_naive and naive_now is not None else None)

            d = dict(zip(group_cols, key[:-1]))
            d.update({
                "cohort": key[-1],
                "base_date": base,
                "scrape_date": date,
                "days_since_base": (datetime.date.fromisoformat(date) - base_d).days,
                "base_cells": len(base_prices),
                "cells_now": len(now),
                "matched_cells": len(matched),
                "index_matched": idx_matched,
                "median_cell_change_pct": (round(statistics.median(relatives), 2)
                                           if relatives else None),
                "index_naive": idx_naive,
                "mix_effect_pp": (round(idx_naive - idx_matched, 2)
                                  if idx_naive is not None
                                  and idx_matched is not None else None),
                "attrition_pct": (round(100 * (len(base_prices) - len(matched))
                                        / len(base_prices), 1)
                                  if base_prices else None),
                "entered_cells": len(set(now) - set(base_prices)),
                "basket_base_pppn": round(basket_base / len(matched), 2) if matched else None,
                "basket_now_pppn": round(basket_now / len(matched), 2) if matched else None,
            })
            if date == base:
                d["sample"] = f"BASE (n={len(matched)})"
            elif len(matched) < min_matched:
                d["sample"] = f"INSUFFICIENT (matched {len(matched)} < {min_matched})"
            elif len(matched) < VERY_THIN_CELLS:
                d["sample"] = f"VERY THIN (n={len(matched)})"
            elif len(matched) < THIN_CELLS:
                d["sample"] = f"THIN (n={len(matched)})"
            else:
                d["sample"] = "ok"
            out.append(d)

    if len(basis.scrape_dates) > 1:
        moved = [r for r in out
                 if r["days_since_base"] > 0 and r["index_matched"] is not None
                 and not r["sample"].startswith("INSUFFICIENT")]
        if not moved:
            basis = basis.with_caveat(
                "NO COMPARABLE REPRICING YET: every cell group either sits on "
                "its own base date or has too few cabins observed on two dates "
                "to form a basket. The panel has more than one collection date "
                "but not yet two readings of the same cabins.")

    return Result("cohort_index", basis, out, notes=(
        "index_matched = 100 x (basket value now) / (same basket at base), "
        "over cells priced on BOTH dates. 100 = unchanged.",
        "index_naive is the same ratio over whatever was priced on each date "
        "-- the contaminated construction this panel exists to replace. "
        "mix_effect_pp = index_naive - index_matched is the composition "
        "artefact in index points.",
        "attrition_pct is the share of the base basket no longer priced: "
        "depletion, and the reason a matched index eventually thins out.",
        "entered_cells never affects index_matched. A fixed basket is why a "
        "widened sail window cannot masquerade as a price move.",
        "median_cell_change_pct is the median per-cabin change, a check on "
        "whether the basket ratio is driven by a few expensive cells.",
        "base = inception per (group, cohort), so rows with different "
        "base_date values are not comparable to each other in levels.",
    ))


# -- 4. depletion rate ------------------------------------------------------

def depletion_rate(conn: sqlite3.Connection, *, tier: str,
                   lines: Sequence[str] | None = None,
                   regions: Sequence[str] | None = None,
                   by: Sequence[str] = ("line", "region", "cabin_category"),
                   min_dates: int = 2, min_matched: int = 5) -> Result:
    """Change in closed share per day, measured on the SAME cabins both times.

    Depletion is a statement about specific inventory: these cabins were
    bookable, now they are not. Computing it over whatever the collector
    happened to return on each date measures something else entirely -- when
    the weekly sweep widened on 2026-09-16 it added 1,047 sailings, and a
    closed-share "slope" across that break is mostly the new arrivals' mix.

    So the endpoints are restricted to common support: a cell is one
    (line, sailing_id, cabin_subcategory, market), and only cells observed on
    BOTH endpoint dates enter either share. Cells that entered in between
    cannot move the number; cells that vanished are counted separately as
    `dropped_cells`, because a sailing leaving the book is not the same event
    as a cabin selling out and must not be silently read as one.

    `naive_*` columns are the unrestricted version, reported so the size of
    the artefact is visible rather than merely asserted.
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
        f"""SELECT {', '.join(group_cols)}, scrape_date, availability_status,
                   line AS _line, sailing_id AS _sid,
                   cabin_subcategory AS _sub, market AS _mkt
            FROM observations WHERE {clause}""", args).fetchall()

    # (group) -> date -> {cell key: availability status}
    book: dict[tuple, dict[str, dict[tuple, str | None]]] = (
        collections.defaultdict(lambda: collections.defaultdict(dict)))
    for r in rows:
        key = tuple(r[g] for g in group_cols)
        book[key][r["scrape_date"]][
            (r["_line"], r["_sid"], r["_sub"], r["_mkt"])] = r["availability_status"]

    def shares(cells: dict[tuple, str | None], keys) -> tuple[int, int]:
        """(bookable, closed) over the given keys. solo_only is not scarcity."""
        bookable = closed = 0
        for k in keys:
            st = cells.get(k)
            if st == AVAIL_SOLO_ONLY:
                continue
            bookable += 1
            if st in AVAIL_CLOSED:
                closed += 1
        return bookable, closed

    out = []
    for key in sorted(book, key=lambda k: tuple(map(str, k))):
        dates = sorted(book[key])
        if len(dates) < min_dates:
            continue
        d0, d1 = dates[0], dates[-1]
        first, last = book[key][d0], book[key][d1]
        matched = set(first) & set(last)

        m_book0, m_closed0 = shares(first, matched)
        m_book1, m_closed1 = shares(last, matched)
        n_book0, n_closed0 = shares(first, first)
        n_book1, n_closed1 = shares(last, last)
        if not m_book0 or not m_book1:
            continue

        s0 = m_closed0 / m_book0
        s1 = m_closed1 / m_book1
        days = (datetime.date.fromisoformat(d1)
                - datetime.date.fromisoformat(d0)).days
        naive0 = n_closed0 / n_book0 if n_book0 else None
        naive1 = n_closed1 / n_book1 if n_book1 else None

        d = dict(zip(group_cols, key))
        d.update({
            "first_date": d0, "last_date": d1, "days": days,
            "matched_cells": len(matched),
            "first_closed_share": round(s0, 4),
            "last_closed_share": round(s1, 4),
            "delta_closed_share": round(s1 - s0, 4),
            "closed_share_per_day": round((s1 - s0) / days, 5) if days else None,
            "newly_closed_cells": sum(
                1 for k in matched
                if first.get(k) not in AVAIL_CLOSED
                and first.get(k) != AVAIL_SOLO_ONLY
                and last.get(k) in AVAIL_CLOSED),
            "reopened_cells": sum(
                1 for k in matched
                if first.get(k) in AVAIL_CLOSED
                and last.get(k) not in AVAIL_CLOSED
                and last.get(k) != AVAIL_SOLO_ONLY),
            "dropped_cells": len(set(first) - matched),
            "entered_cells": len(set(last) - matched),
            "naive_first_closed_share": round(naive0, 4) if naive0 is not None else None,
            "naive_last_closed_share": round(naive1, 4) if naive1 is not None else None,
            "naive_delta": (round(naive1 - naive0, 4)
                            if naive0 is not None and naive1 is not None else None),
            "mix_effect_pp": (round(((naive1 - naive0) - (s1 - s0)) * 100, 2)
                              if naive0 is not None and naive1 is not None else None),
            "observations": len(dates),
        })
        low = min(m_book0, m_book1)
        d["sample"] = ("INSUFFICIENT" if low < min_matched
                       else "VERY THIN" if low < VERY_THIN_CELLS
                       else "THIN" if low < THIN_CELLS
                       else "ok") + f" (n={low})"
        out.append(d)

    if out and not any(r["matched_cells"] >= min_matched for r in out):
        basis = basis.with_caveat(
            "NO COMMON SUPPORT: no group has enough cabins observed on both "
            "endpoint dates to measure depletion. More than one collection "
            "date is not the same as two readings of the same inventory.")

    return Result("depletion_rate", basis, out, notes=(
        "COMMON SUPPORT: both shares are computed over the same cells -- those "
        "observed on the first AND last date. Cells that entered in between "
        "cannot move the slope, so a widened collection scope cannot be read "
        "as depletion.",
        "closed share = (limited + sold_out) / bookable cells; solo_only is a "
        "product restriction, not scarcity, and is excluded from both sides.",
        "dropped_cells left the book entirely (sailed, or delisted). That is "
        "not the same event as selling out and is reported, never folded in.",
        "newly_closed_cells / reopened_cells are the gross flows behind the "
        "net delta: a flat share can hide both.",
        "naive_* is the unrestricted computation over whatever each date "
        "returned. mix_effect_pp = (naive delta - matched delta) in points.",
        "Endpoint difference, not a fit: with few collection dates a "
        "regression would overstate precision.",
    ))


# -- 5. promo diff ----------------------------------------------------------

def decode_promo(promo_text: str | None) -> list[dict[str, Any]]:
    """Turn a stored promo payload into readable offers.

    The payload is the vendor's raw offer array, kept verbatim so the archive
    stays faithful. A hash identifies a BUNDLE of offers, which is the right
    unit for detecting that something changed and the wrong unit for reading:
    nobody can act on `0b0c2413...`. This is the only place that knows the
    vendor's field names, so a payload shape change breaks here rather than
    silently emptying a column downstream.
    """
    if not promo_text:
        return []
    try:
        payload = json.loads(promo_text)
    except (TypeError, ValueError):
        return [{"code": "<unparseable>", "title": "", "description": "",
                 "inclusion": "", "offer_type": "", "featured": None}]
    if isinstance(payload, dict):
        payload = [payload]
    out = []
    for offer in payload:
        if not isinstance(offer, dict):
            continue
        out.append({
            "code": str(offer.get("code") or "").strip() or "<no code>",
            "title": str(offer.get("shortTitle") or offer.get("title") or "").strip(),
            "description": str(offer.get("shortDescription")
                               or offer.get("description") or "").strip(),
            "inclusion": str(offer.get("inclusion") or "").strip(),
            "offer_type": str(offer.get("offerType") or "").strip(),
            "featured": bool(offer.get("isFeatured")) if "isFeatured" in offer else None,
        })
    return out


def _offer_index(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    """promo_hash -> decoded offers, read once."""
    return {r["promo_hash"]: decode_promo(r["promo_text"])
            for r in conn.execute("SELECT promo_hash, promo_text FROM promos")}


def promo_reference(conn: sqlite3.Connection, *, tier: str,
                    lines: Sequence[str] | None = None,
                    regions: Sequence[str] | None = None) -> Result:
    """One row per distinct OFFER actually observed, in readable form.

    This is the lookup table for everything else that mentions a promo. It is
    keyed by the vendor's own offer code and carries the title and description
    verbatim, so a figure like "38% of Caribbean cells carried
    `50-off-all-cruises-offer`" can be checked against what the offer says.

    Scope columns matter as much as the text: an offer that only ever appears
    on one ship in one region is not a fleet-wide promotion, and the raw hash
    gave no way to tell the difference.
    """
    clause, args = _where(tier, lines=lines, regions=regions)
    basis = _basis(conn, tier, clause, args)
    offers = _offer_index(conn)

    rows = conn.execute(
        f"""SELECT promo_hash, line, region, cabin_category, ship,
                   scrape_date, sail_date, COUNT(*) AS n
            FROM observations WHERE {clause} AND promo_hash IS NOT NULL
            GROUP BY promo_hash, line, region, cabin_category, ship,
                     scrape_date, sail_date""", args).fetchall()

    agg: dict[tuple, dict[str, Any]] = {}
    for r in rows:
        for offer in offers.get(r["promo_hash"], []):
            key = (r["line"], offer["code"])
            a = agg.setdefault(key, {
                "line": r["line"], "code": offer["code"],
                "title": offer["title"], "description": offer["description"],
                "inclusion": offer["inclusion"], "offer_type": offer["offer_type"],
                "featured": offer["featured"],
                "_cells": 0, "_regions": set(), "_cats": set(), "_ships": set(),
                "_dates": set(), "_sail": [], "_hashes": set(),
            })
            a["_cells"] += r["n"]
            a["_regions"].add(r["region"])
            a["_cats"].add(r["cabin_category"])
            a["_ships"].add(r["ship"])
            a["_dates"].add(r["scrape_date"])
            a["_sail"].append(r["sail_date"])
            a["_hashes"].add(r["promo_hash"])

    total_by_line = dict(conn.execute(
        f"SELECT line, COUNT(*) FROM observations WHERE {clause} GROUP BY line",
        args))

    out = []
    for key in sorted(agg, key=lambda k: (k[0], -agg[k]["_cells"])):
        a = agg[key]
        total = total_by_line.get(a["line"]) or 0
        sail = [d for d in a["_sail"] if d]
        out.append({
            "line": a["line"], "code": a["code"], "title": a["title"],
            "inclusion": a["inclusion"], "offer_type": a["offer_type"],
            "featured": a["featured"],
            "cells": a["_cells"],
            "share_of_line_cells": round(a["_cells"] / total, 4) if total else None,
            "regions": ", ".join(sorted(x for x in a["_regions"] if x)),
            "cabin_categories": ", ".join(sorted(x for x in a["_cats"] if x)),
            "n_ships": len(a["_ships"]),
            "bundles": len(a["_hashes"]),
            "first_scrape": min(a["_dates"]), "last_scrape": max(a["_dates"]),
            "sail_from": min(sail) if sail else None,
            "sail_to": max(sail) if sail else None,
            "description": a["description"],
        })

    silent = [c.line for k, c in CAPABILITIES.items()
              if not c.exposes_promo_detail]
    if silent:
        basis = basis.with_caveat(
            f"NOT ALL LINES PUBLISH OFFERS: {', '.join(sorted(silent))} expose "
            "no promo detail at all, so their absence from this table is a gap "
            "in the source, not evidence that they are not discounting. Any "
            "cross-line read on promotion intensity is one-sided.")
    return Result("promo_reference", basis, out, notes=(
        "One row per (line, offer code). A promo_hash is a bundle of offers, "
        "so one bundle contributes to several rows; `bundles` counts how many "
        "distinct bundles carried this offer.",
        "share_of_line_cells is out of ALL that line's cells in scope, priced "
        "or not, so it reads as reach across the book.",
        "`inclusion` = Mandatory means the vendor applies it automatically; it "
        "is a headline fare condition, not an opt-in the shopper chose.",
        "Text is the vendor's own, verbatim from the archived payload.",
    ))


def promo_diff(conn: sqlite3.Connection, *, tier: str,
               lines: Sequence[str] | None = None,
               regions: Sequence[str] | None = None,
               by: Sequence[str] = ("line", "region"),
               min_cells: int = 20) -> Result:
    """Which offers appeared, widened, narrowed or disappeared between dates.

    The previous version counted hashes: "3 new, 1 dropped". That tells you
    something moved and not what, which is unusable in a note -- you cannot
    write "NCLH added a promotion" and leave it there. This reports the offer
    codes and titles themselves, with the share of cells carrying each, so a
    change reads as "`Kids Sail Free` went from 30% of Caribbean cells to 36%".

    Reach is the measure, not count: an offer on every sailing and an offer on
    one ship are both "one promo", and promotional intensity is the difference
    between them.

    Reach is computed on COMMON SUPPORT -- the cabins observed on both dates --
    for the same reason as depletion_rate and cohort_index. Share-of-the-book
    moves when the book changes, so over the 2026-09-15/16 scope widening the
    unrestricted figures show `2-For-1 Deposits` collapsing 24 points in a day.
    It did not; the near-term sailings that entered simply carry it less often.
    The `naive_*` columns keep that contaminated version visible beside the
    matched one rather than quietly discarding it.
    """
    clause, args = _where(tier, lines=lines, regions=regions)
    basis = _basis(conn, tier, clause, args)
    offers = _offer_index(conn)
    group_cols = list(by)

    rows = conn.execute(
        f"""SELECT {', '.join(group_cols)}, scrape_date, promo_hash,
                   line AS _line, sailing_id AS _sid,
                   cabin_subcategory AS _sub, market AS _mkt
            FROM observations WHERE {clause}""", args).fetchall()

    # (group) -> date -> {cell key: frozenset of offer codes}
    book: dict[tuple, dict[str, dict[tuple, frozenset]]] = (
        collections.defaultdict(lambda: collections.defaultdict(dict)))
    titles: dict[str, dict[str, str]] = {}
    for r in rows:
        decoded = offers.get(r["promo_hash"], [])
        for offer in decoded:
            titles.setdefault(offer["code"], offer)
        book[tuple(r[g] for g in group_cols)][r["scrape_date"]][
            (r["_line"], r["_sid"], r["_sub"], r["_mkt"])] = frozenset(
                o["code"] for o in decoded)

    def reach(cells: dict[tuple, frozenset], keys, code: str) -> int:
        return sum(1 for k in keys if code in cells.get(k, ()))

    out = []
    for key in sorted(book, key=lambda k: tuple(map(str, k))):
        dates = sorted(book[key])
        for prev_d, cur_d in zip(dates, dates[1:]):
            prev, cur = book[key][prev_d], book[key][cur_d]
            matched = set(prev) & set(cur)
            if not matched:
                continue
            codes = {c for k in matched for c in prev.get(k, ())} | \
                    {c for k in matched for c in cur.get(k, ())}
            n_m = len(matched)
            for code in sorted(codes):
                m_before = reach(prev, matched, code)
                m_after = reach(cur, matched, code)
                s_before, s_after = m_before / n_m, m_after / n_m
                delta = s_after - s_before

                n_before, n_after = len(prev), len(cur)
                nv_before = reach(prev, prev, code) / n_before if n_before else None
                nv_after = reach(cur, cur, code) / n_after if n_after else None

                if m_before == 0:
                    status = "NEW"
                elif m_after == 0:
                    status = "WITHDRAWN"
                else:
                    status = ("widened" if delta > 0.02 else
                              "narrowed" if delta < -0.02 else "unchanged")

                d = dict(zip(group_cols, key))
                offer = titles.get(code, {})
                d.update({
                    "from_date": prev_d, "to_date": cur_d,
                    "code": code, "title": offer.get("title", ""),
                    "inclusion": offer.get("inclusion", ""),
                    "matched_cells": n_m,
                    "cells_before": m_before, "cells_after": m_after,
                    "share_before": round(s_before, 4),
                    "share_after": round(s_after, 4),
                    "delta_share_pp": round(delta * 100, 2),
                    "status": status,
                    "naive_share_before": (round(nv_before, 4)
                                           if nv_before is not None else None),
                    "naive_share_after": (round(nv_after, 4)
                                          if nv_after is not None else None),
                    "mix_effect_pp": (round(((nv_after - nv_before) - delta) * 100, 2)
                                      if nv_before is not None
                                      and nv_after is not None else None),
                    "sample": ("THIN" if n_m < min_cells else "ok") + f" (n={n_m})",
                })
                out.append(d)

    quiet = sorted(c.line for c in CAPABILITIES.values()
                   if not c.exposes_promo_detail)
    if quiet:
        basis = basis.with_caveat(
            f"ONE-SIDED: {', '.join(quiet)} publish no offer detail, so this "
            "table can only describe NCLH's promotional behaviour. It cannot "
            "support 'NCLH is discounting harder than the peer' -- the peer's "
            "discounting is not observable here at all.")
    if not out:
        basis = basis.with_caveat(
            "NO CHURN OBSERVABLE: no group has cabins carrying offer data on "
            "two collection dates in this scope, so nothing can have changed "
            "yet. More than one collection date is not the same as two "
            "readings of the same inventory.")
    return Result("promo_diff", basis, out, notes=(
        "COMMON SUPPORT: shares are the fraction of cabins observed on BOTH "
        "dates that carried the offer. Sailings entering the book cannot move "
        "them, so a widened collection scope cannot read as a promo change.",
        "naive_* is share of everything each date returned; mix_effect_pp is "
        "how many points of the naive move are composition rather than offer.",
        "Reach, not count: an offer on one ship and an offer fleet-wide are "
        "both 'one promo', and the difference is the whole signal.",
        "status: NEW / WITHDRAWN are appearance and disappearance on matched "
        "cabins; widened and narrowed are reach moves beyond 2 points.",
        "A promotion that deepens without widening does not show here: this "
        "measures reach, not discount depth. Depth is in the fare.",
    ))


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
    "promo-reference": promo_reference,
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
