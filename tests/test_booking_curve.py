"""The booking curve: price, dispersion and depletion against days to departure.

This is the pitch's central exhibit, so the tests are mostly about the ways it
could quietly lie:

* A band that mixes itinerary lengths prices the deployment, not the fare.
* A cross-line `closed_share` reads high for NCL because Carnival publishes no
  "limited" state -- so the cross-line depletion measure must be sold-out only.
* A line that publishes no offers must show a NULL promo share, never 0.0,
  which on a slide would read as "runs no promotions".
* Aggregating at the cell level would let a big ship outvote a small one.
"""
import itertools
import sqlite3

import pytest

from panel import analysis as an
from panel.schema import DDL

NCL = "Norwegian Cruise Line"
CCL = "Carnival Cruise Line"
SCRAPE = "2026-09-17"
_ids = itertools.count(1)


def cell(*, line=NCL, sailing=None, sail_date="2027-03-17", nights=7,
         price=200.0, status="available", category="balcony", sub=None,
         promo=None, package=0):
    return {
        "scrape_ts_utc": f"{SCRAPE}T06:00:00+00:00", "scrape_date": SCRAPE,
        "line": line, "brand": "b", "ship": "S",
        "sailing_id": sailing or f"S{next(_ids)}",
        "sail_date": sail_date, "nights": nights, "is_package": package,
        "region": "Caribbean", "market": "US", "currency": "USD",
        # unique per cell: several cabins on one sailing must not collide on
        # the natural key, which now includes cabin_subcategory.
        "cabin_category": category,
        "cabin_subcategory": sub or f"{category.upper()}{next(_ids)}",
        "price_pppn": price, "price_total": (price * nights) if price else None,
        "availability_status": status, "promo_hash": promo,
        "tier": "weekly-full",
    }


def db(tmp_path, rows):
    conn = sqlite3.connect(str(tmp_path / "p.sqlite"))
    conn.executescript(DDL)
    for r in rows:
        conn.execute(f"INSERT INTO observations ({', '.join(r)}) "
                     f"VALUES ({', '.join('?' * len(r))})", list(r.values()))
    conn.commit()
    conn.row_factory = sqlite3.Row
    return conn


def curve(conn, **kw):
    kw.setdefault("tier", "weekly-full")
    kw.setdefault("regions", ["Caribbean"])
    kw.setdefault("min_sailings", 1)
    return an.booking_curve(conn, **kw)


def find(res, band, line=NCL, **extra):
    for r in res.rows:
        if r["dtd_band"] == band and r["line"] == line and all(
                r.get(k) == v for k, v in extra.items()):
            return r
    raise AssertionError(f"no row for {band} {line} {extra}: "
                         f"{[(r['dtd_band'], r['line']) for r in res.rows]}")


# -- banding ----------------------------------------------------------------

class TestBands:
    @pytest.mark.parametrize("days,band", [
        (0, "  0-14"), (29, " 15-29"), (30, " 30-44"),
        (119, "105-119"), (120, "120-134"), (365, "365-449"), (900, "730+"),
    ])
    def test_days_map_to_bands(self, days, band):
        assert an.dtd_band(days) == band

    def test_120_is_a_band_EDGE_not_inside_one(self):
        """No band may straddle final payment, or the 120-day test cannot be
        read off the curve at all."""
        assert 120 in an.DTD_EDGES
        assert an.dtd_band(119) != an.dtd_band(120)

    def test_undated_or_past_sailings_have_no_band(self):
        assert an.dtd_band(None) is None and an.dtd_band(-3) is None

    @pytest.mark.parametrize("n,band", [
        (3, "2-4n"), (5, "5-6n"), (7, "7-8n"), (11, "9-11n"), (14, "12+n"),
        (None, "?"), (0, "?"),
    ])
    def test_nights_bands(self, n, band):
        assert an.nights_band(n) == band


# -- aggregation level -------------------------------------------------------

class TestAggregatesAtSailingLevel:
    def test_a_big_sailing_does_not_outvote_a_small_one(self, tmp_path):
        """One sailing with 20 cheap cabins and one with 2 dear ones is a
        median of two sailings, not of 22 cabins."""
        rows = [cell(sailing="BIG", price=100.0) for _ in range(20)]
        rows += [cell(sailing="SMALL", price=300.0) for _ in range(2)]
        r = find(curve(db(tmp_path, rows), by_nights=False), "180-209")
        assert r["sailings"] == 2
        assert r["median_pppn"] == 200.0          # (100, 300) -> 200
        assert r["cells"] == 22

    def test_dispersion_is_across_sailings(self, tmp_path):
        rows = []
        for i, p in enumerate([100.0, 150.0, 200.0, 250.0]):
            rows += [cell(sailing=f"S{i}", price=p)]
        r = find(curve(db(tmp_path, rows), by_nights=False), "180-209")
        assert r["p25_pppn"] == 150.0 and r["p75_pppn"] == 250.0
        assert r["dispersion_pct"] == pytest.approx(57.1, abs=0.2)

    def test_uniform_pricing_reads_as_zero_dispersion(self, tmp_path):
        rows = [cell(sailing=f"S{i}", price=180.0) for i in range(6)]
        r = find(curve(db(tmp_path, rows), by_nights=False), "180-209")
        assert r["dispersion_pct"] == 0.0


