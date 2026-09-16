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
from .royal import RoyalSource

SOURCES = {"ncl": NCLSource, "carnival": CarnivalSource,
           "royal": RoyalSource}

__all__ = [
    "Source", "CollectResult", "NCLSource", "CarnivalSource", "RoyalSource",
    "SOURCES",
    "CAPABILITIES", "capability", "shared_granularity", "require_granularity",
    "comparison_column", "GranularityError",
]
