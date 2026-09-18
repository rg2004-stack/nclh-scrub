"""The daily marker picker, which now decides whether the 120-day test works.

Three failures this file exists to prevent, all of which happened:

1. Markers selected from a frame that stops before the boundary. Every marker
   then sits wholly inside final payment and nothing can identify.
2. Markers selected on sailing count alone. Carnival's densest Caribbean
   product is 4-5 nights; the 7-8 night analyses discarded all of it, leaving
   13 comparable sailings out of 159 collected.
3. Markers selected from stale rows. MEP, a Carnival code with no current
   sailings, was picked because its dates came from an older scrape.
"""
import datetime
import importlib.util
import os
import sqlite3

import pytest

from panel.schema import DDL

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location(
    "pick_markers", os.path.join(HERE, "scripts", "pick_markers.py"))
pm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pm)

TODAY = datetime.date(2026, 9, 18)
NCL = "Norwegian Cruise Line"
CCL = "Carnival Cruise Line"


def cell(code, sail_date, *, line=NCL, nights=7, tier="weekly-full",
         scrape="2026-09-17", region="Caribbean", sub="BALCONY"):
    return {
        "scrape_ts_utc": f"{scrape}T06:00:00+00:00", "scrape_date": scrape,
        "line": line, "brand": "b", "ship": "S",
        "sailing_id": f"{code}-{sail_date}", "itinerary_code": code,
        "sail_date": sail_date, "nights": nights, "is_package": 0,
        "region": region, "market": "US", "currency": "USD",
        "cabin_category": "balcony", "cabin_subcategory": sub,
        "price_pppn": 200.0, "availability_status": "available", "tier": tier,
    }


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "p.sqlite"))
    c.executescript(DDL)
    c.row_factory = sqlite3.Row
    return c


def insert(conn, rows):
    for r in rows:
        conn.execute(f"INSERT INTO observations ({', '.join(r)}) "
                     f"VALUES ({', '.join('?' * len(r))})", list(r.values()))
    conn.commit()


def days(n):
    return (TODAY + datetime.timedelta(days=n)).isoformat()


class TestStraddleCounts:
    def setup_method(self):
        pm.NEAR_TERM = (TODAY.isoformat(), days(240))

    def test_counts_each_side_of_the_boundary(self):
        dates = [days(30), days(90), days(150), days(200)]
        assert pm.straddle_counts(dates, TODAY) == (2, 2)

    def test_a_wholly_inside_itinerary_cannot_straddle(self):
        inside, outside = pm.straddle_counts([days(10), days(60)], TODAY)
        assert inside and not outside

    def test_dates_outside_the_window_are_ignored(self):
        assert pm.straddle_counts([days(400), days(500)], TODAY) == (0, 0)

    def test_past_sailings_are_ignored(self):
        assert pm.straddle_counts([days(-10)], TODAY) == (0, 0)

    def test_the_boundary_day_itself_counts_as_outside(self):
        assert pm.straddle_counts([days(pm.FINAL_PAYMENT_DAYS)], TODAY) == (0, 1)


class TestCandidateFrame:
    def setup_method(self):
        pm.NEAR_TERM = (TODAY.isoformat(), days(240))

    def test_length_is_carried_and_flagged_against_the_peer_band(self, conn):
        insert(conn, [cell("SEVEN", days(60), nights=7),
                      cell("FOUR", days(60), nights=4)])
        got = {c["itinerary_code"]: c for c in pm.panel_candidates(conn)}
        assert got["SEVEN"]["peer_length"] is True
        assert got["FOUR"]["peer_length"] is False

    def test_stale_itineraries_do_not_enter_the_frame(self, conn):
        """MEP: present in an older scrape and in the daily tier, gone from
        the current weekly catalogue. It must not be selectable."""
        insert(conn, [cell("LIVE", days(60), scrape="2026-09-17"),
                      cell("STALE", days(60), scrape="2026-09-16"),
                      cell("DAILYONLY", days(60), tier="daily-marker",
                           scrape="2026-09-17")])
        codes = {c["itinerary_code"] for c in pm.panel_candidates(conn)}
        assert codes == {"LIVE"}

    def test_straddling_is_computed_per_candidate(self, conn):
        insert(conn, [cell("BOTH", days(60)), cell("BOTH", days(200)),
                      cell("ONESIDE", days(60))])
        got = {c["itinerary_code"]: c for c in pm.panel_candidates(conn)}
        assert got["BOTH"]["straddles"] is True
        assert got["ONESIDE"]["straddles"] is False


class TestScoring:
    def base(self, **kw):
        d = {"line": NCL, "itinerary_code": "X", "region": "Caribbean",
             "ship": "S", "categories": ["balcony"], "cats": 1,
             "near_term": 5, "event": 0, "inside_fp": 5, "outside_fp": 5,
             "straddles": True, "peer_length": True, "sailings": 10,
             "in_panel": True, "is_package": 0, "nights": 7}
        d.update(kw)
        return d

    def test_straddling_beats_not_straddling(self):
        cands = [self.base(itinerary_code="A", straddles=False, inside_fp=20,
                           outside_fp=0),
                 self.base(itinerary_code="B")]
        pm.choose(cands, target=2)
        assert cands[1]["score"] > cands[0]["score"]

    def test_comparable_length_beats_denser_but_wrong_length(self):
        """The Carnival failure: a dense 4-night itinerary scored above a
        7-night one, then the 7-8n analysis discarded it."""
        dense_wrong = self.base(itinerary_code="D4", nights=4,
                                peer_length=False, inside_fp=12, outside_fp=12)
        right = self.base(itinerary_code="D7", inside_fp=6, outside_fp=6)
        pm.choose([dense_wrong, right], target=2)
        assert right["score"] > dense_wrong["score"]

    def test_density_discriminates_between_comparable_itineraries(self):
        thin = self.base(itinerary_code="T", inside_fp=1, outside_fp=1)
        thick = self.base(itinerary_code="K", inside_fp=10, outside_fp=10)
        pm.choose([thin, thick], target=2)
        assert thick["score"] > thin["score"]


class TestSelection:
    def setup_method(self):
        pm.NEAR_TERM = (TODAY.isoformat(), days(240))

    def cands(self, n, line, nights, code_prefix):
        out = []
        for i in range(n):
            out.append({"line": line, "itinerary_code": f"{code_prefix}{i}",
                        "region": "Caribbean", "ship": f"ship{i}",
                        "categories": ["balcony"], "cats": 4, "near_term": 8,
                        "event": 0, "inside_fp": 8, "outside_fp": 8,
                        "straddles": True, "nights": nights,
                        "peer_length": 7 <= nights <= 8, "sailings": 16,
                        "in_panel": True, "is_package": 0})
        return out

    def test_both_lines_are_represented_in_a_peer_region(self):
        cands = self.cands(10, CCL, 7, "C") + self.cands(3, NCL, 7, "N")
        chosen = pm.choose(cands, target=8)
        lines = {c["line"] for c in chosen}
        assert lines == {CCL, NCL}, "a peer region needs both lines"

    def test_a_peer_region_prefers_comparable_lengths(self):
        cands = (self.cands(6, CCL, 4, "S") + self.cands(6, CCL, 7, "L")
                 + self.cands(6, NCL, 7, "N"))
        chosen = [c for c in pm.choose(cands, target=8)
                  if c["line"] == CCL]
        assert chosen, "Carnival must be represented"
        assert all(c["peer_length"] for c in chosen), \
            "4-night markers cannot serve a 7-8 night comparison"
