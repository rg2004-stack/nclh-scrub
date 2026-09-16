"""`panel.export csv` -- raw rows for a spreadsheet, on demand.

Two properties matter. The file must open cleanly in Excel (bare header row,
nothing above it), and a filtered slice must not be able to pass itself off as
the whole panel -- hence the provenance sidecar.
"""
import csv
import json
import os
import sqlite3

import pytest

from panel import export as ex
from panel.schema import DDL

NCL = "Norwegian Cruise Line"
CCL = "Carnival Cruise Line"


def make_row(**kw):
    row = {
        "scrape_ts_utc": "2026-09-16T06:00:00+00:00",
        "scrape_date": "2026-09-16",
        "line": NCL, "sailing_id": "1", "cabin_subcategory": "BALCONY",
        "market": "US", "tier": "weekly-full", "cabin_category": "balcony",
        "region": "Caribbean", "sail_date": "2027-02-01", "nights": 7,
        "is_package": 0, "price_pppn": 200.0, "currency": "USD",
        "availability_status": "available",
    }
    row.update(kw)
    return row


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "panel.sqlite")
    conn = sqlite3.connect(path)
    conn.executescript(DDL)
    rows = [
        make_row(sailing_id="1"),
        make_row(sailing_id="2", line=CCL, price_pppn=100.0),
        make_row(sailing_id="3", region="Alaska", price_pppn=300.0),
        make_row(sailing_id="4", is_package=1, price_pppn=800.0),
        make_row(sailing_id="5", scrape_date="2026-09-15", price_pppn=190.0),
        make_row(sailing_id="6", cabin_category="suite",
                 cabin_subcategory="HAVEN", price_pppn=500.0),
    ]
    for r in rows:
        conn.execute(f"INSERT INTO observations ({', '.join(r)}) "
                     f"VALUES ({', '.join('?' * len(r))})", list(r.values()))
    conn.commit()
    conn.close()
    return path


