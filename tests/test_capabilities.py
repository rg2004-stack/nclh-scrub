"""Cross-line comparison must run at a granularity both sides actually have.

Carnival publishes vendor_category_code and rate_code; NCL publishes neither.
Comparing the two at sub-category level would compare Carnival's real codes
against NCL's NULLs and silently return a meaningless answer.
"""
import pytest

from panel.sources import capabilities as cap


class TestDeclaredCapabilities:
    def test_every_registered_source_declares_capability(self):
        from panel.sources import SOURCES
        for key in SOURCES:
            assert key in cap.CAPABILITIES, f"{key} has no declared capability"

    def test_ncl_resolves_only_to_category(self):
        assert cap.capability("ncl").granularity == "category"

    def test_carnival_resolves_to_rate_code(self):
        assert cap.capability("carnival").granularity == "rate_code"

    def test_unknown_source_raises(self):
        with pytest.raises(KeyError):
            cap.capability("oceania")


class TestSharedGranularity:
    def test_single_source_gets_its_own_level(self):
        assert cap.shared_granularity(["carnival"]) == "rate_code"
        assert cap.shared_granularity(["ncl"]) == "category"

    def test_mixed_lines_fall_back_to_the_coarser_level(self):
        assert cap.shared_granularity(["ncl", "carnival"]) == "category"
        assert cap.shared_granularity(["carnival", "ncl"]) == "category"

    def test_empty_raises(self):
        with pytest.raises(cap.GranularityError):
            cap.shared_granularity([])


class TestRequireGranularity:
    def test_category_is_allowed_across_both_lines(self):
        assert cap.require_granularity(["ncl", "carnival"], "category") == "category"

    def test_subcategory_across_both_lines_is_refused(self):
        with pytest.raises(cap.GranularityError) as exc:
            cap.require_granularity(["ncl", "carnival"], "subcategory")
        assert "ncl" in str(exc.value)
        assert "category" in str(exc.value)

    def test_rate_code_across_both_lines_is_refused(self):
        with pytest.raises(cap.GranularityError):
            cap.require_granularity(["ncl", "carnival"], "rate_code")

    def test_carnival_alone_may_use_its_finer_levels(self):
        assert cap.require_granularity(["carnival"], "subcategory") == "subcategory"
        assert cap.require_granularity(["carnival"], "rate_code") == "rate_code"

    def test_ncl_alone_cannot_use_subcategory(self):
        with pytest.raises(cap.GranularityError):
            cap.require_granularity(["ncl"], "subcategory")

    def test_unknown_granularity_raises(self):
        with pytest.raises(cap.GranularityError):
            cap.require_granularity(["ncl"], "deck_number")


class TestComparisonColumn:
    def test_maps_granularity_to_column(self):
        assert cap.comparison_column("category") == "cabin_category"
        assert cap.comparison_column("subcategory") == "vendor_category_code"
        assert cap.comparison_column("rate_code") == "rate_code"

    def test_peer_gap_style_usage(self):
        """The pattern any cross-line function must follow."""
        lines = ["ncl", "carnival"]
        level = cap.require_granularity(lines, cap.shared_granularity(lines))
        assert cap.comparison_column(level) == "cabin_category"
