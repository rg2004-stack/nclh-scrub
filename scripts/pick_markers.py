"""Choose the daily-marker itinerary list from a completed weekly-full run.

Selection criteria, from the build spec:

  * ~20-30 sailings representing the key regions, weighted to Southern Europe
    and Caribbean
  * all four standard cabin categories covered
  * the near-term control cohort (Oct-Dec 2026 departures)
  * whatever is still open +/- 14 days around 2026-11-04 (NCLH Q3 print)

A marker entry is an *itinerary code*, and one code yields many sailings across
many dates, so the job is to choose a set of codes whose combined sailings
cover the strata -- not to pick individual sailings.

IMPORTANT about near-term coverage
----------------------------------
The weekly-full tier stores only Jan-Aug 2027 rows, so the database alone
cannot say which itineraries also sail Oct-Dec 2026. Where the raw archive is
present (data/raw/) this script reads the unfiltered payloads to verify
near-term and earnings-window coverage. Without it, those two criteria are
reported as UNVERIFIED rather than assumed -- pick on what is actually known.

    python scripts/pick_markers.py              # propose, print rationale
    python scripts/pick_markers.py --write      # also update config/panel.yaml
"""
from __future__ import annotations

import argparse
import collections
import datetime
import glob
import gzip
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PRIORITY_REGIONS = ["Southern Europe", "Caribbean"]   # weighted, per the spec
OTHER_REGIONS = ["Northern Europe", "Bermuda", "Alaska"]
CATEGORIES = ("inside", "oceanview", "balcony", "suite")
TARGET_TOTAL = 26           # midpoint of the spec's 20-30
NEAR_TERM = ("2026-10-01", "2026-12-31")
EVENT_DATE = datetime.date(2026, 11, 4)
EVENT_DAYS = 14


def near_term_coverage() -> dict[str, dict]:
    """Sail dates per itinerary code, read from the unfiltered raw archive."""
    cov: dict[str, set[str]] = collections.defaultdict(set)
    for path in glob.glob("data/raw/**/*.json.gz", recursive=True):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception:
            continue
        # NCL sailings payload
        code = payload.get("itineraryCode")
        for row in payload.get("pricingStateRooms") or []:
            d = str(row.get("sailStartDate") or "")[:10]
            if code and d:
                cov[code].add(d)
        # Carnival search payload
        for it in (payload.get("results") or {}).get("itineraries") or []:
            c = it.get("code")
            for s in it.get("sailings") or []:
                d = str(s.get("departureDate") or "")[:10]
                if c and d:
                    cov[c].add(d)

    lo = (EVENT_DATE - datetime.timedelta(days=EVENT_DAYS)).isoformat()
    hi = (EVENT_DATE + datetime.timedelta(days=EVENT_DAYS)).isoformat()
    out = {}
    for code, dates in cov.items():
        out[code] = {
            "near_term": sorted(d for d in dates if NEAR_TERM[0] <= d <= NEAR_TERM[1]),
            "event": sorted(d for d in dates if lo <= d <= hi),
        }
    return out


