"""Analysis tests, concentrated on the two ways a number here could mislead.

1. Blending evidentiary bases. weekly-full is the Jan-Aug 2027 forward book
   across five regions; daily-marker is the Oct-Dec 2026 near-term cohort,
   which seasonal repositioning makes Caribbean-only for cross-line purposes.
   Pooling them produces a figure that describes no real population.
2. Comparing at a resolution only one line publishes, or across products only
   one line sells.
"""
import itertools
import sqlite3

import pytest

from panel import analysis as an
from panel.schema import DDL
from panel.sources.capabilities import GranularityError

NCL = "Norwegian Cruise Line"
CCL = "Carnival Cruise Line"


_ids = itertools.count(1)


def make_row(**kw):
    """One observation. Unique sailing_id per call unless the test pins one,
    so rows do not collide on the natural key."""
    row = {
        "scrape_ts_utc": "2026-09-15T18:00:00+00:00",
        "scrape_date": "2026-09-15",
        "line": NCL, "brand": "NCL", "ship": "Norwegian Joy",
        "sailing_id": str(next(_ids)), "itinerary_code": "X1",
        "sail_date": "2027-02-01", "return_date": "2027-02-08", "nights": 7,
        "is_package": 0, "itinerary_nights": 7,
        "region": "Caribbean", "market": "US", "currency": "USD",
        "cabin_category": "balcony", "cabin_subcategory": "BALCONY",
        "price_total": 2800.0, "price_pppn": 200.0, "price_per_person": 1400.0,
        "availability_status": "available", "tier": "weekly-full",
    }
    row.update(kw)
    return row


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(DDL)
    return c


def insert(conn, rows):
    for r in rows:
        cols = ", ".join(r)
        conn.execute(f"INSERT INTO observations ({cols}) "
                     f"VALUES ({', '.join('?' * len(r))})", list(r.values()))
    conn.commit()


class TestBasisNeverBlendsTiers:
    def test_combining_tiers_raises_by_default(self):
        a = an.Basis(tier="weekly-full", regions=("Southern Europe",))
        b = an.Basis(tier="daily-marker", regions=("Caribbean",))
        with pytest.raises(an.BasisError, match="different tiers"):
            an.combine_bases([a, b])

    def test_forcing_the_mix_stamps_a_loud_caveat(self):
        a = an.Basis(tier="weekly-full", regions=("Southern Europe",))
        b = an.Basis(tier="daily-marker", regions=("Caribbean",))
        merged = an.combine_bases([a, b], allow_mixed_basis=True)
        assert any("MIXED EVIDENTIARY BASIS" in c for c in merged.caveats)
        assert merged.tier == "daily-marker+weekly-full"

    def test_same_tier_combines_without_complaint(self):
        a = an.Basis(tier="weekly-full", regions=("Caribbean",), n_observations=2)
        b = an.Basis(tier="weekly-full", regions=("Alaska",), n_observations=3)
        merged = an.combine_bases([a, b])
        assert merged.tier == "weekly-full"
        assert merged.regions == ("Alaska", "Caribbean")
        assert merged.n_observations == 5
        assert not any("MIXED" in c for c in merged.caveats)

    def test_a_query_never_returns_rows_from_another_tier(self, conn):
        insert(conn, [
            make_row(tier="weekly-full", region="Southern Europe", price_pppn=400.0),
            make_row(tier="daily-marker", region="Caribbean", price_pppn=100.0,
                     sail_date="2026-11-01"),
        ])
        res = an.availability_snapshot(conn, tier="daily-marker")
        assert res.basis.tier == "daily-marker"
        assert [r["region"] for r in res.rows] == ["Caribbean"]
        assert res.basis.n_observations == 1

    def test_unknown_tier_is_rejected(self, conn):
        with pytest.raises(ValueError, match="unknown tier"):
            an.availability_snapshot(conn, tier="whenever")


