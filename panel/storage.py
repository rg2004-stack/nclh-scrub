"""SQLite storage layer: idempotent upserts, run logging, raw response archive."""
from __future__ import annotations

import gzip
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from .schema import DDL, MIGRATIONS, SCHEMA_VERSION


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(ts: datetime) -> str:
    return ts.replace(microsecond=0).isoformat()


@dataclass
class Observation:
    """One row of `observations`. Field names mirror the columns exactly."""
    scrape_ts_utc: str
    scrape_date: str
    line: str
    sailing_id: str
    cabin_subcategory: str
    market: str
    tier: str
    brand: str | None = None
    ship: str | None = None
    ship_code: str | None = None
    itinerary_code: str | None = None
    package_id: str | None = None
    sail_date: str | None = None
    return_date: str | None = None
    nights: int | None = None
    itinerary_name: str | None = None
    embark_port: str | None = None
    disembark_port: str | None = None
    region: str | None = None
    currency: str | None = None
    cabin_category: str | None = None
    vendor_category_code: str | None = None
    rate_code: str | None = None
    offer_id: str | None = None
    price_total: float | None = None
    price_pppn: float | None = None
    price_per_person: float | None = None
    price_basis: str | None = None
    taxes_fees: float | None = None
    taxes_fees_text: str | None = None
    is_guarantee: int | None = None
    availability_status: str | None = None
    availability_status_raw: str | None = None
    units_remaining: int | None = None
    promo_text: str | None = None
    promo_hash: str | None = None
    source_url: str | None = None
    raw_response_path: str | None = None


# promo_text is carried on the dataclass (parsers produce it) but is NOT an
# observations column: Store splits it into `promos`, keyed by promo_hash.
_PROMO_BODY = "promo_text"
_COLUMNS: Sequence[str] = tuple(
    f.name for f in Observation.__dataclass_fields__.values()  # type: ignore[attr-defined]
    if f.name != _PROMO_BODY
)

_UPSERT = f"""
INSERT INTO observations ({", ".join(_COLUMNS)})
VALUES ({", ".join("?" for _ in _COLUMNS)})
ON CONFLICT (line, sailing_id, cabin_subcategory, market, scrape_date) DO UPDATE SET
  {", ".join(f"{c}=excluded.{c}" for c in _COLUMNS
             if c not in ("line", "sailing_id", "cabin_subcategory", "market", "scrape_date"))}
"""


