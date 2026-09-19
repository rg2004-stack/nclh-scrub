"""The offer-boundary event study: Exhibit 3, rebuilt so it identifies.

The cross-sectional cutoff scan could not separate the boundary from the
calendar, because on one collection date days-to-departure IS the date. This
design compares each sailing to ITSELF a day apart, so the calendar is fixed by
construction, and it takes the event date from the vendor: NCL's risk-free
cancellation offer applies to sailings outside final payment, so losing it IS
the crossing. Nothing here assumes where the boundary sits.
"""
import itertools
import json
import sqlite3

import pytest

from panel import analysis as an
from panel.schema import DDL

NCL = "Norwegian Cruise Line"
CCL = "Carnival Cruise Line"
RFC = an.BOUNDARY_OFFER
_ids = itertools.count(1)


def offer_bundle(*codes):
    payload = json.dumps([
        {"code": c, "shortTitle": c, "inclusion": "Mandatory",
         "offerType": "auto-included-offer"} for c in codes])
    return "h::" + "+".join(codes), payload


def cell(*, sailing, scrape, price, line=NCL, sub="BALCONY", promo=None,
         sail_date="2027-01-15", status="available", category="balcony"):
    return {
        "scrape_ts_utc": scrape + "T06:00:00+00:00", "scrape_date": scrape,
        "line": line, "brand": "b", "ship": "S", "sailing_id": sailing,
        "sail_date": sail_date, "nights": 7, "is_package": 0,
        "region": "Caribbean", "market": "US", "currency": "USD",
        "cabin_category": category, "cabin_subcategory": sub,
        "price_pppn": price, "price_total": price * 7 if price else None,
        "availability_status": status, "promo_hash": promo,
        "tier": "daily-marker",
    }


def db(tmp_path, rows, bundles=()):
    conn = sqlite3.connect(str(tmp_path / ("p%d.sqlite" % next(_ids))))
    conn.executescript(DDL)
    for h, payload in bundles:
        conn.execute("INSERT OR REPLACE INTO promos (promo_hash, promo_text, "
                     "first_seen, last_seen, n_seen) VALUES (?,?,?,?,1)",
                     (h, payload, "2026-09-18", "2026-09-19"))
    for r in rows:
        cols = ", ".join(r)
        marks = ", ".join("?" * len(r))
        conn.execute("INSERT INTO observations (%s) VALUES (%s)" % (cols, marks),
                     list(r.values()))
    conn.commit()
    conn.row_factory = sqlite3.Row
    return conn


def run(conn, **kw):
    kw.setdefault("tier", "daily-marker")
    kw.setdefault("min_events", 1)
    return an.offer_boundary_event_study(conn, **kw)


def grp(res, group):
    return next((r for r in res.rows if r["group"] == group), None)


WITH, PAYLOAD = offer_bundle(RFC, "free-wifi-offer")
WITHOUT, PAYLOAD2 = offer_bundle("free-wifi-offer")
BUNDLES = ((WITH, PAYLOAD), (WITHOUT, PAYLOAD2))
D0, D1 = "2026-09-18", "2026-09-19"


def crossing(sailing, p0, p1, **kw):
    """A sailing that carried the offer on D0 and lost it on D1."""
    return [cell(sailing=sailing, scrape=D0, price=p0, promo=WITH, **kw),
            cell(sailing=sailing, scrape=D1, price=p1, promo=WITHOUT, **kw)]


def holding(sailing, p0, p1, **kw):
    return [cell(sailing=sailing, scrape=D0, price=p0, promo=WITH, **kw),
            cell(sailing=sailing, scrape=D1, price=p1, promo=WITH, **kw)]


class TestEventDetection:
    def test_losing_the_offer_is_a_crossing(self, tmp_path):
        res = run(db(tmp_path, crossing("A", 200.0, 190.0), BUNDLES))
        lost = grp(res, "lost_offer")
        assert lost["sailings"] == 1
        assert lost["median_change_pct"] == -5.0

    def test_keeping_the_offer_is_the_control_not_an_event(self, tmp_path):
        res = run(db(tmp_path, holding("A", 200.0, 190.0), BUNDLES))
        assert grp(res, "lost_offer") is None
        assert grp(res, "kept_offer")["sailings"] == 1

    def test_a_sailing_that_never_had_it_is_neither(self, tmp_path):
        rows = [cell(sailing="A", scrape=D0, price=200.0, promo=WITHOUT),
                cell(sailing="A", scrape=D1, price=190.0, promo=WITHOUT)]
        res = run(db(tmp_path, rows, BUNDLES))
        assert grp(res, "lost_offer") is None and grp(res, "kept_offer") is None

    def test_a_sailing_seen_on_only_one_date_is_skipped(self, tmp_path):
        rows = [cell(sailing="A", scrape=D0, price=200.0, promo=WITH)]
        res = run(db(tmp_path, rows, BUNDLES))
        assert grp(res, "lost_offer") is None

    def test_no_crossings_is_stated_not_silent(self, tmp_path):
        res = run(db(tmp_path, holding("A", 200.0, 200.0), BUNDLES))
        assert any("NO CROSSINGS OBSERVED YET" in c for c in res.basis.caveats)

    def test_a_missing_offer_code_is_reported(self, tmp_path):
        res = run(db(tmp_path, holding("A", 200.0, 200.0), BUNDLES),
                  offer_code="No-Such-Offer")
        assert res.rows == []
        assert any("OFFER NOT FOUND" in c for c in res.basis.caveats)


