"""Confirm the collector is being served the US market.

Run on a GitHub Actions runner (US-hosted) before scheduling recurring
collection.

WHAT IS GATED, AND WHY
----------------------
Hard requirement: currencyCode == "USD" on both endpoints the collector reads.
A CAD-served run would silently produce a panel in the wrong currency.

NOT gated: the presence of a tax amount. An earlier version of this script
required taxesAndFees.amount to be populated, on the mistaken belief that the
US market exposes it and the CAD market does not. That was wrong, and the
evidence for it was a hardcoded literal found in NCL's JS bundle rather than a
live response. Verified since:

  * `taxesAndFees` does not appear on pricingStateRooms rows at all -- 0 of
    1,276 archived rows carry it, and no key containing "tax" or "fee" exists
    anywhere in those payloads.
  * It is absent from /api/vacations/search/{code} and
    /api/vacations/events/{id}/package/{id} too.
  * The itinerary-level taxesAndFees on the search endpoint is {"text": ""}
    on the US market as well as CAD -- a vestigial field.
  * NCL's own /api/vacations/disclaimers states: "Government taxes, fees,
    port expenses, and fuel supplement (where applicable) are additional."

So the published fare is tax-EXCLUSIVE, which is exactly what the panel needs:
price_total / price_pppn are fare-only and the "never fold taxes into price"
rule holds. taxes_fees is simply NULL for NCL because the public API does not
publish the amount. That is a missing column, not a contaminated price, and it
is not a reason to block collection.

Exit 0 = US market confirmed, safe to schedule.
Exit 1 = not confirmed; do not schedule.
"""
from __future__ import annotations

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
        rate=RateLimit(min_interval_s=cfg.min_interval_s, timeout_s=cfg.timeout_s),
        obey_robots=cfg.obey_robots,
    )

    failures: list[str] = []

    banner("EGRESS")
    try:
        geo = client.get_json("https://ipinfo.io/json")
        print(f"  ip       : {geo.get('ip')}")
        print(f"  location : {geo.get('city')}, {geo.get('region')}, {geo.get('country')}")
        print(f"  org      : {geo.get('org')}")
    except Exception as exc:
        print(f"  (geo lookup unavailable: {exc})")

    # --- GATE: currency on the search endpoint ---------------------------
    banner("GATE 1 - /api/v2/vacations/search currency")
    url = f"{line_cfg.base_url}/api/v2/vacations/search?limit=3&offset=0"
    print(f"  GET {url}")
    payload = client.get_json(url)
    itineraries = payload.get("itineraries") or []
    if not itineraries:
        print("  FAIL: no itineraries returned")
        return 1
    for it in itineraries:
        cur = it.get("currencyCode")
        print(f"    {str(it.get('code'))[:26]:<28} currency={cur!r}")
        if str(cur).upper() != EXPECTED_CURRENCY:
            failures.append(f"search: currencyCode {cur!r} != {EXPECTED_CURRENCY}")

    # --- GATE: currency on the endpoint the collector actually reads ----
    banner("GATE 2 - /api/vacations/sailings/{code} currency")
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
        priced = [r for r in rows if isinstance(r.get(PRICE_BASIS), (int, float))]
        print(f"  priced cells: {len(priced)}/{len(rows)}")
        if priced:
            s = priced[0]
            print(f"  sample: {s.get('stateroomType')} {s.get('status')} "
                  f"{PRICE_BASIS}={s.get(PRICE_BASIS)} {s.get('currencyCode')}")

    # --- INFORMATIONAL: tax exposure (never gates) -----------------------
    banner("INFO - tax handling (not a gate)")
    tax_keys = [k for r in rows[:50] for k in r if "tax" in k.lower() or "fee" in k.lower()]
    print(f"  tax/fee keys on pricing rows : {set(tax_keys) or 'none (expected)'}")
    try:
        disc = client.get_json(f"{line_cfg.base_url}/api/vacations/disclaimers")
        text = " ".join(d.get("text", "") for d in disc if isinstance(d, dict))
        idx = text.lower().find("government taxes")
        if idx >= 0:
            print(f"  NCL disclaimer: ...{text[idx:idx + 150]}...")
        print("  => published fare is tax-EXCLUSIVE; taxes_fees stays NULL for NCL.")
        print("     The 'never fold taxes into price' rule holds.")
    except Exception as exc:
        print(f"  (disclaimers unavailable: {exc})")

    banner("VERDICT")
    if failures:
        print(f"  NOT CONFIRMED - {len(failures)} check(s) failed:\n")
        for f in failures:
            print(f"    x {f}")
        print("\n  Do NOT schedule recurring collection from this egress.")
        return 1

    print("  US MARKET CONFIRMED")
    print(f"    - currencyCode == {EXPECTED_CURRENCY} on both endpoints")
    print("    - fare basis is tax-exclusive (taxes_fees NULL by design)")
    print("\n  Safe to schedule recurring collection.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
