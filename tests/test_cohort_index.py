"""The since-inception matched-basket index.

The thing under test is a construction, not a query. An index over "whatever
was priced today" rises when cheap cabins sell out -- which is the exact
artefact this panel exists to avoid publishing, so the index must not be built
that way. It fixes a basket of (line, sailing_id, cabin_subcategory, market)
cells at the base date and reprices that same basket, and it reports the
contaminated construction alongside so the difference can be read off.

Two properties therefore carry the weight:

1. Cells ENTERING the book cannot move `index_matched`, at any price. That is
   what makes the index immune to the collector's own scope changing, and it is
   the difference between a measurement and an artefact.
2. The output must never imply more history than exists. With one collection
   date the index is 100 by construction and has to say so.
"""
import itertools
import sqlite3

import pytest

from panel import analysis as an
from panel.schema import DDL

NCL = "Norwegian Cruise Line"
CCL = "Carnival Cruise Line"

_ids = itertools.count(1)


def cell(sailing, sub="BALCONY", **kw):
    """One cabin grade on one departure, observed on one date."""
    row = {
        "scrape_ts_utc": "2026-09-15T18:00:00+00:00",
        "scrape_date": "2026-09-15",
        "line": NCL, "brand": "NCL", "ship": "Norwegian Joy",
        "sailing_id": sailing, "itinerary_code": "X1",
        "sail_date": "2027-02-01", "nights": 7,
        "is_package": 0, "itinerary_nights": 7,
        "region": "Caribbean", "market": "US", "currency": "USD",
        "cabin_category": "balcony", "cabin_subcategory": sub,
        "price_total": 1400.0, "price_pppn": 200.0,
        "availability_status": "available", "tier": "weekly-full",
    }
    row.update(kw)
    return row


def db(tmp_path, rows, name="panel.sqlite"):
    path = str(tmp_path / name)
    conn = sqlite3.connect(path)
    conn.executescript(DDL)
    for r in rows:
        conn.execute(f"INSERT INTO observations ({', '.join(r)}) "
                     f"VALUES ({', '.join('?' * len(r))})", list(r.values()))
    conn.commit()
    conn.close()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def two_dates(tmp_path, base_prices, later_prices, **kw):
    """Same cells on two dates, priced as given. Keys are sailing ids."""
    rows = [cell(str(s), scrape_date="2026-09-15", price_pppn=p, **kw)
            for s, p in base_prices.items()]
    rows += [cell(str(s), scrape_date="2026-09-22", price_pppn=p, **kw)
             for s, p in later_prices.items()]
    return db(tmp_path, rows)


def at(result, date="2026-09-22"):
    return [r for r in result.rows if r["scrape_date"] == date]


# -- the basket ------------------------------------------------------------

class TestMatchedBasket:
    def test_flat_prices_index_at_100(self, tmp_path):
        conn = two_dates(tmp_path, {1: 200.0, 2: 300.0}, {1: 200.0, 2: 300.0})
        row = at(an.cohort_index(conn, tier="weekly-full", min_matched=1))[0]
        assert row["index_matched"] == 100.0
        assert row["matched_cells"] == 2

    def test_a_ten_percent_rise_reads_as_110(self, tmp_path):
        conn = two_dates(tmp_path, {1: 200.0, 2: 300.0}, {1: 220.0, 2: 330.0})
        row = at(an.cohort_index(conn, tier="weekly-full", min_matched=1))[0]
        assert row["index_matched"] == 110.0
        assert row["median_cell_change_pct"] == 10.0

    def test_index_is_a_basket_ratio_not_a_mean_of_ratios(self, tmp_path):
        """A dear cabin must carry more weight than a cheap one, which is what
        makes this an index rather than an average of percentages."""
        conn = two_dates(tmp_path, {1: 100.0, 2: 900.0}, {1: 200.0, 2: 900.0})
        row = at(an.cohort_index(conn, tier="weekly-full", min_matched=1))[0]
        assert row["index_matched"] == 110.0        # 1100/1000, not +50%
        assert row["median_cell_change_pct"] == 50.0

    def test_base_row_is_emitted_and_marked(self, tmp_path):
        conn = two_dates(tmp_path, {1: 200.0}, {1: 250.0})
        rows = an.cohort_index(conn, tier="weekly-full", min_matched=1).rows
        base = [r for r in rows if r["days_since_base"] == 0][0]
        assert base["index_matched"] == 100.0
        assert base["sample"].startswith("BASE")

    def test_days_since_base_is_real_elapsed_time(self, tmp_path):
        conn = two_dates(tmp_path, {1: 200.0}, {1: 200.0})
        assert at(an.cohort_index(conn, tier="weekly-full",
                                  min_matched=1))[0]["days_since_base"] == 7


