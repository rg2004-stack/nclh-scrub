"""Backfill is_package / itinerary_nights onto rows collected before schema v4.

The panel captured land+cruise packages ("cruisetours") without marking them as
a different product. NCL prices those as a package -- Denali itineraries carry
duration {"itinerary": 14, "cruising": 7} -- while stamping the row with the
cruise segment only, so price_pppn on those rows divides a 14-day package fare
by 7 cruise nights and reads roughly twice the true nightly cruise rate. Left
unmarked they silently inflate any NCL price level and any peer comparison.

The collector now captures the flag, but rows already in the panel predate it.
This script classifies them from data/sail_dates.json, which
scripts/probe_sail_dates.py writes per itinerary. It touches only is_package and
itinerary_nights -- no price is recomputed, because there is no cruise-only fare
to recompute it to: NCL never published one for these products.

    python scripts/backfill_packages.py --dry-run
    python scripts/backfill_packages.py
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CALENDAR = os.path.join("data", "sail_dates.json")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join("data", "panel.sqlite"))
    ap.add_argument("--calendar", default=CALENDAR)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.calendar):
        print(f"no calendar at {args.calendar}; run scripts/probe_sail_dates.py first")
        return 1
    with open(args.calendar, encoding="utf-8") as fh:
        calendar = {k: v for k, v in json.load(fh).items() if isinstance(v, dict)}

    classified = {
        code: rec for code, rec in calendar.items()
        if rec.get("is_package") is not None
    }
    print(f"calendar itineraries          : {len(calendar)}")
    print(f"  with a product classification: {len(classified)}")
    print(f"  packages                     : "
          f"{sum(1 for r in classified.values() if r['is_package'])}")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    before = conn.execute(
        "SELECT COUNT(*) FROM observations WHERE line LIKE 'Norwegian%' "
        "AND is_package IS NULL").fetchone()[0]
    print(f"\nNCL rows needing classification : {before}")

    updated = pkg_rows = 0
    for code, rec in classified.items():
        cur = conn.execute(
            "UPDATE observations SET is_package = ?, itinerary_nights = ? "
            "WHERE line LIKE 'Norwegian%' AND itinerary_code = ? "
            "AND is_package IS NULL",
            (int(rec["is_package"]), rec.get("itinerary_nights"), code))
        updated += cur.rowcount
        if rec["is_package"]:
            pkg_rows += cur.rowcount

    # Carnival's cruise search returns cruise-only voyages: there is no land
    # package product in that payload, so those rows are 0 by construction
    # rather than by lookup. Without this they would stay NULL and be dropped
    # from every cruise-only comparison, which would quietly remove the entire
    # peer side.
    ccl = conn.execute(
        "UPDATE observations SET is_package = 0, itinerary_nights = nights "
        "WHERE line LIKE 'Carnival%' AND is_package IS NULL").rowcount
    print(f"Carnival rows marked cruise-only: {ccl}")

    remaining = conn.execute(
        "SELECT COUNT(*) FROM observations WHERE line LIKE 'Norwegian%' "
        "AND is_package IS NULL").fetchone()[0]

    if args.dry_run:
        conn.rollback()
        print("(dry run -- rolled back)")
    else:
        conn.commit()

    print(f"rows classified                 : {updated}")
    print(f"  of which land+cruise packages : {pkg_rows}")
    print(f"rows still unclassified         : {remaining}")
    if remaining:
        codes = [r[0] for r in conn.execute(
            "SELECT DISTINCT itinerary_code FROM observations "
            "WHERE line LIKE 'Norwegian%' AND is_package IS NULL LIMIT 10")]
        print(f"  itineraries missing from the calendar: {codes}")
        print("  re-run scripts/probe_sail_dates.py to cover them")

    if not args.dry_run and pkg_rows:
        print("\nprice impact of the reclassification (NCL, balcony):")
        for r in conn.execute(
                """SELECT region,
                          ROUND(AVG(CASE WHEN is_package=0 THEN price_pppn END),2) cruise,
                          ROUND(AVG(CASE WHEN is_package=1 THEN price_pppn END),2) pkg,
                          SUM(is_package=1) pkg_rows
                   FROM observations
                   WHERE line LIKE 'Norwegian%' AND cabin_category='balcony'
                     AND price_pppn IS NOT NULL
                   GROUP BY region ORDER BY region"""):
            print(f"  {r['region']:18} cruise-only {str(r['cruise']):>9}  "
                  f"package {str(r['pkg']):>9}  ({r['pkg_rows']} package rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
