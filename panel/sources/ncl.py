"""Norwegian Cruise Line collector.

Endpoints (public JSON, plain GET, same host, none robots-disallowed):

  GET /api/v2/vacations/search?limit=&offset=[&dates=Mon-YYYY][&destinations=CODE]
      -> {itineraries: [{code, packageId, ...}], total, filters, ...}
      Used only to enumerate itinerary codes.

  GET /api/vacations/sailings/{itineraryCode}
      -> {itineraryCode, itineraryDetails: {...},
          pricingStateRooms: [ one row per (sail date x stateroom type) ]}
      This is the panel's real payload. One request returns the whole grid for
      an itinerary: every sail date crossed with every cabin category, each cell
      carrying its own status and price vector. Capturing all of it is the point
      -- the distribution, not the floor.

Row fields observed: packageId, sailId, sailingEventSequence, isFlyCruise,
isPackage, vacationStartDate, sailStartDate, sailEndDate, currencyCode,
stateroomType, title, status, statusText, combinedPrice, xcatPrice, fasPrice,
basePrice, hasSingleSupplement, sortWeight, offerGroups[].

sailId is 1:1 with sail date and (sailId, stateroomType) uniquely keys a row,
so sailId is the sailing_id.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from .. import normalize as norm
from ..config import Config, LineConfig, TierConfig
from ..http_client import FetchError, PoliteClient, RobotsDisallowed
from ..storage import Observation, RawArchive, Store, iso, utcnow
from .base import CollectResult

# Vendor status -> panel availability vocabulary.
# SOLO_GUEST_ONLY is a genuine late-stage depletion signal: the category is
# depleted enough that NCL only offers it on single occupancy. It maps to
# "limited", and the verbatim value is preserved in availability_status_raw.
STATUS_MAP = {
    "AVAILABLE": norm.AVAIL_AVAILABLE,
    "SOLO_GUEST_ONLY": norm.AVAIL_LIMITED,
    "SOLD_OUT": norm.AVAIL_SOLD_OUT,
}

# Which vendor price field becomes price_per_person. combinedPrice is the
# published per-person double-occupancy fare, i.e. the number a shopper sees.
PRICE_BASIS = "combinedPrice"

# Currency -> market code, for recording the market actually served.
_MARKET_BY_CURRENCY = {
    "USD": "US", "CAD": "CA", "GBP": "UK", "EUR": "EU", "AUD": "AU", "NZD": "NZ",
}


def _offers(groups: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Flatten offerGroups[].offers[] into a single list, verbatim."""
    out: list[Mapping[str, Any]] = []
    for group in groups or ():
        for offer in group.get("offers") or ():
            out.append(offer)
    return out


def market_for(currency: str | None) -> str:
    """Record the market actually served, not the one we asked for.

    The request always goes to the US site; Akamai resolves market from client
    IP at the edge. Storing the resolved market stops a CAD-served run from
    silently contaminating a USD panel.
    """
    if not currency:
        return "UNKNOWN"
    return _MARKET_BY_CURRENCY.get(str(currency).upper(), str(currency).upper())


def parse_search_itineraries(payload: Mapping[str, Any]) -> list[str]:
    """Extract distinct itinerary codes from a /search response, order preserved."""
    seen: list[str] = []
    for itinerary in payload.get("itineraries") or ():
        code = itinerary.get("code")
        if code and code not in seen:
            seen.append(code)
    return seen


