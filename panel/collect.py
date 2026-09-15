"""CLI entrypoint.

    python -m panel.collect --tier weekly-full
    python -m panel.collect --tier daily-marker

Both tiers share this module, the schema, the storage layer and the
normalization code. The daily tier is a filtered sailing list run more often,
not a second collector.

Re-running a tier on the same day is harmless: observations upsert on
(line, sailing_id, cabin_subcategory, market, scrape_date), and completed
itineraries are skipped via run_progress, so an interrupted run resumes.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from .config import DEFAULT_CONFIG_PATH, TIERS, load_config
from .http_client import PoliteClient, RateLimit
from .sources import SOURCES
from .sources.base import CollectResult
from .storage import RawArchive, Store, new_run_id


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m panel.collect",
        description="Collect cruise fare and cabin availability observations.")
    p.add_argument("--tier", required=True, choices=list(TIERS),
                   help="which collection tier to run")
    p.add_argument("--config", default=DEFAULT_CONFIG_PATH,
                   help=f"path to the YAML config (default: {DEFAULT_CONFIG_PATH})")
    p.add_argument("--line", action="append", dest="lines", metavar="KEY",
                   help="restrict to one configured line; repeatable")
    p.add_argument("--limit-itineraries", type=int, default=None,
                   help="cap itineraries per line (smoke tests)")
    p.add_argument("--dry-run", action="store_true",
                   help="enumerate and report scope without fetching sailings")
    p.add_argument("--db", default=None, help="override the database path")
    return p


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)

    db_path = args.db or cfg.db_path
    keys = args.lines or [k for k, lc in cfg.lines.items() if lc.enabled]
    if not keys:
        print("no enabled lines in config", file=sys.stderr)
        return 2

    client = PoliteClient(
        user_agent=cfg.user_agent,
        rate=RateLimit(
            min_interval_s=cfg.min_interval_s,
            max_retries=cfg.max_retries,
            backoff_base_s=cfg.backoff_base_s,
            backoff_cap_s=cfg.backoff_cap_s,
            timeout_s=cfg.timeout_s,
        ),
        obey_robots=cfg.obey_robots,
    )
    archive = RawArchive(cfg.raw_archive_path)
    run_id = new_run_id()
    overall = CollectResult()

    print(f"run {run_id}  tier={args.tier}  db={db_path}")
    print(f"rate limit: >= {cfg.min_interval_s}s between requests, "
          f"robots={'enforced' if cfg.obey_robots else 'IGNORED'}")

    with Store(db_path) as store:
        for key in keys:
            line_cfg = cfg.line(key)
            if args.limit_itineraries is not None:
                line_cfg.max_itineraries = args.limit_itineraries
            factory = SOURCES.get(key)
            if factory is None:
                print(f"  no collector implemented for line {key!r}; skipping")
                continue

            print(f"\n[{line_cfg.line}]")
            source = factory(cfg, line_cfg, client, store, archive)

            if args.dry_run:
                probe = CollectResult()
                codes = source.discover_itineraries(cfg.tier(args.tier), probe)
                print(f"  dry run: {len(codes)} itineraries in scope")
                for code in codes[:20]:
                    print(f"    {code}")
                if len(codes) > 20:
                    print(f"    ... and {len(codes) - 20} more")
                for err in probe.errors:
                    print(f"  error: {err}")
                continue

            log_id = store.start_run(run_id, args.tier, line_cfg.line)
            result = source.collect(args.tier)
            store.finish_run(log_id, result.sailings_attempted,
                             result.sailings_captured,
                             result.observations_written, result.errors)
            overall.merge(result)

            print(f"  attempted={result.sailings_attempted} "
                  f"captured={result.sailings_captured} "
                  f"observations={result.observations_written} "
                  f"errors={len(result.errors)}")
            if result.unmapped_labels:
                print(f"  UNMAPPED cabin labels (logged, not guessed): "
                      f"{sorted(result.unmapped_labels)}")

        if not args.dry_run:
            print(f"\ntotal observations in db: {store.count_observations()}")

    if overall.errors:
        print(f"\n{len(overall.errors)} error(s); first few:")
        for err in overall.errors[:5]:
            print("  " + json.dumps(err, ensure_ascii=False)[:200])
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
