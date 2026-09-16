"""Royal Caribbean: the floor-price control, parsed from an archived page.

Three things must hold, and the third is why the line ships disabled.

1. The fare stored is tax-EXCLUSIVE. Royal is the only source that publishes
   the tax component, and its advertised `netPrice` includes it, so the fare
   is a subtraction. Getting that backwards would put a tax-inclusive number
   next to two tax-exclusive ones.
2. Region comes from the record's own destinationCode, never from the page it
   was found on. The Bermuda landing page serves Bahamas sailings.
3. Nothing may enter the panel at cabin granularity. robots.txt disallows the
   only paths that carry cabins, so a row here is a sailing floor and says so.
"""
import gzip

import pytest

from panel.sources import capabilities as caps
from panel.sources import royal

FIXTURE = "tests/fixtures/royal_bermuda_landing_2026-09-16.html.gz"
CARIB = {"CARIB": "Caribbean", "BAHAM": "Caribbean", "BERMU": "Bermuda"}


@pytest.fixture
def page():
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture
def records(page):
    return royal.parse_tabs(page)


def parse(records, region_map=None, **kw):
    return royal.parse_records(
        records, region_map=region_map or CARIB,
        scrape_ts="2026-09-16T12:00:00+00:00", tier="weekly-full",
        source_url="https://www.royalcaribbean.com/bermuda-cruises", **kw)


class TestParsing:
    def test_records_come_out_of_the_embedded_json(self, records):
        assert len(records) == 10
        assert all("sailDate" in r for r in records)

    def test_a_page_with_no_embedded_block_yields_nothing(self):
        assert royal.parse_tabs("<html><body>nothing here</body></html>") == []

    def test_malformed_json_is_skipped_not_raised(self):
        assert royal.parse_tabs('<div :initial-tabs="{not json"></div>') == []

    def test_sailing_id_is_stable_across_scrapes(self, records):
        a = royal.sailing_id(records[0])
        assert a == royal.sailing_id(dict(records[0]))
        assert a.count("|") == 2

    def test_the_same_sailing_on_two_tabs_is_stored_once(self, records):
        obs, _ = parse(list(records) + list(records))
        assert len(obs) == len({o.sailing_id for o in obs})


class TestTaxBasis:
    def test_stored_fare_is_net_of_tax(self, records):
        obs, _ = parse(records)
        rec = next(r for r in records
                   if royal.sailing_id(r) == obs[0].sailing_id)
        gross = float(rec["netPrice"])
        taxes = float(rec["taxedAndFees"])
        assert rec["taxesFeesIncluded"] is True
        assert obs[0].price_total == pytest.approx(gross - taxes)
        assert obs[0].price_total < gross

    def test_taxes_are_stored_separately_never_folded_in(self, records):
        obs, _ = parse(records)
        assert all(o.taxes_fees is not None and o.taxes_fees > 0 for o in obs)

    def test_pppn_divides_the_net_fare_by_nights(self, records):
        obs, _ = parse(records)
        o = obs[0]
        assert o.price_pppn == pytest.approx(o.price_total / o.nights, abs=0.01)

    def test_a_tax_exclusive_advert_is_not_reduced_again(self):
        rec = [{"shipCode": "XX", "sailDate": "2027-01-01", "packageCode": "P",
                "destinationCode": "CARIB", "currency": "USD",
                "numberOfNights": 5, "netPrice": "500",
                "taxedAndFees": "100", "taxesFeesIncluded": False}]
        obs, _ = parse(rec)
        assert obs[0].price_total == 500.0
        assert obs[0].taxes_fees == 100.0

    def test_an_unknown_tax_basis_is_dropped_rather_than_guessed(self):
        rec = [{"shipCode": "XX", "sailDate": "2027-01-01", "packageCode": "P",
                "destinationCode": "CARIB", "currency": "USD",
                "numberOfNights": 5, "netPrice": "500", "taxedAndFees": None,
                "taxesFeesIncluded": None}]
        obs, skipped = parse(rec)
        assert obs == []
        assert "tax basis" in skipped[0]