def parse_sailings(
    payload: Mapping[str, Any],
    *,
    line_cfg: LineConfig,
    tier: str,
    scrape_ts: str,
    scrape_date: str,
    source_url: str,
    raw_path: str | None = None,
    sail_window: tuple[str | None, str | None] = (None, None),
    allowed_regions: Iterable[str] | None = None,
) -> tuple[list[Observation], set[str]]:
    """Turn one /sailings/{code} payload into observations.

    Pure: no network, no database. This is what the tests exercise.
    Returns (observations, unmapped_cabin_labels).
    """
    details = payload.get("itineraryDetails") or {}
    itinerary_code = payload.get("itineraryCode") or details.get("code")
    ship = details.get("ship") or {}
    embark = details.get("embarkationPort") or {}
    disembark = details.get("disembarkationPort") or {}
    dest_codes = [d.get("code") for d in (details.get("destinations") or [])]
    region = norm.region_for(dest_codes, line_cfg.region_map)

    allowed = set(allowed_regions) if allowed_regions else None
    window_start, window_end = sail_window

    rows: list[Observation] = []
    unmapped: set[str] = set()

    # Region is a property of the itinerary, so this filter is all-or-nothing.
    if allowed is not None and region not in allowed:
        return rows, unmapped

    for row in payload.get("pricingStateRooms") or ():
        sail_date = row.get("sailStartDate")
        if not norm.in_window(sail_date, window_start, window_end):
            continue

        raw_label = row.get("stateroomType")
        category = norm.map_cabin_category(raw_label, line_cfg.cabin_map)
        if category is None and raw_label:
            unmapped.add(str(raw_label))

        return_date = row.get("sailEndDate")
        nights = norm.nights_between(sail_date, return_date)

        published = row.get(PRICE_BASIS)
        per_person = float(published) if isinstance(published, (int, float)) else None

        taxes = row.get("taxesAndFees")
        taxes_amount = taxes.get("amount") if isinstance(taxes, Mapping) else None
        taxes_text = taxes.get("text") if isinstance(taxes, Mapping) else None

        promo_text, promo_hash = norm.promo_payload(_offers(row.get("offerGroups") or ()))

        raw_status = row.get("status")
        status = STATUS_MAP.get(str(raw_status).upper(), norm.AVAIL_UNKNOWN)

        rows.append(Observation(
            scrape_ts_utc=scrape_ts,
            scrape_date=scrape_date,
            line=line_cfg.line,
            brand=line_cfg.brand,
            ship=ship.get("title"),
            ship_code=ship.get("code"),
            sailing_id=str(row.get("sailId")),
            itinerary_code=itinerary_code,
            package_id=str(row.get("packageId")) if row.get("packageId") else None,
            sail_date=str(sail_date)[:10] if sail_date else None,
            return_date=str(return_date)[:10] if return_date else None,
            nights=nights,
            itinerary_name=details.get("title"),
            embark_port=embark.get("title") or details.get("startingLocation"),
            disembark_port=disembark.get("title"),
            region=region,
            market=market_for(row.get("currencyCode")),
            currency=row.get("currencyCode"),
            cabin_category=category,
            cabin_subcategory=str(raw_label),
            price_total=norm.price_total_double(per_person),
            price_pppn=norm.price_pppn(per_person, nights),
            price_per_person=per_person,
            price_basis=PRICE_BASIS,
            taxes_fees=float(taxes_amount) if isinstance(taxes_amount, (int, float)) else None,
            taxes_fees_text=taxes_text or None,
            is_guarantee=None,      # NCL's grid does not expose guarantee vs assigned
            availability_status=status,
            availability_status_raw=str(raw_status) if raw_status is not None else None,
            units_remaining=None,   # NCL exposes no inventory count
            promo_text=promo_text,
            promo_hash=promo_hash,
            tier=tier,
            source_url=source_url,
            raw_response_path=raw_path,
        ))

    return rows, unmapped


def months_in_window(start: str | None, end: str | None) -> list[str]:
    """2027-01-01 .. 2027-03-31 -> [Jan-2027, Feb-2027, Mar-2027].

    NCL's search endpoint takes month-granular date filters in this format.
    """
    if not start or not end:
        return []
    from datetime import date

    names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
             "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    out: list[str] = []
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        out.append(f"{names[month - 1]}-{year}")
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return out


