"""`tier` is part of an observation's identity, and the database must agree.

This is a regression test for a bug that silently destroyed data. The natural
key used to be (line, sailing_id, cabin_subcategory, market, scrape_date) --
no tier. That was safe only while the tiers covered disjoint sail windows.
Widening weekly-full to start 2026-10-01 made it cover the same near-term
sailings daily-marker re-reads every day, so the same cabin on the same day
became the same row in both tiers.

The upsert then did something worse than going stale: `DO UPDATE SET` rewrites
every non-key column, and `tier` was a non-key column. So the losing tier's
rows were not merely overwritten, they were relabelled as the winning tier.
On 2026-09-16 a rebuild turned all 562 daily-marker observations into nothing,
and the count came out 562 short with no error anywhere.

The JSONL archive was never affected -- it is keyed by (date, tier, line) on
the filesystem -- which is the reason the archive and not the database is the
record of what was observed.
"""
import gzip
import json
import sqlite3

import pytest

from panel.export import rebuild
from panel.schema import DDL, NATURAL_KEY
from panel.storage import Observation, Store

NCL = "Norwegian Cruise Line"


def obs(tier, **kw):
    """The SAME cabin on the SAME sailing on the SAME day, in a given tier."""
    row = dict(
        scrape_ts_utc="2026-09-16T06:00:00+00:00", scrape_date="2026-09-16",
        line=NCL, brand="NCL", ship="Norwegian Viva", sailing_id="59198",
        itinerary_code="VIVA7GALCZMRTBBPICMAGAL", sail_date="2026-10-31",
        nights=7, is_package=0, itinerary_nights=7, region="Caribbean",
        market="US", currency="USD", cabin_category="balcony",
        cabin_subcategory="BALCONY", price_total=1400.0, price_pppn=200.0,
        availability_status="available", tier=tier,
    )
    row.update(kw)
    return Observation(**row)


class TestNaturalKey:
    def test_tier_is_in_the_key(self):
        assert "tier" in NATURAL_KEY

    def test_the_table_agrees_with_the_declared_key(self, tmp_path):
        store = Store(str(tmp_path / "p.sqlite"))
        assert set(store._observations_key()) == set(NATURAL_KEY)
        store.close()


class TestTiersCoexist:
    def test_both_tiers_survive_the_same_cabin_on_the_same_day(self, tmp_path):
        store = Store(str(tmp_path / "p.sqlite"))
        store.upsert_observations([obs("weekly-full", price_pppn=200.0)])
        store.upsert_observations([obs("daily-marker", price_pppn=210.0)])
        rows = list(store.conn.execute(
            "SELECT tier, price_pppn FROM observations ORDER BY tier"))
        store.close()
        assert [(r[0], r[1]) for r in rows] == [
            ("daily-marker", 210.0), ("weekly-full", 200.0)]

    def test_a_tier_is_never_relabelled_by_the_other(self, tmp_path):
        """The specific mechanism: `tier` was a non-key column, so DO UPDATE
        rewrote it and the losing row changed identity rather than vanishing."""
        store = Store(str(tmp_path / "p.sqlite"))
        store.upsert_observations([obs("daily-marker")])
        store.upsert_observations([obs("weekly-full")])
        counts = dict(store.conn.execute(
            "SELECT tier, COUNT(*) FROM observations GROUP BY tier"))
        store.close()
        assert counts == {"daily-marker": 1, "weekly-full": 1}

    def test_re_running_one_tier_still_updates_in_place(self, tmp_path):
        """The key must not have become so strict that a same-day re-run
        duplicates instead of refreshing."""
        store = Store(str(tmp_path / "p.sqlite"))
        store.upsert_observations([obs("weekly-full", price_pppn=200.0)])
        store.upsert_observations([obs("weekly-full", price_pppn=250.0)])
        rows = list(store.conn.execute("SELECT price_pppn FROM observations"))
        store.close()
        assert [r[0] for r in rows] == [250.0]


class TestRebuildRoundTrip:
    def write_jsonl(self, root, name, records):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")

    def test_rebuild_restores_every_row_from_both_tiers(self, tmp_path):
        """The count the bug produced was silently short. Pin the round trip."""
        root = tmp_path / "observations"
        daily = [dict(vars(obs("daily-marker", sailing_id=str(i))))
                 for i in range(5)]
        weekly = [dict(vars(obs("weekly-full", sailing_id=str(i))))
                  for i in range(5)]
        # 'daily-marker' sorts before 'weekly-full', which is why the loss was
        # order-dependent and looked like a rebuild quirk rather than a bug.
        self.write_jsonl(root, "2026/09/2026-09-16__daily-marker__ncl.jsonl.gz",
                         daily)
        self.write_jsonl(root, "2026/09/2026-09-16__weekly-full__ncl.jsonl.gz",
                         weekly)
        total, _ = rebuild(str(tmp_path / "p.sqlite"), str(root))
        conn = sqlite3.connect(str(tmp_path / "p.sqlite"))
        stored = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        per_tier = dict(conn.execute(
            "SELECT tier, COUNT(*) FROM observations GROUP BY tier"))
        conn.close()
        assert total == 10
        assert stored == total, "rows were dropped between JSONL and database"
        assert per_tier == {"daily-marker": 5, "weekly-full": 5}


class TestMigrationFromV4:
    def old_db(self, path):
        """A database built with the pre-v5 key."""
        conn = sqlite3.connect(path)
        conn.executescript(DDL.replace(
            "UNIQUE (tier, line, sailing_id, cabin_subcategory, market, scrape_date)",
            "UNIQUE (line, sailing_id, cabin_subcategory, market, scrape_date)"))
        conn.commit()
        conn.close()
        return path

    def test_an_old_database_is_rekeyed_on_open(self, tmp_path):
        path = self.old_db(str(tmp_path / "old.sqlite"))
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        before = [tuple(r[2] for r in conn.execute(f"PRAGMA index_info({i['name']})"))
                  for i in conn.execute("PRAGMA index_list(observations)")
                  if i["unique"] and i["origin"] == "u"]
        conn.close()
        assert before and "tier" not in before[0]

        store = Store(path)
        assert "tier" in store._observations_key()
        store.close()

    def test_migration_preserves_the_rows_that_survived(self, tmp_path):
        path = self.old_db(str(tmp_path / "old.sqlite"))
        conn = sqlite3.connect(path)
        cols = [f.name for f in __import__("dataclasses").fields(Observation)
                if f.name != "promo_text"]
        rec = obs("weekly-full")
        conn.execute(
            f"INSERT INTO observations ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))})",
            [getattr(rec, c) for c in cols])
        conn.commit()
        conn.close()

        store = Store(path)
        rows = list(store.conn.execute(
            "SELECT tier, sailing_id, price_pppn FROM observations"))
        # And the previously-colliding tier can now be added alongside it.
        store.upsert_observations([obs("daily-marker")])
        after = dict(store.conn.execute(
            "SELECT tier, COUNT(*) FROM observations GROUP BY tier"))
        store.close()
        assert [tuple(r) for r in rows] == [("weekly-full", "59198", 200.0)]
        assert after == {"daily-marker": 1, "weekly-full": 1}

    def test_migration_is_idempotent(self, tmp_path):
        path = self.old_db(str(tmp_path / "old.sqlite"))
        for _ in range(3):
            store = Store(path)
            key = store._observations_key()
            store.close()
            assert "tier" in key
