"""Build the itinerary calendar that daily-marker selection is chosen from.

Why this exists
---------------
`pick_markers.py` needs to know which itineraries sail in the near-term cohort
(Oct-Dec 2026) and inside the earnings window around 2026-11-04. Neither the
panel nor the local disk can answer that:

  * the weekly-full tier only stores Jan-Aug 2027 rows, and NCL itinerary codes
    are season-specific -- of the 152 NCL itineraries sailing Oct-Dec 2026,
    only 35 appear anywhere in the Jan-Aug 2027 panel. Selecting markers from
    the panel alone therefore misses most of the near-term universe, and badly
    so in the Mediterranean.
  * the raw archive from a GitHub Actions run is an artifact that is never
    committed, so locally there is only whatever scratch payload happens to be
    on disk. Picking off that selects itineraries for being archived, not for
    meeting the criteria.

This script calls NCL's search endpoint to enumerate the near-term universe,
then the sailings endpoint once per itinerary, and keeps only what selection
needs: sail dates, region, ship, embarkation port and which cabin categories
the itinerary offers. It records no price, no currency and no availability, and
writes nothing into the panel -- the output is a selection input, not an
observation. Those fields are market independent, so a non-US egress does not
invalidate the calendar the way it would invalidate prices.

    python scripts/probe_sail_dates.py                 # near-term + panel
    python scripts/probe_sail_dates.py --scope panel   # panel codes only
    python scripts/probe_sail_dates.py --limit 5       # smoke test
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from panel import normalize as norm
from panel.config import LineConfig, load_config
from panel.http_client import FetchError, PoliteClient, RateLimit, RobotsDisallowed
from panel.sources.ncl import months_in_window, parse_search_itineraries

OUT_PATH = os.path.join("data", "sail_dates.json")
NEAR_TERM = ("2026-10-01", "2026-12-31")


def codes_in_panel(db: str, line_like: str = "Norwegian%") -> list[str]:
    if not os.path.exists(db):
        return []
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT DISTINCT itinerary_code FROM observations "
        "WHERE line LIKE ? AND itinerary_code IS NOT NULL ORDER BY itinerary_code",
        (line_like,),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def near_term_codes(client: PoliteClient, line: LineConfig,
                    window: tuple[str, str] = NEAR_TERM) -> list[str]:
    """Enumerate itinerary codes with a departure inside the near-term window."""
    codes: list[str] = []
    seen: set[str] = set()
    for month in months_in_window(*window):
        offset = 0
        while True:
            url = (f"{line.base_url}/api/v2/vacations/search"
                   f"?limit={line.search_page_size}&offset={offset}&dates={month}")
            payload = client.get_json(url)
            found = parse_search_itineraries(payload)
            for code in found:
                if code not in seen:
                    seen.add(code)
                    codes.append(code)
            offset += line.search_page_size
            if not found or offset >= int(payload.get("total") or 0):
                break
        print(f"  {month}: {payload.get('total')} itineraries")
    return codes


def summarise(payload: dict, line: LineConfig) -> dict:
    """Everything marker selection needs from one /sailings/{code} payload."""
    details = payload.get("itineraryDetails") or {}
    dest_codes = [d.get("code") for d in (details.get("destinations") or [])
                  if isinstance(d, dict)]
    rooms = payload.get("pricingStateRooms") or []

    # Land+cruise packages ("cruisetours") are a different product at a package
    # price, stamped with the cruise segment length. Recorded so selection and
    # backfill can separate them from cruise-only fares.
    duration = details.get("duration") if isinstance(details.get("duration"), dict) else {}
    flags = {r.get("isPackage") for r in rooms if isinstance(r.get("isPackage"), bool)}
    is_package = (int(any(flags)) if flags else None)

    dates = sorted({str(r.get("sailStartDate") or "")[:10] for r in rooms} - {""})
    cats = sorted({
        c for c in (norm.map_cabin_category(r.get("stateroomType"), line.cabin_map)
                    for r in rooms) if c
    })
    unmapped = sorted({
        str(r.get("stateroomType")) for r in rooms
        if r.get("stateroomType")
        and norm.map_cabin_category(r.get("stateroomType"), line.cabin_map) is None
    })
    return {
        "dates": dates,
        "region": norm.region_for(dest_codes, line.region_map),
        "destinations": [d for d in dest_codes if d],
        "ship": (details.get("ship") or {}).get("title"),
        "embark_port": (details.get("embarkationPort") or {}).get("code"),
        "categories": cats,
        "unmapped_cabin_labels": unmapped,
        "is_package": is_package,
        "itinerary_nights": duration.get("itinerary"),
        "cruise_nights": duration.get("cruising"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join("data", "panel.sqlite"))
    ap.add_argument("--config", default=os.path.join("config", "panel.yaml"))
    ap.add_argument("--out", default=OUT_PATH)
    ap.add_argument("--scope", choices=["both", "near-term", "panel"], default="both",
                    help="which itineraries to probe (default: both)")
    ap.add_argument("--limit", type=int, default=0, help="cap codes (smoke test)")
    ap.add_argument("--refresh", action="store_true",
                    help="re-probe codes already present in the output file")
    args = ap.parse_args()

    cfg = load_config(args.config)
    line = cfg.line("ncl")
    client = PoliteClient(
        user_agent=cfg.user_agent,
        rate=RateLimit(min_interval_s=cfg.min_interval_s),
    )

    codes: list[str] = []
    if args.scope in ("both", "near-term"):
        print(f"enumerating the near-term universe ({NEAR_TERM[0]}..{NEAR_TERM[1]})")
        codes.extend(near_term_codes(client, line))
        print(f"  -> {len(codes)} itineraries sail in the near-term window")
    if args.scope in ("both", "panel"):
        panel_codes = [c for c in codes_in_panel(args.db) if c not in set(codes)]
        print(f"  + {len(panel_codes)} more from the weekly-full panel")
        codes.extend(panel_codes)
    if args.limit:
        codes = codes[: args.limit]
    print(f"itineraries to probe: {len(codes)}")

    # Keep what is already on disk so an interrupted run resumes cheaply.
    out: dict[str, dict] = {}
    if os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as fh:
            raw = json.load(fh)
        # Tolerate the earlier code -> [dates] format by discarding it: those
        # records lack region/ship/categories and would silently rank badly.
        out = {k: v for k, v in raw.items() if isinstance(v, dict)}
        if out:
            print(f"resuming: {len(out)} already known")

    def flush() -> None:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=1, sort_keys=True)

    errors = 0
    for i, code in enumerate(codes, 1):
        if code in out and not args.refresh:
            continue
        try:
            payload = client.get_json(f"{line.base_url}/api/vacations/sailings/{code}")
        except (FetchError, RobotsDisallowed) as exc:
            errors += 1
            print(f"  [{i}/{len(codes)}] {code}: {type(exc).__name__}: {exc}", flush=True)
            continue
        out[code] = summarise(payload, line)
        if i % 25 == 0 or i == len(codes):
            rec = out[code]
            print(f"  [{i}/{len(codes)}] {code[:34]}: {len(rec['dates'])} dates, "
                  f"{rec['region']}", flush=True)
            flush()

    flush()
    print(f"\nwrote {len(out)} itineraries -> {args.out}  ({errors} errors)")
    return 1 if errors and not out else 0


if __name__ == "__main__":
    raise SystemExit(main())