class TestRegionComesFromTheRecord:
    def test_the_bermuda_page_does_not_produce_bermuda_rows(self, records):
        """The finding that disabled this line: the pages are carousels."""
        obs, _ = parse(records)
        assert {o.region for o in obs} == {"Caribbean"}

    def test_an_unmapped_destination_is_dropped_and_reported(self, records):
        obs, skipped = parse(records, region_map={"CARIB": "Caribbean"})
        assert {o.region for o in obs} == {"Caribbean"}
        assert len(obs) == 3 and len(skipped) == 7
        assert "not mapped" in skipped[0]

    def test_no_region_means_no_row_not_a_default(self):
        rec = [{"shipCode": "XX", "sailDate": "2027-01-01", "packageCode": "P",
                "destinationCode": "MEXCO", "currency": "USD",
                "numberOfNights": 5, "netPrice": "500", "taxedAndFees": "100",
                "taxesFeesIncluded": True}]
        obs, skipped = parse(rec)
        assert obs == [] and len(skipped) == 1


class TestCurrencyGuard:
    def test_non_usd_raises_rather_than_entering_the_panel(self, records):
        bad = [dict(r, currency="CAD") for r in records]
        with pytest.raises(royal.CurrencyMismatch, match="CAD"):
            parse(bad)

    def test_the_archived_page_really_is_usd(self, records):
        assert {r.get("currency") for r in records} == {"USD"}
        assert {r.get("countryCode") for r in records} == {"USA"}

    def test_an_unpriced_record_does_not_trip_the_guard(self):
        rec = [{"shipCode": "XX", "sailDate": "2027-01-01", "packageCode": "P",
                "destinationCode": "CARIB", "currency": "", "numberOfNights": 5,
                "netPrice": None, "taxedAndFees": None,
                "taxesFeesIncluded": False}]
        obs, _ = parse(rec)
        assert obs and obs[0].price_total is None


class TestFloorOnly:
    def test_every_row_is_labelled_a_sailing_floor(self, records):
        obs, _ = parse(records)
        assert {o.cabin_category for o in obs} == {royal.FLOOR_CATEGORY}
        assert {o.cabin_subcategory for o in obs} == {royal.FLOOR_SUBCATEGORY}
        assert {o.price_basis for o in obs} == {royal.PRICE_BASIS}

    def test_no_cabin_resolution_is_claimed(self, records):
        obs, _ = parse(records)
        assert all(o.vendor_category_code is None and o.rate_code is None
                   for o in obs)

    def test_capability_declares_the_floor_rung(self):
        assert caps.capability("royal").granularity == "floor"
        assert caps.GRANULARITIES[0] == "floor"

    def test_royal_is_a_control_and_never_a_default_peer(self):
        assert caps.capability("royal").is_control is True
        assert "royal" not in caps.peer_keys()
        assert {"ncl", "carnival"} <= set(caps.peer_keys())

    def test_comparing_a_cabin_grade_against_the_floor_is_refused(self):
        with pytest.raises(caps.GranularityError, match="floor"):
            caps.require_granularity(["ncl", "royal"], "category")

    def test_two_floors_can_be_compared_with_each_other(self):
        assert caps.shared_granularity(["royal"]) == "floor"
        assert caps.require_granularity(["ncl", "royal"], "floor") == "floor"


class TestConfiguredButDisabled:
    def test_the_line_ships_disabled_with_the_reason_in_config(self):
        from panel.config import load_config
        cfg = load_config("config/panel.yaml")
        royal_cfg = cfg.line("royal")
        assert royal_cfg.enabled is False
        assert royal_cfg.landing_pages, "entry points are still recorded"
        assert royal_cfg.cabin_map == {}, "no cabin map: there are no cabins"

    def test_a_disabled_line_contributes_no_report_regions(self):
        from panel.report import suite_regions
        # suite_regions skips disabled lines, so Royal cannot add a region
        # that nothing collects.
        assert suite_regions("config/panel.yaml")
