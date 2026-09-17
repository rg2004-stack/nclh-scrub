"""The analysis suite, run end to end and written to one XLSX per collection.

Why this exists
---------------
Everything the panel knows was, until now, reachable only by remembering a
command. That is fine while one person is driving it by hand and useless the
moment a scheduled run lands at 06:10 UTC with nobody watching. So every
scheduled collection now finishes by running all six analyses across every
configured region and committing a workbook next to the raw JSONL, in the same
run and the same commit. The evidence and the read on it stay together.

What a workbook is, and is not
------------------------------
It is DERIVED. `data/observations/*.jsonl.gz` is the observed record and is
immutable; a report is a pure function of that record plus this code, and may
legitimately be regenerated -- a same-day single-line backfill, or a fixed
analysis, genuinely should change the numbers.

But "may be regenerated" must not mean "changes silently". So:

  * the workbook is only rewritten when its CONTENT changes, compared through
    a digest of the result rows rather than of the file bytes (an XLSX is a zip
    and its bytes differ on every write regardless), so an unchanged rerun is a
    no-op in git rather than a noisy binary diff;
  * when content does change, the per-sheet row-count deltas are appended to
    `reports/REGENERATIONS.jsonl`, so a future reader hitting a binary diff in
    `git log` can find out which sheets moved and why the run happened.

Mixed time scope, stated rather than assumed
--------------------------------------------
The file is named for a scrape date, but only the cross-sectional analyses are
pinned to that date. The three time-series analyses deliberately span the whole
history of the tier -- that is what makes them time series. The Index sheet
says which is which for every sheet, because a workbook called
`2026-09-16__weekly-full.xlsx` otherwise invites the reading that all of it
describes 16 September.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from panel import analysis as an
from panel.config import DEFAULT_CONFIG_PATH, load_config

REPORT_DIR = "reports"
REGEN_LEDGER = "REGENERATIONS.jsonl"      # plain text: readable in the GitHub UI

# Scope of each analysis in TIME, which is not the same for all of them.
CROSS_SECTION = "this scrape date"
TIME_SERIES = "all scrape dates in the tier"
# A lookup table, not a measurement: it spans every date because it is the
# decoder ring for the other sheets, and no drift warning applies to it.
REFERENCE = "reference (every date in the tier)"

# A time series assumes each collection date sampled the same population. When
# the collector's own scope changes between dates -- a widened sail window, a
# fixed destination sweep, a new region -- that assumption breaks, and a slope
# computed across the break measures the change to the COLLECTOR rather than
# anything about the market. It still looks exactly like a measurement.
SCOPE_DRIFT_PCT = 20.0

# Caveat prefixes that mean "this sheet's numbers cannot yet be read as a
# measurement". They are surfaced in the Index status column and the Actions
# summary, not only on the sheet itself -- a directory that says "ok" beside a
# sheet whose every value is 100 by construction is worse than no directory.
# Renaming a caveat without adding it here is how that regression happens, so
# tests pin each one to the analysis that emits it.
BLOCKING_CAVEATS = (
    "NOT COMPUTABLE",              # depletion_rate, <2 collection dates
    "NO COVERAGE",                 # earnings_window_compare, no sailings in window
    "NO HISTORY",                  # cohort_index, no priced rows
    "SINCE INCEPTION = ONE DAY",   # cohort_index, single collection date
    "NO COMPARABLE REPRICING",     # cohort_index, no cabin seen twice
    "MIXED",                       # combine_bases, pooled tiers
    "COLLECTION SCOPE CHANGED",    # scope_drift
)


@dataclass(frozen=True)
class SheetSpec:
    key: str                  # CLI name, matches analysis.ANALYSES
    sheet: str                # Excel sheet name (<=31 chars, no []:*?/\)
    scope: str                # CROSS_SECTION or TIME_SERIES
    fn: Callable[..., an.Result]
    purpose: str


# Order is the reading order of the workbook: what is in the book, how it is
# priced against the peer, then the three that need history to say anything.
SUITE: tuple[SheetSpec, ...] = (
    SheetSpec("availability", "Availability", CROSS_SECTION,
              an.availability_snapshot,
              "Inventory mix per group. The denominator that separates "
              "'price rose' from 'the cheap cabins are gone'."),
    SheetSpec("booking-curve", "Booking curve", CROSS_SECTION,
              # run_suite passes regions; the curve deliberately overrides it.
              lambda conn, **kw: an.booking_curve(
                  conn, **{**kw, "nights": (7, 8), "by_region": True}),
              "Price, dispersion and sold-out share against days to "
              "departure, per region: 7-8 night, cruise-only, suites and "
              "non-peer-comparable cabins excluded. Bands are 15 days near "
              "departure, where fares move, and widen further out."),
    SheetSpec("final-payment", "Final payment", CROSS_SECTION,
              lambda conn, **kw: an.final_payment_test(
                  conn, **{**kw, "regions": ["Caribbean"], "nights": (7, 8),
                           "cutoffs": tuple(range(15, 211, 15))}),
              "Do fares and sold-out share jump as sailings cross final "
              "payment? A cutoff SCAN, so the boundary is located rather than "
              "assumed. Check peer_own_jump_pct before reading any row: where "
              "the control jumps too, the cutoff has caught the calendar."),
    SheetSpec("peer-gap", "Peer gap", CROSS_SECTION,
              an.peer_gap,
              "NCLH vs peer median pppn, per region and cabin category, on "
              "cruise-only rows."),
    SheetSpec("earnings-window", "Earnings window", CROSS_SECTION,
              an.earnings_window_compare,
              "Sailings departing inside the NCLH print window vs outside it. "
              "Split on sail date, not collection date."),
    SheetSpec("cohort-index", "Cohort index", TIME_SERIES,
              an.cohort_index,
              "Since-inception index on a MATCHED BASKET of cabins, with the "
              "naive all-rows index beside it. index_matched cannot be moved "
              "by sailings entering the book; mix_effect_pp is how much of the "
              "naive move is composition rather than price."),
    SheetSpec("depletion", "Depletion", TIME_SERIES,
              an.depletion_rate,
              "Change in closed share per day. Needs >=2 collection dates or "
              "it reports nothing rather than inventing a slope."),
    SheetSpec("promo-diff", "Promo diff", TIME_SERIES,
              an.promo_diff,
              "Which offers widened, narrowed, appeared or were withdrawn "
              "between dates, by reach on cabins seen both times."),
    SheetSpec("promo-reference", "Promo reference", REFERENCE,
              an.promo_reference,
              "The decoder ring: every offer observed, in the vendor's own "
              "words, with its reach and scope. Read the Promo diff against "
              "this rather than against a hash."),
)


@dataclass
class SheetResult:
    spec: SheetSpec
    result: an.Result | None = None
    error: str | None = None

    @property
    def rows(self) -> list[dict[str, Any]]:
        return self.result.rows if self.result else []

    @property
    def status(self) -> str:
        if self.error:
            return f"FAILED: {self.error}"
        if not self.rows:
            return "no rows"
        blocking = [c for c in self.result.basis.caveats
                    if c.startswith(BLOCKING_CAVEATS)]
        return blocking[0].split(",")[0].split(":")[0] if blocking else "ok"


# -- running the suite ------------------------------------------------------

def suite_regions(config_path: str | os.PathLike[str] = DEFAULT_CONFIG_PATH
                  ) -> list[str]:
    """Every region any enabled line is configured to collect.

    Read from config rather than hardcoded. A hardcoded list is how a region
    added to the collector ends up absent from every report without anyone
    noticing -- Carnival's Transatlantic bucket would have been exactly that.
    """
    cfg = load_config(config_path)
    seen: list[str] = []
    for line in cfg.lines.values():
        if not line.enabled:
            continue
        for region in line.regions:
            if region not in seen:
                seen.append(region)
    return seen


def latest_scrape_date(conn: sqlite3.Connection, tier: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(scrape_date) FROM observations WHERE tier = ?", [tier]
    ).fetchone()
    return row[0] if row else None


def run_suite(conn: sqlite3.Connection, *, tier: str, scrape_date: str | None,
              regions: Sequence[str],
              drift_note: str | None = None) -> list[SheetResult]:
    """Run every analysis, capturing failures instead of aborting the run.

    One analysis raising must not cost us the other five, and must not stop the
    workbook being committed -- a report that says a sheet failed is worth more
    than no report at all. The failure is written into the workbook, not
    swallowed.
    """
    out: list[SheetResult] = []
    for spec in SUITE:
        kwargs: dict[str, Any] = {"tier": tier, "regions": list(regions)}
        if (spec.scope is CROSS_SECTION and scrape_date
                and spec.key != "earnings-window"):
            kwargs["scrape_date"] = scrape_date
        try:
            res = spec.fn(conn, **kwargs)
            # The drift warning goes on the RESULT, not in a footnote on the
            # Index, so it travels with the sheet it invalidates.
            if drift_note and spec.scope is TIME_SERIES:
                res = replace(res, basis=res.basis.with_caveat(drift_note))
            out.append(SheetResult(spec, result=res))
        except Exception as exc:                       # noqa: BLE001
            out.append(SheetResult(spec, error=f"{type(exc).__name__}: {exc}"))
    return out



def scope_drift(conn: sqlite3.Connection, tier: str) -> list[dict[str, Any]]:
    """Compare consecutive collection dates on what the collector actually took.

    Returns one row per adjacent pair with a `comparable` verdict. This is the
    check that stops a widened sail window from being published as depletion:
    between 2026-09-15 and 2026-09-16 the weekly sweep gained six months of
    near-term sailings and a fixed Carnival destination sweep, so every
    endpoint slope across that pair describes the fix, not the book.
    """
    dates = [dict(r) for r in conn.execute(
        """SELECT scrape_date,
                  COUNT(*) AS observations,
                  COUNT(DISTINCT sailing_id) AS sailings,
                  MIN(sail_date) AS sail_from, MAX(sail_date) AS sail_to
           FROM observations WHERE tier = ?
           GROUP BY scrape_date ORDER BY scrape_date""", [tier])]
    for d in dates:
        d["regions"] = {r[0] for r in conn.execute(
            "SELECT DISTINCT region FROM observations WHERE tier = ? AND "
            "scrape_date = ?", [tier, d["scrape_date"]])}
        d["lines"] = {r[0] for r in conn.execute(
            "SELECT DISTINCT line FROM observations WHERE tier = ? AND "
            "scrape_date = ?", [tier, d["scrape_date"]])}

    out: list[dict[str, Any]] = []
    for prev, cur in zip(dates, dates[1:]):
        reasons = []
        if prev["sail_from"] != cur["sail_from"] or prev["sail_to"] != cur["sail_to"]:
            reasons.append(
                f"sail window moved {prev['sail_from']}..{prev['sail_to']} -> "
                f"{cur['sail_from']}..{cur['sail_to']}")
        gained = cur["regions"] - prev["regions"]
        lost = prev["regions"] - cur["regions"]
        if gained:
            reasons.append("regions added: " + ", ".join(sorted(gained)))
        if lost:
            reasons.append("regions dropped: " + ", ".join(sorted(lost)))
        if cur["lines"] != prev["lines"]:
            reasons.append(f"lines changed {sorted(prev['lines'])} -> "
                           f"{sorted(cur['lines'])}")
        base = prev["sailings"] or 1
        delta_pct = round(100.0 * (cur["sailings"] - base) / base, 1)
        if abs(delta_pct) >= SCOPE_DRIFT_PCT:
            reasons.append(f"sailing count moved {delta_pct:+}% "
                           f"({prev['sailings']} -> {cur['sailings']})")
        out.append({
            "from_date": prev["scrape_date"], "to_date": cur["scrape_date"],
            "sailings_before": prev["sailings"], "sailings_after": cur["sailings"],
            "sailings_delta_pct": delta_pct,
            "comparable": "NO" if reasons else "yes",
            "why_not": "; ".join(reasons),
        })
    return out


def drift_caveat(drift: Sequence[dict[str, Any]]) -> str | None:
    """The one sentence a time-series sheet must carry when scope moved."""
    broken = [d for d in drift if d["comparable"] == "NO"]
    if not broken:
        return None
    pairs = "; ".join(f"{d['from_date']}->{d['to_date']}: {d['why_not']}"
                      for d in broken)
    return (
        "COLLECTION SCOPE CHANGED BETWEEN DATES, so any movement here is "
        "partly the collector and not the market. A time series assumes each "
        f"date sampled the same population; on {len(broken)} adjacent pair(s) "
        f"it did not -- {pairs}. Treat every slope and index spanning those "
        "dates as uninterpretable until a run of stable-scope dates exists. "
        "The per-row cell counts show the size of the break."
    )


def panel_facts(conn: sqlite3.Connection, tier: str,
                scrape_date: str | None) -> list[tuple[str, Any]]:
    """Provenance for the Index sheet: what the workbook was computed from."""
    def one(sql: str, args: Sequence[Any] = ()) -> Any:
        return conn.execute(sql, list(args)).fetchone()[0]

    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT scrape_date FROM observations WHERE tier = ? "
        "ORDER BY scrape_date", [tier])]
    currencies = sorted({r[0] for r in conn.execute(
        "SELECT DISTINCT currency FROM observations "
        "WHERE tier = ? AND price_total IS NOT NULL", [tier]) if r[0]})
    markets = sorted({r[0] for r in conn.execute(
        "SELECT DISTINCT market FROM observations WHERE tier = ?", [tier]) if r[0]})
    lines = sorted({r[0] for r in conn.execute(
        "SELECT DISTINCT line FROM observations WHERE tier = ?", [tier])})
    facts = [
        ("Tier", tier),
        ("Scrape date this file is named for", scrape_date or "(none)"),
        ("Collection dates present in tier", ", ".join(dates) or "(none)"),
        ("Lines", ", ".join(lines) or "(none)"),
        ("Observations in tier (all dates)",
         one("SELECT COUNT(*) FROM observations WHERE tier = ?", [tier])),
    ]
    if scrape_date:
        facts.append((
            "Observations on this scrape date",
            one("SELECT COUNT(*) FROM observations WHERE tier = ? AND "
                "scrape_date = ?", [tier, scrape_date])))
    facts += [
        ("Sail window covered",
         "{}..{}".format(*conn.execute(
             "SELECT MIN(sail_date), MAX(sail_date) FROM observations "
             "WHERE tier = ?", [tier]).fetchone())),
        ("Currencies present", ", ".join(currencies) or "(no priced rows)"),
        ("Markets served", ", ".join(markets) or "(none)"),
    ]
    return facts


def region_coverage(conn: sqlite3.Connection, tier: str,
                    scrape_date: str | None,
                    regions: Sequence[str]) -> list[dict[str, Any]]:
    """Rows per configured region, INCLUDING the ones that returned nothing.

    A region absent from every sheet because it has no rows looks identical to
    a region nobody asked for. This table is the difference.
    """
    args: list[Any] = [tier]
    clause = "tier = ?"
    if scrape_date:
        clause += " AND scrape_date = ?"
        args.append(scrape_date)
    counts = {r["region"]: r for r in conn.execute(
        f"""SELECT region, COUNT(*) n, COUNT(DISTINCT sailing_id) sailings,
                   COUNT(DISTINCT line) lines,
                   SUM(is_package = 1) packages
            FROM observations WHERE {clause} GROUP BY region""", args)}
    out = []
    for region in regions:
        r = counts.get(region)
        out.append({
            "region": region,
            "observations": r["n"] if r else 0,
            "sailings": r["sailings"] if r else 0,
            "lines": r["lines"] if r else 0,
            "package_rows": (r["packages"] or 0) if r else 0,
            "note": "" if r else "no rows on this basis -- absent from every sheet",
        })
    for region in sorted(set(counts) - set(regions)):
        r = counts[region]
        out.append({
            "region": region, "observations": r["n"], "sailings": r["sailings"],
            "lines": r["lines"], "package_rows": r["packages"] or 0,
            "note": "PRESENT IN DATA BUT NOT IN CONFIG -- check region mapping",
        })
    return out


# -- content digest ---------------------------------------------------------

def content_digest(sheets: Sequence[SheetResult]) -> tuple[str, dict[str, Any]]:
    """Hash the RESULTS, not the file.

    An XLSX is a zip; its bytes change on every write whatever the numbers do.
    Comparing bytes would make every rerun a diff and make a real change
    indistinguishable from noise, so the comparison is on the payload.
    """
    payload = {
        s.spec.key: {
            "result": s.result.to_dict() if s.result else {"error": s.error},
            # The Index status is rendered into the workbook, so a change to it
            # is a change to the file even when every number is identical.
            "status": s.status,
        }
        for s in sheets
    }
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      default=str).encode("utf-8")
    manifest = {
        "digest": hashlib.sha256(body).hexdigest(),
        "sheets": {s.spec.key: {"rows": len(s.rows), "status": s.status}
                   for s in sheets},
    }
    return manifest["digest"], manifest


def log_regeneration(out_dir: str, *, report: str, reason: str, tool: str,
                     before: dict | None, after: dict) -> str:
    """Append one line explaining why a committed workbook's numbers moved."""
    changed = {}
    for key, now in after["sheets"].items():
        was = (before or {}).get("sheets", {}).get(key)
        if was != now:
            changed[key] = {"before": was, "after": now}
    record = {
        "regenerated_at_utc": datetime.now(timezone.utc)
                              .replace(microsecond=0).isoformat(),
        "report": report,
        "reason": reason,
        "tool": tool,
        "digest_before": (before or {}).get("digest"),
        "digest_after": after["digest"],
        "sheets_changed": changed,
    }
    path = os.path.join(out_dir, REGEN_LEDGER)
    os.makedirs(out_dir, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return path


# -- workbook ---------------------------------------------------------------

_HEADER_FILL = "FFE8EEF7"
_WARN_FILL = "FFFFF2CC"
_BAD_FILL = "FFF8CBAD"

_MONEY = ("pppn", "price", "fare", "taxes")
_SHARE = ("share", "pct", "index", "rate")


def _sheet_styles():
    from openpyxl.styles import Alignment, Font, PatternFill
    return {
        "title": Font(bold=True, size=13),
        "head": Font(bold=True),
        "small": Font(size=9, italic=True),
        "warn": Font(size=9, color="FF9C5700"),
        "fill": PatternFill("solid", fgColor=_HEADER_FILL),
        "warnfill": PatternFill("solid", fgColor=_WARN_FILL),
        "badfill": PatternFill("solid", fgColor=_BAD_FILL),
        "wrap": Alignment(wrap_text=True, vertical="top"),
    }


def _write_table(ws, rows: Sequence[dict[str, Any]], start_row: int, st) -> int:
    """Write a header + rows, freeze the header, autofilter it. Returns next row."""
    if not rows:
        ws.cell(row=start_row, column=1, value="(no rows)").font = st["small"]
        return start_row + 1
    cols = list(rows[0].keys())
    for j, name in enumerate(cols, start=1):
        c = ws.cell(row=start_row, column=j, value=str(name))
        c.font, c.fill = st["head"], st["fill"]
    for i, row in enumerate(rows, start=start_row + 1):
        for j, name in enumerate(cols, start=1):
            v = row.get(name)
            cell = ws.cell(row=i, column=j,
                           value=v if isinstance(v, (int, float, str)) or v is None
                           else str(v))
            low = str(name).lower()
            if isinstance(v, float):
                if any(k in low for k in _MONEY):
                    cell.number_format = "#,##0.00"
                elif any(k in low for k in _SHARE):
                    cell.number_format = "0.0000"
            if low == "sample" and isinstance(v, str) and v != "ok":
                cell.fill = st["warnfill"] if v.startswith("THIN") else st["badfill"]
            if low == "status" and isinstance(v, str) and v.startswith(("FAILED",
                                                                       "insufficient")):
                cell.fill = st["warnfill"]

    end = start_row + len(rows)
    ws.auto_filter.ref = f"A{start_row}:{ws.cell(row=start_row, column=len(cols)).column_letter}{end}"
    ws.freeze_panes = ws.cell(row=start_row + 1, column=1)
    for j, name in enumerate(cols, start=1):
        width = max(len(str(name)),
                    *(len(str(r.get(name, ""))) for r in rows[:200]))
        ws.column_dimensions[ws.cell(row=1, column=j).column_letter].width = \
            min(max(width + 2, 9), 46)
    return end + 1


def build_workbook(sheets: Sequence[SheetResult], *, tier: str,
                   scrape_date: str | None, facts: Sequence[tuple[str, Any]],
                   coverage: Sequence[dict[str, Any]], regions: Sequence[str],
                   drift: Sequence[dict[str, Any]] = ()):
    from openpyxl import Workbook

    st = _sheet_styles()
    wb = Workbook()
    idx = wb.active
    idx.title = "Index"

    r = 1
    idx.cell(row=r, column=1,
             value=f"Cruise fare and inventory panel -- {tier}").font = st["title"]
    r += 1
    idx.cell(row=r, column=1, value=(
        "Generated automatically by the collection run. This workbook is "
        "DERIVED from data/observations/*.jsonl.gz, which is the observed "
        "record; regenerate it any time with `python -m panel.report`."
    )).font = st["small"]
    r += 2

    idx.cell(row=r, column=1, value="Provenance").font = st["head"]
    r += 1
    for key, value in facts:
        idx.cell(row=r, column=1, value=key).font = st["head"]
        idx.cell(row=r, column=2, value=value)
        r += 1
    idx.cell(row=r, column=1, value="Generated at (UTC)").font = st["head"]
    idx.cell(row=r, column=2,
             value=datetime.now(timezone.utc).replace(microsecond=0).isoformat())
    r += 2

    idx.cell(row=r, column=1, value="Sheets").font = st["head"]
    r += 1
    idx.cell(row=r, column=1, value=(
        "NOTE: this file is named for one scrape date, but only the "
        "cross-sectional sheets are pinned to it. The time-series sheets span "
        "every collection date in the tier, which is what makes them series. "
        "The `time scope` column below is not decoration."
    )).font = st["warn"]
    r += 2
    r = _write_table(idx, [
        {"sheet": s.spec.sheet, "analysis": s.spec.key,
         "time scope": s.spec.scope, "rows": len(s.rows), "status": s.status,
         "what it measures": s.spec.purpose}
        for s in sheets
    ], r, st)
    r += 1

    idx.cell(row=r, column=1, value="Region coverage on this basis").font = st["head"]
    r += 1
    idx.cell(row=r, column=1, value=(
        "Every region the config asks for, including those that returned "
        "nothing. A region with 0 rows is absent from every sheet; without "
        "this table that is indistinguishable from a region nobody requested."
    )).font = st["small"]
    r += 2
    r = _write_table(idx, list(coverage), r, st)
    r += 1

    idx.cell(row=r, column=1,
             value="Scope comparability between collection dates").font = st["head"]
    r += 1
    idx.cell(row=r, column=1, value=(
        "Whether each pair of adjacent collection dates sampled the same "
        "population. Where `comparable` is NO, the collector itself changed, "
        "and every slope or index spanning that pair is measuring the change "
        "to the collector as well as the market."
    )).font = st["small"]
    r += 2
    _write_table(idx, list(drift) or [{"from_date": "", "to_date": "",
                                       "comparable": "n/a",
                                       "why_not": "fewer than two collection "
                                                  "dates in this tier"}], r, st)
    idx.freeze_panes = None
    idx.column_dimensions["A"].width = 38
    idx.column_dimensions["B"].width = 52

    for s in sheets:
        ws = wb.create_sheet(s.spec.sheet[:31])
        row = 1
        ws.cell(row=row, column=1, value=s.spec.sheet).font = st["title"]
        row += 1
        ws.cell(row=row, column=1, value=s.spec.purpose).font = st["small"]
        row += 1
        ws.cell(row=row, column=1,
                value=f"Time scope: {s.spec.scope}.").font = st["small"]
        row += 1
        if s.error:
            c = ws.cell(row=row, column=1,
                        value=f"THIS ANALYSIS FAILED: {s.error}")
            c.font, c.fill = st["head"], st["badfill"]
            ws.column_dimensions["A"].width = 100
            continue
        ws.cell(row=row, column=1, value=s.result.basis.label()).font = st["small"]
        row += 2
        for caveat in s.result.basis.caveats:
            c = ws.cell(row=row, column=1, value="! " + caveat)
            c.font, c.fill = st["warn"], st["warnfill"]
            row += 1
        for note in s.result.notes:
            ws.cell(row=row, column=1, value="- " + note).font = st["small"]
            row += 1
        row += 1
        _write_table(ws, s.rows, row, st)
    return wb


# -- top level --------------------------------------------------------------

def report_name(tier: str, scrape_date: str | None) -> str:
    return f"{scrape_date or 'all-dates'}__{tier}.xlsx"


def write_report(db_path: str, *, tier: str, scrape_date: str | None = None,
                 out_dir: str = REPORT_DIR,
                 config_path: str | os.PathLike[str] = DEFAULT_CONFIG_PATH,
                 regions: Sequence[str] | None = None,
                 reason: str = "scheduled collection run",
                 tool: str = "panel.report",
                 force: bool = False) -> dict[str, Any]:
    """Run the suite and write one workbook. Returns a summary dict.

    Unchanged content does not rewrite the file, so a rerun that finds nothing
    new is a no-op in git. Changed content is written AND logged, so the binary
    diff has a plain-text explanation sitting beside it.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        scrape_date = scrape_date or latest_scrape_date(conn, tier)
        regions = list(regions) if regions else suite_regions(config_path)
        drift = scope_drift(conn, tier)
        note = drift_caveat(drift)
        sheets = run_suite(conn, tier=tier, scrape_date=scrape_date,
                           regions=regions, drift_note=note)
        facts = panel_facts(conn, tier, scrape_date)
        coverage = region_coverage(conn, tier, scrape_date, regions)
    finally:
        conn.close()

    if scrape_date is None:
        # An empty tier has nothing to report on. Writing an `all-dates` file
        # here would commit a workbook of empty sheets whose name implies it
        # covers everything, which is worse than no file.
        return {"path": None, "tier": tier, "scrape_date": None,
                "regions": list(regions), "digest": None, "sheets": {},
                "failed": [], "written": False, "regenerated": False,
                "ledger": None, "scope_drift": [],
                "note": f"no observations in tier {tier!r}"}

    digest, manifest = content_digest(sheets)
    manifest.update({
        "tier": tier, "scrape_date": scrape_date, "regions": list(regions),
        "generated_at_utc": datetime.now(timezone.utc)
                            .replace(microsecond=0).isoformat(),
    })

    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, report_name(tier, scrape_date))
    manifest_path = path.replace(".xlsx", ".manifest.json")
    before = None
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, encoding="utf-8") as fh:
                before = json.load(fh)
        except (OSError, ValueError):
            before = None

    existed = os.path.exists(path)
    unchanged = (before or {}).get("digest") == digest and existed
    summary = {
        "scope_drift": [d for d in drift if d["comparable"] == "NO"],
        "path": path, "tier": tier, "scrape_date": scrape_date,
        "regions": list(regions), "digest": digest,
        "sheets": manifest["sheets"],
        "failed": [s.spec.key for s in sheets if s.error],
        "written": False, "regenerated": False, "ledger": None,
    }
    if unchanged and not force:
        summary["note"] = "content identical to the committed report; not rewritten"
        return summary

    wb = build_workbook(sheets, tier=tier, scrape_date=scrape_date, facts=facts,
                        coverage=coverage, regions=regions, drift=drift)
    wb.save(path)
    with open(manifest_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, indent=1, sort_keys=True)
        fh.write("\n")
    summary["written"] = True

    if existed:
        summary["regenerated"] = True
        summary["ledger"] = log_regeneration(
            out_dir, report=os.path.basename(path), reason=reason, tool=tool,
            before=before, after=manifest)
    return summary


def format_summary(summary: dict[str, Any], *, markdown: bool = False) -> str:
    """Human-readable run report. Markdown form goes in the Actions summary."""
    lines: list[str] = []
    head = (f"## Analysis suite - {summary['tier']} {summary['scrape_date']}"
            if markdown else
            f"analysis suite -- {summary['tier']} {summary['scrape_date']}")
    lines.append(head)
    lines.append("")
    if markdown:
        lines += ["| sheet | rows | status |", "|---|---|---|"]
        for key, s in summary["sheets"].items():
            lines.append(f"| {key} | {s['rows']} | {s['status']} |")
    else:
        for key, s in summary["sheets"].items():
            lines.append(f"  {key:<17} {s['rows']:>6} rows   {s['status']}")
    lines.append("")
    if summary.get("scope_drift"):
        pairs = ", ".join(f"{d['from_date']}->{d['to_date']}"
                          for d in summary["scope_drift"])
        lines.append(
            (f"**SCOPE DRIFT on {pairs}** - time-series sheets span a change "
             f"in what the collector took; slopes across it are not "
             f"interpretable." if markdown else
             f"  SCOPE DRIFT on {pairs}: time-series slopes are not "
             f"interpretable across it."))
    if summary["failed"]:
        lines.append(f"**{len(summary['failed'])} analysis(es) FAILED: "
                     f"{', '.join(summary['failed'])}**" if markdown else
                     f"  FAILED: {', '.join(summary['failed'])}")
    if not summary["written"]:
        lines.append(summary.get("note", "not written"))
    elif summary["regenerated"]:
        lines.append(f"Regenerated an existing report; change logged to "
                     f"`{summary['ledger']}`.")
    else:
        lines.append(f"Wrote `{summary['path']}`.")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tier", required=True, choices=list(an.TIERS),
                    help="required: results are never pooled across tiers")
    ap.add_argument("--db", default=os.path.join("data", "panel.sqlite"))
    ap.add_argument("--scrape-date", default=None,
                    help="default: the latest scrape date present in the tier")
    ap.add_argument("--out-dir", default=REPORT_DIR)
    ap.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    ap.add_argument("--region", action="append", dest="regions",
                    help="default: every region any enabled line is configured for")
    ap.add_argument("--reason", default="scheduled collection run",
                    help="recorded in REGENERATIONS.jsonl if this rewrites a report")
    ap.add_argument("--force", action="store_true",
                    help="rewrite even if the content is byte-identical")
    ap.add_argument("--github-summary", action="store_true",
                    help="also append a markdown summary to $GITHUB_STEP_SUMMARY")
    ap.add_argument("--fail-on-analysis-error", action="store_true",
                    help="exit non-zero if any single analysis raised")
    args = ap.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"ERROR: no database at {args.db}", file=sys.stderr)
        return 2

    summary = write_report(args.db, tier=args.tier, scrape_date=args.scrape_date,
                           out_dir=args.out_dir, config_path=args.config,
                           regions=args.regions, reason=args.reason,
                           force=args.force)
    if summary["scrape_date"] is None:
        print(f"no observations in tier {args.tier!r}; nothing to report",
              file=sys.stderr)
        return 0

    print(format_summary(summary))
    dest = os.environ.get("GITHUB_STEP_SUMMARY")
    if args.github_summary and dest:
        with open(dest, "a", encoding="utf-8") as fh:
            fh.write(format_summary(summary, markdown=True) + "\n\n")
    return 1 if (summary["failed"] and args.fail_on_analysis_error) else 0


if __name__ == "__main__":
    raise SystemExit(main())
