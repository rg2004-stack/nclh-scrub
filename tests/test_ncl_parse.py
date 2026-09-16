"""NCL parsing tests, run against a real archived API response.

Fixture: tests/fixtures/ncl_sailings_JOY3MIANASNPIMIA.json
  -- a genuine /api/vacations/sailings/{code} body captured during recon,
     15 sail dates x 6 cabin categories = 90 cells.
"""
import pytest

from panel import normalize as norm
from panel.sources import ncl

SCRAPE_TS = "2026-09-14T21:00:00+00:00"
SCRAPE_DATE = "2026-09-14"
URL = "https://www.ncl.com/api/vacations/sailings/JOY3MIANASNPIMIA"


def parse(payload, line_cfg, **kw):
    kw.setdefault("tier", "weekly-full")
    kw.setdefault("scrape_ts", SCRAPE_TS)
    kw.setdefault("scrape_date", SCRAPE_DATE)
    kw.setdefault("source_url", URL)
    return ncl.parse_sailings(payload, line_cfg=line_cfg, **kw)


class TestFixtureShape:
    def test_fixture_is_the_expected_grid(self, sailings_payload):
        rows = sailings_payload["pricingStateRooms"]
        assert len(rows) == 90
        assert len({r["sailStartDate"] for r in rows}) == 15
        assert len({r["stateroomType"] for r in rows}) == 6

    def test_sailid_uniquely_keys_a_cell_with_stateroom_type(self, sailings_payload):
        rows = sailings_payload["pricingStateRooms"]
        keys = {(r["sailId"], r["stateroomType"]) for r in rows}
        assert len(keys) == len(rows)


class TestParseAllCabins:
    def test_captures_every_cabin_category_not_just_the_cheapest(
            self, sailings_payload, ncl_line_cfg):
        rows, _ = parse(sailings_payload, ncl_line_cfg)
        assert len(rows) == 90
        assert {r.cabin_subcategory for r in rows} == {
            "INSIDE", "OCEANVIEW", "BALCONY", "MINISUITE", "SUITE", "HAVEN"}

    def test_price_distribution_is_preserved_within_one_sail_date(
            self, sailings_payload, ncl_line_cfg):
        rows, _ = parse(sailings_payload, ncl_line_cfg)
        target = min(r.sail_date for r in rows)
        same = [r for r in rows if r.sail_date == target]
        prices = {r.cabin_subcategory: r.price_per_person for r in same}
        # Six genuinely distinct points -- this is the whole edge.
        assert len(set(prices.values())) == 6
        assert prices["INSIDE"] < prices["OCEANVIEW"] < prices["BALCONY"]
        assert prices["MINISUITE"] < prices["SUITE"] < prices["HAVEN"]

    def test_no_row_is_silently_dropped(self, sailings_payload, ncl_line_cfg):
        rows, unmapped = parse(sailings_payload, ncl_line_cfg)
        assert len(rows) == len(sailings_payload["pricingStateRooms"])
        assert unmapped == set()


class TestIdentity:
    def test_sailing_id_is_the_sail_id(self, sailings_payload, ncl_line_cfg):
        rows, _ = parse(sailings_payload, ncl_line_cfg)
        raw = {r["sailId"] for r in sailings_payload["pricingStateRooms"]}
        assert {r.sailing_id for r in rows} == raw

    def test_natural_key_is_unique_across_the_payload(
            self, sailings_payload, ncl_line_cfg):
        rows, _ = parse(sailings_payload, ncl_line_cfg)
        keys = {(r.line, r.sailing_id, r.cabin_subcategory, r.market, r.scrape_date)
                for r in rows}
        assert len(keys) == len(rows)

    def test_itinerary_metadata_is_carried(self, sailings_payload, ncl_line_cfg):
        rows, _ = parse(sailings_payload, ncl_line_cfg)
        row = rows[0]
        assert row.itinerary_code == "JOY3MIANASNPIMIA"
        assert row.ship == "Norwegian Joy"
        assert row.ship_code == "JOY"
        assert row.embark_port == "Miami, Florida"
        assert row.region == "Caribbean"
        assert row.line == "Norwegian Cruise Line"
        assert row.brand == "NCL"