class TestPriceChange:
    def test_change_is_a_matched_basket(self, tmp_path):
        """A cabin that stops being priced must not read as a price move."""
        rows = [cell(sailing="A", scrape=D0, price=200.0, sub="BALCONY", promo=WITH),
                cell(sailing="A", scrape=D0, price=100.0, sub="INSIDE", promo=WITH),
                cell(sailing="A", scrape=D1, price=200.0, sub="BALCONY", promo=WITHOUT)]
        lost = grp(run(db(tmp_path, rows, BUNDLES)), "lost_offer")
        assert lost["matched_cabins"] == 1
        assert lost["median_change_pct"] == 0.0

    def test_share_cut_counts_sailings_that_fell(self, tmp_path):
        rows = (crossing("A", 200.0, 190.0) + crossing("B", 200.0, 180.0)
                + crossing("C", 200.0, 210.0))
        lost = grp(run(db(tmp_path, rows, BUNDLES)), "lost_offer")
        assert lost["sailings"] == 3
        assert lost["share_cut"] == pytest.approx(2 / 3, abs=0.01)

    def test_a_basket_weights_by_cabin_value(self, tmp_path):
        rows = [cell(sailing="A", scrape=D0, price=100.0, sub="I", promo=WITH),
                cell(sailing="A", scrape=D0, price=300.0, sub="B", promo=WITH),
                cell(sailing="A", scrape=D1, price=100.0, sub="I", promo=WITHOUT),
                cell(sailing="A", scrape=D1, price=200.0, sub="B", promo=WITHOUT)]
        lost = grp(run(db(tmp_path, rows, BUNDLES)), "lost_offer")
        assert lost["median_change_pct"] == -25.0        # 300/400, not -33%


class TestControls:
    def test_the_same_line_control_is_differenced_out(self, tmp_path):
        rows = crossing("A", 200.0, 180.0)                # -10%
        rows += holding("B", 200.0, 190.0)                # -5% line-wide
        rows += holding("C", 100.0, 95.0)
        lost = grp(run(db(tmp_path, rows, BUNDLES)), "lost_offer")
        assert lost["median_change_pct"] == -10.0
        assert lost["vs_kept_pp"] == -5.0

    def test_the_peer_control_is_differenced_out(self, tmp_path):
        rows = crossing("A", 200.0, 180.0)
        rows += [cell(sailing="P", scrape=D0, price=100.0, line=CCL),
                 cell(sailing="P", scrape=D1, price=98.0, line=CCL)]
        lost = grp(run(db(tmp_path, rows, BUNDLES)), "lost_offer")
        assert lost["vs_peer_pp"] == -8.0

    def test_peers_far_from_the_treated_lead_time_are_excluded(self, tmp_path):
        """A peer sailing a year away is not a control for one crossing now."""
        rows = crossing("A", 200.0, 180.0, sail_date="2027-01-15")
        rows += [cell(sailing="FAR", scrape=D0, price=100.0, line=CCL,
                      sail_date="2028-01-15"),
                 cell(sailing="FAR", scrape=D1, price=50.0, line=CCL,
                      sail_date="2028-01-15")]
        res = run(db(tmp_path, rows, BUNDLES), lead_tolerance_days=21)
        assert grp(res, "peer_same_lead") is None
        assert grp(res, "lost_offer")["vs_peer_pp"] is None

    def test_controls_carry_no_difference_columns(self, tmp_path):
        rows = crossing("A", 200.0, 180.0) + holding("B", 200.0, 200.0)
        kept = grp(run(db(tmp_path, rows, BUNDLES)), "kept_offer")
        assert kept["vs_kept_pp"] is None and kept["vs_peer_pp"] is None


class TestHonesty:
    def test_a_thin_event_count_is_flagged_on_the_row(self, tmp_path):
        res = run(db(tmp_path, crossing("A", 200.0, 190.0), BUNDLES),
                  min_events=5)
        assert grp(res, "lost_offer")["sample"].startswith("INSUFFICIENT")

    def test_a_thin_event_count_is_flagged_on_the_basis(self, tmp_path):
        res = run(db(tmp_path, crossing("A", 200.0, 190.0), BUNDLES),
                  min_events=5)
        assert any("ONLY 1 CROSSING" in c for c in res.basis.caveats)

    def test_the_design_is_explained_in_the_notes(self, tmp_path):
        res = run(db(tmp_path, crossing("A", 200.0, 190.0), BUNDLES))
        joined = " ".join(res.notes)
        assert "COMPARED TO ITSELF" in joined
        assert "dated by the vendor" in joined

    def test_one_collection_date_cannot_support_an_event_study(self, tmp_path):
        rows = [cell(sailing="A", scrape=D0, price=200.0, promo=WITH)]
        res = an.offer_boundary_event_study(db(tmp_path, rows, BUNDLES),
                                            tier="daily-marker")
        assert any("NOT COMPUTABLE" in c for c in res.basis.caveats)

    def test_suites_are_out_of_scope_by_default(self, tmp_path):
        rows = crossing("A", 200.0, 190.0)
        rows += [cell(sailing="A", scrape=D0, price=900.0, sub="HAVEN",
                      category="suite", promo=WITH),
                 cell(sailing="A", scrape=D1, price=450.0, sub="HAVEN",
                      category="suite", promo=WITHOUT)]
        lost = grp(run(db(tmp_path, rows, BUNDLES)), "lost_offer")
        assert lost["matched_cabins"] == 1 and lost["median_change_pct"] == -5.0
