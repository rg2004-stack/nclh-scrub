"""Decide what a workflow run should collect, including catch-up runs.

Why this exists
---------------
GitHub places hosted runners in whatever Azure region has capacity. NCL decides
the market at its CDN from the client IP, so a runner in `canadaeast` gets CAD,
the US-market gate correctly refuses to collect, and that day's data is lost.
It happened on 2026-10-07 (egress Quebec, CA) after 24 clean runs in US
regions. Forging the CDN's geolocation cookies would be falsifying our location,
which this project does not do. The honest fix is to try again on a different
runner: placement varies run to run, so a later attempt almost always lands in
a US region.

So the workflow has catch-up crons. Each one runs this planner first:

* primary crons always run their tier, exactly as before;
* a catch-up cron looks at what is already committed for TODAY (UTC, which is
  the scrape_date the collector writes) and collects only what is missing --
  the weekly tier first on a Monday, then the daily tier;
* it collects only the MISSING LINES. Re-collecting a line that already
  succeeded today would produce different content for an existing dated file,
  and the export's immutability guard would (rightly) refuse it.

When nothing is missing the planner says so and the collection job is skipped:
a green run that costs a few seconds, not a red one.

Output goes to $GITHUB_OUTPUT as `run`, `tier`, `lines` (space-separated line
keys; empty = every line) and `reason`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from typing import Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from panel.config import DEFAULT_CONFIG_PATH, load_config  # noqa: E402
from panel.export import DEFAULT_EXPORT_DIR, export_path  # noqa: E402

# Must match `on.schedule` in .github/workflows/collect.yml. A test reads the
# workflow file and fails if these drift apart, so an edited cron string cannot
# silently turn a primary run into a skipped one.
WEEKLY_CRON = "10 6 * * 1"
DAILY_CRON = "40 6 * * *"
CATCHUP_CRONS = ("40 9 * * *", "40 13 * * *", "40 17 * * *")
WEEKLY_WEEKDAY = 0          # Monday, matching WEEKLY_CRON


def expected_lines(cfg, tier: str) -> list[tuple[str, str]]:
    """(line key, line name) that a successful run of `tier` writes a file for.

    Enabled lines only; for the marker tier, only lines that have markers
    configured -- a line with none produces no file, and expecting one would
    send every catch-up chasing a file that can never exist.
    """
    tier_cfg = cfg.tier(tier)
    out = []
    for key, lc in cfg.lines.items():
        if not lc.enabled:
            continue
        if tier_cfg.marker_only and not tier_cfg.markers_for(key):
            continue
        out.append((key, lc.line))
    return out


def missing_lines(cfg, tier: str, day: str, root: str) -> list[str]:
    """Line keys whose dated file for (day, tier) is not on disk."""
    return [key for key, name in expected_lines(cfg, tier)
            if not os.path.exists(export_path(root, day, tier, name))]


def plan(*, event: str, schedule: str = "", input_tier: str = "",
         input_line: str = "", today: dt.date | None = None,
         config_path: str = str(DEFAULT_CONFIG_PATH),
         root: str = DEFAULT_EXPORT_DIR) -> dict[str, str]:
    today = today or dt.datetime.now(dt.timezone.utc).date()
    day = today.isoformat()

    if event == "workflow_dispatch":
        tier = input_tier or "weekly-full"
        lines = "" if input_line in ("", "all") else input_line
        return {"run": "true", "tier": tier, "lines": lines,
                "reason": f"manual run of {tier}"}

    if schedule == WEEKLY_CRON:
        return {"run": "true", "tier": "weekly-full", "lines": "",
                "reason": "primary weekly schedule"}
    if schedule == DAILY_CRON:
        return {"run": "true", "tier": "daily-marker", "lines": "",
                "reason": "primary daily schedule"}

    if schedule in CATCHUP_CRONS:
        cfg = load_config(config_path)
        tiers = (["weekly-full", "daily-marker"]
                 if today.weekday() == WEEKLY_WEEKDAY else ["daily-marker"])
        for tier in tiers:
            missing = missing_lines(cfg, tier, day, root)
            if not missing:
                continue
            everything = [k for k, _ in expected_lines(cfg, tier)]
            lines = "" if sorted(missing) == sorted(everything) else " ".join(missing)
            return {"run": "true", "tier": tier, "lines": lines,
                    "reason": f"catch-up: {tier} for {day} missing "
                              f"{', '.join(missing)}"}
        return {"run": "false", "tier": "", "lines": "",
                "reason": f"catch-up: everything for {day} already collected"}

    # An unrecognised trigger must not quietly skip a collection. Fail so the
    # mismatch between this file and the workflow is seen and fixed.
    raise SystemExit(f"plan_run: unrecognised trigger event={event!r} "
                     f"schedule={schedule!r}; update the cron constants")


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--event", default=os.environ.get("EVENT_NAME", ""))
    ap.add_argument("--schedule", default=os.environ.get("SCHEDULE", ""))
    ap.add_argument("--input-tier", default=os.environ.get("INPUT_TIER", ""))
    ap.add_argument("--input-line", default=os.environ.get("INPUT_LINE", ""))
    ap.add_argument("--today", default=None, help="YYYY-MM-DD (default: UTC today)")
    args = ap.parse_args(argv)
    today = dt.date.fromisoformat(args.today) if args.today else None
    p = plan(event=args.event, schedule=args.schedule,
             input_tier=args.input_tier, input_line=args.input_line, today=today)
    print(json.dumps(p, indent=1))
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            for k, v in p.items():
                fh.write(f"{k}={v}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