class NCLSource:
    key = "ncl"

    def __init__(self, cfg: Config, line_cfg: LineConfig, client: PoliteClient,
                 store: Store, archive: RawArchive):
        self.cfg = cfg
        self.line_cfg = line_cfg
        self.client = client
        self.store = store
        self.archive = archive

    # -- urls -------------------------------------------------------------
    def search_url(self, *, limit: int, offset: int,
                   destinations: str | None = None, dates: str | None = None) -> str:
        query = [f"limit={limit}", f"offset={offset}"]
        if destinations:
            query.append(f"destinations={destinations}")
        if dates:
            query.append(f"dates={dates}")
        return f"{self.line_cfg.base_url}/api/v2/vacations/search?" + "&".join(query)

    def sailings_url(self, itinerary_code: str) -> str:
        return f"{self.line_cfg.base_url}/api/vacations/sailings/{itinerary_code}"

    # -- enumeration ------------------------------------------------------
    def discover_itineraries(self, tier_cfg: TierConfig,
                             result: CollectResult) -> list[str]:
        """Page the search endpoint to enumerate itinerary codes in scope."""
        if tier_cfg.marker_only and tier_cfg.marker_itineraries:
            return list(tier_cfg.marker_itineraries)

        months = months_in_window(tier_cfg.sail_window_start, tier_cfg.sail_window_end)
        page_size = self.line_cfg.search_page_size
        cap = self.line_cfg.max_itineraries
        codes: list[str] = []

        for month in months:
            offset = 0
            while True:
                url = self.search_url(limit=page_size, offset=offset, dates=month)
                try:
                    payload, body = self.client.get_json_with_body(url)
                except (FetchError, RobotsDisallowed) as exc:
                    result.errors.append(
                        {"stage": "search", "url": url, "error": str(exc)})
                    break
                self.archive.write(self.key, "search", f"{month}_{offset}", body)

                found = parse_search_itineraries(payload)
                for code in found:
                    if code not in codes:
                        codes.append(code)

                total = int(payload.get("total") or 0)
                offset += page_size
                if not found or offset >= total:
                    break
                if cap and len(codes) >= cap:
                    break
            if cap and len(codes) >= cap:
                codes = codes[:cap]
                break
        return codes

    # -- collection -------------------------------------------------------
    def collect(self, tier: str) -> CollectResult:
        tier_cfg = self.cfg.tier(tier)
        result = CollectResult()
        now = utcnow()
        scrape_ts, scrape_date = iso(now), now.strftime("%Y-%m-%d")

        codes = self.discover_itineraries(tier_cfg, result)
        done = self.store.completed_itineraries(tier, self.line_cfg.line, scrape_date)
        todo = [c for c in codes if c not in done]
        print(f"  {len(codes)} itineraries in scope, "
              f"{len(done)} already captured today, {len(todo)} to fetch")

        window = (tier_cfg.sail_window_start, tier_cfg.sail_window_end)
        for index, code in enumerate(todo, 1):
            result.sailings_attempted += 1
            url = self.sailings_url(code)
            try:
                payload, body = self.client.get_json_with_body(url)
            except (FetchError, RobotsDisallowed) as exc:
                result.errors.append({"stage": "sailings", "itinerary_code": code,
                                      "url": url, "error": str(exc)})
                continue

            raw_path = self.archive.write(self.key, "sailings", code, body, ts=now)
            rows, unmapped = parse_sailings(
                payload,
                line_cfg=self.line_cfg,
                tier=tier,
                scrape_ts=scrape_ts,
                scrape_date=scrape_date,
                source_url=url,
                raw_path=raw_path,
                sail_window=window,
                allowed_regions=self.line_cfg.regions or None,
            )

            for label in unmapped:
                self.store.log_unmapped_label(self.line_cfg.line, label)
            result.unmapped_labels |= unmapped

            written = self.store.upsert_observations(rows)
            result.observations_written += written
            if rows:
                result.sailings_captured += 1
            self.store.mark_itinerary_done(tier, self.line_cfg.line, scrape_date,
                                           code, written)
            if index % 10 == 0 or index == len(todo):
                print(f"    [{index}/{len(todo)}] {code}: {written} observations")

        return result
