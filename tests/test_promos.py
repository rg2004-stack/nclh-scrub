"""Promo decoding, the reference table, and offer-level churn.

A promo_hash identifies a BUNDLE of offers. That is the right unit for
detecting that something changed and the wrong unit for reading one: nobody
can act on `0b0c2413...`. These tests pin the decode, and pin that churn is
measured as REACH on common support -- the same cabins on both dates -- because
share-of-the-book moves whenever the book changes, and over the 2026-09-15/16
scope widening the unrestricted figures showed a 24-point collapse that did
not happen.
"""
import itertools
import json
import sqlite3

import pytest

from panel import analysis as an
from panel.schema import DDL

NCL = "Norwegian Cruise Line"
CCL = "Carnival Cruise Line"
_ids = itertools.count(1)


def offer(code, title="", inclusion="Mandatory", otype="auto-included-offer",
          featured=False):
    return {"code": code, "title": title or code.replace("-", " ").title(),
            "shortTitle": title or code.replace("-", " ").title(),
            "description": f"{code} description", "inclusion": inclusion,
            "offerType": otype, "isFeatured": featured}


def bundle(*codes):
    """A promo payload plus the hash the panel would key it by."""
    payload = json.dumps([offer(c) for c in codes])
    return f"h::{'+'.join(codes)}", payload


def row(**kw):
    r = {
        "scrape_ts_utc": "2026-09-15T18:00:00+00:00", "scrape_date": "2026-09-15",
        "line": NCL, "brand": "NCL", "ship": "Norwegian Joy",
        "sailing_id": str(next(_ids)), "sail_date": "2027-02-01", "nights": 7,
        "is_package": 0, "region": "Caribbean", "market": "US",
        "currency": "USD", "cabin_category": "balcony",
        "cabin_subcategory": "BALCONY", "price_pppn": 200.0,
        "price_total": 1400.0, "availability_status": "available",
        "tier": "weekly-full",
    }
    r.update(kw)
    return r


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "p.sqlite"))
    c.row_factory = sqlite3.Row
    c.executescript(DDL)
    return c


def add_promos(conn, *bundles):
    for h, payload in bundles:
        conn.execute("INSERT OR REPLACE INTO promos "
                     "(promo_hash, promo_text, first_seen, last_seen, n_seen) "
                     "VALUES (?,?,?,?,1)", (h, payload, "2026-09-15", "2026-09-16"))
    conn.commit()


def insert(conn, rows):
    for r in rows:
        conn.execute(f"INSERT INTO observations ({', '.join(r)}) "
                     f"VALUES ({', '.join('?' * len(r))})", list(r.values()))
    conn.commit()


# -- decoding ---------------------------------------------------------------

class TestDecode:
    def test_a_bundle_decodes_to_its_offers(self):
        _, payload = bundle("kids-sail-free", "free-wifi")
        out = an.decode_promo(payload)
        assert [o["code"] for o in out] == ["kids-sail-free", "free-wifi"]
        assert out[0]["title"] and out[0]["inclusion"] == "Mandatory"

    def test_empty_and_none_decode_to_nothing(self):
        assert an.decode_promo(None) == [] and an.decode_promo("") == []

    def test_unparseable_payload_is_flagged_not_dropped_silently(self):
        out = an.decode_promo("{not json")
        assert len(out) == 1 and out[0]["code"] == "<unparseable>"

    def test_a_bare_object_is_accepted(self):
        out = an.decode_promo(json.dumps(offer("solo")))
        assert [o["code"] for o in out] == ["solo"]

    def test_an_offer_without_a_code_is_named_not_blank(self):
        out = an.decode_promo(json.dumps([{"title": "Mystery"}]))
        assert out[0]["code"] == "<no code>"


# -- reference table --------------------------------------------------------

class TestPromoReference:
    def test_one_row_per_offer_not_per_bundle(self, conn):
        h1, p1 = bundle("kids-sail-free", "free-wifi")
        h2, p2 = bundle("kids-sail-free")
        add_promos(conn, (h1, p1), (h2, p2))
        insert(conn, [row(promo_hash=h1), row(promo_hash=h2)])
        res = an.promo_reference(conn, tier="weekly-full")
        codes = {r["code"]: r for r in res.rows}
        assert set(codes) == {"kids-sail-free", "free-wifi"}
        assert codes["kids-sail-free"]["bundles"] == 2
        assert codes["kids-sail-free"]["cells"] == 2
        assert codes["free-wifi"]["cells"] == 1

    def test_reach_is_a_share_of_all_that_lines_cells(self, conn):
        h, p = bundle("kids-sail-free")
        add_promos(conn, (h, p))
        insert(conn, [row(promo_hash=h), row(promo_hash=h),
                      row(promo_hash=None), row(promo_hash=None)])
        res = an.promo_reference(conn, tier="weekly-full")
        assert res.rows[0]["share_of_line_cells"] == 0.5

    def test_scope_columns_show_a_narrow_offer_is_narrow(self, conn):
        h, p = bundle("one-ship-only")
        add_promos(conn, (h, p))
        insert(conn, [row(promo_hash=h, ship="Joy", region="Caribbean"),
                      row(promo_hash=h, ship="Joy", region="Caribbean")])
        r = an.promo_reference(conn, tier="weekly-full").rows[0]
        assert r["n_ships"] == 1 and r["regions"] == "Caribbean"

    def test_the_text_is_readable_not_a_hash(self, conn):
        h, p = bundle("kids-sail-free")
        add_promos(conn, (h, p))
        insert(conn, [row(promo_hash=h)])
        r = an.promo_reference(conn, tier="weekly-full").rows[0]
        assert "kids-sail-free" == r["code"]
        assert r["title"] == "Kids Sail Free"
        assert r["description"]
        assert "h::" not in json.dumps(r)

    def test_a_silent_line_is_declared_a_source_gap(self, conn):
        h, p = bundle("kids-sail-free")
        add_promos(conn, (h, p))
        insert(conn, [row(promo_hash=h), row(line=CCL, promo_hash=None)])
        res = an.promo_reference(conn, tier="weekly-full")
        assert any("NOT ALL LINES PUBLISH OFFERS" in c for c in res.basis.caveats)


