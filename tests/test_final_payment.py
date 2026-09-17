"""Exhibit 3: the final-payment discontinuity, and its identification guards.

On a single collection date, days-to-departure is a one-to-one map onto the
calendar: the window "inside final payment" is always a different few weeks of
the year from the window outside it. So a cutoff that happens to put Christmas
on one side moves BOTH lines and has nothing to do with final payment.

These tests pin the guards that make that visible rather than publishable:
the peer's own jump, the sail-date range on each side, and the sign convention.
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


def cell(*, line=NCL, dtd, price=200.0, status="available", sailing=None,
         category="balcony", sub=None, promo=None):
    import datetime
    sail = (datetime.date(2026, 9, 17) + datetime.timedelta(days=dtd)).isoformat()
    return {
        "scrape_ts_utc": f"{SCRAPE}T06:00:00+00:00", "scrape_date": SCRAPE,
        "line": line, "brand": "b", "ship": "S",
        "sailing_id": sailing or f"S{next(_ids)}",
        "sail_date": sail, "nights": 7, "is_package": 0,
        "region": "Caribbean", "market": "US", "currency": "USD",
        "cabin_category": category,
        "cabin_subcategory": sub or f"{category.upper()}{next(_ids)}",
        "price_pppn": price, "price_total": price * 7 if price else None,
        "availability_status": status, "promo_hash": promo,
        "tier": "weekly-full",
    }


def db(tmp_path, rows):
    # unique per call: a test may build two panels, and reopening one file
    # would re-insert the same rows and collide on the natural key.
    conn = sqlite3.connect(str(tmp_path / f"p{next(_ids)}.sqlite"))
    conn.executescript(DDL)
    for r in rows:
        conn.execute(f"INSERT INTO observations ({', '.join(r)}) "
                     f"VALUES ({', '.join('?' * len(r))})", list(r.values()))
    conn.commit()
    conn.row_factory = sqlite3.Row
    return conn


def run(conn, **kw):
    kw.setdefault("tier", "weekly-full")
    kw.setdefault("regions", ["Caribbean"])
    kw.setdefault("min_sailings", 1)
    return an.final_payment_test(conn, **kw)


def row(res, line=NCL, cutoff=120):
    return next(r for r in res.rows
                if r["line"] == line and r["cutoff_days"] == cutoff)


def population(*, line, lo, hi, price, n=6, status="available"):
    """n sailings spread across a days-to-departure window."""
    step = max(1, (hi - lo) // n)
    return [cell(line=line, dtd=lo + i * step, price=price, status=status)
            for i in range(n)]


class TestSignConvention:
    def test_discounting_after_final_payment_reads_negative(self, tmp_path):
        rows = population(line=NCL, lo=90, hi=119, price=100.0)     # inside
        rows += population(line=NCL, lo=120, hi=149, price=200.0)   # outside
        r = row(run(db(tmp_path, rows)))
        assert r["median_inside"] == 100.0 and r["median_outside"] == 200.0
        assert r["price_jump_pct"] == -50.0

    def test_holding_price_reads_zero(self, tmp_path):
        rows = population(line=NCL, lo=90, hi=119, price=150.0)
        rows += population(line=NCL, lo=120, hi=149, price=150.0)
        assert row(run(db(tmp_path, rows)))["price_jump_pct"] == 0.0

    def test_sold_out_jump_is_in_percentage_points(self, tmp_path):
        rows = population(line=NCL, lo=90, hi=119, price=None, status="sold_out")
        rows += population(line=NCL, lo=120, hi=149, price=100.0)
        r = row(run(db(tmp_path, rows)))
        assert r["sold_out_inside"] == 1.0 and r["sold_out_outside"] == 0.0
        assert r["sold_out_jump_pp"] == 100.0


class TestDifferenceInDifferences:
    def test_the_peers_jump_is_subtracted(self, tmp_path):
        rows = population(line=NCL, lo=90, hi=119, price=80.0)
        rows += population(line=NCL, lo=120, hi=149, price=100.0)   # -20%
        rows += population(line=CCL, lo=90, hi=119, price=95.0)
        rows += population(line=CCL, lo=120, hi=149, price=100.0)   # -5%
        r = row(run(db(tmp_path, rows)))
        assert r["price_jump_pct"] == -20.0
        assert r["peer_own_jump_pct"] == -5.0
        assert r["did_price_pct"] == -15.0

    def test_common_seasonality_cancels_out(self, tmp_path):
        """Both lines moving together is the calendar, not final payment."""
        rows = population(line=NCL, lo=90, hi=119, price=150.0)
        rows += population(line=NCL, lo=120, hi=149, price=100.0)
        rows += population(line=CCL, lo=90, hi=119, price=300.0)
        rows += population(line=CCL, lo=120, hi=149, price=200.0)
        assert row(run(db(tmp_path, rows)))["did_price_pct"] == 0.0

    def test_the_peer_row_carries_no_did(self, tmp_path):
        rows = population(line=NCL, lo=90, hi=149, price=100.0)
        rows += population(line=CCL, lo=90, hi=149, price=100.0)
        assert row(run(db(tmp_path, rows)), line=CCL)["did_price_pct"] is None


class TestIdentificationGuards:
    def test_a_jumping_control_is_flagged_as_not_identified(self, tmp_path):
        rows = population(line=NCL, lo=90, hi=119, price=100.0)
        rows += population(line=NCL, lo=120, hi=149, price=100.0)
        rows += population(line=CCL, lo=90, hi=119, price=200.0)
        rows += population(line=CCL, lo=120, hi=149, price=100.0)   # +100%
        r = row(run(db(tmp_path, rows)))
        assert "CONTROL ALSO JUMPS" in r["sample"]
        assert "not identified" in r["sample"]

    def test_a_still_control_is_not_flagged(self, tmp_path):
        rows = population(line=NCL, lo=90, hi=119, price=100.0)
        rows += population(line=NCL, lo=120, hi=149, price=100.0)
        rows += population(line=CCL, lo=90, hi=149, price=100.0, n=12)
        assert "CONTROL ALSO JUMPS" not in row(run(db(tmp_path, rows)))["sample"]

    def test_each_side_reports_the_calendar_it_covers(self, tmp_path):
        """Without this the reader cannot see that a cutoff straddles a holiday."""
        rows = population(line=NCL, lo=90, hi=119, price=100.0)
        rows += population(line=NCL, lo=120, hi=149, price=100.0)
        r = row(run(db(tmp_path, rows)))
        assert r["inside_sails"].startswith("2026-12")
        assert r["outside_sails"].startswith("2027-01")

    def test_seasonality_is_named_in_the_notes(self, tmp_path):
        res = run(db(tmp_path, population(line=NCL, lo=90, hi=149, price=100.0)))
        assert any("SEASONALITY IS THE BINDING CONSTRAINT" in n for n in res.notes)

    def test_thin_sides_are_flagged(self, tmp_path):
        rows = population(line=NCL, lo=90, hi=119, price=100.0, n=2)
        rows += population(line=NCL, lo=120, hi=149, price=100.0, n=2)
        r = row(run(db(tmp_path, rows), min_sailings=10))
        assert r["sample"].startswith("INSUFFICIENT")


class TestCutoffScan:
    def test_several_cutoffs_produce_a_profile(self, tmp_path):
        rows = population(line=NCL, lo=30, hi=200, price=100.0, n=40)
        res = run(db(tmp_path, rows), cutoffs=(90, 105, 120, 135))
        assert {r["cutoff_days"] for r in res.rows} == {90, 105, 120, 135}

    def test_scanning_warns_against_picking_the_best_row(self, tmp_path):
        rows = population(line=NCL, lo=30, hi=200, price=100.0, n=40)
        res = run(db(tmp_path, rows), cutoffs=(90, 120))
        assert any("CUTOFF SCAN" in c and "p-hacking" in c
                   for c in res.basis.caveats)

    def test_a_single_cutoff_does_not_warn(self, tmp_path):
        rows = population(line=NCL, lo=30, hi=200, price=100.0, n=40)
        res = run(db(tmp_path, rows), cutoffs=(120,))
        assert not any("CUTOFF SCAN" in c for c in res.basis.caveats)

    def test_bandwidth_sets_the_window_width(self, tmp_path):
        rows = population(line=NCL, lo=110, hi=129, price=100.0, n=20)
        wide = row(run(db(tmp_path, rows), bandwidth=30))
        narrow = row(run(db(tmp_path, rows), bandwidth=5))
        assert narrow["n_inside"] < wide["n_inside"]


class TestScope:
    def test_suites_and_minisuites_are_out_by_default(self, tmp_path):
        rows = population(line=NCL, lo=90, hi=119, price=100.0)
        rows += population(line=NCL, lo=120, hi=149, price=100.0)
        rows += [cell(line=NCL, dtd=100, price=900.0, category="suite"),
                 cell(line=NCL, dtd=100, price=800.0, sub="MINISUITE")]
        r = row(run(db(tmp_path, rows)))
        assert r["median_inside"] == 100.0

    def test_empty_scope_says_so(self, tmp_path):
        res = run(db(tmp_path, [cell(dtd=100)]), regions=["Alaska"])
        assert res.rows == [] and "no rows in scope" in res.notes[0]

    def test_tier_is_required(self, tmp_path):
        with pytest.raises(TypeError):
            an.final_payment_test(db(tmp_path, [cell(dtd=100)]))