class TestPricesAndTaxes:
    def test_taxes_are_never_folded_into_price(self, sailings_payload, ncl_line_cfg):
        """price_total/price_pppn must equal the fare alone."""
        rows, _ = parse(sailings_payload, ncl_line_cfg)
        by_key = {(r.sailing_id, r.cabin_subcategory): r for r in rows}
        for raw in sailings_payload["pricingStateRooms"]:
            row = by_key[(raw["sailId"], raw["stateroomType"])]
            assert row.price_per_person == raw["combinedPrice"]
            assert row.price_total == pytest.approx(raw["combinedPrice"] * 2)

    def test_pppn_is_consistent_with_nights(self, sailings_payload, ncl_line_cfg):
        rows, _ = parse(sailings_payload, ncl_line_cfg)
        for row in rows:
            assert row.nights == 3
            assert row.price_pppn == pytest.approx(row.price_per_person / 3, abs=1e-3)

    def test_price_basis_is_recorded(self, sailings_payload, ncl_line_cfg):
        rows, _ = parse(sailings_payload, ncl_line_cfg)
        assert {r.price_basis for r in rows} == {"combinedPrice"}

    def test_absent_tax_amount_is_null_not_zero(self, sailings_payload, ncl_line_cfg):
        """NCL publishes no tax amount at all. Null != 0, and must not be
        confused for a genuine zero-tax sailing."""
        rows, _ = parse(sailings_payload, ncl_line_cfg)
        assert all(r.taxes_fees is None for r in rows)

    def test_tax_amount_would_be_captured_separately_if_ncl_ever_sent_one(
            self, ncl_line_cfg):
        """Forward-looking guard, not a description of live data.

        NCL publishes no tax amount on any reachable endpoint (absent from all
        1,276 archived rows, USD and CAD alike). This synthesised row proves
        that if NCL ever does start sending one, it lands in taxes_fees and is
        never folded into price_total.
        """
        payload = {
            "itineraryCode": "TEST1",
            "itineraryDetails": {"code": "TEST1", "title": "Test",
                                 "destinations": [{"code": "CARIBBEAN"}],
                                 "ship": {"code": "X", "title": "Test Ship"}},
            "pricingStateRooms": [{
                "sailId": "1", "packageId": "p1", "stateroomType": "BALCONY",
                "sailStartDate": "2027-03-01T00:00", "sailEndDate": "2027-03-08T00:00",
                "currencyCode": "USD", "status": "AVAILABLE",
                "combinedPrice": 700, "basePrice": 900,
                "taxesAndFees": {"text": "Includes taxes, fees and port expenses",
                                 "amount": 146},
            }],
        }
        rows, _ = parse(payload, ncl_line_cfg)
        row = rows[0]
        assert row.taxes_fees == 146.0
        assert row.price_total == 1400.0          # fare only, taxes excluded
        assert row.price_pppn == pytest.approx(100.0)
        assert row.market == "US"
        assert row.currency == "USD"


class TestAvailability:
    def test_status_values_map_to_the_panel_vocabulary(self, ncl_line_cfg):
        base = {
            "sailId": "1", "packageId": "p", "stateroomType": "INSIDE",
            "sailStartDate": "2027-03-01T00:00", "sailEndDate": "2027-03-04T00:00",
            "currencyCode": "USD", "combinedPrice": 100,
        }
        cases = {
            "AVAILABLE": norm.AVAIL_AVAILABLE,
            "SOLD_OUT": norm.AVAIL_SOLD_OUT,
            "SOLO_GUEST_ONLY": norm.AVAIL_SOLO_ONLY,
            "SOMETHING_NEW": norm.AVAIL_UNKNOWN,
        }
        for raw_status, expected in cases.items():
            payload = {
                "itineraryCode": "T", "itineraryDetails": {
                    "destinations": [{"code": "CARIBBEAN"}]},
                "pricingStateRooms": [dict(base, status=raw_status)],
            }
            rows, _ = parse(payload, ncl_line_cfg)
            assert rows[0].availability_status == expected
            # verbatim value is never lost
            assert rows[0].availability_status_raw == raw_status

    def test_solo_guest_only_is_not_treated_as_scarcity(self, ncl_line_cfg):
        """SOLO_GUEST_ONLY marks single-occupancy Studio cabins.

        It is a property of the product, not a signal that inventory is running
        down, and it must stay out of any depletion measure. Mapping it to
        `limited` put 43% of NCL's Caribbean inside cells into a "closing"
        bucket that contained no closing at all.
        """
        base = {
            "sailId": "1", "packageId": "p", "stateroomType": "STUDIO",
            "sailStartDate": "2027-03-01T00:00", "sailEndDate": "2027-03-04T00:00",
            "currencyCode": "USD", "combinedPrice": 100,
            "status": "SOLO_GUEST_ONLY",
        }
        payload = {"itineraryCode": "T",
                   "itineraryDetails": {"destinations": [{"code": "CARIBBEAN"}]},
                   "pricingStateRooms": [base]}
        rows, _ = parse(payload, ncl_line_cfg)
        assert rows[0].availability_status == norm.AVAIL_SOLO_ONLY
        assert rows[0].availability_status not in norm.AVAIL_CLOSED
        assert rows[0].availability_status_raw == "SOLO_GUEST_ONLY"

    def test_units_remaining_is_null_because_ncl_exposes_no_count(
            self, sailings_payload, ncl_line_cfg):
        rows, _ = parse(sailings_payload, ncl_line_cfg)
        assert all(r.units_remaining is None for r in rows)

    def test_is_guarantee_is_null_not_false(self, sailings_payload, ncl_line_cfg):
        """The grid does not distinguish guarantee from assigned cabins;
        unknown must not be recorded as False."""
        rows, _ = parse(sailings_payload, ncl_line_cfg)
        assert all(r.is_guarantee is None for r in rows)


