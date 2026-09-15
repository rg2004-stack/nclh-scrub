"""Normalization layer tests.

The two invariants the thesis depends on:
  * taxes and fees never get folded into a price field
  * cabin labels are mapped explicitly, never guessed
"""
import pytest

from panel import normalize as norm


class TestCabinMapping:
    def test_maps_every_known_ncl_label(self, ncl_line_cfg):
        expected = {
            "STUDIO": "inside",
            "INSIDE": "inside",
            "OCEANVIEW": "oceanview",
            "BALCONY": "balcony",
            "MINISUITE": "balcony",
            "SUITE": "suite",
            "HAVEN": "suite",
        }
        for label, want in expected.items():
            assert norm.map_cabin_category(label, ncl_line_cfg.cabin_map) == want

    def test_mapping_is_case_and_whitespace_insensitive(self, ncl_line_cfg):
        for variant in ("balcony", "Balcony", "  BALCONY  ", "bAlCoNy"):
            assert norm.map_cabin_category(variant, ncl_line_cfg.cabin_map) == "balcony"

    def test_unknown_label_returns_none_rather_than_guessing(self, ncl_line_cfg):
        # A plausible-looking new label must NOT be fuzzy matched onto balcony.
        for unknown in ("BALCONY_PLUS", "CLUB_BALCONY", "SPA_SUITE", "NEW_THING"):
            assert norm.map_cabin_category(unknown, ncl_line_cfg.cabin_map) is None

    def test_empty_and_none_are_safe(self, ncl_line_cfg):
        assert norm.map_cabin_category(None, ncl_line_cfg.cabin_map) is None
        assert norm.map_cabin_category("", ncl_line_cfg.cabin_map) is None
        assert norm.map_cabin_category("   ", ncl_line_cfg.cabin_map) is None

    def test_mapping_to_a_nonstandard_category_is_rejected(self):
        with pytest.raises(ValueError, match="not one of"):
            norm.map_cabin_category("WEIRD", {"WEIRD": "penthouse"})

    def test_every_configured_target_is_a_standard_category(self, ncl_line_cfg):
        for target in ncl_line_cfg.cabin_map.values():
            assert target in norm.STANDARD_CATEGORIES

    def test_minisuite_is_balcony_not_suite(self, ncl_line_cfg):
        """Documented judgement call -- guard it so a silent flip is caught."""
        assert norm.map_cabin_category("MINISUITE", ncl_line_cfg.cabin_map) == "balcony"


class TestNights:
    def test_counts_whole_nights(self):
        assert norm.nights_between("2026-11-20T00:00", "2026-11-23T00:00") == 3
        assert norm.nights_between("2027-03-19", "2027-03-26") == 7

    def test_crosses_month_and_year_boundaries(self):
        assert norm.nights_between("2026-12-28", "2027-01-04") == 7

    def test_missing_or_malformed_dates_give_none(self):
        assert norm.nights_between(None, "2027-01-04") is None
        assert norm.nights_between("2027-01-04", None) is None
        assert norm.nights_between("not-a-date", "2027-01-04") is None

    def test_zero_or_negative_span_gives_none(self):
        assert norm.nights_between("2027-01-04", "2027-01-04") is None
        assert norm.nights_between("2027-01-10", "2027-01-04") is None


class TestPrices:
    def test_pppn_divides_by_nights(self):
        assert norm.price_pppn(497.0, 3) == pytest.approx(165.6667, abs=1e-3)

    def test_price_total_is_double_occupancy(self):
        assert norm.price_total_double(497.0) == 994.0

    def test_pppn_matches_spec_formula(self):
        """price_pppn == (price_total for 2 pax) / 2 / nights."""
        per_person, nights = 1204.0, 7
        total = norm.price_total_double(per_person)
        assert norm.price_pppn(per_person, nights) == pytest.approx(
            total / 2 / nights, abs=1e-6)

    def test_none_and_zero_nights_are_safe(self):
        assert norm.price_pppn(None, 7) is None
        assert norm.price_pppn(497.0, None) is None
        assert norm.price_pppn(497.0, 0) is None
        assert norm.price_total_double(None) is None


class TestPromo:
    def test_empty_offers_give_none(self):
        assert norm.promo_payload([]) == (None, None)

    def test_text_is_verbatim_json_of_the_offers(self):
        offers = [{"code": "a", "title": "A", "description": "keep me verbatim"}]
        text, _ = norm.promo_payload(offers)
        assert "keep me verbatim" in text

    def test_hash_is_stable_across_reordering(self):
        a = [{"code": "x", "title": "X"}, {"code": "y", "title": "Y"}]
        b = [{"code": "y", "title": "Y"}, {"code": "x", "title": "X"}]
        assert norm.promo_payload(a)[1] == norm.promo_payload(b)[1]

    def test_hash_changes_when_an_offer_is_added(self):
        a = [{"code": "x", "title": "X"}]
        b = [{"code": "x", "title": "X"}, {"code": "z", "title": "Z"}]
        assert norm.promo_payload(a)[1] != norm.promo_payload(b)[1]

    def test_hash_changes_when_an_offer_is_retitled(self):
        a = [{"code": "x", "title": "Free Wi-Fi"}]
        b = [{"code": "x", "title": "Free Wi-Fi and Drinks"}]
        assert norm.promo_payload(a)[1] != norm.promo_payload(b)[1]


class TestRegion:
    def test_first_mapped_code_wins(self, ncl_line_cfg):
        assert norm.region_for(["BAHAMAS", "WEEKEND"],
                               ncl_line_cfg.region_map) == "Caribbean"

    def test_marketing_tags_are_skipped_not_treated_as_regions(self, ncl_line_cfg):
        # WEEKEND is a marketing tag; the real geography follows it.
        assert norm.region_for(["WEEKEND", "MEDITERRANEAN"],
                               ncl_line_cfg.region_map) == "Southern Europe"

    def test_fully_unmapped_gives_none(self, ncl_line_cfg):
        assert norm.region_for(["ASIA", "HAWAII"], ncl_line_cfg.region_map) is None
        assert norm.region_for([], ncl_line_cfg.region_map) is None


class TestWindow:
    def test_inclusive_at_both_ends(self):
        assert norm.in_window("2027-01-01", "2027-01-01", "2027-08-31")
        assert norm.in_window("2027-08-31", "2027-01-01", "2027-08-31")

    def test_excludes_outside(self):
        assert not norm.in_window("2026-12-31", "2027-01-01", "2027-08-31")
        assert not norm.in_window("2027-09-01", "2027-01-01", "2027-08-31")

    def test_handles_full_timestamps(self):
        assert norm.in_window("2027-03-19T00:00", "2027-01-01", "2027-08-31")

    def test_missing_date_is_not_in_window(self):
        assert not norm.in_window(None, "2027-01-01", "2027-08-31")

    def test_open_ended_bounds(self):
        assert norm.in_window("2030-01-01", None, None)


class TestMonthKey:
    def test_extracts_year_month(self):
        assert norm.month_key("2027-03-19T00:00") == "2027-03"

    def test_rejects_junk(self):
        assert norm.month_key(None) is None
        assert norm.month_key("nope") is None
