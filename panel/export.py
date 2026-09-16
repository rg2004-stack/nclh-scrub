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

Dated files are immutable, and that is enforced here
----------------------------------------------------
"Immutable" used to be a claim in this docstring that the code did not check.
`export --tier weekly-full` selected every (scrape_date, tier, line) group in
the database and overwrote each path, so a normal-looking export could silently
rewrite a past day's observations. It did: correcting `is_package` and
`solo_only` in the local database and re-exporting rewrote 2026-09-15 with no
record of what changed or why, leaving only an unexplained binary diff.

So a write that would change the CONTENT of an existing dated file is refused.
Rewriting history requires `--amend --reason "..."`, which also appends an entry
to a plain-text ledger committed next to the data:

    data/observations/CORRECTIONS.jsonl

The ledger records, per correction: when, why, by which tool, which files, the
row count, the sha256 before and after, and which fields changed. A future
reader hitting a binary diff in `git log` can look up that commit's entry and
find out what was corrected and why, instead of guessing.

Output is deterministic: gzip is written with mtime=0, so identical rows
produce identical bytes and re-exporting unchanged data is a no-op in git
rather than a spurious diff.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from dataclasses import fields
from typing import Iterator, Sequence

from .storage import Observation, Store

DEFAULT_EXPORT_DIR = os.path.join("data", "observations")
PROMOS_FILE = "promos.jsonl.gz"
CORRECTIONS_FILE = "CORRECTIONS.jsonl"   # plain text: readable in the GitHub UI


class HistoryRewriteRefused(Exception):
    """Raised when an export would change an existing dated file's content."""

_OBS_FIELDS = [f.name for f in fields(Observation) if f.name != "promo_text"]


def _encode(records: Sequence[dict]) -> bytes:
    """The canonical JSONL body for a set of rows, before compression."""
    buf = io.StringIO()
    for rec in records:
        buf.write(json.dumps(rec, ensure_ascii=False,
                             separators=(",", ":"), sort_keys=True) + "\n")
    return buf.getvalue().encode("utf-8")