def read(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


class TestPlainCsv:
    def test_header_is_the_first_line_with_no_preamble(self, db, tmp_path):
        out = str(tmp_path / "o.csv")
        ex.csv_export(db, out)
        with open(out, encoding="utf-8-sig") as fh:
            first = fh.readline()
        assert not first.startswith("#")
        assert first.split(",")[0] == "scrape_ts_utc"

    def test_writes_every_row_when_unfiltered(self, db, tmp_path):
        out, n = ex.csv_export(db, str(tmp_path / "o.csv"))
        assert n == 6
        assert len(read(out)) == 6

    def test_id_column_is_dropped_by_default(self, db, tmp_path):
        out, _ = ex.csv_export(db, str(tmp_path / "o.csv"))
        assert "id" not in read(out)[0]

    def test_utf8_bom_so_excel_reads_it_correctly(self, db, tmp_path):
        out, _ = ex.csv_export(db, str(tmp_path / "o.csv"))
        assert open(out, "rb").read(3) == b"\xef\xbb\xbf"


class TestFilters:
    def test_by_date(self, db, tmp_path):
        _, n = ex.csv_export(db, str(tmp_path / "o.csv"), scrape_date="2026-09-15")
        assert n == 1

    def test_by_line_and_region(self, db, tmp_path):
        _, n = ex.csv_export(db, str(tmp_path / "o.csv"), line=[NCL],
                             region=["Caribbean"])
        assert n == 4        # excludes the Carnival row and the Alaska row

    def test_repeatable_filters_are_or_ed(self, db, tmp_path):
        _, n = ex.csv_export(db, str(tmp_path / "o.csv"),
                             region=["Caribbean", "Alaska"])
        assert n == 6

    def test_cruise_only_excludes_packages(self, db, tmp_path):
        _, n = ex.csv_export(db, str(tmp_path / "o.csv"), product="cruise_only")
        assert n == 5

    def test_package_only_selects_them(self, db, tmp_path):
        _, n = ex.csv_export(db, str(tmp_path / "o.csv"), product="package_only")
        assert n == 1

    def test_sail_date_range(self, db, tmp_path):
        _, n = ex.csv_export(db, str(tmp_path / "o.csv"), sail_from="2027-03-01")
        assert n == 0

    def test_unknown_filter_is_rejected(self, db, tmp_path):
        with pytest.raises(ValueError, match="unknown filter"):
            ex.csv_export(db, str(tmp_path / "o.csv"), ship="Norwegian Joy")

    def test_unknown_product_is_rejected(self, db, tmp_path):
        with pytest.raises(ValueError, match="unknown product filter"):
            ex.csv_export(db, str(tmp_path / "o.csv"), product="sometimes")

    def test_empty_result_still_writes_a_header(self, db, tmp_path):
        out, n = ex.csv_export(db, str(tmp_path / "o.csv"), region=["Bermuda"])
        assert n == 0
        with open(out, encoding="utf-8-sig") as fh:
            assert fh.readline().strip()


class TestColumns:
    def test_subset_is_honoured_in_order(self, db, tmp_path):
        out, _ = ex.csv_export(db, str(tmp_path / "o.csv"),
                               columns=["line", "price_pppn", "sail_date"])
        assert list(read(out)[0]) == ["line", "price_pppn", "sail_date"]

    def test_unknown_column_is_rejected_by_name(self, db, tmp_path):
        with pytest.raises(ValueError, match="nonsense"):
            ex.csv_export(db, str(tmp_path / "o.csv"), columns=["nonsense"])

    def test_promo_text_is_joined_on_request(self, db, tmp_path):
        out, _ = ex.csv_export(db, str(tmp_path / "o.csv"),
                               columns=["line"], with_promos=True)
        assert list(read(out)[0]) == ["line", "promo_text"]


class TestProvenanceSidecar:
    def test_sidecar_records_filters_and_count(self, db, tmp_path):
        out, n = ex.csv_export(db, str(tmp_path / "o.csv"),
                               region=["Alaska"], product="cruise_only")
        meta = json.load(open(out + ".meta.json", encoding="utf-8"))
        assert meta["rows"] == n == 1
        assert meta["filters"]["region"] == ["Alaska"]
        assert meta["filters"]["product"] == "cruise_only"
        assert "SELECT" in meta["sql"]
        assert meta["generated_at_utc"].endswith("+00:00")

    def test_sidecar_can_be_suppressed(self, db, tmp_path):
        out, _ = ex.csv_export(db, str(tmp_path / "o.csv"), write_meta=False)
        assert not os.path.exists(out + ".meta.json")

    def test_sidecar_warns_that_a_slice_is_not_the_panel(self, db, tmp_path):
        out, _ = ex.csv_export(db, str(tmp_path / "o.csv"), region=["Alaska"])
        meta = json.load(open(out + ".meta.json", encoding="utf-8"))
        assert "not the panel" in meta["note"]


class TestDefaultNaming:
    def test_filename_describes_the_slice(self, db, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        out, _ = ex.csv_export(db, None, scrape_date="2026-09-16",
                               tier="weekly-full", region=["Alaska"],
                               product="cruise_only")
        name = os.path.basename(out)
        for bit in ("2026-09-16", "weekly-full", "alaska", "cruise_only"):
            assert bit in name

    def test_lands_in_the_outputs_directory(self, db, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        out, _ = ex.csv_export(db, None)
        assert os.path.dirname(out) == ex.CSV_DEFAULT_DIR


class TestCli:
    def test_csv_action_runs(self, db, tmp_path):
        rc = ex.run(["csv", "--db", db, "--out", str(tmp_path / "o.csv"),
                     "--region", "Alaska"])
        assert rc == 0
        assert os.path.exists(tmp_path / "o.csv")

    def test_bad_column_exits_two(self, db, tmp_path):
        rc = ex.run(["csv", "--db", db, "--out", str(tmp_path / "o.csv"),
                     "--columns", "nope"])
        assert rc == 2