class TestCoverageIsLabelled:
    def test_daily_marker_carries_the_seasonality_note(self, conn):
        insert(conn, [make_row(tier="daily-marker", sail_date="2026-11-01")])
        res = an.availability_snapshot(conn, tier="daily-marker")
        joined = " ".join(res.basis.caveats)
        assert "Southern Europe" in joined
        assert "seasonal deployment" in joined

    def test_missing_southern_europe_is_called_out(self, conn):
        insert(conn, [make_row(tier="daily-marker", region="Caribbean",
                               sail_date="2026-11-01")])
        res = an.availability_snapshot(conn, tier="daily-marker")
        assert any("no Southern Europe rows" in c for c in res.basis.caveats)

    def test_single_region_basis_is_flagged(self, conn):
        insert(conn, [make_row(tier="daily-marker", sail_date="2026-11-01")])
        res = an.availability_snapshot(conn, tier="daily-marker")
        assert any("SINGLE-REGION BASIS" in c for c in res.basis.caveats)

    def test_regions_with_only_one_line_are_named_as_not_peer_comparable(self, conn):
        insert(conn, [
            make_row(line=NCL, region="Caribbean"),
            make_row(line=CCL, region="Caribbean"),
            make_row(line=NCL, region="Southern Europe"),
        ])
        res = an.peer_gap(conn, tier="weekly-full", min_cells=1)
        joined = " ".join(res.basis.caveats)
        assert "PEER-COMPARABLE REGIONS" in joined
        assert "Southern Europe" in joined

    def test_label_names_tier_window_and_counts(self, conn):
        insert(conn, [make_row()])
        label = an.availability_snapshot(conn, tier="weekly-full").basis.label()
        assert "[weekly-full]" in label
        assert "Caribbean" in label
        assert "1 obs" in label


class TestPeerGapGuards:
    def test_refuses_a_granularity_only_one_line_publishes(self, conn):
        insert(conn, [make_row(), make_row(line=CCL)])
        with pytest.raises(GranularityError, match="rate_code"):
            an.peer_gap(conn, tier="weekly-full", granularity="rate_code")

    def test_category_comparison_is_allowed(self, conn):
        insert(conn, [
            *[make_row(price_pppn=200.0) for _ in range(5)],
            *[make_row(line=CCL, price_pppn=100.0) for _ in range(5)],
        ])
        res = an.peer_gap(conn, tier="weekly-full", min_cells=5)
        row = next(r for r in res.rows if r["status"].startswith("ok"))
        assert row["gap_pppn"] == 100.0
        assert row["gap_pct"] == 100.0

    def test_thin_cells_are_reported_not_silently_dropped(self, conn):
        insert(conn, [make_row(), make_row(line=CCL)])
        res = an.peer_gap(conn, tier="weekly-full", min_cells=5)
        assert res.rows
        assert all("insufficient cells" in r["status"] for r in res.rows)
        assert all(r["gap_pppn"] is None for r in res.rows)


class TestProductFilter:
    def test_packages_are_excluded_from_cross_line_price_comparison(self, conn):
        insert(conn, [
            *[make_row(price_pppn=200.0) for _ in range(5)],
            # Land+cruise packages at 4x the nightly rate; must not enter.
            *[make_row(price_pppn=800.0, is_package=1, itinerary_nights=14)
              for _ in range(5)],
            *[make_row(line=CCL, price_pppn=100.0) for _ in range(5)],
        ])
        res = an.peer_gap(conn, tier="weekly-full", min_cells=5)
        row = next(r for r in res.rows if r["status"].startswith("ok"))
        assert row["treatment_median_pppn"] == 200.0
        assert row["treatment_n"] == 5

    def test_unclassified_rows_are_excluded_and_counted(self, conn):
        insert(conn, [
            *[make_row(price_pppn=200.0) for _ in range(5)],
            *[make_row(price_pppn=900.0, is_package=None) for _ in range(3)],
            *[make_row(line=CCL, price_pppn=100.0) for _ in range(5)],
        ])
        res = an.peer_gap(conn, tier="weekly-full", min_cells=5)
        row = next(r for r in res.rows if r["status"].startswith("ok"))
        assert row["treatment_median_pppn"] == 200.0
        assert any("3 are unclassified" in c for c in res.basis.caveats)

    def test_including_packages_is_possible_but_explicit(self, conn):
        insert(conn, [
            *[make_row(price_pppn=200.0) for _ in range(5)],
            *[make_row(price_pppn=800.0, is_package=1) for _ in range(5)],
            *[make_row(line=CCL, price_pppn=100.0) for _ in range(5)],
        ])
        res = an.peer_gap(conn, tier="weekly-full", min_cells=5, product="all")
        row = next(r for r in res.rows if r["status"].startswith("ok"))
        assert row["treatment_n"] == 10

    def test_unknown_product_filter_is_rejected(self, conn):
        insert(conn, [make_row()])
        with pytest.raises(ValueError, match="unknown product filter"):
            an.peer_gap(conn, tier="weekly-full", product="sometimes")


