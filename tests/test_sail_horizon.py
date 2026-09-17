"""The weekly sail window: how far out it reaches, and what it drops.

Until 2026-09-17 the window ended 2027-08-31. The endpoints publish to
Oct 2028 (NCL) and Apr 2029 (Carnival), so a third of NCL's pricing rows were
discarded without a count, and ~365 itineraries sailing only after Aug 2027
were never enumerated. Three things are pinned here:

1. The shipped window reaches NCL's horizon and no further.
2. Rows the window excludes are COUNTED, by side, after the region filter --
   a date exclusion must never be confused with a region exclusion.
3. NCL's search stops honouring its month filter past the horizon and returns
   the whole catalogue; discovery must detect that and stop, not re-page the
   full catalogue once per month.
"""
import pytest

from panel import normalize as norm
from panel.config import TierConfig, load_config
from panel.sources import carnival, ncl
from panel.sources.base import CollectResult


# -- 1. the shipped window ---------------------------------------------------

class TestShippedWindow:
    def test_weekly_window_reaches_ncls_horizon(self):
        tier = load_config("config/panel.yaml").tier("weekly-full")
        assert tier.sail_window_start == "2026-10-01"
        assert tier.sail_window_end == "2028-10-31"

    def test_the_back_of_2027_is_no_longer_cut_off(self):
        tier = load_config("config/panel.yaml").tier("weekly-full")
        for d in ("2027-09-01", "2027-12-31", "2028-06-15", "2028-10-31"):
            assert norm.in_window(d, tier.sail_window_start, tier.sail_window_end), d

    def test_discovery_months_cover_25_months(self):
        months = ncl.months_in_window("2026-10-01", "2028-10-31")
        assert months[0] == "Oct-2026" and months[-1] == "Oct-2028"
        assert len(months) == 25

    def test_daily_marker_window_is_untouched(self):
        tier = load_config("config/panel.yaml").tier("daily-marker")
        assert (tier.sail_window_start, tier.sail_window_end) == (
            "2026-10-01", "2026-12-31")


# -- 2. counting what the window drops ---------------------------------------

class TestWindowSide:
    @pytest.mark.parametrize("d,side", [
        ("2027-06-01", None),
        ("2026-09-30", "before"),
        ("2028-11-01", "after"),
        ("2028-10-31T00:00", None),
        (None, "undated"),
        ("not a date", "undated"),
    ])
    def test_sides(self, d, side):
        assert norm.window_side(d, "2026-10-01", "2028-10-31") == side

    def test_count_is_a_no_op_without_a_stats_dict(self):
        norm.count_window_drop(None, "2030-01-01", "2026-10-01", "2028-10-31")

    def test_inside_dates_are_not_counted(self):
        stats = {}
        norm.count_window_drop(stats, "2027-01-01", "2026-10-01", "2028-10-31")
        assert stats == {}

    def test_outside_dates_accumulate_by_side(self):
        stats = {}
        for d in ("2029-01-01", "2029-02-01", "2025-01-01"):
            norm.count_window_drop(stats, d, "2026-10-01", "2028-10-31")
        assert stats == {"after": 2, "before": 1}


def ncl_parse(payload, line_cfg, **kw):
    kw.setdefault("tier", "weekly-full")
    kw.setdefault("scrape_ts", "2026-09-14T21:00:00+00:00")
    kw.setdefault("scrape_date", "2026-09-14")
    kw.setdefault("source_url", "https://www.ncl.com/x")
    kw.setdefault("expected_currency", "CAD")      # the fixture is a CAD capture
    return ncl.parse_sailings(payload, line_cfg=line_cfg, **kw)


class TestNclCountsDrops:
    def test_every_excluded_row_is_counted(self, sailings_payload, ncl_line_cfg):
        stats = {}
        rows, _ = ncl_parse(sailings_payload, ncl_line_cfg,
                            sail_window=("2027-01-01", "2027-08-31"),
                            window_stats=stats)
        total = len(sailings_payload["pricingStateRooms"])
        assert len(rows) + sum(stats.values()) == total
        assert stats.get("before", 0) > 0          # the Nov/Dec 2026 dates

    def test_a_window_that_excludes_nothing_counts_nothing(
            self, sailings_payload, ncl_line_cfg):
        stats = {}
        rows, _ = ncl_parse(sailings_payload, ncl_line_cfg, window_stats=stats)
        assert len(rows) == 90 and stats == {}

    def test_region_exclusion_is_not_counted_as_a_date_drop(
            self, sailings_payload, ncl_line_cfg):
        stats = {}
        rows, _ = ncl_parse(sailings_payload, ncl_line_cfg,
                            sail_window=("2030-01-01", "2030-12-31"),
                            allowed_regions=["Alaska"], window_stats=stats)
        assert rows == [] and stats == {}

    def test_counting_is_optional(self, sailings_payload, ncl_line_cfg):
        rows, _ = ncl_parse(sailings_payload, ncl_line_cfg,
                            sail_window=("2027-01-01", "2027-08-31"))
        assert rows


