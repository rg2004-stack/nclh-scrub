"""Append-only JSONL export, and rebuild of the SQLite panel from it.

Why this exists
---------------
A GitHub Actions runner has no persistent disk, so a scheduled run must write
its output somewhere durable. Committing `panel.sqlite` back to the repo does
not work at this panel's volume: SQLite is binary, so git stores a full copy on
every commit (no delta compression), ~37,800 rows/week compounds to multiple GB
of repo growth inside a year, and two concurrent runs (weekly + daily) cannot
merge a binary file.

So each run writes an immutable, gzipped JSONL file at a path unique to
(date, tier, line):

    data/observations/2027/03/2027-03-08__weekly-full__ncl.jsonl.gz

Concurrent runs never touch the same path, so they cannot conflict. Promo
bodies are stored once, keyed by hash, in a single small file. SQLite is a
derived artifact: `rebuild` reconstructs it from the JSONL at any time, so the
database itself never needs to be committed.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sqlite3
import sys
from dataclasses import fields
from typing import Iterator, Sequence

from .storage import Observation, Store

DEFAULT_EXPORT_DIR = os.path.join("data", "observations")
PROMOS_FILE = "promos.jsonl.gz"

_OBS_FIELDS = [f.name for f in fields(Observation) if f.name != "promo_text"]


def export_path(root: str, scrape_date: str, tier: str, line_key: str) -> str:
    """One immutable path per (date, tier, line) -- concurrent runs never collide."""
    year, month = scrape_date[:4], scrape_date[5:7]
    safe_line = "".join(c if c.isalnum() else "-" for c in line_key.lower()).strip("-")
    return os.path.join(root, year, month,
                        f"{scrape_date}__{tier}__{safe_line}.jsonl.gz")


def export(db_path: str, root: str = DEFAULT_EXPORT_DIR,
           tier: str | None = None, scrape_date: str | None = None) -> list[str]:
    """Write observations to gzipped JSONL, partitioned by date/tier/line."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    where, params = [], []
    if tier:
        where.append("tier = ?")
        params.append(tier)
    if scrape_date:
        where.append("scrape_date = ?")
        params.append(scrape_date)
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    groups = conn.execute(
        f"SELECT DISTINCT scrape_date, tier, line FROM observations{clause}",
        params).fetchall()

    written: list[str] = []
    for g in groups:
        rows = conn.execute(
            "SELECT * FROM observations "
            "WHERE scrape_date=? AND tier=? AND line=? ORDER BY id",
            (g["scrape_date"], g["tier"], g["line"])).fetchall()
        path = export_path(root, g["scrape_date"], g["tier"], g["line"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8", newline="\n") as fh:
            for r in rows:
                rec = {k: r[k] for k in r.keys() if k != "id"}
                fh.write(json.dumps(rec, ensure_ascii=False,
                                    separators=(",", ":"), sort_keys=True) + "\n")
        written.append(path)
        print(f"  {path}  ({len(rows)} observations, "
              f"{os.path.getsize(path) / 1024:.1f} KB)")

    # Promo bodies: small, deduped by hash, rewritten whole each time.
    promos = conn.execute("SELECT * FROM promos ORDER BY promo_hash").fetchall()
    if promos:
        ppath = os.path.join(root, PROMOS_FILE)
        os.makedirs(root, exist_ok=True)
        merged: dict[str, dict] = {}
        if os.path.exists(ppath):
            for rec in _read_jsonl(ppath):
                merged[rec["promo_hash"]] = rec
        for p in promos:
            merged[p["promo_hash"]] = {k: p[k] for k in p.keys()}
        with gzip.open(ppath, "wt", encoding="utf-8", newline="\n") as fh:
            for h in sorted(merged):
                fh.write(json.dumps(merged[h], ensure_ascii=False,
                                    separators=(",", ":"), sort_keys=True) + "\n")
        written.append(ppath)
        print(f"  {ppath}  ({len(merged)} promo bodies, "
              f"{os.path.getsize(ppath) / 1024:.1f} KB)")

    conn.close()
    return written


def _read_jsonl(path: str) -> Iterator[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def rebuild(db_path: str, root: str = DEFAULT_EXPORT_DIR) -> tuple[int, int]:
    """Reconstruct the SQLite panel from the JSONL archive.

    Idempotent: observations upsert on their natural key, so rebuilding over an
    existing database is safe.
    """
    store = Store(db_path)

    promos = 0
    ppath = os.path.join(root, PROMOS_FILE)
    if os.path.exists(ppath):
        for rec in _read_jsonl(ppath):
            store.conn.execute(
                "INSERT INTO promos (promo_hash, promo_text, first_seen, last_seen, n_seen) "
                "VALUES (?,?,?,?,?) ON CONFLICT (promo_hash) DO UPDATE SET "
                "last_seen=excluded.last_seen, n_seen=excluded.n_seen",
                (rec["promo_hash"], rec["promo_text"], rec["first_seen"],
                 rec["last_seen"], rec.get("n_seen", 1)))
            promos += 1
        store.conn.commit()

    total = 0
    files = sorted(
        os.path.join(dirpath, fn)
        for dirpath, _, fns in os.walk(root)
        for fn in fns
        if fn.endswith(".jsonl.gz") and fn != PROMOS_FILE
    )
    for path in files:
        batch = [Observation(**{k: rec.get(k) for k in _OBS_FIELDS})
                 for rec in _read_jsonl(path)]
        n = store.upsert_observations(batch)
        total += n
        print(f"  {path}  -> {n} observations")

    store.close()
    return total, promos


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m panel.export",
        description="Export the panel to append-only JSONL, or rebuild it from JSONL.")
    p.add_argument("action", choices=["export", "rebuild"])
    p.add_argument("--db", default=os.path.join("data", "panel.sqlite"))
    p.add_argument("--dir", default=DEFAULT_EXPORT_DIR, dest="root")
    p.add_argument("--tier", default=None, help="export only this tier")
    p.add_argument("--date", default=None, help="export only this scrape_date")
    return p


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.action == "export":
        print(f"exporting {args.db} -> {args.root}")
        written = export(args.db, args.root, args.tier, args.date)
        if not written:
            print("  nothing to export")
        return 0
    print(f"rebuilding {args.db} <- {args.root}")
    if not os.path.isdir(args.root):
        print(f"  no export directory at {args.root}", file=sys.stderr)
        return 1
    total, promos = rebuild(args.db, args.root)
    print(f"\nrebuilt {total} observations and {promos} promo bodies into {args.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