class TestDepletionRefusesToInventASlope:
    def test_one_collection_date_yields_no_slope(self, conn):
        insert(conn, [make_row()])
        res = an.depletion_rate(conn, tier="weekly-full")
        assert res.rows == []
        assert any("NOT COMPUTABLE" in c for c in res.basis.caveats)

    def test_two_dates_produce_a_slope(self, conn):
        insert(conn, [
            make_row(scrape_date="2026-09-15", availability_status="available"),
            make_row(scrape_date="2026-09-25", availability_status="sold_out"),
        ])
        res = an.depletion_rate(conn, tier="weekly-full")
        assert len(res.rows) == 1
        r = res.rows[0]
        assert r["days"] == 10
        assert r["delta_closed_share"] == 1.0
        assert r["closed_share_per_day"] == 0.1


class TestEarningsWindow:
    def test_absent_coverage_is_stated_not_implied_by_empty_rows(self, conn):
        insert(conn, [make_row(sail_date="2027-02-01")])
        res = an.earnings_window_compare(conn, tier="weekly-full")
        assert any("NO COVERAGE" in c for c in res.basis.caveats)

    def test_sailings_inside_the_window_are_split_out(self, conn):
        insert(conn, [
            make_row(tier="daily-marker", sail_date="2026-11-01", price_pppn=300.0),
            make_row(tier="daily-marker", sail_date="2026-12-20", price_pppn=100.0),
        ])
        res = an.earnings_window_compare(conn, tier="daily-marker")
        row = res.rows[0]
        assert row["in_window_cells"] == 1
        assert row["outside_cells"] == 1
        assert row["in_window_mean_pppn"] == 300.0
        assert row["outside_mean_pppn"] == 100.0


class TestAvailabilitySnapshot:
    def test_closed_share_counts_limited_and_sold_out(self, conn):
        insert(conn, [
            make_row(availability_status="available"),
            make_row(availability_status="limited"),
            make_row(availability_status="sold_out"),
            make_row(availability_status="available"),
        ])
        res = an.availability_snapshot(conn, tier="weekly-full")
        row = res.rows[0]
        assert row["cells"] == 4
        assert row["closed_share"] == 0.5
        assert row["sold_out_share"] == 0.25

    def test_rejects_an_ungroupable_column(self, conn):
        insert(conn, [make_row()])
        with pytest.raises(ValueError, match="cannot group availability"):
            an.availability_snapshot(conn, tier="weekly-full",
                                     group_by=("price_total",))


class TestSoloOnlyIsNotDepletion:
    """NCL's SOLO_GUEST_ONLY Studio cabins are single-occupancy by design.

    They were never open to the panel's 2-pax basis, so counting them as
    "closed" measures how many Studio cabins a ship has, not scarcity.
    """

    def test_solo_only_is_excluded_from_both_sides_of_closed_share(self, conn):
        insert(conn, [
            *[make_row(availability_status="available") for _ in range(4)],
            *[make_row(availability_status="solo_only", price_pppn=None)
              for _ in range(6)],
        ])
        res = an.availability_snapshot(conn, tier="weekly-full")
        row = res.rows[0]
        assert row["cells"] == 10
        assert row["solo_only"] == 6
        assert row["bookable_cells"] == 4
        # Not 6/10: none of those cells were ever 2-pax bookable.
        assert row["closed_share"] == 0.0

    def test_real_scarcity_still_registers_against_bookable_cells(self, conn):
        insert(conn, [
            *[make_row(availability_status="available") for _ in range(2)],
            *[make_row(availability_status="sold_out", price_pppn=None)
              for _ in range(2)],
            *[make_row(availability_status="solo_only", price_pppn=None)
              for _ in range(6)],
        ])
        row = an.availability_snapshot(conn, tier="weekly-full").rows[0]
        assert row["bookable_cells"] == 4
        assert row["closed_share"] == 0.5
        assert row["sold_out_share"] == 0.5

    def test_priced_share_uses_the_bookable_denominator(self, conn):
        insert(conn, [
            *[make_row(price_pppn=200.0) for _ in range(4)],
            *[make_row(availability_status="solo_only", price_pppn=None)
              for _ in range(4)],
        ])
        row = an.availability_snapshot(conn, tier="weekly-full").rows[0]
        assert row["priced_share"] == 1.0

    def test_depletion_slope_ignores_solo_only_cells(self, conn):
        insert(conn, [
            make_row(scrape_date="2026-09-15", availability_status="available"),
            make_row(scrape_date="2026-09-15", availability_status="solo_only"),
            make_row(scrape_date="2026-09-25", availability_status="sold_out"),
            make_row(scrape_date="2026-09-25", availability_status="solo_only"),
        ])
        row = an.depletion_rate(conn, tier="weekly-full").rows[0]
        assert row["first_closed_share"] == 0.0
        assert row["last_closed_share"] == 1.0

    def test_snapshot_can_be_restricted_to_cruise_only_rows(self, conn):
        insert(conn, [
            *[make_row() for _ in range(3)],
            *[make_row(is_package=1) for _ in range(5)],
        ])
        allp = an.availability_snapshot(conn, tier="weekly-full")
        cruise = an.availability_snapshot(conn, tier="weekly-full",
                                          product="cruise_only")
        assert allp.rows[0]["cells"] == 8
        assert cruise.rows[0]["cells"] == 3


