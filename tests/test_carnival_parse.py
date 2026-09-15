"""Carnival parsing tests, run against real archived API responses.

Fixtures are genuine /cruisesearch/api/search bodies captured during recon.
"""
import copy

import pytest

from panel import normalize as norm
from panel.sources import carnival
from panel.sources.carnival import CurrencyMismatch

SCRAPE_TS = "2026-09-15T12:00:00+00:00"
SCRAPE_DATE = "2026-09-15"
URL = "https://www.carnival.com/cruisesearch/api/search?pagesize=12&pagenumber=1"


def parse(payload, line_cfg, **kw):
    kw.setdefault("tier", "weekly-full")
    kw.setdefault("scrape_ts", SCRAPE_TS)
    kw.setdefault("scrape_date", SCRAPE_DATE)
    kw.setdefault("source_url", URL)
    return carnival.parse_search(payload, line_cfg=line_cfg, **kw)


def one_room_payload(*, region_code="CW", port="MIA", **room):
    """Minimal payload with a single room cell, for targeted assertions."""
    cell = {"metacode": "OB", "price": 351, "priceCurrency": "USD",
            "taxesAndFees": 0.0, "soldOut": False, "rateCode": "OTR",
            "categoryCode": "8A", "offerId": 258073}
    cell.update(room)
    return {"results": {"itineraries": [{
        "code": "TST", "shipCode": "CQ", "shipName": "Test Ship",
        "itineraryTitle": "Test", "regionCode": region_code,
        "departurePortCode": port, "departurePortName": "Test Port",
        "dur": 7, "roundtrip": True,
        "sailings": [{
            "sailingId": "99999",
            "departureDate": "2027-03-01T00:00:00.000Z",
            "arrivalDate": "2027-03-08T00:00:00.000Z",
            "rooms": {"balcony": cell},
        }],
    }]}}


class TestFixtureShape:
    def test_real_fixture_parses(self, ccl_search_us, ccl_line_cfg):
        rows, _, _ = parse(ccl_search_us, ccl_line_cfg)
        assert rows, "expected observations from the archived response"

    def test_all_four_categories_captured(self, ccl_page1, ccl_line_cfg):
        rows, _, _ = parse(ccl_page1, ccl_line_cfg)
        assert {r.cabin_subcategory for r in rows} == {"IS", "OS", "OB", "SU"}

    def test_natural_key_is_unique(self, ccl_page1, ccl_line_cfg):
        rows, _, _ = parse(ccl_page1, ccl_line_cfg)
        keys = {(r.line, r.sailing_id, r.cabin_subcategory, r.market, r.scrape_date)
                for r in rows}
        assert len(keys) == len(rows)


class TestSoldOutTrap:
    """Carnival reports price=0 (not null) on sold-out cells."""

    def test_no_row_has_soldout_with_a_price(self, ccl_page1, ccl_page2, ccl_line_cfg):
        for payload in (ccl_page1, ccl_page2):
            rows, _, _ = parse(payload, ccl_line_cfg)
            offenders = [r for r in rows
                         if r.availability_status == norm.AVAIL_SOLD_OUT
                         and r.price_total is not None]
            assert offenders == [], (
                f"{len(offenders)} sold-out rows carry a price; "
                "price=0 must become NULL")

    def test_soldout_row_nulls_every_price_field(self, ccl_line_cfg):
        payload = one_room_payload(soldOut=True, price=0, priceCurrency=None,
                                   categoryCode=None, taxesAndFees=None)
        rows, _, _ = parse(payload, ccl_line_cfg)
        row = rows[0]
        assert row.availability_status == norm.AVAIL_SOLD_OUT
        assert row.price_total is None
        assert row.price_pppn is None
        assert row.price_per_person is None
        assert row.taxes_fees is None

    def test_zero_price_is_nulled_even_if_not_flagged_soldout(self, ccl_line_cfg):
        """Defensive: a 0 price is never a real fare."""
        payload = one_room_payload(soldOut=False, price=0)
        rows, _, _ = parse(payload, ccl_line_cfg)
        assert rows[0].price_total is None

    def test_the_fixture_actually_contains_soldout_cells(self, ccl_page1, ccl_line_cfg):
        """Guard the guard: if the fixture had no sold-out cells the test above
        would pass vacuously."""
        rows, _, _ = parse(ccl_page1, ccl_line_cfg)
        sold = [r for r in rows if r.availability_status == norm.AVAIL_SOLD_OUT]
        assert sold, "fixture no longer exercises the sold-out path"

    def test_available_rows_do_carry_prices(self, ccl_page1, ccl_line_cfg):
        rows, _, _ = parse(ccl_page1, ccl_line_cfg)
        priced = [r for r in rows
                  if r.availability_status == norm.AVAIL_AVAILABLE
                  and r.price_total is not None]
        assert priced, "expected available rows to be priced"


