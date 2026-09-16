"""Resume markers must not survive a change in collection scope.

A marker means "this itinerary was already collected today under the settings
in force at the time". Widening the sail window does not change which search
pages Carnival returns -- it changes which rows pass the filter -- so a resumed
run would skip every page and the day's file would silently keep the narrower
scope while the config claimed otherwise.
"""
import sqlite3

from panel.storage import Store

TIER = "weekly-full"
NCL = "Norwegian Cruise Line"
CCL = "Carnival Cruise Line"
DATE = "2026-09-16"


def seed(store, line, codes, date=DATE):
    for c in codes:
        store.mark_itinerary_done(TIER, line, date, c, 10)


class TestResetProgress:
    def test_clears_only_the_named_line(self, tmp_path):
        with Store(tmp_path / "p.sqlite") as store:
            seed(store, NCL, ["A", "B"])
            seed(store, CCL, ["ALL_p1", "ALL_p2"])
            removed = store.reset_progress(TIER, DATE, CCL)
            assert removed == 2
            assert store.completed_itineraries(TIER, NCL, DATE) == {"A", "B"}
            assert store.completed_itineraries(TIER, CCL, DATE) == set()

    def test_clears_every_line_when_none_named(self, tmp_path):
        with Store(tmp_path / "p.sqlite") as store:
            seed(store, NCL, ["A"])
            seed(store, CCL, ["ALL_p1"])
            assert store.reset_progress(TIER, DATE) == 2
            assert store.completed_itineraries(TIER, NCL, DATE) == set()
            assert store.completed_itineraries(TIER, CCL, DATE) == set()

    def test_leaves_other_dates_alone(self, tmp_path):
        """Resetting today must not erase the record of last week's run."""
        with Store(tmp_path / "p.sqlite") as store:
            seed(store, NCL, ["A"], date="2026-09-09")
            seed(store, NCL, ["B"], date=DATE)
            store.reset_progress(TIER, DATE)
            assert store.completed_itineraries(TIER, NCL, "2026-09-09") == {"A"}
            assert store.completed_itineraries(TIER, NCL, DATE) == set()

    def test_leaves_other_tiers_alone(self, tmp_path):
        with Store(tmp_path / "p.sqlite") as store:
            seed(store, NCL, ["A"])
            store.mark_itinerary_done("daily-marker", NCL, DATE, "M1", 5)
            store.reset_progress(TIER, DATE)
            assert store.completed_itineraries("daily-marker", NCL, DATE) == {"M1"}

    def test_is_a_no_op_when_nothing_is_recorded(self, tmp_path):
        with Store(tmp_path / "p.sqlite") as store:
            assert store.reset_progress(TIER, DATE) == 0

    def test_does_not_touch_observations(self, tmp_path):
        """It forgets that work was done, not the data the work produced."""
        path = tmp_path / "p.sqlite"
        with Store(path) as store:
            store.conn.execute(
                "INSERT INTO observations (scrape_ts_utc, scrape_date, line, "
                "sailing_id, cabin_subcategory, market, tier) "
                "VALUES ('2026-09-16T00:00:00+00:00', ?, ?, '1', 'BALCONY', 'US', ?)",
                (DATE, NCL, TIER))
            store.conn.commit()
            seed(store, NCL, ["A"])
            store.reset_progress(TIER, DATE)
            assert store.count_observations() == 1
