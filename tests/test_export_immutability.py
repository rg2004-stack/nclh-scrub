"""A dated observation file records what was seen that day, and stays that way.

The defect these tests pin: `export --tier weekly-full` selected every
(scrape_date, tier, line) group in the database and overwrote each path, so
correcting rows locally and re-exporting silently rewrote past days. The only
trace was an unexplained binary diff -- no record of what changed or why.

Rewriting history is now an explicit, named, logged action.
"""
import gzip
import json
import os
import sqlite3

import pytest

from panel import export as ex
from panel.schema import DDL

NCL = "Norwegian Cruise Line"


def make_row(**kw):
    row = {
        "scrape_ts_utc": "2026-09-15T18:00:00+00:00",
        "scrape_date": "2026-09-15",
        "line": NCL, "sailing_id": "1", "cabin_subcategory": "BALCONY",
        "market": "US", "tier": "weekly-full",
        "cabin_category": "balcony", "region": "Caribbean",
        "availability_status": "available", "price_pppn": 200.0,
    }
    row.update(kw)
    return row


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "panel.sqlite")
    conn = sqlite3.connect(path)
    conn.executescript(DDL)
    conn.commit()
    conn.close()
    return path


def insert(db, rows):
    conn = sqlite3.connect(db)
    for r in rows:
        conn.execute(f"INSERT OR REPLACE INTO observations ({', '.join(r)}) "
                     f"VALUES ({', '.join('?' * len(r))})", list(r.values()))
    conn.commit()
    conn.close()


def mutate(db, sql):
    conn = sqlite3.connect(db)
    conn.execute(sql)
    conn.commit()
    conn.close()


class TestDatedFilesAreImmutable:
    def test_first_export_writes_the_file(self, db, tmp_path):
        insert(db, [make_row()])
        written = ex.export(db, str(tmp_path / "obs"))
        assert any("2026-09-15" in w for w in written)

    def test_re_exporting_identical_rows_is_a_no_op(self, db, tmp_path):
        root = str(tmp_path / "obs")
        insert(db, [make_row()])
        ex.export(db, root)
        path = ex.export_path(root, "2026-09-15", "weekly-full", NCL)
        before = open(path, "rb").read()
        ex.export(db, root)
        # Byte-identical: gzip is written with mtime=0, so an unchanged export
        # does not show up as a diff in git.
        assert open(path, "rb").read() == before

    def test_changed_content_is_refused(self, db, tmp_path):
        root = str(tmp_path / "obs")
        insert(db, [make_row()])
        ex.export(db, root)
        mutate(db, "UPDATE observations SET price_pppn = 999.0")
        with pytest.raises(ex.HistoryRewriteRefused) as exc:
            ex.export(db, root)
        assert "price_pppn" in str(exc.value)
        assert "--amend" in str(exc.value)

    def test_a_refusal_writes_nothing_at_all(self, db, tmp_path):
        """A refusal must not leave a half-finished export behind."""
        root = str(tmp_path / "obs")
        insert(db, [make_row()])
        ex.export(db, root)
        path = ex.export_path(root, "2026-09-15", "weekly-full", NCL)
        before = open(path, "rb").read()
        # One corrected old row plus one genuinely new day.
        mutate(db, "UPDATE observations SET price_pppn = 999.0")
        insert(db, [make_row(scrape_date="2026-09-22", sailing_id="2")])
        with pytest.raises(ex.HistoryRewriteRefused):
            ex.export(db, root)
        assert open(path, "rb").read() == before
        assert not os.path.exists(
            ex.export_path(root, "2026-09-22", "weekly-full", NCL))

    def test_a_new_date_is_never_blocked_on_its_own(self, db, tmp_path):
        root = str(tmp_path / "obs")
        insert(db, [make_row()])
        ex.export(db, root)
        insert(db, [make_row(scrape_date="2026-09-22", sailing_id="2")])
        ex.export(db, root)      # no correction pending, so no refusal
        assert os.path.exists(
            ex.export_path(root, "2026-09-22", "weekly-full", NCL))


class TestAmendIsExplicitAndLogged:
    def test_amend_requires_a_reason(self, db, tmp_path):
        insert(db, [make_row()])
        with pytest.raises(ValueError, match="requires a reason"):
            ex.export(db, str(tmp_path / "obs"), amend=True)

    def test_amend_requires_a_non_blank_reason(self, db, tmp_path):
        insert(db, [make_row()])
        with pytest.raises(ValueError, match="requires a reason"):
            ex.export(db, str(tmp_path / "obs"), amend=True, reason="   ")

    def test_amend_rewrites_and_logs(self, db, tmp_path):
        root = str(tmp_path / "obs")
        insert(db, [make_row()])
        ex.export(db, root)
        mutate(db, "UPDATE observations SET availability_status = 'solo_only'")
        ex.export(db, root, amend=True, reason="Studio cabins are not scarcity")

        ledger = os.path.join(root, ex.CORRECTIONS_FILE)
        assert os.path.exists(ledger)
        entries = [json.loads(l) for l in open(ledger, encoding="utf-8")]
        assert len(entries) == 1
        e = entries[0]
        assert e["reason"] == "Studio cabins are not scarcity"
        assert e["tool"] == "panel.export"
        assert e["corrected_at_utc"].endswith("+00:00")
        f = e["files"][0]
        assert f["fields_changed"] == {"availability_status": 1}
        assert f["rows_before"] == f["rows_after"] == 1
        assert f["sha256_before"] != f["sha256_after"]
        assert len(f["sha256_after"]) == 64

    def test_ledger_is_plain_text_and_append_only(self, db, tmp_path):
        """Readable in the GitHub UI, and a second correction does not erase
        the first -- the whole point is an accumulating record."""
        root = str(tmp_path / "obs")
        insert(db, [make_row()])
        ex.export(db, root)
        for i, status in enumerate(("limited", "sold_out")):
            mutate(db, f"UPDATE observations SET availability_status = '{status}'")
            ex.export(db, root, amend=True, reason=f"correction {i}")
        entries = [json.loads(l)
                   for l in open(os.path.join(root, ex.CORRECTIONS_FILE),
                                 encoding="utf-8")]
        assert [e["reason"] for e in entries] == ["correction 0", "correction 1"]

    def test_amend_records_row_count_changes(self, db, tmp_path):
        root = str(tmp_path / "obs")
        insert(db, [make_row()])
        ex.export(db, root)
        insert(db, [make_row(sailing_id="9")])     # same date, extra row
        ex.export(db, root, amend=True, reason="late arrival")
        e = [json.loads(l) for l in
             open(os.path.join(root, ex.CORRECTIONS_FILE), encoding="utf-8")][0]
        f = e["files"][0]
        assert (f["rows_before"], f["rows_after"]) == (1, 2)
        assert f["fields_changed"] == {"<row added>": 1}


class TestRoundTripSurvives:
    def test_amended_file_still_rebuilds(self, db, tmp_path):
        root = str(tmp_path / "obs")
        insert(db, [make_row()])
        ex.export(db, root)
        mutate(db, "UPDATE observations SET is_package = 1")
        ex.export(db, root, amend=True, reason="package backfill")

        out = str(tmp_path / "rebuilt.sqlite")
        total, _ = ex.rebuild(out, root)
        assert total == 1
        conn = sqlite3.connect(out)
        assert conn.execute(
            "SELECT is_package FROM observations").fetchone()[0] == 1