class TestEntriesCannotMoveTheIndex:
    """The property that makes the index immune to the collector's scope."""

    def test_new_cells_at_any_price_leave_index_matched_alone(self, tmp_path):
        rows = [cell("1", scrape_date="2026-09-15", price_pppn=200.0)]
        rows += [cell("1", scrape_date="2026-09-22", price_pppn=200.0)]
        # A whole widened sail window arriving at ten times the price.
        rows += [cell(f"new{i}", scrape_date="2026-09-22", price_pppn=2000.0)
                 for i in range(50)]
        res = an.cohort_index(db(tmp_path, rows), tier="weekly-full",
                              min_matched=1)
        row = at(res)[0]
        assert row["index_matched"] == 100.0
        assert row["entered_cells"] == 50

    def test_but_the_naive_index_does_move_and_the_gap_is_reported(self, tmp_path):
        rows = [cell("1", scrape_date="2026-09-15", price_pppn=200.0)]
        rows += [cell("1", scrape_date="2026-09-22", price_pppn=200.0)]
        rows += [cell("new", scrape_date="2026-09-22", price_pppn=600.0)]
        row = at(an.cohort_index(db(tmp_path, rows), tier="weekly-full",
                                 min_matched=1))[0]
        assert row["index_naive"] == 200.0          # mean 200 -> 400
        assert row["index_matched"] == 100.0
        assert row["mix_effect_pp"] == 100.0        # every point of it is mix

    def test_mix_effect_is_zero_when_the_panel_is_stable(self, tmp_path):
        conn = two_dates(tmp_path, {1: 200.0, 2: 300.0}, {1: 210.0, 2: 315.0})
        row = at(an.cohort_index(conn, tier="weekly-full", min_matched=1))[0]
        assert row["mix_effect_pp"] == 0.0


class TestAttrition:
    def test_cells_leaving_the_basket_are_reported_not_hidden(self, tmp_path):
        rows = [cell(str(i), scrape_date="2026-09-15", price_pppn=200.0)
                for i in range(4)]
        rows += [cell("0", scrape_date="2026-09-22", price_pppn=200.0)]
        row = at(an.cohort_index(db(tmp_path, rows), tier="weekly-full",
                                 min_matched=1))[0]
        assert row["base_cells"] == 4
        assert row["matched_cells"] == 1
        assert row["attrition_pct"] == 75.0

    def test_a_sold_out_cell_leaves_the_basket_rather_than_pricing_at_zero(
            self, tmp_path):
        """An unpriced cell must not enter the ratio as a cheap one."""
        rows = [cell("1", scrape_date="2026-09-15", price_pppn=200.0),
                cell("2", scrape_date="2026-09-15", price_pppn=100.0)]
        rows += [cell("1", scrape_date="2026-09-22", price_pppn=200.0),
                 cell("2", scrape_date="2026-09-22", price_pppn=None,
                      price_total=None, availability_status="sold_out")]
        row = at(an.cohort_index(db(tmp_path, rows), tier="weekly-full",
                                 min_matched=1))[0]
        assert row["matched_cells"] == 1
        assert row["index_matched"] == 100.0
        assert row["attrition_pct"] == 50.0


# -- honesty about how much history exists ---------------------------------

