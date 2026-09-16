"""Land+cruise packages must be marked, never silently averaged in.

NCL sells "cruisetours" (Denali, London/Reykjavik land tours) at a package
price while stamping the row with the cruise segment only: a Denali itinerary
carries duration {"itinerary": 14, "cruising": 7}. Dividing the package fare by
cruise nights reads ~2x the true nightly cruise rate, and comparing it against a
peer's cruise-only fare is not a like-for-like comparison. These tests pin that
the flag is captured and that an unknown product is never treated as a cruise.
"""
import copy

import pytest

from panel.sources import ncl

SCRAPE_TS = "2026-09-14T21:00:00+00:00"
SCRAPE_DATE = "2026-09-14"
URL = "https://www.ncl.com/api/vacations/sailings/TEST"


def parse(payload, line_cfg, **kw):
    kw.setdefault("tier", "weekly-full")
    kw.setdefault("scrape_ts", SCRAPE_TS)
    kw.setdefault("scrape_date", SCRAPE_DATE)
    kw.setdefault("source_url", URL)
    # The archived fixture is a CAD capture from the original recon,
    # taken before the US-egress workflow existed. Parsing mechanics are
    # currency-independent, so these tests state the fixture's currency
    # explicitly rather than weakening the USD guard. The guard itself is
    # tested in TestCurrencyGuard.
    kw.setdefault("expected_currency", "CAD")
    return ncl.parse_sailings(payload, line_cfg=line_cfg, **kw)


@pytest.fixture
def cruise_only(sailings_payload):
    return copy.deepcopy(sailings_payload)


@pytest.fixture
def package(sailings_payload):
    """The same payload rebadged as a 14-day package with 7 cruise nights."""
    p = copy.deepcopy(sailings_payload)
    p["itineraryDetails"]["duration"] = {"itinerary": 14, "cruising": 7}
    for row in p["pricingStateRooms"]:
        row["isPackage"] = True
    return p


class TestPackageFlag:
    def test_cruise_only_rows_are_marked_zero_not_null(self, cruise_only, ncl_line_cfg):
        for row in cruise_only["pricingStateRooms"]:
            row["isPackage"] = False
        rows, _ = parse(cruise_only, ncl_line_cfg)
        assert rows
        assert {r.is_package for r in rows} == {0}

    def test_package_rows_are_flagged(self, package, ncl_line_cfg):
        rows, _ = parse(package, ncl_line_cfg)
        assert rows
        assert {r.is_package for r in rows} == {1}

    def test_package_length_is_captured_separately_from_cruise_nights(
            self, package, ncl_line_cfg):
        rows, _ = parse(package, ncl_line_cfg)
        r = rows[0]
        assert r.itinerary_nights == 14
        # `nights` stays the cruise segment: it is what the sail dates support.
        assert r.nights and r.nights < r.itinerary_nights

    def test_price_is_not_rewritten_to_invent_a_cruise_only_fare(
            self, cruise_only, package, ncl_line_cfg):
        """The package fare is reported as published; only the label changes.

        Deriving a cruise-only price by prorating would fabricate a number NCL
        never quoted, which is exactly the kind of silent adjustment the panel
        exists to avoid.
        """
        plain, _ = parse(cruise_only, ncl_line_cfg)
        pkg, _ = parse(package, ncl_line_cfg)
        assert [r.price_pppn for r in pkg] == [r.price_pppn for r in plain]
        assert [r.price_total for r in pkg] == [r.price_total for r in plain]


class TestFallbackWhenFlagAbsent:
    def test_duration_mismatch_implies_package_when_flag_missing(
            self, package, ncl_line_cfg):
        for row in package["pricingStateRooms"]:
            row.pop("isPackage", None)
        rows, _ = parse(package, ncl_line_cfg)
        assert {r.is_package for r in rows} == {1}

    def test_matching_durations_imply_cruise_only(self, cruise_only, ncl_line_cfg):
        cruise_only["itineraryDetails"]["duration"] = {"itinerary": 3, "cruising": 3}
        for row in cruise_only["pricingStateRooms"]:
            row.pop("isPackage", None)
        rows, _ = parse(cruise_only, ncl_line_cfg)
        assert {r.is_package for r in rows} == {0}

    def test_unknown_stays_null_rather_than_defaulting_to_cruise(
            self, cruise_only, ncl_line_cfg):
        """No flag and no durations means unknown, and unknown is not 'no'."""
        cruise_only["itineraryDetails"].pop("duration", None)
        for row in cruise_only["pricingStateRooms"]:
            row.pop("isPackage", None)
        rows, _ = parse(cruise_only, ncl_line_cfg)
        assert rows
        assert {r.is_package for r in rows} == {None}
        assert {r.itinerary_nights for r in rows} == {None}

    def test_explicit_flag_beats_the_duration_heuristic(self, package, ncl_line_cfg):
        """A vendor-published False wins over mismatched durations."""
        for row in package["pricingStateRooms"]:
            row["isPackage"] = False
        rows, _ = parse(package, ncl_line_cfg)
        assert {r.is_package for r in rows} == {0}