def _write_gz(path: str, body: bytes) -> None:
    """Deterministic gzip: mtime=0 so identical rows give identical bytes."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            gz.write(body)


def _read_gz(path: str) -> bytes | None:
    if not os.path.exists(path):
        return None
    with gzip.open(path, "rb") as fh:
        return fh.read()


def _sha(body: bytes | None) -> str | None:
    return hashlib.sha256(body).hexdigest() if body is not None else None


def _changed_fields(before: bytes, after: bytes) -> dict[str, int]:
    """Which fields differ, and in how many rows. Keyed by natural key."""
    def index(body: bytes) -> dict[tuple, dict]:
        out = {}
        for line in body.decode("utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            out[(r.get("line"), r.get("sailing_id"), r.get("cabin_subcategory"),
                 r.get("market"), r.get("scrape_date"))] = r
        return out

    a, b = index(before), index(after)
    counts: dict[str, int] = {}
    for key, new in b.items():
        old = a.get(key)
        if old is None:
            counts["<row added>"] = counts.get("<row added>", 0) + 1
            continue
        for field in set(new) | set(old):
            if new.get(field) != old.get(field):
                counts[field] = counts.get(field, 0) + 1
    for key in a.keys() - b.keys():
        counts["<row removed>"] = counts.get("<row removed>", 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def log_correction(root: str, *, reason: str, tool: str,
                   entries: Sequence[dict]) -> str:
    """Append one correction record to the committed ledger."""
    path = os.path.join(root, CORRECTIONS_FILE)
    os.makedirs(root, exist_ok=True)
    record = {
        "corrected_at_utc": datetime.now(timezone.utc)
                            .replace(microsecond=0).isoformat(),
        "reason": reason,
        "tool": tool,
        "files": list(entries),
    }
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def export_path(root: str, scrape_date: str, tier: str, line_key: str) -> str:
    """One immutable path per (date, tier, line) -- concurrent runs never collide."""
    year, month = scrape_date[:4], scrape_date[5:7]
    safe_line = "".join(c if c.isalnum() else "-" for c in line_key.lower()).strip("-")
    return os.path.join(root, year, month,
                        f"{scrape_date}__{tier}__{safe_line}.jsonl.gz")


def export(db_path: str, root: str = DEFAULT_EXPORT_DIR,
           tier: str | None = None, scrape_date: str | None = None,
           amend: bool = False, reason: str | None = None,
           tool: str = "panel.export") -> list[str]:
    """Write observations to gzipped JSONL, partitioned by date/tier/line.

    A dated file is immutable. If the rows for an existing (date, tier, line)
    would produce different content, the write is refused unless `amend` is set
    with a `reason`, and an amended write is recorded in CORRECTIONS.jsonl.
    """
    if amend and not (reason or "").strip():
        raise ValueError("amend requires a reason: it is what the ledger records")
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

    # Plan first, write second: a refusal must not leave a half-written export.
    plan: list[dict] = []
    for g in groups:
        rows = conn.execute(
            "SELECT * FROM observations "
            "WHERE scrape_date=? AND tier=? AND line=? ORDER BY id",
            (g["scrape_date"], g["tier"], g["line"])).fetchall()
        path = export_path(root, g["scrape_date"], g["tier"], g["line"])
        body = _encode([{k: r[k] for k in r.keys() if k != "id"} for r in rows])
        before = _read_gz(path)
        if before is None:
            state = "new"
        elif before == body:
            state = "unchanged"
        else:
            state = "changed"
        plan.append({"path": path, "body": body, "before": before,
                     "state": state, "rows": len(rows)})

    changed = [p for p in plan if p["state"] == "changed"]
    if changed and not amend:
        conn.close()
        detail = "\n".join(
            f"    {p['path']}\n"
            f"      rows {len(_read_gz(p['path']).decode().splitlines())} -> {p['rows']}, "
            f"fields changed: {_changed_fields(p['before'], p['body'])}"
            for p in changed)
        raise HistoryRewriteRefused(
            f"refusing to rewrite {len(changed)} existing dated file(s):\n{detail}\n"
            "  A dated file records what was observed that day. If this change is\n"
            "  a deliberate correction, re-run with:\n"
            '    python -m panel.export export --amend --reason "what and why"\n'
            "  which writes the file and logs the correction to "
            f"{os.path.join(root, CORRECTIONS_FILE)}.")

    written: list[str] = []
    ledger: list[dict] = []
    for p in plan:
        if p["state"] == "unchanged":
            written.append(p["path"])
            print(f"  {p['path']}  ({p['rows']} observations, unchanged)")
            continue
        if p["state"] == "changed":
            ledger.append({
                "path": p["path"].replace(os.sep, "/"),
                "rows_before": len(p["before"].decode("utf-8").splitlines()),
                "rows_after": p["rows"],
                "sha256_before": _sha(p["before"]),
                "sha256_after": _sha(p["body"]),
                "fields_changed": _changed_fields(p["before"], p["body"]),
            })
        _write_gz(p["path"], p["body"])
        written.append(p["path"])
        tag = "AMENDED" if p["state"] == "changed" else "new"
        print(f"  {p['path']}  ({p['rows']} observations, "
              f"{os.path.getsize(p['path']) / 1024:.1f} KB) [{tag}]")

    if ledger:
        lpath = log_correction(root, reason=reason or "", tool=tool, entries=ledger)
        print(f"  logged {len(ledger)} correction(s) -> {lpath}")

    # Promo bodies: small, deduped by hash, rewritten whole each time.
    promos = conn.execute("SELECT * FROM promos ORDER BY promo_hash").fetchall()
    if promos:
        ppath = os.path.join(root, PROMOS_FILE)
        os.makedirs(root, exist_ok=True)
        merged: dict[str, dict] = {}
        if os.path.exists(ppath):
            for rec in _read_jsonl(ppath):
                merged[rec["promo_hash"]] = rec
        # The promo file is an index, not a dated observation: counters like
        # last_seen/n_seen are meant to move. What must never change silently is
        # a promo BODY under an existing hash, which would mean the same offer
        # id now says something different.
        rewritten = [h for h in merged
                     if h in {p["promo_hash"] for p in promos}
                     and merged[h].get("promo_text") not in (None, "")
                     and any(p["promo_hash"] == h and p["promo_text"] != merged[h]["promo_text"]
                             for p in promos)]
        for p in promos:
            merged[p["promo_hash"]] = {k: p[k] for k in p.keys()}
        if rewritten and not amend:
            conn.close()
            raise HistoryRewriteRefused(
                f"refusing to rewrite {len(rewritten)} promo body/bodies under an "
                f"existing hash: {rewritten[:5]}. Re-run with --amend --reason.")
        _write_gz(ppath, _encode([merged[h] for h in sorted(merged)]))
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
    p.add_argument("--amend", action="store_true",
                   help="permit rewriting existing dated files (requires --reason)")
    p.add_argument("--reason", default=None,
                   help="why history is being corrected; recorded in CORRECTIONS.jsonl")
    p.add_argument("--db", default=os.path.join("data", "panel.sqlite"))
    p.add_argument("--dir", default=DEFAULT_EXPORT_DIR, dest="root")
    p.add_argument("--tier", default=None, help="export only this tier")
    p.add_argument("--date", default=None, help="export only this scrape_date")
    return p


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.action == "export":
        print(f"exporting {args.db} -> {args.root}")
        try:
            written = export(args.db, args.root, args.tier, args.date,
                             amend=args.amend, reason=args.reason)
        except (HistoryRewriteRefused, ValueError) as exc:
            print(f"\nREFUSED: {exc}", file=sys.stderr)
            return 2
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
