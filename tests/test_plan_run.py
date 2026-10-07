"""The run planner, and the two ways a scheduled run silently lost a day.

1. Runner region (2026-10-07): the daily landed in Azure canadaeast, NCL priced
   in CAD, the US-market gate refused. Catch-up crons retry on a fresh runner,
   collecting only what is still missing for today.
2. Monday dailies (09-21, 09-28, 10-05): the daily, queued behind the weekly,
   started from the pre-weekly commit; both rewrite promos.jsonl.gz, the rebase
   conflicted, and the push of a mid-rebase HEAD reported success. The
   workflow must check out the branch tip and fail when the push did not land.
"""
import datetime as dt
import os

import pytest
import yaml

from panel.config import load_config
from panel.export import export_path
from scripts import plan_run as pr

WORKFLOW = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        ".github", "workflows", "collect.yml")
MONDAY = dt.date(2026, 10, 5)
TUESDAY = dt.date(2026, 10, 6)


def _touch(root, day, tier, line_name):
    p = export_path(root, day.isoformat(), tier, line_name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").close()


def _names(tier):
    return dict(pr.expected_lines(load_config(), tier))


def _catchup(root, today, cron=pr.CATCHUP_CRONS[0]):
    return pr.plan(event="schedule", schedule=cron, today=today, root=str(root))


# -- primary and manual triggers are unchanged --------------------------------

def test_primary_crons_always_run_their_tier(tmp_path):
    w = pr.plan(event="schedule", schedule=pr.WEEKLY_CRON, today=MONDAY, root=str(tmp_path))
    d = pr.plan(event="schedule", schedule=pr.DAILY_CRON, today=MONDAY, root=str(tmp_path))
    assert (w["run"], w["tier"], w["lines"]) == ("true", "weekly-full", "")
    assert (d["run"], d["tier"], d["lines"]) == ("true", "daily-marker", "")


def test_manual_run_passes_inputs_through(tmp_path):
    p = pr.plan(event="workflow_dispatch", input_tier="daily-marker",
                input_line="ncl", root=str(tmp_path))
    assert (p["run"], p["tier"], p["lines"]) == ("true", "daily-marker", "ncl")
    p = pr.plan(event="workflow_dispatch", input_tier="weekly-full",
                input_line="all", root=str(tmp_path))
    assert p["lines"] == ""


def test_unknown_trigger_fails_loudly(tmp_path):
    with pytest.raises(SystemExit):
        pr.plan(event="schedule", schedule="1 2 * * *", root=str(tmp_path))


# -- catch-up -----------------------------------------------------------------

def test_catchup_collects_everything_when_the_day_is_missing(tmp_path):
    p = _catchup(tmp_path, TUESDAY)
    assert (p["run"], p["tier"], p["lines"]) == ("true", "daily-marker", "")


def test_catchup_collects_only_the_missing_line(tmp_path):
    names = _names("daily-marker")
    assert len(names) >= 2, "test needs two marker lines"
    have, *rest = sorted(names)
    _touch(tmp_path, TUESDAY, "daily-marker", names[have])
    p = _catchup(tmp_path, TUESDAY)
    # Re-collecting `have` would rewrite an existing dated file; it must not be named.
    assert p["run"] == "true" and p["tier"] == "daily-marker"
    assert p["lines"].split() == rest


def test_catchup_skips_when_the_day_is_complete(tmp_path):
    for name in _names("daily-marker").values():
        _touch(tmp_path, TUESDAY, "daily-marker", name)
    for cron in pr.CATCHUP_CRONS:
        assert _catchup(tmp_path, TUESDAY, cron)["run"] == "false"


def test_monday_catchup_does_weekly_first_then_daily(tmp_path):
    assert _catchup(tmp_path, MONDAY)["tier"] == "weekly-full"
    for name in _names("weekly-full").values():
        _touch(tmp_path, MONDAY, "weekly-full", name)
    # The exact Monday gap: weekly committed, daily never landed.
    p = _catchup(tmp_path, MONDAY)
    assert (p["run"], p["tier"], p["lines"]) == ("true", "daily-marker", "")


def test_weekday_catchup_ignores_the_weekly_tier(tmp_path):
    for name in _names("daily-marker").values():
        _touch(tmp_path, TUESDAY, "daily-marker", name)
    assert _catchup(tmp_path, TUESDAY)["run"] == "false"


def test_marker_tier_expects_only_lines_with_markers():
    cfg = load_config()
    tier = cfg.tier("daily-marker")
    for key, _ in pr.expected_lines(cfg, "daily-marker"):
        assert cfg.lines[key].enabled and tier.markers_for(key)
    assert all(cfg.lines[k].enabled for k, _ in pr.expected_lines(cfg, "weekly-full"))


def test_main_writes_github_output(tmp_path, monkeypatch, capsys):
    out = tmp_path / "gh_out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    assert pr.main(["--event", "schedule", "--schedule", pr.DAILY_CRON]) == 0
    kv = dict(l.split("=", 1) for l in out.read_text().splitlines())
    assert kv["run"] == "true" and kv["tier"] == "daily-marker"


# -- the workflow file agrees with the planner ----------------------------------

@pytest.fixture(scope="module")
def workflow():
    with open(WORKFLOW, encoding="utf-8") as fh:
        text = fh.read()
    doc = yaml.safe_load(text)
    return text, doc


def test_workflow_crons_match_planner(workflow):
    _, doc = workflow
    on = doc.get("on", doc.get(True))           # PyYAML reads `on:` as True
    crons = [c["cron"] for c in on["schedule"]]
    assert sorted(crons) == sorted([pr.WEEKLY_CRON, pr.DAILY_CRON, *pr.CATCHUP_CRONS])


def test_workflow_checks_out_branch_tip(workflow):
    _, doc = workflow
    for name, job in doc["jobs"].items():
        checkouts = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout")]
        assert checkouts, name
        for s in checkouts:
            assert (s.get("with") or {}).get("ref") == "${{ github.ref }}", (
                f"{name}: a queued run must start from the branch tip, not the trigger SHA")


def test_workflow_commit_step_fails_when_push_does_not_land(workflow):
    _, doc = workflow
    step = next(s for s in doc["jobs"]["collect"]["steps"]
                if s.get("name") == "Commit the export")
    run = step["run"]
    assert "git rebase --abort" in run
    assert 'if [ "$rebased" != true ]' in run and "exit 1" in run
    assert "git fetch origin" in run and "origin/${GITHUB_REF_NAME}" in run


def test_collect_job_uses_planner_outputs(workflow):
    _, doc = workflow
    job = doc["jobs"]["collect"]
    assert job["needs"] == "plan"
    assert job["if"] == "needs.plan.outputs.run == 'true'"
    text, _ = workflow
    assert "needs.plan.outputs.lines" in text and "github.event.inputs.line" not in (
        next(s for s in job["steps"] if s.get("name") == "Collect")["run"])