class Store:
    """Thin wrapper over a SQLite file. Safe to open repeatedly."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = str(path)
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(DDL)
        self._migrate()
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT (key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    def _migrate(self) -> None:
        """Apply additive column migrations to a database built by an older version.

        Only ADD COLUMN steps: existing rows keep their values and the new
        columns read NULL, which is exactly what "this source never had that
        resolution" should look like.
        """
        for _version, table, column, ddl in MIGRATIONS:
            cols = {r[1] for r in self.conn.execute(
                f"PRAGMA table_info({table})").fetchall()}
            if column not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

        # v3: move promo bodies out of observations into `promos`.
        cols = {r[1] for r in self.conn.execute(
            "PRAGMA table_info(observations)").fetchall()}
        if _PROMO_BODY in cols:
            self.conn.execute(
                "INSERT OR IGNORE INTO promos "
                "  (promo_hash, promo_text, first_seen, last_seen, n_seen) "
                "SELECT promo_hash, promo_text, MIN(scrape_ts_utc), "
                "       MAX(scrape_ts_utc), COUNT(*) "
                "FROM observations "
                "WHERE promo_hash IS NOT NULL AND promo_text IS NOT NULL "
                "GROUP BY promo_hash"
            )
            self.conn.execute(
                f"ALTER TABLE observations DROP COLUMN {_PROMO_BODY}")
        self.conn.commit()

    # -- observations -----------------------------------------------------
    def upsert_observations(self, rows: Iterable[Observation]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        self._store_promos(rows)
        payload = [tuple(asdict(r)[c] for c in _COLUMNS) for r in rows]
        self.conn.executemany(_UPSERT, payload)
        self.conn.commit()
        return len(payload)

    def _store_promos(self, rows: list[Observation]) -> None:
        """Store each distinct promo body once, keyed by its hash."""
        bodies: dict[str, tuple[str, str]] = {}
        for r in rows:
            if r.promo_hash and r.promo_text:
                bodies[r.promo_hash] = (r.promo_text, r.scrape_ts_utc)
        if not bodies:
            return
        self.conn.executemany(
            "INSERT INTO promos (promo_hash, promo_text, first_seen, last_seen, n_seen) "
            "VALUES (?,?,?,?,1) "
            "ON CONFLICT (promo_hash) DO UPDATE SET "
            "last_seen=excluded.last_seen, n_seen=promos.n_seen+1",
            [(h, text, ts, ts) for h, (text, ts) in bodies.items()],
        )

    def promo_text(self, promo_hash: str) -> str | None:
        """Read a promo body back by hash."""
        row = self.conn.execute(
            "SELECT promo_text FROM promos WHERE promo_hash=?", (promo_hash,)).fetchone()
        return row["promo_text"] if row else None

    def promos(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM promos ORDER BY n_seen DESC").fetchall()

    def count_observations(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]

    # -- resumability -----------------------------------------------------
    def completed_itineraries(self, tier: str, line: str, scrape_date: str) -> set[str]:
        cur = self.conn.execute(
            "SELECT itinerary_code FROM run_progress "
            "WHERE tier=? AND line=? AND scrape_date=?",
            (tier, line, scrape_date),
        )
        return {r[0] for r in cur.fetchall()}

    def mark_itinerary_done(self, tier: str, line: str, scrape_date: str,
                            itinerary_code: str, n_observations: int) -> None:
        self.conn.execute(
            "INSERT INTO run_progress (tier, line, scrape_date, itinerary_code, "
            "completed_ts, n_observations) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT (tier, line, scrape_date, itinerary_code) DO UPDATE SET "
            "completed_ts=excluded.completed_ts, n_observations=excluded.n_observations",
            (tier, line, scrape_date, itinerary_code, iso(utcnow()), n_observations),
        )
        self.conn.commit()

    # -- unmapped cabin labels -------------------------------------------
    def log_unmapped_label(self, line: str, raw_label: str) -> None:
        now = iso(utcnow())
        self.conn.execute(
            "INSERT INTO unmapped_labels (line, raw_label, first_seen, last_seen, n_seen) "
            "VALUES (?,?,?,?,1) "
            "ON CONFLICT (line, raw_label) DO UPDATE SET "
            "last_seen=excluded.last_seen, n_seen=unmapped_labels.n_seen+1",
            (line, raw_label, now, now),
        )
        self.conn.commit()

    def unmapped_labels(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM unmapped_labels ORDER BY n_seen DESC").fetchall()

    # -- run log ----------------------------------------------------------
    def start_run(self, run_id: str, tier: str, line: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO collection_log (run_id, run_ts, tier, line) VALUES (?,?,?,?)",
            (run_id, iso(utcnow()), tier, line),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, log_id: int, attempted: int, captured: int,
                   written: int, errors: list[dict[str, Any]]) -> None:
        self.conn.execute(
            "UPDATE collection_log SET finished_ts=?, sailings_attempted=?, "
            "sailings_captured=?, observations_written=?, errors_json=? WHERE id=?",
            (iso(utcnow()), attempted, captured, written,
             json.dumps(errors, ensure_ascii=False), log_id),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class RawArchive:
    """Gzipped, dated archive of every raw response, so normalization can be redone."""

    def __init__(self, root: str | os.PathLike[str]):
        self.root = str(root)

    def write(self, line: str, kind: str, ident: str, body: str,
              ts: datetime | None = None) -> str:
        ts = ts or utcnow()
        day = ts.strftime("%Y-%m-%d")
        directory = os.path.join(self.root, line, day)
        os.makedirs(directory, exist_ok=True)
        # No dots: keeps ".." out of archive filenames entirely.
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in ident)[:120]
        name = f"{kind}__{safe}__{ts.strftime('%H%M%S')}.json.gz"
        path = os.path.join(directory, name)
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            fh.write(body)
        return os.path.relpath(path, self.root)


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]
