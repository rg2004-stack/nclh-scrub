"""Confirm the collector is being served the US market.

Run this on a GitHub Actions runner (US-hosted) before scheduling anything
recurring. It makes a minimal number of NCL requests and checks the two things
the panel depends on:

  1. currencyCode == "USD"
  2. taxesAndFees.amount is present and non-null

(2) is the one that actually matters and the reason a Canadian egress is
unusable: on the CAD market NCL omits taxesAndFees.amount entirely, so the
"taxes captured separately, never folded into price" rule cannot be satisfied.
Currency alone is not sufficient evidence.

Exit code 0 = US market confirmed, safe to schedule.
Exit code 1 = not confirmed; do not schedule, the panel would be contaminated.

    python scripts/verify_us_market.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from panel.config import load_config                      # noqa: E402
from panel.http_client import PoliteClient, RateLimit     # noqa: E402
from panel.sources.ncl import PRICE_BASIS                 # noqa: E402

EXPECTED_CURRENCY = "USD"


def banner(text: str) -> None:
    print("\n" + "=" * 68)
    print(text)
    print("=" * 68)


def main() -> int:
    cfg = load_config(os.environ.get("PANEL_CONFIG", "config/panel.yaml"))
    line_cfg = cfg.line("ncl")
    client = PoliteClient(
        user_agent=cfg.user_agent,
        rate=RateLimit(min_interval_s=cfg.min_interval_s,
                       timeout_s=cfg.timeout_s),
        obey_robots=cfg.obey_robots,
    )

    failures: list[str] = []
    notes: list[str] = []

    # --- where are we actually coming from? ------------------------------
    banner("EGRESS")
    try:
        geo = client.get_json("https://ipinfo.io/json")
        print(f"  ip       : {geo.get('ip')}")
        print(f"  location : {geo.get('city')}, {geo.get('region')}, "
              f"{geo.get('country')}")
        print(f"  org      : {geo.get('org')}")
        if str(geo.get("country", "")).upper() != "US":
            notes.append(f"egress country is {geo.get('country')!r}, not US")
    except Exception as exc:                                  # non-fatal
        print(f"  (geo lookup unavailable: {exc})")

    # --- 1. search endpoint ----------------------------------------------
    banner("CHECK 1 - /api/v2/vacations/search")
    search_url = f"{line_cfg.base_url}/api/v2/vacations/search?limit=3&offset=0"
    print(f"  GET {search_url}")
    payload = client.get_json(search_url)
    itineraries = payload.get("itineraries") or []
    if not itineraries:
        print("  FAIL: no itineraries returned")
        return 1

    for it in itineraries:
        cur = it.get("currencyCode")
        taxes = it.get("taxesAndFees") or {}
        amount = taxes.get("amount") if isinstance(taxes, dict) else None
        text = taxes.get("text") if isinstance(taxes, dict) else None
        print(f"    {str(it.get('code'))[:26]:<28} currency={cur!r:<7} "
              f"taxesAndFees.amount={amount!r} text={str(text)[:38]!r}")
        if str(cur).upper() != EXPECTED_CURRENCY:
            failures.append(f"search: currencyCode {cur!r} != {EXPECTED_CURRENCY}")
        if amount is None:
            failures.append(
                f"search: taxesAndFees.amount missing for {it.get('code')} "
                "- taxes cannot be captured separately on this market")

    # --- 2. sailings endpoint (what the collector actually reads) --------
    banner("CHECK 2 - /api/vacations/sailings/{itineraryCode}")
    code = itineraries[0].get("code")
    sail_url = f"{line_cfg.base_url}/api/vacations/sailings/{code}"
    print(f"  GET {sail_url}")
    sailings = client.get_json(sail_url)
    rows = sailings.get("pricingStateRooms") or []
    print(f"  {len(rows)} pricing cells returned")
    if not rows:
        failures.append("sailings: no pricingStateRooms returned")
    else:
        currencies = {r.get("currencyCode") for r in rows}
        print(f"  currencies present: {currencies}")
        if currencies != {EXPECTED_CURRENCY}:
            failures.append(
                f"sailings: currencies {currencies} != {{'{EXPECTED_CURRENCY}'}}")

        sample = rows[0]
        print(f"  sample cell: type={sample.get('stateroomType')} "
              f"status={sample.get('status')} "
              f"{PRICE_BASIS}={sample.get(PRICE_BASIS)} "
              f"currency={sample.get('currencyCode')}")
        tax_fields = {k: v for k, v in sample.items() if "ax" in k.lower()}
        print(f"  tax-ish fields on the cell: {tax_fields or 'none'}")

    # --- verdict ----------------------------------------------------------
    banner("VERDICT")
    for n in notes:
        print(f"  NOTE: {n}")
    if failures:
        print(f"  NOT CONFIRMED - {len(failures)} check(s) failed:\n")
        for f in failures:
            print(f"    x {f}")
        print("\n  Do NOT schedule recurring collection from this egress.")
        print("  The panel would record a non-US market and could not satisfy")
        print("  the 'taxes captured separately' rule.")
        return 1

    print("  US MARKET CONFIRMED")
    print(f"    - currencyCode == {EXPECTED_CURRENCY} on both endpoints")
    print("    - taxesAndFees.amount is populated and non-null")
    print("\n  Safe to schedule recurring collection.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