class TestHistoryIsNotOverstated:
    def test_one_date_says_the_index_measures_nothing(self, tmp_path):
        conn = db(tmp_path, [cell("1"), cell("2")])
        res = an.cohort_index(conn, tier="weekly-full", min_matched=1)
        note = " ".join(res.basis.caveats)
        assert "SINCE INCEPTION = ONE DAY" in note
        assert "100 by construction" in note
        assert "not a series" in note
        assert all(r["index_matched"] == 100.0 for r in res.rows)

    def test_a_short_span_is_named_in_days_and_intervals(self, tmp_path):
        conn = two_dates(tmp_path, {1: 200.0}, {1: 200.0})
        note = " ".join(an.cohort_index(conn, tier="weekly-full",
                                        min_matched=1).basis.caveats)
        assert "2 COLLECTION DATES" in note
        assert "7 days end to end" in note
        assert "1 interval" in note
        assert "too short a window to read a trend" in note

    def test_a_longer_span_drops_the_no_trend_language(self, tmp_path):
        rows = []
        for d in ("2026-09-15", "2026-09-22", "2026-09-29", "2026-10-20"):
            rows.append(cell("1", scrape_date=d, price_pppn=200.0))
        note = " ".join(an.cohort_index(db(tmp_path, rows), tier="weekly-full",
                                        min_matched=1).basis.caveats)
        assert "4 COLLECTION DATES" in note and "35 days" in note
        assert "too short a window" not in note
        assert "provisional" in note

    def test_no_priced_rows_says_there_is_no_series(self, tmp_path):
        conn = db(tmp_path, [cell("1", price_pppn=None, price_total=None)])
        res = an.cohort_index(conn, tier="weekly-full")
        assert "NO HISTORY" in " ".join(res.basis.caveats)
        assert res.rows == []

    def test_two_dates_that_never_share_a_cabin_say_so(self, tmp_path):
        """More than one collection date is not the same as two readings of
        the same cabin, and the output must not let those be confused."""
        rows = [cell("a", scrape_date="2026-09-15"),
                cell("b", scrape_date="2026-09-22")]
        res = an.cohort_index(db(tmp_path, rows), tier="weekly-full")
        assert "NO COMPARABLE REPRICING YET" in " ".join(res.basis.caveats)


# -- inception -------------------------------------------------------------

class TestInception:
    def test_base_is_per_group_so_late_arrivals_are_not_discarded(self, tmp_path):
        """Alaska entering the panel a week late must still get an index."""
        rows = [cell("1", scrape_date="2026-09-15", price_pppn=200.0),
                cell("1", scrape_date="2026-09-22", price_pppn=200.0)]
        rows += [cell("2", region="Alaska", scrape_date="2026-09-22",
                      price_pppn=400.0),
                 cell("2", region="Alaska", scrape_date="2026-09-29",
                      price_pppn=440.0),
                 cell("1", scrape_date="2026-09-29", price_pppn=200.0)]
        res = an.cohort_index(db(tmp_path, rows), tier="weekly-full",
                              min_matched=1)
        alaska = [r for r in res.rows if r["region"] == "Alaska"]
        assert {r["base_date"] for r in alaska} == {"2026-09-22"}
        later = [r for r in alaska if r["scrape_date"] == "2026-09-29"]
        assert later[0]["index_matched"] == 110.0

    def test_base_date_is_on_every_row(self, tmp_path):
        conn = two_dates(tmp_path, {1: 200.0}, {1: 200.0})
        rows = an.cohort_index(conn, tier="weekly-full", min_matched=1).rows
        assert all(r["base_date"] for r in rows)

    def test_an_explicit_base_overrides_inception(self, tmp_path):
        rows = [cell("1", scrape_date=d, price_pppn=p) for d, p in
                (("2026-09-15", 200.0), ("2026-09-22", 400.0),
                 ("2026-09-29", 600.0))]
        res = an.cohort_index(db(tmp_path, rows), tier="weekly-full",
                              base_scrape_date="2026-09-22", min_matched=1)
        assert {r["base_date"] for r in res.rows} == {"2026-09-22"}
        assert at(res, "2026-09-29")[0]["index_matched"] == 150.0

    def test_an_absent_explicit_base_falls_back_to_inception(self, tmp_path):
        conn = two_dates(tmp_path, {1: 200.0}, {1: 300.0})
        res = an.cohort_index(conn, tier="weekly-full", min_matched=1,
                              base_scrape_date="2020-01-01")
        assert {r["base_date"] for r in res.rows} == {"2026-09-15"}


