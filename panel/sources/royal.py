"""Royal Caribbean: a floor-price control line, and nothing more.

Why this source is deliberately shallow
---------------------------------------
Royal is here as a third reference point for the ONE number the published
sell-side datasets actually carry -- the cheapest advertised fare per sailing.
Having a third line's floor makes the panel's central claim checkable: if the
floor moves while the distribution behind it does not, that is the artefact
this project exists to expose, and one line's worth of it could be coincidence.

It is NOT a peer for cross-line fare comparison. It publishes no cabin
distribution here, so `capabilities` declares its granularity as `floor`, the
coarsest rung of the lattice, and `peer_gap` excludes it by role. A gap
computed between NCL's balcony median and Royal's sailing floor would be
comparing a cabin grade with a marketing headline.

robots.txt decides the boundary, and it happens to agree
--------------------------------------------------------
Checked 2026-09-16 against https://www.royalcaribbean.com/robots.txt, fetched
with this project's descriptive User-Agent (an unidentified client gets a 403,
which a robots parser correctly reads as disallow-all -- so the permissive file
is what we are actually served, not a workaround):

    Disallow: /booking/          <- cabin pricing. NEVER fetched.
    Disallow: /room-selection/   <- cabin selection. NEVER fetched.
    /cruises, /caribbean-cruises and the other destination landing pages are
    not disallowed.

So the only paths that would yield a cabin distribution are exactly the paths
robots.txt forbids. We take the floor from the allowed landing pages and stop
there. The records carry a `bookNowUrl` pointing into /booking/; it is parsed
for the package code and never requested.

Shape of the data
-----------------
Each landing page server-renders a Vue component whose `:initial-tabs`
attribute is HTML-escaped JSON: a handful of tabs, each with `tabItems` that
are one cheapest-fare record per sailing.

    {"currency": "USD", "countryCode": "USA", "netPrice": "348",
     "taxedAndFees": "133.53", "taxesFeesIncluded": true,
     "sailDate": "2026-09-28", "shipCode": "WN", "numberOfNights": 5, ...}

`taxesFeesIncluded` is true and `netPrice` is the advertised, tax-INCLUSIVE
number, so the comparable fare is `netPrice - taxedAndFees`. Royal is the only
source in the panel that publishes the tax component at all; NCL and Carnival
do not, so all three end up tax-exclusive and comparable, and `taxes_fees` is
stored separately here exactly as the spec requires -- never folded into price.
"""
from __future__ import annotations

import html
import json
import re
from typing import Any, Sequence

from panel import normalize as norm
from panel.config import LineConfig
from panel.http_client import FetchError, PoliteClient, RobotsDisallowed
from panel.sources.base import CollectResult
from panel.storage import Observation, iso, utcnow

LINE = "Royal Caribbean International"
BRAND = "Royal Caribbean"
EXPECTED_CURRENCY = "USD"
EXPECTED_COUNTRY = "USA"

CurrencyMismatch = norm.CurrencyMismatch

# The floor is one price for a whole sailing, not a cabin. These constants make
# that visible in every row rather than leaving a reader to infer it.
FLOOR_SUBCATEGORY = "<SAILING FLOOR>"
FLOOR_CATEGORY = "floor"
PRICE_BASIS = "sailing_floor_tax_exclusive"

_TABS_RE = re.compile(r':initial-tabs="([^"]+)"')


def landing_paths(line_cfg: LineConfig) -> dict[str, str]:
    """region -> landing page path, from config. Never hardcoded here."""
    return dict(line_cfg.landing_pages)


def parse_tabs(body: str) -> list[dict[str, Any]]:
    """Pull every sailing record out of a landing page's embedded JSON."""
    out: list[dict[str, Any]] = []
    for m in _TABS_RE.finditer(body):
        try:
            tabs = json.loads(html.unescape(m.group(1)))
        except (TypeError, ValueError):
            continue
        if not isinstance(tabs, list):
            continue
        for tab in tabs:
            if isinstance(tab, dict):
                for item in tab.get("tabItems") or []:
                    if isinstance(item, dict):
                        out.append(item)
    return out


def sailing_id(rec: dict[str, Any]) -> str:
    """Stable across scrapes: ship + sail date + package.

    Royal exposes no sailing id on these pages. This triple is what the
    booking link itself keys on, so it identifies the same departure on a
    later collection date.
    """
    return "|".join(str(rec.get(k) or "") for k in
                    ("shipCode", "sailDate", "packageCode"))


