"""Choose the daily-marker itinerary list.

Selection criteria, from the build spec:

  * ~20-30 sailings representing the key regions, weighted to Southern Europe
    and Caribbean
  * all four standard cabin categories covered
  * the near-term control cohort (Oct-Dec 2026 departures)
  * whatever is still open +/- 14 days around 2026-11-04 (NCLH Q3 print)

A marker entry is an *itinerary code*, and one code yields many sailings across
many dates, so the job is to choose a set of codes whose combined sailings
cover the strata -- not to pick individual sailings.

The sampling frame is the near-term universe, NOT the weekly-full panel
-----------------------------------------------------------------------
daily-marker filters to 2026-10-01..2026-12-31, so an itinerary with no
departure in that window collects nothing however good it looks otherwise.
NCL itinerary codes are season-specific: of the 152 NCL itineraries sailing
Oct-Dec 2026, only 35 appear anywhere in the Jan-Aug 2027 weekly-full panel.
Choosing markers from the panel therefore picks mostly dead codes, and worst of
all in the Mediterranean. So candidates come from:

  * NCL       -- data/sail_dates.json, the calendar written by
                 scripts/probe_sail_dates.py, which enumerates the near-term
                 window directly. Run that first.
  * Carnival  -- the panel plus data/raw/, whose search payloads carry their
                 own sailing dates and already cover every Carnival itinerary
                 in the panel.

Any candidate without a near-term departure is dropped, not merely ranked low.

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
from typing import Mapping

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PRIORITY_REGIONS = ["Caribbean", "Southern Europe"]  # Caribbean first:
# it is the only region carrying both lines at depth, so it is the only one
# that can identify a cross-line test. Southern Europe stays weighted for
# the NCL-only series the pitch also needs.
OTHER_REGIONS = ["Northern Europe", "Bermuda", "Alaska"]
CATEGORIES = ("inside", "oceanview", "balcony", "suite")
TARGET_TOTAL = 26           # midpoint of the spec's 20-30
# The window markers are selected over. Defaults to the daily tier's own
# rolling horizon, so the picker and the collector can never disagree about
# what "near term" means.
NEAR_TERM = ("2026-10-01", "2026-12-31")     # overwritten in main() from config
FINAL_PAYMENT_DAYS = 120

# The itinerary-length band the cross-line analyses actually run at. Per-night
# price is not comparable across lengths, so a marker outside this band cannot
# pair with one from the other line however many sailings it has. NCL's
# Caribbean markers are 7-night; Carnival's densest Caribbean product is 4-5
# night, and picking on density alone selected 4-5n itineraries that the 7-8n
# analysis then discarded -- leaving Carnival with 13 sailings and the
# final-payment test unidentified while the raw data held 159.
PEER_NIGHTS = (7, 8)
EVENT_DATE = datetime.date(2026, 11, 4)
EVENT_DAYS = 14
CALENDAR_PATH = os.path.join("data", "sail_dates.json")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from panel.config import load_config  # noqa: E402


def event_window() -> tuple[str, str]:
    lo = (EVENT_DATE - datetime.timedelta(days=EVENT_DAYS)).isoformat()
    hi = (EVENT_DATE + datetime.timedelta(days=EVENT_DAYS)).isoformat()
    return lo, hi


def split_dates(dates) -> tuple[list[str], list[str]]:
    """(in-window departures, earnings-window departures) from a date list."""
    lo, hi = event_window()
    ds = sorted({str(d)[:10] for d in dates or ()} - {""})
    return ([d for d in ds if NEAR_TERM[0] <= d <= NEAR_TERM[1]],
            [d for d in ds if lo <= d <= hi])


def straddle_counts(dates, today: datetime.date | None = None
                    ) -> tuple[int, int]:
    """(departures inside final payment, departures outside it), in window.

    An itinerary is only useful for the 120-day test if the SAME itinerary has
    sailings on both sides of the boundary. One that sits wholly inside it, as
    every marker picked from an Oct-Dec 2026 frame did, can never identify the
    discontinuity however many sailings it has.
    """
    today = today or datetime.date.today()
    inside = outside = 0
    for d in sorted({str(x)[:10] for x in dates or ()} - {""}):
        if not (NEAR_TERM[0] <= d <= NEAR_TERM[1]):
            continue
        try:
            dtd = (datetime.date.fromisoformat(d) - today).days
        except ValueError:
            continue
        if dtd < 0:
            continue
        if dtd < FINAL_PAYMENT_DAYS:
            inside += 1
        else:
            outside += 1
    return inside, outside


# -- candidate frames -------------------------------------------------------

def load_calendar(path: str = CALENDAR_PATH) -> dict[str, dict]:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return {k: v for k, v in json.load(fh).items() if isinstance(v, dict)}


def raw_archive_dates() -> dict[str, set[str]]:
    """Sail dates per itinerary code, scraped out of archived raw payloads."""
    cov: dict[str, set[str]] = collections.defaultdict(set)
    for path in glob.glob("data/raw/**/*.json.gz", recursive=True):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception:
            continue
        code = payload.get("itineraryCode")                 # NCL sailings
        for row in payload.get("pricingStateRooms") or []:
            d = str(row.get("sailStartDate") or "")[:10]
            if code and d:
                cov[code].add(d)
        for it in (payload.get("results") or {}).get("itineraries") or []:  # Carnival
            c = it.get("code")
            for s in it.get("sailings") or []:
                d = str(s.get("departureDate") or "")[:10]
                if c and d:
                    cov[c].add(d)
    return cov


def ncl_candidates(calendar: dict[str, dict], panel_codes: set[str]) -> list[dict]:
    out = []
    for code, rec in calendar.items():
        if not rec.get("region"):
            continue                       # unmapped destination -- see report
        near, event = split_dates(rec.get("dates"))
        out.append({
            "line": "Norwegian Cruise Line",
            "itinerary_code": code,
            "region": rec["region"],
            "ship": rec.get("ship"),
            "categories": list(rec.get("categories") or ()),
            "cats": len(rec.get("categories") or ()),
            "near_term": len(near),
            "event": len(event),
            "sailings": len(rec.get("dates") or ()),
            "in_panel": code in panel_codes,
            "is_package": rec.get("is_package"),
        })
    return out


def panel_candidates(conn: sqlite3.Connection,
                     line_like: str = "%") -> list[dict]:
    """Candidates straight from the weekly panel, for any line.

    The panel now spans Oct 2026 - Oct 2028 for every collected line, so it is
    a better frame than the near-term probe file it replaces: it carries real
    sail dates, regions, cabin ladders and the package flag for both lines, and
    it is refreshed by every weekly run rather than by a separate script.
    """
    rows = conn.execute(
        """SELECT line, itinerary_code, region, MAX(ship) ship,
                  COUNT(DISTINCT cabin_category) cats,
                  COUNT(DISTINCT sailing_id) sailings,
                  MAX(nights) nights,
                  MAX(COALESCE(is_package, 0)) is_package
           FROM observations
           WHERE line LIKE ? AND itinerary_code IS NOT NULL
             AND region IS NOT NULL AND tier = 'weekly-full'
             AND scrape_date = (SELECT MAX(scrape_date) FROM observations
                                WHERE tier = 'weekly-full')
           GROUP BY line, itinerary_code, region""", (line_like,)).fetchall()
    out = []
    for r in rows:
        # Same scrape as the aggregate above: without this an itinerary that
        # has since left the catalogue still shows sailings from an older
        # scrape and can be selected. MEP was picked that way -- a Carnival
        # Mediterranean code with no current sailings at all.
        dates = [d[0] for d in conn.execute(
            "SELECT DISTINCT sail_date FROM observations "
            "WHERE itinerary_code = ? AND line = ? AND tier = 'weekly-full' "
            "AND scrape_date = (SELECT MAX(scrape_date) FROM observations "
            "WHERE tier='weekly-full')",
            (r["itinerary_code"], r["line"]))]
        near, event = split_dates(dates)
        inside, outside = straddle_counts(dates)
        nights = r["nights"]
        peer_len = bool(nights and PEER_NIGHTS[0] <= nights <= PEER_NIGHTS[1])
        cats = [c[0] for c in conn.execute(
            "SELECT DISTINCT cabin_category FROM observations "
            "WHERE itinerary_code = ? AND line = ? AND tier = 'weekly-full' "
            "AND cabin_category IS NOT NULL",
            (r["itinerary_code"], r["line"]))]
        out.append({
            "line": r["line"], "itinerary_code": r["itinerary_code"],
            "region": r["region"], "ship": r["ship"],
            "categories": sorted(cats), "cats": r["cats"],
            "near_term": len(near), "event": len(event),
            "inside_fp": inside, "outside_fp": outside,
            "straddles": bool(inside and outside),
            "nights": nights, "peer_length": peer_len,
            "sailings": r["sailings"], "in_panel": True,
            "is_package": r["is_package"],
        })
    return out


# -- selection --------------------------------------------------------------

def choose(cands: list[dict], target: int = TARGET_TOTAL) -> list[dict]:
    """Weighted pick across regions, spread across ships.

    Quota unfilled in one region is handed back and re-spent, so a region with
    no eligible itineraries shrinks the others' competition rather than
    silently shrinking the marker list.
    """
    for c in cands:
        # Earnings-window coverage is the scarcest and most valuable property,
        # then the near-term run length, then the cabin ladder.
        c["score"] = (
            # An itinerary that cannot be observed on both sides of final
            # payment cannot identify the 120-day test, whatever else it has.
            (40 if c.get("straddles") else 0)
            # Nor can one whose length the cross-line analyses will discard.
            + (30 if c.get("peer_length") else 0)
            # DENSITY, not count: ten itineraries with two sailings each are
            # ten thin cells, while two with twenty sailings fill a window.
            # Caps are high enough that density actually discriminates.
            + min(c.get("inside_fp", 0), 12) * 2
            + min(c.get("outside_fp", 0), 12) * 2
            + min(c["event"], 4) * 10
            + min(c["near_term"], 8) * 5
            + c["cats"] * 6
            + (2 if c["in_panel"] else 0)     # continuity with weekly-full
            # Land+cruise packages are a different product at a package price.
            # Not excluded -- they carry real inventory -- but a cruise-only
            # marker is the cleaner series, so it wins a tie.
            - (6 if c.get("is_package") else 0)
        )

    by_region: dict[str, list[dict]] = collections.defaultdict(list)
    for c in sorted(cands, key=lambda x: -x["score"]):
        by_region[c["region"]].append(c)

    prio = [r for r in PRIORITY_REGIONS if by_region.get(r)]
    other = [r for r in OTHER_REGIONS if by_region.get(r)]
    quota: dict[str, int] = {}
    if prio:
        per = int(target * 0.7) // len(prio)
        quota = {r: per for r in prio}
    if other:
        rest = max(0, target - sum(quota.values()))
        quota.update({r: max(1, rest // len(other)) for r in other})

    chosen: list[dict] = []
    spare = 0
    order = prio + other                      # priority regions spend first
    for region in order:
        pool = by_region[region]
        budget = quota.get(region, 0)
        picked, seen_ships = [], set()
        # A cross-line test needs BOTH lines in the region. Reserve half the
        # budget per line before the open round, otherwise the line with more
        # itineraries takes the whole region and the region can no longer
        # identify anything: Caribbean came back 8 Carnival to 1 NCL without
        # this, despite 31 eligible NCL itineraries.
        if region in PRIORITY_REGIONS:
            # Where both lines are present the region's job is the CROSS-LINE
            # test, and a marker outside the comparable length band cannot
            # serve it at all. Restrict to that band when enough exist --
            # density alone kept selecting Carnival's dense 4-5 night product,
            # which the 7-8 night analyses then discarded entirely.
            if len({c["line"] for c in pool}) > 1:
                matched = [c for c in pool if c.get("peer_length")]
                per_line = collections.Counter(c["line"] for c in matched)
                if len(per_line) > 1 and min(per_line.values()) >= max(1, budget // 2):
                    pool = matched
            for line in sorted({c["line"] for c in pool}):
                floor = max(1, budget // 2)
                for c in [x for x in pool if x["line"] == line]:
                    if sum(1 for x in picked if x["line"] == line) >= floor:
                        break
                    key = (c["line"], c["ship"])
                    if key in seen_ships:
                        continue
                    seen_ships.add(key)
                    picked.append(c)
        for c in pool:
            if len(picked) >= budget:
                break
            if any(c is x for x in picked):
                continue
            key = (c["line"], c["ship"])
            if key in seen_ships and len(picked) >= budget // 2:
                continue
            seen_ships.add(key)
            picked.append(c)
        picked = picked[:budget]
        spare += budget - len(picked)
        chosen.extend(picked)

    # Re-spend whatever the thin regions could not use, priority regions first.
    if spare:
        taken = {c["itinerary_code"] for c in chosen}
        for region in order:
            for c in by_region[region]:
                if spare <= 0:
                    break
                if c["itinerary_code"] not in taken:
                    taken.add(c["itinerary_code"])
                    chosen.append(c)
                    spare -= 1
    return chosen


# -- reporting --------------------------------------------------------------

def brand(line: str) -> str:
    return "NCL" if "Norwegian" in line else line.split()[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join("data", "panel.sqlite"))
    ap.add_argument("--config", default=os.path.join("config", "panel.yaml"))
    ap.add_argument("--calendar", default=CALENDAR_PATH)
    ap.add_argument("--target", type=int, default=TARGET_TOTAL)
    ap.add_argument("--window-start", default=None,
                    help="override the selection window (default: the "
                         "daily-marker tier's rolling horizon)")
    ap.add_argument("--window-end", default=None)
    ap.add_argument("--write", action="store_true",
                    help="write the selection into the config file")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # The selection window is the daily tier's OWN rolling horizon, so picker
    # and collector cannot disagree about what the tier covers. The previous
    # hardcoded Oct-Dec 2026 frame is exactly why only 2 of 17 NCL markers had
    # a Jan-Apr 2027 sailing: NCL itinerary codes are season-specific, so a
    # frame that stops in December selects codes that stop in December.
    global NEAR_TERM
    cfg = load_config(args.config)
    NEAR_TERM = cfg.tier("daily-marker").resolve_window()
    if args.window_start:
        NEAR_TERM = (args.window_start, args.window_end or NEAR_TERM[1])

    cands = panel_candidates(conn)
    if not cands:
        print(f"No weekly-full observations in {args.db}; "
              "run a collection first (or rebuild from JSONL).")
        return 1
    eligible = [c for c in cands if c["near_term"]]

    lo, hi = event_window()
    print(f"near-term window      : {NEAR_TERM[0]} .. {NEAR_TERM[1]}")
    print(f"earnings window       : {lo} .. {hi}")
    print(f"candidates            : {len(cands)}")
    print(f"  with a near-term departure : {len(eligible)}")
    print(f"  dropped (none)             : {len(cands) - len(eligible)}")

    print("\navailable supply by region (eligible candidates)")
    print(f"  {'region':20}{'cands':>7}{'w/event':>9}{'NCL':>6}{'CCL':>6}")
    sup = collections.defaultdict(lambda: [0, 0, 0, 0])
    for c in eligible:
        s = sup[c["region"]]
        s[0] += 1
        s[1] += 1 if c["event"] else 0
        s[2 if "Norwegian" in c["line"] else 3] += 1
    for r in PRIORITY_REGIONS + OTHER_REGIONS + sorted(
            set(sup) - set(PRIORITY_REGIONS) - set(OTHER_REGIONS)):
        s = sup.get(r)
        if s:
            print(f"  {r:20}{s[0]:>7}{s[1]:>9}{s[2]:>6}{s[3]:>6}")
        else:
            print(f"  {r:20}{0:>7}{'':>9}{'':>6}{'':>6}   <-- NO eligible supply")

    if not eligible:
        print("\nNothing sails in the near-term window. Nothing to pick.")
        return 1

    chosen = choose(eligible, args.target)

    print(f"\nselected {len(chosen)} itineraries\n")
    hdr = (f"  {'line':<10}{'code':<32}{'region':<17}{'cats':>5}"
           f"{'near':>6}{'event':>7}{'panel':>7}{'pkg':>5}  ship")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for c in sorted(chosen, key=lambda x: (x["region"], x["line"], x["itinerary_code"])):
        print(f"  {brand(c['line']):<10}{c['itinerary_code'][:31]:<32}"
              f"{c['region'][:16]:<17}{c['cats']:>5}{c['near_term']:>6}"
              f"{c['event']:>7}{'yes' if c['in_panel'] else 'no':>7}"
              f"{('yes' if c.get('is_package') else '-'):>5}  "
              f"{str(c['ship'])[:22]}")

    print("\ncoverage check")
    regions = collections.Counter(c["region"] for c in chosen)
    for r in PRIORITY_REGIONS + OTHER_REGIONS:
        tag = "  <-- PRIORITY" if r in PRIORITY_REGIONS else ""
        print(f"  {r:<18}{regions.get(r, 0):>3} itineraries{tag}")

    have: set[str] = set()
    for c in chosen:
        have.update(c["categories"])
    missing = set(CATEGORIES) - have
    print(f"\n  cabin categories covered: {sorted(have)}")
    print(f"  missing categories      : {sorted(missing) if missing else 'none'}")
    print(f"  near-term departures covered     : {sum(c['near_term'] for c in chosen)}")
    print(f"  earnings-window departures covered: {sum(c['event'] for c in chosen)}")
    if not any(c["event"] for c in chosen):
        print("  WARNING: no earnings-window coverage in this selection.")
    pkgs = [c for c in chosen if c.get("is_package")]
    if pkgs:
        print(f"  land+cruise packages selected    : {len(pkgs)} "
              f"({', '.join(c['itinerary_code'][:18] for c in pkgs)})")
        print("    -> priced as packages, not cruise fares; analysis separates "
              "them by is_package.")
    off_panel = [c for c in chosen if not c["in_panel"]]
    if off_panel:
        print(f"  not in the weekly-full panel     : {len(off_panel)}"
              " (near-term only, no Jan-Aug 2027 baseline)")

    # Straddling the boundary is what this tier exists for, so report it.
    straddling = [c for c in chosen if c.get("straddles")]
    print(f"\n  markers straddling the {FINAL_PAYMENT_DAYS}-day boundary: "
          f"{len(straddling)} of {len(chosen)}")
    per_rl = collections.defaultdict(lambda: collections.Counter())
    for c in straddling:
        per_rl[c["region"]][brand(c["line"])] += 1
    for region in sorted(per_rl):
        counts = dict(per_rl[region])
        tag = "both lines" if len(counts) > 1 else "ONE LINE ONLY"
        print(f"     {region:<16} {counts}  <- {tag}")
    thin = [r for r, cts in per_rl.items() if len(cts) < 2]
    if thin:
        print("     NOTE: " + ", ".join(thin) + " cannot identify the "
              f"{FINAL_PAYMENT_DAYS}-day test; it needs both lines on both "
              "sides of the boundary.")

    per_line: dict[str, list[str]] = collections.defaultdict(list)
    for c in chosen:
        key = "ncl" if "Norwegian" in c["line"] else "carnival"
        if c["itinerary_code"] not in per_line[key]:
            per_line[key].append(c["itinerary_code"])

    print("\nconfig block:\n")
    print("    marker_itineraries:")
    for key in ("ncl", "carnival"):
        print(f"      {key}: [{', '.join(per_line.get(key, []))}]")

    if args.write:
        write_markers(args.config, per_line)
        print(f"\nwrote selection into {args.config}")
    return 0


def write_markers(config_path: str, per_line: Mapping[str, list[str]]) -> None:
    """Replace the marker_itineraries block in place, preserving comments.

    yaml.safe_dump would round-trip the document and drop every comment in it,
    and this config's comments are load-bearing: the MINISUITE mapping
    judgement call, the BAHAMAS-into-Caribbean note, the tier definitions. So
    the block is rewritten as text and the rest of the file is left untouched.
    """
    import yaml

    with open(config_path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()

    start = None
    indent = ""
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("marker_itineraries:"):
            start = i
            indent = line[: len(line) - len(line.lstrip())]
            break
    if start is None:
        raise SystemExit(f"no marker_itineraries block found in {config_path}")

    # The block runs until the next line at or above its own indent level.
    end = len(lines)
    for j in range(start + 1, len(lines)):
        line = lines[j]
        if not line.strip():
            continue
        if len(line) - len(line.lstrip()) <= len(indent):
            end = j
            break

    block = [f"{indent}marker_itineraries:"]
    for key in ("ncl", "carnival"):
        codes = list(per_line.get(key, []))
        if not codes:
            block.append(f"{indent}  {key}: []")
            continue
        block.append(f"{indent}  {key}:")
        block.extend(f"{indent}    - {c}" for c in codes)

    updated = lines[:start] + block + lines[end:]
    text = "\n".join(updated) + "\n"

    # Refuse to write a file that would not load, or that lost the selection.
    doc = yaml.safe_load(text)
    got = doc["tiers"]["daily-marker"]["marker_itineraries"]
    for key in ("ncl", "carnival"):
        if list(got.get(key) or []) != list(per_line.get(key, [])):
            raise SystemExit(f"refusing to write: {key} markers did not round-trip")

    with open(config_path, "w", encoding="utf-8") as fh:
        fh.write(text)


if __name__ == "__main__":
    raise SystemExit(main())
