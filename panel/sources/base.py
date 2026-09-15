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

    def merge(self, other: "CollectResult") -> None:
        self.sailings_attempted += other.sailings_attempted
        self.sailings_captured += other.sailings_captured
        self.observations_written += other.observations_written
        self.errors.extend(other.errors)
        self.unmapped_labels |= other.unmapped_labels


class Source(Protocol):
    key: str

    def collect(self, tier: str) -> CollectResult:
        ...