class TestPromo:
    def test_promo_text_and_hash_are_populated(self, sailings_payload, ncl_line_cfg):
        rows, _ = parse(sailings_payload, ncl_line_cfg)
        assert all(r.promo_text for r in rows)
        assert all(r.promo_hash for r in rows)

    def test_promo_text_keeps_offer_codes_verbatim(
            self, sailings_payload, ncl_line_cfg):
        rows, _ = parse(sailings_payload, ncl_line_cfg)
        assert "free-wifi-offer" in rows[0].promo_text
        assert "50-off-all-cruises-offer" in rows[0].promo_text


class TestFiltering:
    def test_sail_window_excludes_out_of_range_dates(
            self, sailings_payload, ncl_line_cfg):
        rows, _ = parse(sailings_payload, ncl_line_cfg,
                        sail_window=("2027-01-01", "2027-08-31"))
        assert rows, "expected some 2027 sailings in the fixture"
        assert all("2027-01-01" <= r.sail_date <= "2027-08-31" for r in rows)
        assert len(rows) < 90  # the Nov/Dec 2026 dates are excluded

    def test_region_filter_drops_the_whole_itinerary(
            self, sailings_payload, ncl_line_cfg):
        rows, _ = parse(sailings_payload, ncl_line_cfg,
                        allowed_regions=["Alaska"])
        assert rows == []

    def test_region_filter_keeps_matching_itinerary(
            self, sailings_payload, ncl_line_cfg):
        rows, _ = parse(sailings_payload, ncl_line_cfg,
                        allowed_regions=["Caribbean"])
        assert len(rows) == 90


class TestUnmappedLabels:
    def test_unknown_cabin_label_is_reported_and_stored_with_null_category(
            self, ncl_line_cfg):
        payload = {
            "itineraryCode": "T",
            "itineraryDetails": {"destinations": [{"code": "CARIBBEAN"}]},
            "pricingStateRooms": [{
                "sailId": "9", "packageId": "p", "stateroomType": "SPA_VILLA",
                "sailStartDate": "2027-03-01T00:00", "sailEndDate": "2027-03-04T00:00",
                "currencyCode": "USD", "status": "AVAILABLE", "combinedPrice": 500,
            }],
        }
        rows, unmapped = parse(payload, ncl_line_cfg)
        assert unmapped == {"SPA_VILLA"}
        assert rows[0].cabin_category is None          # not guessed
        assert rows[0].cabin_subcategory == "SPA_VILLA"  # raw label kept


class TestSearchParsing:
    def test_extracts_itinerary_codes(self, search_payload):
        codes = ncl.parse_search_itineraries(search_payload)
        assert codes
        assert all(isinstance(c, str) and c for c in codes)

    def test_codes_are_deduplicated(self):
        payload = {"itineraries": [{"code": "A"}, {"code": "A"}, {"code": "B"}]}
        assert ncl.parse_search_itineraries(payload) == ["A", "B"]

    def test_empty_payload_is_safe(self):
        assert ncl.parse_search_itineraries({}) == []


class TestMonthWindow:
    def test_enumerates_months_inclusively(self):
        assert ncl.months_in_window("2027-01-01", "2027-03-31") == [
            "Jan-2027", "Feb-2027", "Mar-2027"]

    def test_crosses_year_boundary(self):
        assert ncl.months_in_window("2026-11-01", "2027-01-31") == [
            "Nov-2026", "Dec-2026", "Jan-2027"]

    def test_full_weekly_window_is_eight_months(self):
        assert len(ncl.months_in_window("2027-01-01", "2027-08-31")) == 8

    def test_missing_bounds_give_empty(self):
        assert ncl.months_in_window(None, "2027-01-01") == []


class TestMarket:
    def test_currency_determines_recorded_market(self):
        assert ncl.market_for("USD") == "US"
        assert ncl.market_for("CAD") == "CA"

    def test_unknown_currency_is_preserved_not_assumed(self):
        assert ncl.market_for("JPY") == "JPY"
        assert ncl.market_for(None) == "UNKNOWN"

    def test_fixture_is_recorded_as_the_canadian_market(
            self, sailings_payload, ncl_line_cfg):
        """Guards against a CAD-served run being mistaken for a USD panel."""
        rows, _ = parse(sailings_payload, ncl_line_cfg)
        assert {r.market for r in rows} == {"CA"}
        assert {r.currency for r in rows} == {"CAD"}
