"""Carnival Cruise Line collector.

Endpoint (public JSON, plain GET, same host, robots-allowed):

  GET /cruisesearch/api/search?pagesize=&pagenumber=&numadults=2[&dest=][&itincodes=]
      -> {results: {itineraries: [...], totalResults, lastPage}, filters, options}

The useful payload is nested inside each itinerary, not at results.sailings
(which is always null):

  results.itineraries[]                  itinerary metadata
    .sailings[]                          one entry per departure date
      .rooms.{interior,oceanview,balcony,suite}
                                         one cell per cabin category

Each room cell carries price, taxesAndFees, soldOut, rateCode, categoryCode
and offerId. Carnival therefore resolves two things NCL does not: a vendor
sub-category (8A, GS, 6K) and a rate code (OB7, PSV, OTR). Those land in
vendor_category_code / rate_code and stay NULL for sources that lack them --
see capabilities.py, which stops cross-line analysis running at a resolution
only one side has.

Two traps this module handles explicitly:

1. A sold-out cell comes back as price=0 (not null) with categoryCode,
   priceCurrency and taxesAndFees all null. Writing that through unchecked
   would put a stream of $0 fares into the panel. Sold-out cells are stored
   with NULL prices.

2. The USD response is served from a Redis cache we do not control -- the
   `locality` parameter is echoed back as "1" whatever we send. Every priced
   row is therefore asserted to be USD, and a mismatch raises rather than
   being dropped or converted, so a currency flip surfaces mid-panel instead
   of being discovered later in the data.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from .. import normalize as norm
from ..config import Config, LineConfig, TierConfig
from ..http_client import FetchError, PoliteClient, RobotsDisallowed
from ..storage import Observation, RawArchive, Store, iso, utcnow
from .base import CollectResult

EXPECTED_CURRENCY = "USD"

# Safety stop: paging must terminate even if lastPage is absent or wrong.
HARD_PAGE_CEILING = 500

# rooms{} slot -> vendor meta code. The meta code is the stable per-sailing
# cabin label and is what cabin_subcategory holds; categoryCode is finer but
# goes null on sold-out cells, so it cannot carry the natural key.
SLOT_METACODE = {
    "interior": "IS",
    "oceanview": "OS",
    "balcony": "OB",
    "suite": "SU",
}


class CurrencyMismatch(Exception):
    """Raised when a priced row is not in the expected currency.

    Deliberately fatal for the itinerary rather than silently skipped: the USD
    result depends on a server-side cache, so a change must be noticed while
    the panel is being collected.
    """


def parse_search(
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
) -> tuple[list[Observation], set[str], set[str]]:
    """Turn one /cruisesearch/api/search response into observations.

    Pure: no network, no database. Returns
    (observations, unmapped_cabin_labels, unmapped_region_keys).
    Raises CurrencyMismatch if a priced cell is not in `expected_currency`.
    """
    rows: list[Observation] = []
    unmapped_cabins: set[str] = set()
    unmapped_regions: set[str] = set()

    allowed = set(allowed_regions) if allowed_regions else None
    window_start, window_end = sail_window

    results = payload.get("results") or {}
    for itinerary in results.get("itineraries") or ():
        region, region_key = resolve_region(itinerary, line_cfg)
        if region is None and region_key:
            unmapped_regions.add(region_key)
        if allowed is not None and region not in allowed:
            continue

        itinerary_code = itinerary.get("code")
        ship_code = itinerary.get("shipCode")
        ship_name = itinerary.get("shipName")
        title = itinerary.get("itineraryTitle") or itinerary.get("itineraryTitleFormatted")
        embark_code = itinerary.get("departurePortCode")
        embark_name = itinerary.get("departurePortName")
        nights_declared = itinerary.get("dur")

        for sailing in itinerary.get("sailings") or ():
            sail_date = sailing.get("departureDate")
            if not norm.in_window(sail_date, window_start, window_end):
                continue
            return_date = sailing.get("arrivalDate")
            nights = norm.nights_between(sail_date, return_date)
            if nights is None and isinstance(nights_declared, (int, str)):
                try:
                    nights = int(nights_declared)
                except (TypeError, ValueError):
                    nights = None

            sailing_id = sailing.get("sailingId")
            if not sailing_id:
                continue

            for slot, room in (sailing.get("rooms") or {}).items():
                if not room:
                    continue
                meta = room.get("metacode") or SLOT_METACODE.get(slot)
                if not meta:
                    continue

                sold_out = bool(room.get("soldOut"))
                category = norm.map_cabin_category(meta, line_cfg.cabin_map)
                if category is None:
                    unmapped_cabins.add(str(meta))

                currency = room.get("priceCurrency")
                raw_price = room.get("price")

                # Trap 1: sold-out cells report price 0, not null.
                if sold_out or not isinstance(raw_price, (int, float)) or raw_price <= 0:
                    per_person = None
                else:
                    # Trap 2: only assert currency on rows that carry money.
                    if str(currency).upper() != expected_currency.upper():
                        raise CurrencyMismatch(
                            f"expected {expected_currency} but got {currency!r} "
                            f"for itinerary {itinerary_code} sailing {sailing_id} "
                            f"cabin {meta} (price={raw_price}). "
                            f"Carnival's USD result comes from a server-side cache; "
                            f"this run must not be trusted as a USD panel."
                        )
                    per_person = float(raw_price)

                taxes = room.get("taxesAndFees")
                taxes_amount = (float(taxes)
                                if isinstance(taxes, (int, float)) and taxes > 0
                                else None)

                rows.append(Observation(
                    scrape_ts_utc=scrape_ts,
                    scrape_date=scrape_date,
                    line=line_cfg.line,
                    brand=line_cfg.brand,
                    ship=ship_name,
                    ship_code=ship_code,
                    sailing_id=str(sailing_id),
                    itinerary_code=itinerary_code,
                    package_id=itinerary.get("id"),
                    sail_date=str(sail_date)[:10] if sail_date else None,
                    return_date=str(return_date)[:10] if return_date else None,
                    nights=nights,
                    itinerary_name=title,
                    embark_port=embark_name or embark_code,
                    disembark_port=embark_name if itinerary.get("roundtrip") else None,
                    region=region,
                    market=_market_for(currency, expected_currency),
                    currency=currency or (expected_currency if per_person else None),
                    cabin_category=category,
                    cabin_subcategory=str(meta),
                    # Finer resolution, genuinely published by this source.
                    vendor_category_code=room.get("categoryCode") or None,
                    rate_code=room.get("rateCode") or None,
                    offer_id=str(room["offerId"]) if room.get("offerId") else None,
                    price_total=norm.price_total_double(per_person),
                    price_pppn=norm.price_pppn(per_person, nights),
                    price_per_person=per_person,
                    price_basis="rooms.price",
                    taxes_fees=taxes_amount,
                    taxes_fees_text=None,
                    is_guarantee=None,
                    availability_status=(norm.AVAIL_SOLD_OUT if sold_out
                                         else norm.AVAIL_AVAILABLE),
                    availability_status_raw=("soldOut=true" if sold_out
                                             else "soldOut=false"),
                    units_remaining=None,
                    promo_text=None,
                    promo_hash=None,
                    tier=tier,
                    source_url=source_url,
                    raw_response_path=raw_path,
                ))

    return rows, unmapped_cabins, unmapped_regions


def resolve_region(itinerary: Mapping[str, Any],
                   line_cfg: LineConfig) -> tuple[str | None, str | None]:
    """Region from regionCode first, embark port only as a fallback.

    Carnival's `dest` filter has a single undifferentiated `E` = Europe, which
    is useless for a thesis concentrated on Southern Europe. `regionCode` is
    far finer (ME Mediterranean, GI Greek Isles, IB Spain/Portugal/France,
    ES Scandinavia & Baltic, BI British Isles) and is authoritative here.

    Port is only a fallback because it is genuinely ambiguous: London (LON) is
    the embark port for Northern itineraries (EN, ES, BI) AND for Iberian ones
    (IB, EC). Any port that serves both regions is deliberately absent from
    the port map rather than guessed at.

    Returns (region, key_used_for_logging_when_unmapped).
    """
    region_code = itinerary.get("regionCode")
    if region_code:
        mapped = line_cfg.region_map.get(str(region_code).strip().upper())
        if mapped:
            return mapped, None

    port_map = getattr(line_cfg, "port_region_map", None) or {}
    port = itinerary.get("departurePortCode")
    if port:
        mapped = port_map.get(str(port).strip().upper())
        if mapped:
            return mapped, None

    key = f"regionCode={region_code!r} port={itinerary.get('departurePortCode')!r}"
    return None, key


def _market_for(currency: str | None, expected: str) -> str:
    if not currency:
        return "US" if expected.upper() == "USD" else "UNKNOWN"
    return {"USD": "US", "CAD": "CA", "GBP": "UK", "EUR": "EU",
            "AUD": "AU"}.get(str(currency).upper(), str(currency).upper())


class CarnivalSource:
    key = "carnival"

    def __init__(self, cfg: Config, line_cfg: LineConfig, client: PoliteClient,
                 store: Store, archive: RawArchive):
        self.cfg = cfg
        self.line_cfg = line_cfg
        self.client = client
        self.store = store
        self.archive = archive

    def _collect_markers(self, markers, tier, scrape_ts, scrape_date,
                         window, now, result):
        """Collect only the named itinerary codes, in batches."""
        done = self.store.completed_itineraries(tier, self.line_cfg.line, scrape_date)
        BATCH = 10
        for i in range(0, len(markers), BATCH):
            batch = markers[i:i + BATCH]
            marker = "markers_" + "_".join(batch)[:80]
            if marker in done:
                continue
            result.sailings_attempted += 1
            url = self.search_url(page_size=100, page_number=1,
                                  itin_codes=",".join(batch))
            try:
                payload, body = self.client.get_json_with_body(url)
            except (FetchError, RobotsDisallowed) as exc:
                result.errors.append({"stage": "markers", "codes": batch,
                                      "url": url, "error": str(exc)})
                continue
            raw_path = self.archive.write(self.key, "markers", marker, body, ts=now)
            try:
                rows, unmapped_cabins, unmapped_regions = parse_search(
                    payload, line_cfg=self.line_cfg, tier=tier,
                    scrape_ts=scrape_ts, scrape_date=scrape_date,
                    source_url=url, raw_path=raw_path,
                    sail_window=window,
                    allowed_regions=self.line_cfg.regions or None)
            except CurrencyMismatch as exc:
                print(f"  !! CURRENCY MISMATCH on {marker}: {exc}")
                result.errors.append({"stage": "currency", "codes": batch,
                                      "url": url, "error": str(exc), "fatal": True})
                continue
            for label in unmapped_cabins:
                self.store.log_unmapped_label(self.line_cfg.line, label)
            for key in unmapped_regions:
                self.store.log_unmapped_label(self.line_cfg.line, f"REGION {key}")
            result.unmapped_labels |= unmapped_cabins | {
                f"REGION {k}" for k in unmapped_regions}
            written = self.store.upsert_observations(rows)
            result.observations_written += written
            if rows:
                result.sailings_captured += 1
            self.store.mark_itinerary_done(tier, self.line_cfg.line, scrape_date,
                                           marker, written)
            print(f"    [markers {','.join(batch)}] {written} observations")
        return result

    def search_url(self, *, page_size: int, page_number: int,
                   dest: str | None = None, itin_codes: str | None = None) -> str:
        q = [f"pagesize={page_size}", f"pagenumber={page_number}",
             "numadults=2", "sort=fromprice", "locality=1", "currency=USD"]
        if dest:
            q.append(f"dest={dest}")
        if itin_codes:
            q.append(f"itincodes={itin_codes}")
        return f"{self.line_cfg.base_url}/cruisesearch/api/search?" + "&".join(q)

    def collect(self, tier: str) -> CollectResult:
        tier_cfg = self.cfg.tier(tier)
        result = CollectResult()
        now = utcnow()
        scrape_ts, scrape_date = iso(now), now.strftime("%Y-%m-%d")
        window = (tier_cfg.sail_window_start, tier_cfg.sail_window_end)

        page_size = self.line_cfg.search_page_size
        done = self.store.completed_itineraries(tier, self.line_cfg.line, scrape_date)

        # The daily-marker tier is a filtered sailing list, not a second sweep of
        # the whole fleet. Carnival's API takes itincodes directly, so a marker
        # run is one narrow query per batch instead of paging every destination.
        markers = tier_cfg.markers_for(self.key)
        if tier_cfg.marker_only and markers:
            return self._collect_markers(
                markers, tier, scrape_ts, scrape_date, window, now, result)

        dests = self.line_cfg.dest_codes or [None]

        for dest in dests:
            label = dest or "ALL"
            # A fully-paged dest records a completion marker, so a resumed run
            # skips it outright instead of walking every page again -- and
            # cannot walk past the end when lastPage is not yet known.
            if f"{label}_complete" in done:
                print(f"    [{label}] already complete today, skipping")
                continue
            page = 1
            while True:
                if self.line_cfg.max_pages and page > self.line_cfg.max_pages:
                    break
                if page > HARD_PAGE_CEILING:
                    result.errors.append({"stage": "paging", "dest": dest,
                                          "error": f"exceeded {HARD_PAGE_CEILING} pages"})
                    break
                marker = f"{label}_p{page}"
                if marker in done:
                    page += 1
                    continue
                url = self.search_url(page_size=page_size, page_number=page, dest=dest)
                result.sailings_attempted += 1
                try:
                    payload, body = self.client.get_json_with_body(url)
                except (FetchError, RobotsDisallowed) as exc:
                    result.errors.append({"stage": "search", "dest": dest,
                                          "page": page, "url": url,
                                          "error": str(exc)})
                    break

                raw_path = self.archive.write(self.key, "search", marker, body, ts=now)
                try:
                    rows, unmapped_cabins, unmapped_regions = parse_search(
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
                except CurrencyMismatch as exc:
                    # Loud, not silent: recorded in collection_log, printed, and
                    # surfaced as a non-zero exit via CollectResult.errors.
                    print(f"  !! CURRENCY MISMATCH on {marker}: {exc}")
                    result.errors.append({"stage": "currency", "dest": dest,
                                          "page": page, "url": url,
                                          "error": str(exc),
                                          "fatal": True})
                    break

                for label in unmapped_cabins:
                    self.store.log_unmapped_label(self.line_cfg.line, label)
                for key in unmapped_regions:
                    self.store.log_unmapped_label(self.line_cfg.line, f"REGION {key}")
                result.unmapped_labels |= unmapped_cabins | {
                    f"REGION {k}" for k in unmapped_regions}

                written = self.store.upsert_observations(rows)
                result.observations_written += written
                if rows:
                    result.sailings_captured += 1
                self.store.mark_itinerary_done(tier, self.line_cfg.line,
                                               scrape_date, marker, written)

                last_page = int((payload.get("results") or {}).get("lastPage") or 1)
                total = int((payload.get("results") or {}).get("totalResults") or 0)
                print(f"    [{marker}] {written} observations "
                      f"(page {page}/{last_page}, {total} itineraries)")
                if page >= last_page:
                    self.store.mark_itinerary_done(
                        tier, self.line_cfg.line, scrape_date,
                        f"{label}_complete", 0)
                    break
                page += 1

        return result
