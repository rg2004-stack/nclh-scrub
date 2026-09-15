"""Storage layer tests: idempotency, resumability, logging, raw archive."""
import gzip
import json
import os

import pytest

from panel.sources import ncl
from panel.storage import Observation, RawArchive, Store, new_run_id


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "panel.sqlite")
    yield s
    s.close()


def make_obs(**kw) -> Observation:
    base = dict(
        scrape_ts_utc="2026-09-14T21:00:00+00:00",
        scrape_date="2026-09-14",
        line="Norwegian Cruise Line",
        sailing_id="59333",
        cabin_subcategory="BALCONY",
        market="US",
        tier="weekly-full",
        price_per_person=700.0,
        price_total=1400.0,
        price_pppn=100.0,
        nights=7,
    )
    base.update(kw)
    return Observation(**base)


class TestIdempotency:
    def test_rerunning_the_same_day_updates_rather_than_duplicates(self, store):
        store.upsert_observations([make_obs(price_per_person=700.0)])
        store.upsert_observations([make_obs(price_per_person=650.0)])
        assert store.count_observations() == 1
        row = store.conn.execute(
            "SELECT price_per_person FROM observations").fetchone()
        assert row["price_per_person"] == 650.0

    def test_a_later_scrape_date_creates_a_new_row(self, store):
        store.upsert_observations([make_obs()])
        store.upsert_observations([make_obs(scrape_date="2026-09-15")])
        assert store.count_observations() == 2

    def test_each_cabin_subcategory_gets_its_own_row(self, store):
        store.upsert_observations([
            make_obs(cabin_subcategory=c)
            for c in ("INSIDE", "OCEANVIEW", "BALCONY", "SUITE")
        ])
        assert store.count_observations() == 4

    def test_market_is_part_of_the_key(self, store):
        store.upsert_observations([make_obs(market="US"), make_obs(market="CA")])
        assert store.count_observations() == 2

    def test_double_run_of_a_full_payload_is_harmless(
            self, sailings_payload, ncl_line_cfg, store):
        rows, _ = ncl.parse_sailings(
            sailings_payload, line_cfg=ncl_line_cfg, tier="weekly-full",
            scrape_ts="2026-09-14T21:00:00+00:00", scrape_date="2026-09-14",
            source_url="https://example.invalid")
        store.upsert_observations(rows)
        first = store.count_observations()
        store.upsert_observations(rows)
        assert store.count_observations() == first == 90


class TestResumability:
    def test_completed_itineraries_are_remembered(self, store):
        assert store.completed_itineraries("weekly-full", "NCL", "2026-09-14") == set()
        store.mark_itinerary_done("weekly-full", "NCL", "2026-09-14", "JOY3", 90)
        assert store.completed_itineraries("weekly-full", "NCL", "2026-09-14") == {"JOY3"}

    def test_progress_is_scoped_per_tier_and_day(self, store):
        store.mark_itinerary_done("weekly-full", "NCL", "2026-09-14", "JOY3", 90)
        assert store.completed_itineraries("daily-marker", "NCL", "2026-09-14") == set()
        assert store.completed_itineraries("weekly-full", "NCL", "2026-09-15") == set()

    def test_marking_twice_is_safe(self, store):
        store.mark_itinerary_done("weekly-full", "NCL", "2026-09-14", "JOY3", 90)
        store.mark_itinerary_done("weekly-full", "NCL", "2026-09-14", "JOY3", 95)
        rows = store.conn.execute("SELECT * FROM run_progress").fetchall()
        assert len(rows) == 1
        assert rows[0]["n_observations"] == 95


class TestCollectionLog:
    def test_run_is_logged_with_holes_visible(self, store):
        run_id = new_run_id()
        log_id = store.start_run(run_id, "weekly-full", "Norwegian Cruise Line")
        errors = [{"stage": "sailings", "itinerary_code": "BAD1", "error": "500"}]
        store.finish_run(log_id, attempted=10, captured=9, written=540, errors=errors)

        row = store.conn.execute(
            "SELECT * FROM collection_log WHERE id=?", (log_id,)).fetchone()
        assert row["tier"] == "weekly-full"
        assert row["sailings_attempted"] == 10
        assert row["sailings_captured"] == 9
        assert row["observations_written"] == 540
        assert row["finished_ts"] is not None
        assert json.loads(row["errors_json"])[0]["itinerary_code"] == "BAD1"


class TestUnmappedLabels:
    def test_labels_are_logged_and_counted(self, store):
        store.log_unmapped_label("NCL", "SPA_VILLA")
        store.log_unmapped_label("NCL", "SPA_VILLA")
        rows = store.unmapped_labels()
        assert len(rows) == 1
        assert rows[0]["raw_label"] == "SPA_VILLA"
        assert rows[0]["n_seen"] == 2


class TestRawArchive:
    def test_writes_dated_gzip_and_roundtrips(self, tmp_path):
        archive = RawArchive(tmp_path / "raw")
        body = json.dumps({"hello": "world"})
        rel = archive.write("ncl", "sailings", "JOY3MIANASNPIMIA", body)
        full = os.path.join(str(tmp_path / "raw"), rel)
        assert os.path.exists(full)
        assert full.endswith(".json.gz")
        with gzip.open(full, "rt", encoding="utf-8") as fh:
            assert json.load(fh) == {"hello": "world"}

    def test_path_is_partitioned_by_line_and_day(self, tmp_path):
        rel = RawArchive(tmp_path / "raw").write("ncl", "search", "Jan-2027_0", "{}")
        parts = rel.replace("\\", "/").split("/")
        assert parts[0] == "ncl"
        assert len(parts[1]) == 10 and parts[1][4] == "-"  # YYYY-MM-DD

    def test_unsafe_identifiers_do_not_escape_the_archive(self, tmp_path):
        root = tmp_path / "raw"
        rel = RawArchive(root).write("ncl", "sailings", "../../etc/passwd", "{}")
        assert ".." not in rel
        resolved = os.path.realpath(os.path.join(str(root), rel))
        assert resolved.startswith(os.path.realpath(str(root)))


class TestSchema:
    def test_expected_tables_exist(self, store):
        names = {r["name"] for r in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert {"observations", "collection_log", "run_progress",
                "unmapped_labels", "meta"} <= names

    def test_opening_an_existing_db_is_non_destructive(self, tmp_path):
        path = tmp_path / "panel.sqlite"
        first = Store(path)
        first.upsert_observations([make_obs()])
        first.close()
        second = Store(path)
        assert second.count_observations() == 1
        second.close()