class TestCurrencyAssertion:
    """USD comes from a Redis cache we do not control -- a flip must be loud."""

    def test_usd_is_accepted(self, ccl_line_cfg):
        rows, _, _ = parse(one_room_payload(priceCurrency="USD"), ccl_line_cfg)
        assert rows[0].currency == "USD"
        assert rows[0].market == "US"

    def test_non_usd_raises_rather_than_dropping_or_converting(self, ccl_line_cfg):
        with pytest.raises(CurrencyMismatch) as exc:
            parse(one_room_payload(priceCurrency="CAD"), ccl_line_cfg)
        assert "CAD" in str(exc.value)

    def test_mismatch_message_names_the_sailing(self, ccl_line_cfg):
        with pytest.raises(CurrencyMismatch) as exc:
            parse(one_room_payload(priceCurrency="GBP"), ccl_line_cfg)
        msg = str(exc.value)
        assert "99999" in msg and "TST" in msg

    def test_soldout_rows_do_not_trip_the_assertion(self, ccl_line_cfg):
        """Sold-out cells legitimately carry priceCurrency=None."""
        payload = one_room_payload(soldOut=True, price=0, priceCurrency=None)
        rows, _, _ = parse(payload, ccl_line_cfg)
        assert rows[0].availability_status == norm.AVAIL_SOLD_OUT

    def test_whole_real_fixture_is_usd(self, ccl_page1, ccl_page2, ccl_line_cfg):
        for payload in (ccl_page1, ccl_page2):
            rows, _, _ = parse(payload, ccl_line_cfg)
            priced = [r for r in rows if r.price_total is not None]
            assert {r.currency for r in priced} == {"USD"}
            assert {r.market for r in priced} == {"US"}


class TestRegionMapping:
    """Southern Europe is where the thesis concentrates -- pin it hard."""

    @pytest.mark.parametrize("region_code,expected", [
        ("ME", "Southern Europe"),   # Mediterranean
        ("GI", "Southern Europe"),   # Greek Isles, Turkey & Italy
        ("CG", "Southern Europe"),   # Croatia, Greece & Italy
        ("IB", "Southern Europe"),   # Spain, Portugal & France
        ("EC", "Southern Europe"),   # Eclipse, Spain, Portugal
        ("EN", "Northern Europe"),   # Northern Europe
        ("ES", "Northern Europe"),   # Scandinavia & Baltic
        ("BI", "Northern Europe"),   # British Isles
        ("CE", "Caribbean"),
        ("CW", "Caribbean"),
        ("BH", "Caribbean"),
        ("BM", "Bermuda"),
        ("GL", "Alaska"),
    ])
    def test_region_code_is_authoritative(self, region_code, expected, ccl_line_cfg):
        region, unmapped = carnival.resolve_region(
            {"regionCode": region_code, "departurePortCode": "XXX"}, ccl_line_cfg)
        assert region == expected
        assert unmapped is None

    @pytest.mark.parametrize("port,expected", [
        ("BCN", "Southern Europe"),  # Barcelona
        ("CIV", "Southern Europe"),  # Civitavecchia (Rome)
        ("LIS", "Southern Europe"),  # Lisbon
    ])
    def test_port_fallback_when_region_code_is_unknown(self, port, expected,
                                                       ccl_line_cfg):
        region, unmapped = carnival.resolve_region(
            {"regionCode": "ZZZ", "departurePortCode": port}, ccl_line_cfg)
        assert region == expected
        assert unmapped is None

    def test_region_code_beats_port_when_they_disagree(self, ccl_line_cfg):
        """An Iberian sailing out of Barcelona is Southern either way; the point
        is that the code wins, so a Northern code from a Southern port cannot be
        flipped by the fallback."""
        region, _ = carnival.resolve_region(
            {"regionCode": "ES", "departurePortCode": "BCN"}, ccl_line_cfg)
        assert region == "Northern Europe"

    def test_london_is_not_in_the_port_map(self, ccl_line_cfg):
        """LON hosts Northern (EN/ES/BI) AND Iberian (IB/EC) itineraries, so a
        port-based guess would misclassify Southern Europe sailings."""
        assert "LON" not in ccl_line_cfg.port_region_map

    def test_london_iberian_sailing_is_southern_via_region_code(self, ccl_line_cfg):
        region, _ = carnival.resolve_region(
            {"regionCode": "IB", "departurePortCode": "LON"}, ccl_line_cfg)
        assert region == "Southern Europe"

    def test_london_scandinavian_sailing_is_northern(self, ccl_line_cfg):
        region, _ = carnival.resolve_region(
            {"regionCode": "ES", "departurePortCode": "LON"}, ccl_line_cfg)
        assert region == "Northern Europe"

    def test_unknown_region_is_logged_not_guessed(self, ccl_line_cfg):
        region, unmapped = carnival.resolve_region(
            {"regionCode": "QQ", "departurePortCode": "ZZZ"}, ccl_line_cfg)
        assert region is None
        assert "QQ" in unmapped and "ZZZ" in unmapped

    def test_unmapped_regions_surface_from_parse(self, ccl_line_cfg):
        payload = one_room_payload(region_code="QQ", port="ZZZ")
        rows, _, unmapped_regions = parse(payload, ccl_line_cfg)
        assert unmapped_regions
        assert rows[0].region is None

    def test_region_filter_excludes_other_regions(self, ccl_line_cfg):
        payload = one_room_payload(region_code="GL")   # Alaska
        rows, _, _ = parse(payload, ccl_line_cfg,
                           allowed_regions=["Southern Europe"])
        assert rows == []


