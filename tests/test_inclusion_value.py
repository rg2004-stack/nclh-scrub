"""Valuing an inclusion, and the frequency test on the boundary event study.

Two ideas are under test.

1. An inclusion is a discount that never touches the advertised price. Sizing
   it mixes a number we measured (the fare) with one we did not (the vendor's
   published per-day rate). Every output must keep those apart, because the
   slide has to say which half is cited.
2. The event study's informative statistic is a FREQUENCY. Fares are sticky,
   so most cabins contribute a zero and a median of five events says little;
   "4 of 5 cut against a 26% base rate" is a claim the sample can carry.
"""
import itertools
import json
import sqlite3

import pytest

from panel import analysis as an
from panel.schema import DDL

NCL = "Norwegian Cruise Line"
_ids = itertools.count(1)
GRAT = an.GRATUITY_OFFER


def bundle(*codes):
    payload = json.dumps([{"code": c, "shortTitle": c, "inclusion": "Mandatory",
                           "offerType": "auto-included-offer"} for c in codes])
    return "h::" + "+".join(codes), payload


WITH, PAYLOAD = bundle(GRAT)
WITHOUT, PAYLOAD2 = bundle("free-wifi-offer")
BUNDLES = ((WITH, PAYLOAD), (WITHOUT, PAYLOAD2))


def cell(*, price, nights=7, category="balcony", promo=WITH, region="Caribbean",
         sailing=None, sub=None, ship="S"):
    return {
        "scrape_ts_utc": "2026-09-19T06:00:00+00:00", "scrape_date": "2026-09-19",
        "line": NCL, "brand": "b", "ship": ship,
        "sailing_id": sailing or ("S%d" % next(_ids)),
        "sail_date": "2027-01-20", "nights": nights, "is_package": 0,
        "region": region, "market": "US", "currency": "USD",
        "cabin_category": category,
        "cabin_subcategory": sub or ("%s%d" % (category.upper(), next(_ids))),
        "price_pppn": price, "price_total": price * nights,
        "availability_status": "available", "promo_hash": promo,
        "tier": "daily-marker",
    }


def db(tmp_path, rows):
    conn = sqlite3.connect(str(tmp_path / ("p%d.sqlite" % next(_ids))))
    conn.executescript(DDL)
    for h, payload in BUNDLES:
        conn.execute("INSERT OR REPLACE INTO promos (promo_hash, promo_text, "
                     "first_seen, last_seen, n_seen) VALUES (?,?,?,?,1)",
                     (h, payload, "2026-09-19", "2026-09-19"))
    for r in rows:
        conn.execute("INSERT INTO observations (%s) VALUES (%s)"
                     % (", ".join(r), ", ".join("?" * len(r))), list(r.values()))
    conn.commit()
    conn.row_factory = sqlite3.Row
    return conn


def run(conn, **kw):
    kw.setdefault("tier", "daily-marker")
    kw.setdefault("min_cells", 1)
    return an.inclusion_discount(conn, **kw)


def row(res, region="Caribbean", cat="balcony"):
    return next(r for r in res.rows
                if r["region"] == region and r["cabin_category"] == cat)