def candidates(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT line, itinerary_code, region,
                  COUNT(DISTINCT cabin_category) cats,
                  COUNT(DISTINCT sailing_id) sailings,
                  COUNT(*) obs, MIN(sail_date) a, MAX(sail_date) b,
                  MAX(ship) ship, MAX(embark_port) port,
                  SUM(CASE WHEN availability_status='sold_out' THEN 1 ELSE 0 END) sold
           FROM observations
           WHERE itinerary_code IS NOT NULL AND region IS NOT NULL
           GROUP BY line, itinerary_code, region"""
    ).fetchall()
    return [dict(r) for r in rows]


def choose(cands: list[dict], coverage: dict[str, dict]) -> list[dict]:
    """Pick codes covering the priority regions first, all categories, and
    (where verifiable) the near-term and earnings windows."""
    for c in cands:
        cov = coverage.get(c["itinerary_code"], {})
        c["near_term"] = len(cov.get("near_term", []))
        c["event"] = len(cov.get("event", []))
        c["verified"] = c["itinerary_code"] in coverage
        # Prefer: full category ladder, earnings-window coverage, near-term
        # coverage, then breadth of sailings.
        c["score"] = (
            c["cats"] * 10
            + min(c["event"], 4) * 8
            + min(c["near_term"], 6) * 4
            + min(c["sailings"], 12)
        )

    chosen: list[dict] = []
    by_region = collections.defaultdict(list)
    for c in sorted(cands, key=lambda x: -x["score"]):
        by_region[c["region"]].append(c)

    # Weighted allocation: priority regions get the bulk of the budget.
    quota = {}
    prio = [r for r in PRIORITY_REGIONS if by_region.get(r)]
    other = [r for r in OTHER_REGIONS if by_region.get(r)]
    if prio:
        per = int(TARGET_TOTAL * 0.7) // len(prio)
        for r in prio:
            quota[r] = per
    if other:
        rest = TARGET_TOTAL - sum(quota.values())
        per = max(1, rest // len(other))
        for r in other:
            quota[r] = per

    for region, budget in quota.items():
        picked, seen_ships = [], set()
        for c in by_region[region]:
            if len(picked) >= budget:
                break
            # spread across ships/ports rather than stacking one vessel
            k = (c["line"], c["ship"])
            if k in seen_ships and len(picked) >= budget // 2:
                continue
            seen_ships.add(k)
            picked.append(c)
        chosen.extend(picked)
    return chosen


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join("data", "panel.sqlite"))
    ap.add_argument("--config", default=os.path.join("config", "panel.yaml"))
    ap.add_argument("--write", action="store_true",
                    help="write the selection into the config file")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    cands = candidates(conn)
    if not cands:
        print("No candidate itineraries in the database. Run weekly-full first.")
        return 1

    coverage = near_term_coverage()
    verified = bool(coverage)
    print(f"candidate itineraries : {len(cands)}")
    print(f"raw archive present   : {'yes' if verified else 'NO'}")
    if not verified:
        print("  -> near-term and earnings-window coverage CANNOT be verified.")
        print("     Selection will use region/category coverage only.")

    chosen = choose(cands, coverage)

    print(f"\nselected {len(chosen)} itineraries\n")
    hdr = (f"  {'line':<10}{'code':<26}{'region':<17}{'cats':>5}"
           f"{'sail':>6}{'near':>6}{'event':>7}  ship")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for c in sorted(chosen, key=lambda x: (x["region"], x["line"], x["itinerary_code"])):
        brand = "NCL" if "Norwegian" in c["line"] else c["line"].split()[0]
        near = str(c["near_term"]) if c["verified"] else "?"
        ev = str(c["event"]) if c["verified"] else "?"
        print(f"  {brand:<10}{c['itinerary_code'][:25]:<26}{c['region'][:16]:<17}"
              f"{c['cats']:>5}{c['sailings']:>6}{near:>6}{ev:>7}  {str(c['ship'])[:22]}")

    print("\ncoverage check")
    regions = collections.Counter(c["region"] for c in chosen)
    for r in PRIORITY_REGIONS + OTHER_REGIONS:
        n = regions.get(r, 0)
        tag = "  <-- PRIORITY" if r in PRIORITY_REGIONS else ""
        print(f"  {r:<18}{n:>3} itineraries{tag}")
    cats = conn.execute(
        "SELECT DISTINCT cabin_category FROM observations "
        "WHERE itinerary_code IN (%s) AND cabin_category IS NOT NULL"
        % ",".join("?" * len(chosen)),
        [c["itinerary_code"] for c in chosen]).fetchall()
    have = {r[0] for r in cats}
    print(f"\n  cabin categories covered: {sorted(have)}")
    missing = set(CATEGORIES) - have
    print(f"  missing categories      : {sorted(missing) if missing else 'none'}")

    if verified:
        ev_total = sum(c["event"] for c in chosen)
        nt_total = sum(c["near_term"] for c in chosen)
        print(f"\n  near-term sailings covered     : {nt_total}")
        print(f"  earnings-window sailings covered: {ev_total}")
        if not ev_total:
            print("  WARNING: no earnings-window coverage in this selection.")
    else:
        print("\n  near-term / earnings coverage  : UNVERIFIED (no raw archive)")

    per_line: dict[str, list[str]] = collections.defaultdict(list)
    for c in chosen:
        key = "ncl" if "Norwegian" in c["line"] else "carnival"
        if c["itinerary_code"] not in per_line[key]:
            per_line[key].append(c["itinerary_code"])

    print("\nconfig block:\n")
    print("    marker_itineraries:")
    for key in ("ncl", "carnival"):
        codes = per_line.get(key, [])
        print(f"      {key}: [{', '.join(codes)}]")

    if args.write:
        import yaml
        with open(args.config, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        doc["tiers"]["daily-marker"]["marker_itineraries"] = {
            "ncl": per_line.get("ncl", []),
            "carnival": per_line.get("carnival", []),
        }
        with open(args.config, "w", encoding="utf-8") as fh:
            yaml.safe_dump(doc, fh, sort_keys=False, default_flow_style=False)
        print(f"\nwrote selection into {args.config}")
        print("NOTE: safe_dump rewrites the file and drops comments. Review the")
        print("diff before committing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