class TestGranularity:
    """Carnival genuinely resolves finer than NCL -- record it, don't fake it."""

    def test_vendor_category_and_rate_code_are_populated(self, ccl_line_cfg):
        rows, _, _ = parse(one_room_payload(), ccl_line_cfg)
        row = rows[0]
        assert row.vendor_category_code == "8A"
        assert row.rate_code == "OTR"
        assert row.offer_id == "258073"

    def test_cabin_subcategory_is_the_stable_meta_code(self, ccl_line_cfg):
        """categoryCode goes null on sold-out cells, so it cannot carry the key."""
        rows, _, _ = parse(one_room_payload(soldOut=True, price=0,
                                            categoryCode=None), ccl_line_cfg)
        assert rows[0].cabin_subcategory == "OB"
        assert rows[0].vendor_category_code is None

    def test_real_fixture_has_many_distinct_vendor_categories(self, ccl_page1,
                                                              ccl_line_cfg):
        rows, _, _ = parse(ccl_page1, ccl_line_cfg)
        codes = {r.vendor_category_code for r in rows if r.vendor_category_code}
        assert len(codes) > 5, f"expected real sub-category spread, got {codes}"

    def test_real_fixture_has_multiple_rate_codes(self, ccl_page1, ccl_line_cfg):
        rows, _, _ = parse(ccl_page1, ccl_line_cfg)
        rates = {r.rate_code for r in rows if r.rate_code}
        assert len(rates) > 1, f"expected several rate codes, got {rates}"


class TestPricesAndTaxes:
    def test_price_total_is_double_occupancy(self, ccl_line_cfg):
        rows, _, _ = parse(one_room_payload(price=351), ccl_line_cfg)
        assert rows[0].price_per_person == 351.0
        assert rows[0].price_total == 702.0

    def test_pppn_divides_by_nights(self, ccl_line_cfg):
        rows, _, _ = parse(one_room_payload(price=700), ccl_line_cfg)
        assert rows[0].nights == 7
        assert rows[0].price_pppn == pytest.approx(100.0)

    def test_zero_tax_is_null_not_zero(self, ccl_line_cfg):
        """Carnival returns taxesAndFees 0.0 everywhere; 0 must not be recorded
        as a genuine zero-tax sailing."""
        rows, _, _ = parse(one_room_payload(taxesAndFees=0.0), ccl_line_cfg)
        assert rows[0].taxes_fees is None

    def test_real_tax_amount_is_captured_if_it_ever_appears(self, ccl_line_cfg):
        rows, _, _ = parse(one_room_payload(taxesAndFees=146.0), ccl_line_cfg)
        assert rows[0].taxes_fees == 146.0
        assert rows[0].price_total == 702.0    # fare only, taxes excluded

    def test_price_basis_is_recorded(self, ccl_line_cfg):
        rows, _, _ = parse(one_room_payload(), ccl_line_cfg)
        assert rows[0].price_basis == "rooms.price"


