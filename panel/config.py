"""YAML config loading with light validation."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping

import yaml

DEFAULT_CONFIG_PATH = os.path.join("config", "panel.yaml")

TIERS = ("weekly-full", "daily-marker")


@dataclass
class TierConfig:
    name: str
    sail_window_start: str | None
    sail_window_end: str | None
    # Per-line: {"ncl": ["CODE1", ...], "carnival": ["BAW", ...]}. Keyed by line
    # so each source only ever asks its own API for its own codes.
    marker_itineraries: dict[str, list[str]] = field(default_factory=dict)
    marker_only: bool = False
    event_date: str | None = None
    event_window_days: int = 14
    # A ROLLING horizon, in days from the run date. Fixed calendar bounds go
    # stale: the daily tier's job is to watch sailings cross final payment, and
    # a fixed end date walks toward the run date until the far side of the
    # boundary drops out of scope entirely and the test quietly stops working.
    horizon_days: int | None = None


    def resolve_window(self, today: "date | None" = None
                       ) -> tuple[str | None, str | None]:
        """The sail window to collect, for a run on `today`.

        With `horizon_days` set this is (today, today + horizon_days) and moves
        with the run. Otherwise the configured fixed bounds are used unchanged.
        """
        if self.horizon_days is None:
            return self.sail_window_start, self.sail_window_end
        from datetime import date as _date, timedelta
        base = today or _date.today()
        return base.isoformat(), (base + timedelta(days=self.horizon_days)).isoformat()

    def markers_for(self, line_key: str) -> list[str]:
        """Marker itinerary codes for one line (empty if none configured)."""
        return list(self.marker_itineraries.get(line_key) or [])


@dataclass
class LineConfig:
    key: str
    line: str
    brand: str
    enabled: bool
    base_url: str
    cabin_map: dict[str, str]
    region_map: dict[str, str]
    regions: list[str] = field(default_factory=list)
    search_page_size: int = 50
    max_itineraries: int | None = None
    max_pages: int | None = None
    # Carnival only: regionCode is authoritative, port is a narrow fallback for
    # ports that serve exactly one region. Ambiguous ports are deliberately absent.
    port_region_map: dict[str, str] = field(default_factory=dict)
    dest_codes: list[str] = field(default_factory=list)
    # Royal only: region -> allowed landing-page path carrying the sailing
    # floor. Cabin-level paths are disallowed by robots.txt and are absent
    # from this map by design, not by oversight.
    landing_pages: dict[str, str] = field(default_factory=dict)
    # class -> [ship names]. Hull class is the one composition axis the curve
    # does not otherwise control, and it is mapped explicitly rather than
    # inferred from the ship name.
    ship_classes: dict[str, list[str]] = field(default_factory=dict)
    # class -> generation label, comparable ACROSS lines.
    ship_generation: dict[str, str] = field(default_factory=dict)


@dataclass
class Config:
    db_path: str
    raw_archive_path: str
    user_agent: str
    min_interval_s: float
    max_retries: int
    backoff_base_s: float
    backoff_cap_s: float
    timeout_s: float
    obey_robots: bool
    lines: dict[str, LineConfig]
    tiers: dict[str, TierConfig]
    raw: Mapping[str, Any] = field(default_factory=dict)

    def line(self, key: str) -> LineConfig:
        if key not in self.lines:
            raise KeyError(f"unknown line {key!r}; configured: {sorted(self.lines)}")
        return self.lines[key]

    def tier(self, name: str) -> TierConfig:
        if name not in self.tiers:
            raise KeyError(f"unknown tier {name!r}; configured: {sorted(self.tiers)}")
        return self.tiers[name]


def _upper_keys(d: Mapping[str, Any] | None) -> dict[str, str]:
    return {str(k).strip().upper(): str(v).strip() for k, v in (d or {}).items()}


def load_config(path: str | os.PathLike[str] = DEFAULT_CONFIG_PATH) -> Config:
    with open(path, "r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}

    storage = doc.get("storage", {})
    http = doc.get("http", {})

    lines: dict[str, LineConfig] = {}
    for key, raw in (doc.get("lines") or {}).items():
        lines[key] = LineConfig(
            key=key,
            line=raw["line"],
            brand=raw.get("brand", raw["line"]),
            enabled=bool(raw.get("enabled", True)),
            base_url=raw["base_url"].rstrip("/"),
            cabin_map=_upper_keys(raw.get("cabin_map")),
            region_map=_upper_keys(raw.get("region_map")),
            regions=list(raw.get("regions") or []),
            search_page_size=int(raw.get("search_page_size", 50)),
            max_itineraries=raw.get("max_itineraries"),
            max_pages=raw.get("max_pages"),
            port_region_map=_upper_keys(raw.get("port_region_map")),
            dest_codes=list(raw.get("dest_codes") or []),
            landing_pages={str(k): str(v) for k, v
                           in (raw.get("landing_pages") or {}).items()},
            ship_classes={str(k): [str(x) for x in (v or [])]
                          for k, v in (raw.get("ship_classes") or {}).items()},
            ship_generation={str(k): str(v) for k, v
                             in (raw.get("ship_generation") or {}).items()},
        )

    tiers: dict[str, TierConfig] = {}
    for name, raw in (doc.get("tiers") or {}).items():
        if name not in TIERS:
            raise ValueError(f"tier {name!r} is not one of {TIERS}")
        window = raw.get("sail_window") or {}
        tiers[name] = TierConfig(
            name=name,
            sail_window_start=window.get("start"),
            sail_window_end=window.get("end"),
            horizon_days=(int(window["horizon_days"])
                          if window.get("horizon_days") is not None else None),
            marker_itineraries={
                str(k): list(v or [])
                for k, v in (raw.get("marker_itineraries") or {}).items()
            },
            marker_only=bool(raw.get("marker_only", False)),
            event_date=raw.get("event_date"),
            event_window_days=int(raw.get("event_window_days", 14)),
        )

    for required in TIERS:
        if required not in tiers:
            raise ValueError(f"config is missing tier {required!r}")

    return Config(
        db_path=storage.get("db_path", os.path.join("data", "panel.sqlite")),
        raw_archive_path=storage.get("raw_archive_path", os.path.join("data", "raw")),
        user_agent=http.get("user_agent", "cruise-panel/0.1"),
        min_interval_s=float(http.get("min_interval_s", 2.0)),
        max_retries=int(http.get("max_retries", 4)),
        backoff_base_s=float(http.get("backoff_base_s", 2.0)),
        backoff_cap_s=float(http.get("backoff_cap_s", 60.0)),
        timeout_s=float(http.get("timeout_s", 60.0)),
        obey_robots=bool(http.get("obey_robots", True)),
        lines=lines,
        tiers=tiers,
        raw=doc,
    )