def _num(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def parse_records(records: Sequence[dict[str, Any]], *,
                  region_map: dict[str, str],
                  scrape_ts: str, tier: str, source_url: str,
                  raw_path: str | None = None,
                  expected_currency: str = EXPECTED_CURRENCY,
                  ) -> tuple[list[Observation], list[str]]:
    """Normalise floor records. Returns (observations, skipped reasons).

    Region comes from the record's own `destinationCode`, NEVER from which
    landing page it was found on. The pages are marketing carousels: the
    Bermuda page serves Bahamas sailings, and trusting the page would file
    them as Bermuda. That is the same error as inferring a cruise's region
    from its embarkation port, which this project has already made once.
    An unmapped code is dropped and reported, never bucketed by fallback.

    Raises CurrencyMismatch on a non-USD priced record: the collector must die
    loudly rather than let a CAD panel in, which is exactly how 4,470 bad NCL
    rows got written once already.
    """
    out: list[Observation] = []
    skipped: list[str] = []
    seen: set[str] = set()

    for rec in records:
        sid = sailing_id(rec)
        if not rec.get("sailDate") or not rec.get("shipCode"):
            skipped.append(f"incomplete record {sid!r}")
            continue
        if sid in seen:
            continue                      # the same sailing appears on several tabs
        seen.add(sid)

        dest = str(rec.get("destinationCode") or "").strip().upper()
        region = region_map.get(dest)
        if not region:
            skipped.append(f"{sid}: destinationCode={dest!r} is not mapped to a "
                           "panel region; dropped rather than guessed")
            continue

        gross = _num(rec.get("netPrice"))
        taxes = _num(rec.get("taxedAndFees"))
        nights = rec.get("numberOfNights")
        currency = str(rec.get("currency") or "").upper()

        if gross is not None and currency != expected_currency.upper():
            raise CurrencyMismatch(
                f"Royal returned {currency or '<none>'} for sailing {sid!r} "
                f"(country={rec.get('countryCode')!r}); expected "
                f"{expected_currency}. Refusing to write a non-{expected_currency} "
                "row. This is an egress-geography problem, not a parse problem."
            )

        # taxesFeesIncluded says the advertised number already contains taxes,
        # so the comparable fare is the difference. If the vendor ever flips
        # that flag the arithmetic must change with it, hence no default.
        included = rec.get("taxesFeesIncluded")
        if gross is None:
            fare = None
        elif included is True and taxes is not None:
            fare = gross - taxes
        elif included is False:
            fare = gross
        else:
            skipped.append(f"{sid}: taxesFeesIncluded={included!r} with "
                           f"taxes={taxes!r}; cannot establish a tax basis")
            continue

        pppn = (round(fare / nights, 2)
                if fare is not None and isinstance(nights, int) and nights > 0
                else None)

        out.append(Observation(
            scrape_ts_utc=scrape_ts, scrape_date=scrape_ts[:10],
            line=LINE, brand=BRAND, tier=tier, market="US",
            sailing_id=sid,
            ship_code=str(rec.get("shipCode") or "") or None,
            ship=str(rec.get("shipName") or "") or None,
            itinerary_code=str(rec.get("packageCode") or "") or None,
            itinerary_name=str(rec.get("itineraryName") or "") or None,
            embark_port=str(rec.get("departureCode") or "") or None,
            sail_date=str(rec.get("sailDate")),
            nights=nights if isinstance(nights, int) else None,
            itinerary_nights=nights if isinstance(nights, int) else None,
            is_package=0,
            region=region,
            currency=currency or None,
            # One floor per sailing: no cabin resolution exists on an allowed
            # path, so these say 'floor' rather than pretending to a grade.
            cabin_subcategory=FLOOR_SUBCATEGORY,
            cabin_category=FLOOR_CATEGORY,
            price_total=fare,
            price_per_person=fare,
            price_pppn=pppn,
            price_basis=PRICE_BASIS,
            taxes_fees=taxes,
            taxes_fees_text=("advertised price includes taxes and fees; "
                             "stored fare is net of them"),
            availability_status=norm.AVAIL_AVAILABLE,
            availability_status_raw="listed",
            source_url=source_url,
            raw_response_path=raw_path,
        ))
    return out, skipped


class RoyalSource:
    """Floor-price control. Sweeps only robots-allowed landing pages."""

    key = "royal"

    def __init__(self, cfg, line_cfg: LineConfig, client: PoliteClient,
                 store, archive):
        self.cfg = cfg
        self.line_cfg = line_cfg
        self.client = client
        self.store = store
        self.archive = archive

    def collect(self, tier: str) -> CollectResult:
        result = CollectResult()
        tier_cfg = self.cfg.tier(tier)
        scrape_ts = iso(utcnow())
        pages = landing_paths(self.line_cfg)
        # Pages are ENTRY POINTS, not region assertions: each returns whatever
        # Royal wants to promote, and the region of every row comes from the
        # record's own destinationCode.
        for region in sorted(pages):
            url = self.line_cfg.base_url + pages[region]
            result.sailings_attempted += 1
            try:
                body = self.client.get(url, accept="text/html")
            except RobotsDisallowed as exc:
                # Never worked around: a disallowed path is a gap we own.
                result.errors.append({"stage": "robots", "region": region,
                                      "url": url, "error": str(exc)})
                continue
            except FetchError as exc:
                result.errors.append({"stage": "fetch", "region": region,
                                      "url": url, "error": str(exc)})
                continue

            raw_path = self.archive.write(line="royal", kind="landing",
                                          ident=region, body=body)
            records = parse_tabs(body)
            try:
                obs, skipped = parse_records(
                    records, region_map=self.line_cfg.region_map,
                    scrape_ts=scrape_ts, tier=tier,
                    source_url=url, raw_path=raw_path)
            except CurrencyMismatch as exc:
                print(f"  !! CURRENCY MISMATCH ({region}): {exc}")
                result.errors.append({"stage": "currency", "region": region,
                                      "error": str(exc), "fatal": True})
                break

            lo, hi = tier_cfg.resolve_window()
            if lo:
                obs = [o for o in obs if o.sail_date and o.sail_date >= lo]
            if hi:
                obs = [o for o in obs if o.sail_date and o.sail_date <= hi]

            for reason in skipped:
                result.errors.append({"stage": "parse", "region": region,
                                      "error": reason})
            if obs:
                result.observations_written += self.store.upsert_observations(obs)
                result.sailings_captured += len({o.sailing_id for o in obs})
            print(f"  via {region} page: {len(records)} listed -> "
                  f"{len(obs)} mapped and in window")
        return result