# -- churn ------------------------------------------------------------------

class TestPromoDiff:
    def two_dates(self, conn, before_codes, after_codes, n=10):
        pairs = {}
        for codes in (before_codes, after_codes):
            if codes is not None:
                h, p = bundle(*codes)
                pairs[h] = p
        add_promos(conn, *pairs.items())
        hb = f"h::{'+'.join(before_codes)}" if before_codes else None
        ha = f"h::{'+'.join(after_codes)}" if after_codes else None
        rows = []
        for i in range(n):
            rows.append(row(sailing_id=f"S{i}", scrape_date="2026-09-15",
                            promo_hash=hb))
            rows.append(row(sailing_id=f"S{i}", scrape_date="2026-09-22",
                            promo_hash=ha))
        insert(conn, rows)
        return an.promo_diff(conn, tier="weekly-full", min_cells=1)

    def test_an_offer_appearing_reads_as_new(self, conn):
        res = self.two_dates(conn, ["free-wifi"], ["free-wifi", "kids-sail-free"])
        d = {r["code"]: r for r in res.rows}
        assert d["kids-sail-free"]["status"] == "NEW"
        assert d["kids-sail-free"]["delta_share_pp"] == 100.0
        assert d["free-wifi"]["status"] == "unchanged"

    def test_an_offer_disappearing_reads_as_withdrawn(self, conn):
        res = self.two_dates(conn, ["free-wifi", "kids-sail-free"], ["free-wifi"])
        d = {r["code"]: r for r in res.rows}
        assert d["kids-sail-free"]["status"] == "WITHDRAWN"
        assert d["kids-sail-free"]["share_after"] == 0.0

    def test_the_row_carries_a_readable_title(self, conn):
        res = self.two_dates(conn, ["free-wifi"], ["free-wifi"])
        assert res.rows[0]["title"] == "Free Wifi"

    def test_cells_entering_cannot_create_churn(self, conn):
        """A widened collection scope must not read as a promo change."""
        h1, p1 = bundle("free-wifi")
        add_promos(conn, (h1, p1))
        rows = []
        for i in range(10):            # same cabins, same offer, both dates
            rows.append(row(sailing_id=f"S{i}", scrape_date="2026-09-15",
                            promo_hash=h1))
            rows.append(row(sailing_id=f"S{i}", scrape_date="2026-09-22",
                            promo_hash=h1))
        for i in range(40):            # a widened window, none carrying it
            rows.append(row(sailing_id=f"NEW{i}", scrape_date="2026-09-22",
                            promo_hash=None))
        insert(conn, rows)
        r = an.promo_diff(conn, tier="weekly-full", min_cells=1).rows[0]
        assert r["status"] == "unchanged"
        assert r["delta_share_pp"] == 0.0
        assert r["naive_share_after"] == 0.2      # the contaminated read
        assert r["mix_effect_pp"] == -80.0

    def test_a_genuine_withdrawal_still_shows_through(self, conn):
        h1, p1 = bundle("free-wifi")
        h2, p2 = bundle("other")
        add_promos(conn, (h1, p1), (h2, p2))
        rows = []
        for i in range(10):
            rows.append(row(sailing_id=f"S{i}", scrape_date="2026-09-15",
                            promo_hash=h1))
            rows.append(row(sailing_id=f"S{i}", scrape_date="2026-09-22",
                            promo_hash=h2))
        insert(conn, rows)
        d = {r["code"]: r for r in
             an.promo_diff(conn, tier="weekly-full", min_cells=1).rows}
        assert d["free-wifi"]["status"] == "WITHDRAWN"
        assert d["other"]["status"] == "NEW"

    def test_no_shared_cabins_means_no_churn_claim(self, conn):
        h, p = bundle("free-wifi")
        add_promos(conn, (h, p))
        insert(conn, [row(sailing_id="A", scrape_date="2026-09-15", promo_hash=h),
                      row(sailing_id="B", scrape_date="2026-09-22", promo_hash=None)])
        res = an.promo_diff(conn, tier="weekly-full", min_cells=1)
        assert res.rows == []
        assert any("NO CHURN OBSERVABLE" in c for c in res.basis.caveats)

    def test_one_sided_coverage_is_declared(self, conn):
        res = self.two_dates(conn, ["free-wifi"], ["free-wifi"])
        assert any(c.startswith("ONE-SIDED") for c in res.basis.caveats)

    def test_reach_moves_under_two_points_are_not_called_a_change(self, conn):
        h1, p1 = bundle("free-wifi")
        h2, p2 = bundle("free-wifi", "extra")
        add_promos(conn, (h1, p1), (h2, p2))
        rows = []
        for i in range(100):
            rows.append(row(sailing_id=f"S{i}", scrape_date="2026-09-15",
                            promo_hash=h1))
            rows.append(row(sailing_id=f"S{i}", scrape_date="2026-09-22",
                            promo_hash=h2 if i == 0 else h1))
        insert(conn, rows)
        d = {r["code"]: r for r in
             an.promo_diff(conn, tier="weekly-full", min_cells=1).rows}
        assert d["extra"]["status"] == "NEW"        # it did appear
        assert d["extra"]["delta_share_pp"] == 1.0  # but on 1% of cabins
