"""Source interface shared by every line."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..storage import Observation


@dataclass
class CollectResult:
    sailings_attempted: int = 0
    sailings_captured: int = 0
    observations_written: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    unmapped_labels: set[str] = field(default_factory=set)
    # Rows the sail window excluded, by side ("before"/"after"/"undated").
    # Counted rather than silently discarded: until 2026-09-17 the weekly
    # window dropped a third of NCL's pricing rows without a trace.
    outside_window: dict[str, int] = field(default_factory=dict)
    # Informational, not errors: e.g. where a vendor's date filter stops
    # being honoured. Printed by the CLI, never counted as a failure.
    notes: list[str] = field(default_factory=list)

    def merge(self, other: "CollectResult") -> None:
        self.sailings_attempted += other.sailings_attempted
        self.sailings_captured += other.sailings_captured
        self.observations_written += other.observations_written
        self.errors.extend(other.errors)
        self.unmapped_labels |= other.unmapped_labels
        for side, n in other.outside_window.items():
            self.outside_window[side] = self.outside_window.get(side, 0) + n
        self.notes.extend(other.notes)


class Source(Protocol):
    key: str

    def collect(self, tier: str) -> CollectResult:
        ...
