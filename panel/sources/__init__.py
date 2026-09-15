from .base import CollectResult, Source
from .ncl import NCLSource

SOURCES = {"ncl": NCLSource}

__all__ = ["Source", "CollectResult", "NCLSource", "SOURCES"]