class TestCohorts:
    def test_sail_month_separates_the_series(self, tmp_path):
        rows = [cell("jan", sail_date="2027-01-10", scrape_date=d, price_pppn=p)
                for d, p in (("2026-09-15", 200.0), ("2026-09-22", 240.0))]
        rows += [cell("feb", sail_date="2027-02-10", scrape_date=d, price_pppn=p)
                 for d, p in (("2026-09-15", 200.0), ("2026-09-22", 200.0))]
        res = an.cohort_index(db(tmp_path, rows), tier="weekly-full",
                              min_matched=1)
        by_cohort = {r["cohort"]: r for r in at(res)}
        assert by_cohort["2027-01"]["index_matched"] == 120.0
        assert by_cohort["2027-02"]["index_matched"] == 100.0


# -- product filter --------------------------------------------------------

class TestProductFilter:
    def test_packages_are_excluded_by_default(self, tmp_path):
        rows = [cell("1", scrape_date=d, price_pppn=200.0)
                for d in ("2026-09-15", "2026-09-22")]
        rows += [cell("pkg", scrape_date=d, price_pppn=900.0, is_package=1)
                 for d in ("2026-09-15", "2026-09-22")]
        res = an.cohort_index(db(tmp_path, rows), tier="weekly-full",
                              min_matched=1)
        assert at(res)[0]["matched_cells"] == 1
        assert "PRODUCT FILTER" in " ".join(res.basis.caveats)

    def test_all_products_can_be_asked_for_explicitly(self, tmp_path):
        rows = [cell("1", scrape_date=d, price_pppn=200.0)
                for d in ("2026-09-15", "2026-09-22")]
        rows += [cell("pkg", scrape_date=d, price_pppn=900.0, is_package=1)
                 for d in ("2026-09-15", "2026-09-22")]
        res = an.cohort_index(db(tmp_path, rows), tier="weekly-full",
                              product="all", min_matched=1)
        assert at(res)[0]["matched_cells"] == 2


# -- sample grading --------------------------------------------------------

class TestSampleFlag:
    @pytest.mark.parametrize("n,expected", [
        (2, "INSUFFICIENT"), (7, "VERY THIN"), (20, "THIN"), (40, "ok"),
    ])
    def test_grades_on_matched_cells(self, tmp_path, n, expected):
        rows = [cell(str(i), scrape_date=d, price_pppn=200.0)
                for i in range(n) for d in ("2026-09-15", "2026-09-22")]
        row = at(an.cohort_index(db(tmp_path, rows), tier="weekly-full"))[0]
        assert row["sample"].startswith(expected)

    def test_grade_counts_matched_cells_not_rows_present(self, tmp_path):
        """40 cells at base and 40 now, but only 2 in common, is a sample of 2."""
        rows = [cell(f"a{i}", scrape_date="2026-09-15", price_pppn=200.0)
                for i in range(40)]
        rows += [cell(f"b{i}", scrape_date="2026-09-22", price_pppn=200.0)
                 for i in range(38)]
        rows += [cell(f"a{i}", scrape_date="2026-09-22", price_pppn=200.0)
                 for i in range(2)]
        row = at(an.cohort_index(db(tmp_path, rows), tier="weekly-full"))[0]
        assert row["matched_cells"] == 2
        assert row["sample"].startswith("INSUFFICIENT")


class TestBasisStillApplies:
    def test_tier_is_required(self, tmp_path):
        conn = db(tmp_path, [cell("1")])
        with pytest.raises(TypeError):
            an.cohort_index(conn)

    def test_unknown_tier_is_rejected(self, tmp_path):
        conn = db(tmp_path, [cell("1")])
        with pytest.raises(ValueError, match="unknown tier"):
            an.cohort_index(conn, tier="made-up")