class TestSampleCaveatsTravelWithTheRow:
    """A caveat in the footer gets separated from the number when someone
    copies a row into a deck. These live in the row."""

    def test_thin_and_very_thin_are_graded(self):
        assert an.sample_flag(100, 100, 5) == "ok"
        assert an.sample_flag(100, 25, 5).startswith("THIN")
        assert an.sample_flag(8, 100, 5).startswith("VERY THIN")
        assert an.sample_flag(3, 100, 5).startswith("INSUFFICIENT")

    def test_flag_grades_the_smaller_side(self):
        """A 900-row treatment against 6 peer rows is a 6-row comparison."""
        assert an.sample_flag(900, 6, 5).startswith("VERY THIN")

    def test_peer_gap_rows_carry_a_sample_grade(self, conn):
        insert(conn, [
            *[make_row(price_pppn=200.0) for _ in range(6)],
            *[make_row(line=CCL, price_pppn=100.0) for _ in range(6)],
        ])
        res = an.peer_gap(conn, tier="weekly-full", min_cells=5)
        row = next(r for r in res.rows if r["status"].startswith("ok"))
        assert row["sample"].startswith("VERY THIN")

    def test_peer_gap_reports_what_the_product_filter_removed(self, conn):
        insert(conn, [
            *[make_row(price_pppn=200.0) for _ in range(6)],
            *[make_row(price_pppn=800.0, is_package=1) for _ in range(14)],
            *[make_row(line=CCL, price_pppn=100.0) for _ in range(6)],
        ])
        res = an.peer_gap(conn, tier="weekly-full", min_cells=5)
        row = next(r for r in res.rows if r["status"].startswith("ok"))
        assert row["pkg_excluded_t"] == 14
        assert row["pkg_excluded_pct_t"] == 70.0
        # Heavy exclusion is said out loud in the status, not just the number.
        assert "70.0% of treatment rows were packages" in row["status"]

    def test_light_exclusion_does_not_clutter_the_status(self, conn):
        insert(conn, [
            *[make_row(price_pppn=200.0) for _ in range(40)],
            *[make_row(price_pppn=800.0, is_package=1) for _ in range(2)],
            *[make_row(line=CCL, price_pppn=100.0) for _ in range(40)],
        ])
        res = an.peer_gap(conn, tier="weekly-full", min_cells=5)
        row = next(r for r in res.rows if r["status"].startswith("ok"))
        assert row["status"] == "ok"
        assert row["sample"] == "ok"

    def test_availability_rows_carry_a_sample_grade(self, conn):
        insert(conn, [make_row() for _ in range(7)])
        res = an.availability_snapshot(conn, tier="weekly-full")
        assert res.rows[0]["sample"].startswith("VERY THIN")

    def test_availability_reports_excluded_packages_per_row(self, conn):
        insert(conn, [
            *[make_row() for _ in range(10)],
            *[make_row(is_package=1) for _ in range(30)],
        ])
        res = an.availability_snapshot(conn, tier="weekly-full",
                                       product="cruise_only")
        row = res.rows[0]
        assert row["cells"] == 10
        assert row["pkg_excluded"] == 30
        assert row["pkg_excluded_pct"] == 75.0

    def test_no_exclusion_columns_when_no_product_filter(self, conn):
        insert(conn, [make_row()])
        res = an.availability_snapshot(conn, tier="weekly-full")
        assert "pkg_excluded" not in res.rows[0]

    def test_insufficient_rows_still_report_what_was_excluded(self, conn):
        """A cell too thin to compare is exactly where you want to know that
        most of its rows were a product you filtered out."""
        insert(conn, [
            *[make_row(price_pppn=200.0) for _ in range(2)],
            *[make_row(price_pppn=800.0, is_package=1) for _ in range(18)],
            *[make_row(line=CCL, price_pppn=100.0) for _ in range(2)],
        ])
        res = an.peer_gap(conn, tier="weekly-full", min_cells=5)
        row = res.rows[0]
        assert "insufficient" in row["status"]
        assert row["pkg_excluded_t"] == 18
        assert row["pkg_excluded_pct_t"] == 90.0
