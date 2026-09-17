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
# SOLO_GUEST_ONLY is a product restriction, not scarcity: it marks NCL's Studio
# staterooms, which are single-occupancy by design and therefore never quotable
# on the panel's 2-pax basis. Mapping it to `limited` would have counted a fixed
# attribute of the cabin as inventory depletion. See normalize.AVAIL_SOLO_ONLY.
STATUS_MAP = {
    "AVAILABLE": norm.AVAIL_AVAILABLE,
    "SOLO_GUEST_ONLY": norm.AVAIL_SOLO_ONLY,
    "SOLD_OUT": norm.AVAIL_SOLD_OUT,
}

# Which vendor price field becomes price_per_person. combinedPrice is the
# published per-person double-occupancy fare, i.e. the number a shopper sees.
PRICE_BASIS = "combinedPrice"

# NCL resolves market from client IP, so this is the assertion that the runner
# is where we think it is. See scripts/verify_us_market.py for the preflight.
EXPECTED_CURRENCY = "USD"

# Re-exported so callers can catch one type for either source.
CurrencyMismatch = norm.CurrencyMismatch

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
    IP at the edge, so what comes back depends on where the runner sits.
    Recording the resolved market is only half the defence -- `parse_sailings`
    raises CurrencyMismatch on any priced row that is not EXPECTED_CURRENCY, so
    a non-US egress fails the run instead of quietly filling the panel with CAD.
    That is not hypothetical: a local run on 2026-09-16 wrote 4,470 CAD rows
    before this guard existed.
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
    expected_currency: str = EXPECTED_CURRENCY,
    window_stats: dict[str, int] | None = None,
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

    # Land+cruise packages ("cruisetours"): NCL publishes a package fare but
    # stamps the row with the cruise segment only, e.g. Denali itineraries carry
    # duration {"itinerary": 14, "cruising": 7}. Dividing the package fare by
    # cruise nights would overstate the nightly cruise rate ~2x and put a
    # bundled land tour up against a peer's cruise-only fare. Capture the flag
    # and the package length; do not adjust the price, which would invent a
    # cruise-only fare NCL never published.
    duration = details.get("duration") if isinstance(details.get("duration"), Mapping) else {}
    itinerary_nights = duration.get("itinerary")
    itinerary_nights = int(itinerary_nights) if isinstance(itinerary_nights, int) else None

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
            # Counted, after the region filter above, so this measures only
            # what the DATE window removed from in-scope itineraries.
            norm.count_window_drop(window_stats, sail_date,
                                   window_start, window_end)
            continue

        raw_label = row.get("stateroomType")
        category = norm.map_cabin_category(raw_label, line_cfg.cabin_map)
        if category is None and raw_label:
            unmapped.add(str(raw_label))

        return_date = row.get("sailEndDate")
        nights = norm.nights_between(sail_date, return_date)

        published = row.get(PRICE_BASIS)
        per_person = float(published) if isinstance(published, (int, float)) else None

        # A priced row in the wrong currency means the edge served a different
        # market. Fail the itinerary loudly rather than record a CAD fare.
        currency = row.get("currencyCode")
        if per_person is not None and str(currency or "").upper() != expected_currency.upper():
            raise norm.CurrencyMismatch(
                f"{itinerary_code}: expected {expected_currency}, got {currency!r} "
                f"on sailing {row.get('sailId')!r}. The egress is not resolving "
                f"as the {expected_currency} market; no rows written.")

        # NCL does not publish a tax amount on any reachable endpoint: this key
        # is absent from every pricingStateRooms row observed, in both USD and
        # CAD. Read defensively anyway so the panel picks it up for free if NCL
        # ever starts sending it. The fare itself is tax-exclusive (NCL's
        # disclaimers: taxes and fees "are additional"), so price_total and
        # price_pppn are fare-only regardless.
        taxes = row.get("taxesAndFees")
        taxes_amount = taxes.get("amount") if isinstance(taxes, Mapping) else None
        taxes_text = taxes.get("text") if isinstance(taxes, Mapping) else None

        promo_text, promo_hash = norm.promo_payload(_offers(row.get("offerGroups") or ()))

        raw_status = row.get("status")
        status = STATUS_MAP.get(str(raw_status).upper(), norm.AVAIL_UNKNOWN)

        # Prefer the per-row flag; fall back to the published durations
        # disagreeing, which is what a package looks like when isPackage is
        # absent. Unknown stays NULL rather than defaulting to "not a package".
        flag = row.get("isPackage")
        if isinstance(flag, bool):
            is_package = int(flag)
        elif itinerary_nights is not None and nights is not None:
            is_package = int(itinerary_nights > nights)
        else:
            is_package = None

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
            is_package=is_package,
            itinerary_nights=itinerary_nights,
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
    window_drop_unit = "cabin pricing rows"

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
    def catalogue_size(self, result: CollectResult) -> int:
        """Total itineraries with NO date filter; 0 if it cannot be read."""
        url = self.search_url(limit=1, offset=0)
        try:
            payload = self.client.get_json(url)
        except (FetchError, RobotsDisallowed) as exc:
            result.errors.append({"stage": "catalogue", "url": url,
                                  "error": str(exc)})
            return 0
        return int(payload.get("total") or 0)

    def discover_itineraries(self, tier_cfg: TierConfig,
                             result: CollectResult) -> list[str]:
        """Page the search endpoint to enumerate itinerary codes in scope."""
        markers = tier_cfg.markers_for(self.key)
        if tier_cfg.marker_only and markers:
            return markers

        months = months_in_window(tier_cfg.sail_window_start, tier_cfg.sail_window_end)
        page_size = self.line_cfg.search_page_size
        cap = self.line_cfg.max_itineraries
        codes: list[str] = []

        # NCL ignores `dates=` for months beyond its published horizon and
        # returns the whole catalogue instead (verified 2026-09-16: Nov-2028,
        # Jan-2029 and no filter at all all report total=801). A window that
        # reaches past the horizon would re-page the full catalogue once per
        # month. So learn the catalogue size once, and stop at the first month
        # whose "filtered" total equals it.
        catalogue = self.catalogue_size(result)
        horizon_hit = False

        for month in months:
            offset = 0
            first_page = True
            while True:
                url = self.search_url(limit=page_size, offset=offset, dates=month)
                try:
                    payload, body = self.client.get_json_with_body(url)
                except (FetchError, RobotsDisallowed) as exc:
                    result.errors.append(
                        {"stage": "search", "url": url, "error": str(exc)})
                    break
                total = int(payload.get("total") or 0)
                if first_page and catalogue and total >= catalogue:
                    result.notes.append(
                        f"NCL search horizon reached at {month}: the date filter "
                        f"returned the full catalogue ({total}), so it is not "
                        f"being honoured. Stopped discovery there; later months "
                        f"in the window add nothing a filtered search can find.")
                    horizon_hit = True
                    break
                first_page = False
                self.archive.write(self.key, "search", f"{month}_{offset}", body)

                found = parse_search_itineraries(payload)
                for code in found:
                    if code not in codes:
                        codes.append(code)

                offset += page_size
                if not found or offset >= total:
                    break
                if cap and len(codes) >= cap:
                    break
            if horizon_hit:
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
            try:
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
                    window_stats=result.outside_window,
                )
            except norm.CurrencyMismatch as exc:
                # The edge resolved a market we did not ask for. Every remaining
                # itinerary would be wrong the same way, so stop the line rather
                # than write hundreds more rows in the wrong currency. Recorded
                # as fatal so the CLI exits non-zero and the run is not silently
                # treated as a good collection.
                print(f"  !! CURRENCY MISMATCH: {exc}")
                result.errors.append({"stage": "currency", "code": code,
                                      "url": url, "error": str(exc),
                                      "fatal": True})
                break

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