# -- the depletion measure ---------------------------------------------------

class TestDepletionIsCrossLineSafe:
    def test_sold_out_share_excludes_solo_only_from_the_denominator(self, tmp_path):
        rows = [cell(sailing="A", status="sold_out", price=None),
                cell(sailing="A", status="available"),
                cell(sailing="A", status="solo_only", price=None)]
        r = find(curve(db(tmp_path, rows), by_nights=False), "180-209")
        assert r["bookable_cells"] == 2
        assert r["sold_out_share"] == 0.5

    def test_limited_counts_as_closed_but_not_as_sold_out(self, tmp_path):
        """The whole reason the cross-line measure is sold-out only."""
        rows = [cell(sailing="A", status="limited", price=None),
                cell(sailing="A", status="available")]
        r = find(curve(db(tmp_path, rows), by_nights=False), "180-209")
        assert r["sold_out_share"] == 0.0
        assert r["closed_share_within_line"] == 0.5


class TestPromoShare:
    def test_a_line_that_publishes_offers_reports_a_share(self, tmp_path):
        rows = [cell(sailing="A", promo="h1"), cell(sailing="A", promo=None)]
        r = find(curve(db(tmp_path, rows), by_nights=False), "180-209")
        assert r["promo_share"] == 0.5

    def test_a_silent_line_reports_null_not_zero(self, tmp_path):
        """0.0 next to NCL's 0.80 would read as 'Carnival runs no promotions'."""
        rows = [cell(line=CCL, sailing="C", promo=None) for _ in range(3)]
        r = find(curve(db(tmp_path, rows), by_nights=False), "180-209", line=CCL)
        assert r["promo_share"] is None


# -- like-for-like comparison ------------------------------------------------

class TestPremium:
    def test_premium_is_computed_inside_a_band(self, tmp_path):
        rows = [cell(line=NCL, sailing="N", price=200.0),
                cell(line=CCL, sailing="C", price=160.0)]
        res = curve(db(tmp_path, rows), by_nights=False)
        assert find(res, "180-209", NCL)["premium_vs_peer_pct"] == 25.0
        assert find(res, "180-209", CCL)["premium_vs_peer_pct"] is None

    def test_length_is_held_fixed_by_default(self, tmp_path):
        """NCL 7n at 200 vs Carnival 5n at 100 is not a 100% premium; with
        lengths split there is no comparable peer at all."""
        rows = [cell(line=NCL, sailing="N", nights=7, price=200.0),
                cell(line=CCL, sailing="C", nights=5, price=100.0)]
        res = curve(db(tmp_path, rows))
        assert find(res, "180-209", NCL, nights_band="7-8n")["premium_vs_peer_pct"] is None
        assert any("NO PEER OVERLAP" in c for c in res.basis.caveats)

    def test_pooling_lengths_is_possible_but_opt_in(self, tmp_path):
        rows = [cell(line=NCL, sailing="N", nights=7, price=200.0),
                cell(line=CCL, sailing="C", nights=5, price=100.0)]
        res = curve(db(tmp_path, rows), by_nights=False)
        assert find(res, "180-209", NCL)["premium_vs_peer_pct"] == 100.0

    def test_same_length_compares(self, tmp_path):
        rows = [cell(line=NCL, sailing="N", nights=7, price=200.0),
                cell(line=CCL, sailing="C", nights=8, price=160.0)]
        res = curve(db(tmp_path, rows))
        assert find(res, "180-209", NCL, nights_band="7-8n")["premium_vs_peer_pct"] == 25.0


# -- filters and scope -------------------------------------------------------

