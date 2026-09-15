from .base import CollectResult, Source
from .capabilities import (
    CAPABILITIES,
    GranularityError,
    capability,
    comparison_column,
    require_granularity,
    shared_granularity,
)
from .carnival import CarnivalSource
from .ncl import NCLSource

SOURCES = {"ncl": NCLSource, "carnival": CarnivalSource}

__all__ = [
    "Source", "CollectResult", "NCLSource", "CarnivalSource", "SOURCES",
    "CAPABILITIES", "capability", "shared_granularity", "require_granularity",
    "comparison_column", "GranularityError",
]
