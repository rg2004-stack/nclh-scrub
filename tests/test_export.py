"""Promo dedupe and JSONL export/rebuild.

The panel is collected on ephemeral CI runners, so JSONL is the durable
artifact and SQLite is derived from it. If the round trip is lossy, history is
lost silently -- hence the strict equality checks here.
"""
import gzip
import json
import os

import pytest

from panel import export as ex
from panel.sources import ncl
from panel.storage import Observation, Store


def make_obs(**kw) -> Observation:
    base = dict(
        scrape_ts_utc="2026-09-15T12:00:00+00:00",
        scrape_date="2026-09-15",
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


class TestPromoDedupe:
    def test_promo_body_is_stored_once_per_hash(self, tmp_path):
        store = Store(tmp_path / "p.sqlite")
        body = json.dumps([{"code": "free-wifi", "title": "Free Wi-Fi"}])
        store.upsert_observations([
            make_obs(sailing_id=str(i), promo_hash="abc123", promo_text=body)
            for i in range(50)
        ])
        assert store.count_observations() == 50
        promos = store.promos()
        assert len(promos) == 1
        assert promos[0]["promo_hash"] == "abc123"
        store.close()

    def test_body_is_not_duplicated_onto_observations(self, tmp_path):
        store = Store(tmp_path / "p.sqlite")
        cols = {r[1] for r in store.conn.execute("PRAGMA table_info(observations)")}
        assert "promo_text" not in cols, "promo body must not live on observations"
        assert "promo_hash" in cols
        store.close()

    def test_body_reads_back_by_hash(self, tmp_path):
        store = Store(tmp_path / "p.sqlite")
        body = json.dumps([{"code": "x", "title": "X"}])
        store.upsert_observations([make_obs(promo_hash="h1", promo_text=body)])
        assert store.promo_text("h1") == body
        assert store.promo_text("nope") is None
        store.close()

    def test_distinct_hashes_get_distinct_bodies(self, tmp_path):
        store = Store(tmp_path / "p.sqlite")
        store.upsert_observations([
            make_obs(sailing_id="1", promo_hash="h1", promo_text='["a"]'),
            make_obs(sailing_id="2", promo_hash="h2", promo_text='["b"]'),
        ])
        assert len(store.promos()) == 2
        assert store.promo_text("h1") == '["a"]'
        assert store.promo_text("h2") == '["b"]'
        store.close()

    def test_real_fixture_collapses_many_rows_to_few_bodies(
            self, sailings_payload, ncl_line_cfg, tmp_path):
        rows, _ = ncl.parse_sailings(
            sailings_payload, line_cfg=ncl_line_cfg, tier="weekly-full",
            scrape_ts="2026-09-15T12:00:00+00:00", scrape_date="2026-09-15",
            source_url="https://example.invalid")
        store = Store(tmp_path / "p.sqlite")
        store.upsert_observations(rows)
        with_promo = [r for r in rows if r.promo_hash]
        assert len(with_promo) == 90
        assert len(store.promos()) < len(with_promo), "bodies must be deduped"
        store.close()

    def test_rows_without_promos_are_fine(self, tmp_path):
        store = Store(tmp_path / "p.sqlite")
        store.upsert_observations([make_obs(promo_hash=None, promo_text=None)])
        assert store.promos() == []
        store.close()


class TestExportPaths:
    def test_path_is_unique_per_date_tier_and_line(self):
        a = ex.export_path("root", "2027-03-08", "weekly-full", "Norwegian Cruise Line")
        b = ex.export_path("root", "2027-03-08", "daily-marker", "Norwegian Cruise Line")
        c = ex.export_path("root", "2027-03-08", "weekly-full", "Carnival Cruise Line")
        assert len({a, b, c}) == 3, "concurrent runs must not share a path"

    def test_path_is_partitioned_by_year_and_month(self):
        p = ex.export_path("root", "2027-03-08", "weekly-full", "ncl").replace("\\", "/")
        assert "/2027/03/" in p
        assert p.endswith(".jsonl.gz")


class TestRoundTrip:
    @pytest.fixture
    def populated(self, tmp_path, sailings_payload, ncl_line_cfg):
        rows, _ = ncl.parse_sailings(
            sailings_payload, line_cfg=ncl_line_cfg, tier="weekly-full",
            scrape_ts="2026-09-15T12:00:00+00:00", scrape_date="2026-09-15",
            source_url="https://example.invalid")
        db = tmp_path / "src.sqlite"
        store = Store(db)
        store.upsert_observations(rows)
        store.close()
        return db, rows

    def test_export_then_rebuild_is_lossless(self, populated, tmp_path):
        db, rows = populated
        root = tmp_path / "obs"
        ex.export(str(db), str(root))

        dst = tmp_path / "rebuilt.sqlite"
        total, promos = ex.rebuild(str(dst), str(root))
        assert total == len(rows)

        store = Store(dst)
        assert store.count_observations() == len(rows)
        assert len(store.promos()) > 0
        store.close()

    def test_every_field_survives_the_round_trip(self, populated, tmp_path):
        db, rows = populated
        root = tmp_path / "obs"
        ex.export(str(db), str(root))
        dst = tmp_path / "rebuilt.sqlite"
        ex.rebuild(str(dst), str(root))

        src, out = Store(db), Store(dst)
        cols = [r[1] for r in src.conn.execute("PRAGMA table_info(observations)")
                if r[1] != "id"]
        order = "ORDER BY sailing_id, cabin_subcategory"
        sel = ",".join(cols)
        a = src.conn.execute(f"SELECT {sel} FROM observations {order}").fetchall()
        b = out.conn.execute(f"SELECT {sel} FROM observations {order}").fetchall()
        assert [tuple(r) for r in a] == [tuple(r) for r in b]
        src.close()
        out.close()

    def test_rebuild_is_idempotent(self, populated, tmp_path):
        db, rows = populated
        root = tmp_path / "obs"
        ex.export(str(db), str(root))
        dst = tmp_path / "rebuilt.sqlite"
        ex.rebuild(str(dst), str(root))
        ex.rebuild(str(dst), str(root))
        store = Store(dst)
        assert store.count_observations() == len(rows)
        store.close()

    def test_export_is_valid_gzipped_jsonl(self, populated, tmp_path):
        db, rows = populated
        root = tmp_path / "obs"
        written = ex.export(str(db), str(root))
        obs_files = [p for p in written if not p.endswith(ex.PROMOS_FILE)]
        assert obs_files
        with gzip.open(obs_files[0], "rt", encoding="utf-8") as fh:
            lines = [json.loads(line) for line in fh if line.strip()]
        assert len(lines) == len(rows)
        assert "promo_text" not in lines[0], "body belongs in promos.jsonl.gz"
        assert "promo_hash" in lines[0]

    def test_promo_bodies_land_in_their_own_file(self, populated, tmp_path):
        db, _ = populated
        root = tmp_path / "obs"
        ex.export(str(db), str(root))
        ppath = os.path.join(str(root), ex.PROMOS_FILE)
        assert os.path.exists(ppath)
        with gzip.open(ppath, "rt", encoding="utf-8") as fh:
            promos = [json.loads(line) for line in fh if line.strip()]
        assert promos
        assert {"promo_hash", "promo_text"} <= set(promos[0])

    def test_reexport_merges_rather_than_dropping_older_promos(self, populated, tmp_path):
        db, _ = populated
        root = tmp_path / "obs"
        ex.export(str(db), str(root))
        ppath = os.path.join(str(root), ex.PROMOS_FILE)
        with gzip.open(ppath, "wt", encoding="utf-8") as fh:
            fh.write(json.dumps({"promo_hash": "historic", "promo_text": "old",
                                 "first_seen": "2026-01-01",
                                 "last_seen": "2026-01-01",
                                 "n_seen": 1}) + "\n")
        ex.export(str(db), str(root))
        with gzip.open(ppath, "rt", encoding="utf-8") as fh:
            hashes = {json.loads(line)["promo_hash"] for line in fh if line.strip()}
        assert "historic" in hashes, "re-export must not drop historic promo bodies"


class TestExportFiltering:
    def test_tier_filter_limits_what_is_written(self, tmp_path):
        db = tmp_path / "s.sqlite"
        store = Store(db)
        store.upsert_observations([
            make_obs(sailing_id="1", tier="weekly-full"),
            make_obs(sailing_id="2", tier="daily-marker"),
        ])
        store.close()
        root = tmp_path / "obs"
        written = ex.export(str(db), str(root), tier="daily-marker")
        obs = [p for p in written if not p.endswith(ex.PROMOS_FILE)]
        assert len(obs) == 1
        assert "daily-marker" in obs[0]