class TestCarnivalCountsDrops:
    def test_every_excluded_sailing_is_counted(self, ccl_page1, ccl_line_cfg):
        stats = {}
        rows, _, _ = carnival.parse_search(
            ccl_page1, line_cfg=ccl_line_cfg, tier="weekly-full",
            scrape_ts="2026-09-14T21:00:00+00:00", scrape_date="2026-09-14",
            source_url="https://www.carnival.com/x",
            sail_window=("2000-01-01", "2000-01-02"), window_stats=stats)
        assert rows == []
        sailings = sum(len(it.get("sailings") or [])
                       for it in ccl_page1["results"]["itineraries"])
        assert stats == {"after": sailings}


class TestResultMerge:
    def test_counts_and_notes_merge(self):
        a = CollectResult(outside_window={"after": 3}, notes=["x"])
        b = CollectResult(outside_window={"after": 2, "before": 1}, notes=["y"])
        a.merge(b)
        assert a.outside_window == {"after": 5, "before": 1}
        assert a.notes == ["x", "y"]


# -- 3. the search horizon ---------------------------------------------------

class FakeClient:
    """NCL search as observed live on 2026-09-16: months up to the horizon are
    filtered; past it the filter is ignored and the full catalogue returns."""

    CATALOGUE = 801

    def __init__(self, horizon_month_index, per_month=3):
        self.horizon = horizon_month_index
        self.per_month = per_month
        self.months = ncl.months_in_window("2026-10-01", "2028-10-31")
        self.search_calls = []

    def get_json(self, url):
        assert "dates=" not in url, "catalogue probe must be unfiltered"
        return {"total": self.CATALOGUE, "itineraries": [{"code": "ANY"}]}

    def get_json_with_body(self, url):
        self.search_calls.append(url)
        month = url.split("dates=")[1]
        i = self.months.index(month)
        if i >= self.horizon:
            return {"total": self.CATALOGUE,
                    "itineraries": [{"code": f"CAT{n}"} for n in range(50)]}, "{}"
        return {"total": self.per_month,
                "itineraries": [{"code": f"{month}-{n}"}
                                for n in range(self.per_month)]}, "{}"


class NullArchive:
    def write(self, *a, **k):
        return None


def source(client):
    cfg = load_config("config/panel.yaml")
    return ncl.NCLSource(cfg, cfg.line("ncl"), client, store=None,
                         archive=NullArchive())


def weekly(end="2028-10-31"):
    return TierConfig(name="weekly-full", sail_window_start="2026-10-01",
                      sail_window_end=end)


class TestSearchHorizon:
    def test_window_inside_the_horizon_searches_every_month(self):
        client = FakeClient(horizon_month_index=99)
        result = CollectResult()
        codes = source(client).discover_itineraries(weekly(), result)
        assert len(client.search_calls) == 25
        assert len(codes) == 25 * 3
        assert result.notes == []

    def test_discovery_stops_where_the_filter_stops_being_honoured(self):
        client = FakeClient(horizon_month_index=10)
        result = CollectResult()
        codes = source(client).discover_itineraries(weekly(), result)
        # 10 real months, then ONE probe of the first fallback month, then stop.
        assert len(client.search_calls) == 11
        assert len(codes) == 10 * 3
        assert not any(c.startswith("CAT") for c in codes), \
            "the unfiltered catalogue must not be taken as in-window"
        assert len(result.notes) == 1
        assert "horizon reached" in result.notes[0]
        assert result.errors == []

    def test_a_window_far_past_the_horizon_costs_one_extra_request(self):
        client = FakeClient(horizon_month_index=5)
        result = CollectResult()
        source(client).discover_itineraries(weekly("2032-12-31"), result)
        assert len(client.search_calls) == 6

    def test_marker_tier_does_not_probe_the_catalogue(self):
        class NoCalls(FakeClient):
            def get_json(self, url):
                raise AssertionError("marker tier must not search")
        tier = TierConfig(name="daily-marker", sail_window_start="2026-10-01",
                          sail_window_end="2026-12-31", marker_only=True,
                          marker_itineraries={"ncl": ["A", "B"]})
        codes = source(NoCalls(99)).discover_itineraries(tier, CollectResult())
        assert codes == ["A", "B"]

    def test_unreadable_catalogue_disables_the_guard_not_discovery(self):
        from panel.http_client import FetchError

        class Broken(FakeClient):
            def get_json(self, url):
                raise FetchError(url, 503, "down")
        client = Broken(horizon_month_index=99)
        result = CollectResult()
        codes = source(client).discover_itineraries(weekly(), result)
        assert len(codes) == 75
        assert result.errors and result.errors[0]["stage"] == "catalogue"