class TestUnsupportedFields:
    def test_units_remaining_is_null(self, ccl_page1, ccl_line_cfg):
        rows, _, _ = parse(ccl_page1, ccl_line_cfg)
        assert all(r.units_remaining is None for r in rows)

    def test_promo_is_null_because_carnival_exposes_no_offer_detail(
            self, ccl_page1, ccl_line_cfg):
        rows, _, _ = parse(ccl_page1, ccl_line_cfg)
        assert all(r.promo_text is None for r in rows)
        assert all(r.promo_hash is None for r in rows)

    def test_no_limited_state(self, ccl_page1, ccl_line_cfg):
        """Carnival availability is a boolean: available or sold_out."""
        rows, _, _ = parse(ccl_page1, ccl_line_cfg)
        assert {r.availability_status for r in rows} <= {
            norm.AVAIL_AVAILABLE, norm.AVAIL_SOLD_OUT}


class TestWindowFiltering:
    def test_sail_window_is_applied(self, ccl_page1, ccl_line_cfg):
        rows, _, _ = parse(ccl_page1, ccl_line_cfg,
                           sail_window=("2027-01-01", "2027-08-31"))
        assert all("2027-01-01" <= r.sail_date <= "2027-08-31" for r in rows)

    def test_out_of_window_sailing_is_excluded(self, ccl_line_cfg):
        payload = one_room_payload()
        rows, _, _ = parse(payload, ccl_line_cfg,
                           sail_window=("2028-01-01", "2028-12-31"))
        assert rows == []


class TestPagingAndResume:
    """Regression: the resume skip-branch must not walk past the page cap.

    The first implementation `continue`d on an already-done marker before
    checking max_pages, so a resumed run kept incrementing the page number and
    fetched pages beyond the cap.
    """

    def _fake_source(self, ccl_line_cfg, tmp_path, done_markers, max_pages=2):
        import json as _json
        from panel.config import load_config
        from panel.sources.carnival import CarnivalSource
        from panel.storage import RawArchive, Store

        cfg = load_config("config/panel.yaml")
        ccl_line_cfg.max_pages = max_pages
        ccl_line_cfg.dest_codes = ["E"]
        store = Store(tmp_path / "t.sqlite")
        for m in done_markers:
            store.mark_itinerary_done("weekly-full", ccl_line_cfg.line,
                                      __import__("datetime").datetime.now(
                                          __import__("datetime").timezone.utc
                                      ).strftime("%Y-%m-%d"), m, 0)

        fetched = []

        class FakeClient:
            def get_json_with_body(self, url):
                import re
                page = int(re.search(r"pagenumber=(\d+)", url).group(1))
                fetched.append(page)
                payload = {"results": {"itineraries": [], "lastPage": 9,
                                       "totalResults": 90}}
                return payload, _json.dumps(payload)

        src = CarnivalSource(cfg, ccl_line_cfg, FakeClient(), store,
                             RawArchive(tmp_path / "raw"))
        return src, store, fetched

    def test_fresh_run_respects_the_page_cap(self, ccl_line_cfg, tmp_path):
        src, store, fetched = self._fake_source(ccl_line_cfg, tmp_path, [])
        src.collect("weekly-full")
        store.close()
        assert fetched == [1, 2], f"cap breached: fetched pages {fetched}"

    def test_resumed_run_still_respects_the_page_cap(self, ccl_line_cfg, tmp_path):
        """With page 1 already done, the run must fetch only page 2 -- not 2 and 3."""
        src, store, fetched = self._fake_source(ccl_line_cfg, tmp_path, ["E_p1"])
        src.collect("weekly-full")
        store.close()
        assert fetched == [2], f"cap breached on resume: fetched pages {fetched}"

    def test_all_pages_done_fetches_nothing(self, ccl_line_cfg, tmp_path):
        src, store, fetched = self._fake_source(ccl_line_cfg, tmp_path,
                                                ["E_p1", "E_p2"])
        src.collect("weekly-full")
        store.close()
        assert fetched == [], f"expected no fetches, got {fetched}"

    def test_completed_dest_is_skipped_entirely(self, ccl_line_cfg, tmp_path):
        src, store, fetched = self._fake_source(ccl_line_cfg, tmp_path,
                                                ["E_complete"], max_pages=None)
        src.collect("weekly-full")
        store.close()
        assert fetched == [], f"completed dest re-paged: {fetched}"

    def test_uncapped_run_records_a_completion_marker(self, ccl_line_cfg, tmp_path):
        src, store, fetched = self._fake_source(ccl_line_cfg, tmp_path, [],
                                                max_pages=None)
        src.collect("weekly-full")
        markers = store.completed_itineraries(
            "weekly-full", ccl_line_cfg.line,
            __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc).strftime("%Y-%m-%d"))
        store.close()
        assert "E_complete" in markers
        assert fetched == list(range(1, 10))   # paged to lastPage=9, then stopped