class TestScope:
    def test_suites_are_excluded_by_default(self, tmp_path):
        rows = [cell(sailing="A", category="balcony", price=200.0),
                cell(sailing="A", category="suite", price=900.0)]
        r = find(curve(db(tmp_path, rows), by_nights=False), "180-209")
        assert r["cells"] == 1 and r["median_pppn"] == 200.0

    def test_packages_are_excluded_by_default(self, tmp_path):
        rows = [cell(sailing="A", price=200.0),
                cell(sailing="P", price=900.0, package=1)]
        r = find(curve(db(tmp_path, rows), by_nights=False), "180-209")
        assert r["sailings"] == 1

    def test_nights_filter_narrows_the_universe(self, tmp_path):
        rows = [cell(sailing="A", nights=7, price=200.0),
                cell(sailing="B", nights=4, price=100.0)]
        res = curve(db(tmp_path, rows), nights=(7, 8))
        assert sum(r["sailings"] for r in res.rows) == 1

    def test_split_by_year_separates_the_books(self, tmp_path):
        rows = [cell(sailing="A", sail_date="2027-03-17", price=200.0),
                cell(sailing="B", sail_date="2028-03-17", price=300.0)]
        res = curve(db(tmp_path, rows), split="year", by_nights=False)
        periods = {r["sail_period"] for r in res.rows}
        assert periods == {"2027", "2028"}

    def test_an_unknown_split_is_refused(self, tmp_path):
        with pytest.raises(ValueError, match="unknown split"):
            curve(db(tmp_path, [cell()]), split="quarter")

    def test_tier_is_required(self, tmp_path):
        with pytest.raises(TypeError):
            an.booking_curve(db(tmp_path, [cell()]))


class TestHonestyOfOutput:
    def test_thin_bands_are_flagged_not_hidden(self, tmp_path):
        rows = [cell(sailing="A", price=200.0)]
        r = find(curve(db(tmp_path, rows), by_nights=False, min_sailings=5),
                 "180-209")
        assert r["sample"].startswith("INSUFFICIENT")

    def test_the_band_carries_its_own_sail_window_and_length(self, tmp_path):
        # both inside one band: 181 and 205 days out
        rows = [cell(sailing="A", sail_date="2027-03-17", nights=7),
                cell(sailing="B", sail_date="2027-04-10", nights=7)]
        r = find(curve(db(tmp_path, rows)), "180-209", nights_band="7-8n")
        assert r["sail_from"] == "2027-03-17" and r["sail_to"] == "2027-04-10"
        assert r["median_nights"] == 7

    def test_the_seasonal_confound_is_stated(self, tmp_path):
        res = curve(db(tmp_path, [cell()]))
        assert any("CONFOUND" in n for n in res.notes)
        assert any("collection date" in n for n in res.notes)

    def test_empty_scope_says_so(self, tmp_path):
        res = curve(db(tmp_path, [cell(sail_date="2027-03-17")]),
                    regions=["Alaska"])
        assert res.rows == []


# -- peer-comparable cabins --------------------------------------------------

class TestPeerComparableCabins:
    """NCL maps MINISUITE to `balcony`; Carnival has no counterpart there.
    Leaving it in reports a mapping artefact as a price premium."""

    def rows(self):
        return [
            cell(line=NCL, sailing="N", sub="BALCONY", price=200.0),
            cell(line=NCL, sailing="N", sub="MINISUITE", price=400.0),
            cell(line=CCL, sailing="C", sub="OB", price=200.0),
        ]

    def test_minisuite_is_dropped_from_the_cross_line_comparison(self, tmp_path):
        r = find(curve(db(tmp_path, self.rows())), "180-209",
                 nights_band="7-8n")
        assert r["cells"] == 1
        assert r["median_pppn"] == 200.0
        assert r["premium_vs_peer_pct"] == 0.0

    def test_leaving_it_in_manufactures_a_premium(self, tmp_path):
        r = find(curve(db(tmp_path, self.rows()), peer_comparable=False),
                 "180-209", nights_band="7-8n")
        assert r["median_pppn"] == 300.0        # (200, 400)
        assert r["premium_vs_peer_pct"] == 50.0

    def test_the_exclusion_is_declared_in_the_basis(self, tmp_path):
        res = curve(db(tmp_path, self.rows()))
        assert any("PEER-COMPARABLE CABINS ONLY" in c and "MINISUITE" in c
                   for c in res.basis.caveats)

    def test_peer_gap_drops_it_too(self, tmp_path):
        conn = db(tmp_path, self.rows())
        res = an.peer_gap(conn, tier="weekly-full", regions=["Caribbean"],
                          min_cells=1)
        bal = [r for r in res.rows if r.get("category") == "balcony"]
        assert bal and bal[0]["treatment_median_pppn"] == 200.0
        assert bal[0]["gap_pct"] == 0.0

    def test_the_capability_owns_the_list_not_the_analysis(self):
        from panel.sources import capabilities as caps
        assert caps.capability("ncl").peer_excluded_subcategories == ("MINISUITE",)
        assert caps.capability("carnival").peer_excluded_subcategories == ()
        assert caps.peer_excluded_subcategories() == ["MINISUITE"]