class TestTheArithmetic:
    def test_discount_is_rate_over_nightly_fare(self, tmp_path):
        r = row(run(db(tmp_path, [cell(price=200.0)])))
        assert r["rate_per_person_per_day"] == 20.0
        assert r["effective_discount_pct"] == 10.0

    def test_itinerary_length_cancels_out(self, tmp_path):
        """Charge is per DAY, fare is per NIGHT, so a 4-night and an 11-night
        sailing at the same nightly fare carry the same percentage."""
        short = row(run(db(tmp_path, [cell(price=200.0, nights=4)])))
        long_ = row(run(db(tmp_path, [cell(price=200.0, nights=11)])))
        assert short["effective_discount_pct"] == long_["effective_discount_pct"]
        assert short["value_per_person"] != long_["value_per_person"]

    def test_dollar_value_restores_the_length(self, tmp_path):
        r = row(run(db(tmp_path, [cell(price=200.0, nights=7)])))
        assert r["value_per_person"] == 140.0
        assert r["fare_per_person"] == 1400.0

    def test_suites_take_the_suite_rate(self, tmp_path):
        r = row(run(db(tmp_path, [cell(price=500.0, category="suite")])),
                cat="suite")
        assert r["rate_per_person_per_day"] == 25.0
        assert r["effective_discount_pct"] == 5.0

    def test_a_dearer_cabin_gets_a_smaller_percentage(self, tmp_path):
        """The inclusion is flat in dollars, so it is worth relatively less
        the dearer the cabin -- which is why suites read ~5% and balconies
        ~10% off the same offer."""
        res = run(db(tmp_path, [cell(price=200.0),
                                cell(price=500.0, category="suite")]))
        assert (row(res, cat="balcony")["effective_discount_pct"]
                > row(res, cat="suite")["effective_discount_pct"])


class TestScope:
    def test_only_cabins_carrying_the_offer_are_counted(self, tmp_path):
        rows = [cell(price=200.0, promo=WITH), cell(price=900.0, promo=WITHOUT)]
        r = row(run(db(tmp_path, rows)))
        assert r["cells_with_offer"] == 1 and r["median_pppn_observed"] == 200.0

    def test_an_absent_offer_is_reported_not_silently_empty(self, tmp_path):
        res = run(db(tmp_path, [cell(price=200.0, promo=WITHOUT)]))
        assert res.rows == []
        assert any("OFFER NOT PRESENT" in c for c in res.basis.caveats)

    def test_thin_groups_are_flagged(self, tmp_path):
        r = row(run(db(tmp_path, [cell(price=200.0)]), min_cells=10))
        assert r["sample"].startswith("INSUFFICIENT")

    def test_ships_and_sailings_are_counted(self, tmp_path):
        rows = [cell(price=200.0, sailing="A", ship="One"),
                cell(price=200.0, sailing="B", ship="Two")]
        r = row(run(db(tmp_path, rows)))
        assert r["sailings"] == 2 and r["ships"] == 2


class TestProvenanceIsKeptApart:
    def test_every_row_carries_the_rate_date_and_source(self, tmp_path):
        r = row(run(db(tmp_path, [cell(price=200.0)])))
        assert r["rate_as_of"] and r["rate_source"]

    def test_the_basis_says_which_half_is_cited(self, tmp_path):
        res = run(db(tmp_path, [cell(price=200.0)]))
        caveat = " ".join(res.basis.caveats)
        assert "HALF MEASURED, HALF CITED" in caveat
        assert "verify the current rate" in caveat

    def test_the_fare_is_our_own_observation(self, tmp_path):
        """The measured half must be a real median of collected prices."""
        rows = [cell(price=180.0), cell(price=200.0), cell(price=260.0)]
        assert row(run(db(tmp_path, rows)))["median_pppn_observed"] == 200.0


class TestBinomialTail:
    @pytest.mark.parametrize("k,n,p,expected", [
        (0, 5, 0.3, 1.0),          # at least zero successes is certain
        (5, 5, 0.5, 0.03125),      # all five
        (1, 1, 0.25, 0.25),
    ])
    def test_known_values(self, k, n, p, expected):
        assert an.binomial_tail(k, n, p) == pytest.approx(expected, abs=1e-6)

    def test_the_live_reading_is_what_it_claims(self):
        """4 of 5 cut against the observed control rates."""
        assert an.binomial_tail(4, 5, 0.259) == pytest.approx(0.018, abs=0.002)
        assert an.binomial_tail(4, 5, 0.113) == pytest.approx(0.0007, abs=0.0005)

    def test_a_rarer_base_rate_makes_the_result_more_surprising(self):
        assert an.binomial_tail(4, 5, 0.10) < an.binomial_tail(4, 5, 0.50)

    def test_degenerate_inputs_return_none(self):
        assert an.binomial_tail(1, 0, 0.5) is None
        assert an.binomial_tail(1, 5, 1.5) is None
