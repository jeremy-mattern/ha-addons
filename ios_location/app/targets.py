"""Work out where the iPhone should be, from Home Assistant state. Pure logic, no device I/O."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

MODE_OFF = "off"
MODE_MIRROR = "mirror"
MODE_FIXED = "fixed"

_COORD_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*$")


@dataclass(frozen=True)
class Target:
    mode: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    source: str = ""
    # Set when the mode wants a location but none is usable right now.
    problem: str = ""

    @property
    def has_location(self) -> bool:
        return self.latitude is not None and self.longitude is not None


def normalize_mode(raw: Optional[str]) -> str:
    """Map the mode helper's state to off / mirror / fixed. Unknown or missing means off."""
    value = (raw or "").strip().lower()
    if value.startswith("mirror"):
        return MODE_MIRROR
    if value.startswith("fixed") or value.startswith("manual") or value.startswith("pin"):
        return MODE_FIXED
    return MODE_OFF


def _valid(lat: float, lon: float) -> bool:
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0 and not (lat == 0.0 and lon == 0.0)


def _coords_from_attributes(state: dict[str, Any]) -> Optional[tuple[float, float]]:
    attrs = state.get("attributes") or {}
    lat, lon = attrs.get("latitude"), attrs.get("longitude")
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    return (lat_f, lon_f) if _valid(lat_f, lon_f) else None


def parse_fixed(
    text: Optional[str], get_state: Callable[[str], Optional[dict[str, Any]]]
) -> tuple[Optional[tuple[float, float]], str, str]:
    """Parse the fixed-location helper.

    Accepts "29.7604, -95.3698", an entity id with latitude/longitude attributes
    ("zone.home", "device_tracker.x", "person.y"), or a zone's friendly name ("Home").
    Returns (coords or None, description of what was used, problem text).
    """
    raw = (text or "").strip()
    if not raw or raw.lower() in ("unknown", "unavailable"):
        return None, "", "fixed location is empty"

    match = _COORD_RE.match(raw)
    if match:
        lat, lon = float(match.group(1)), float(match.group(2))
        if not _valid(lat, lon):
            return None, raw, f"'{raw}' is not a valid latitude, longitude"
        return (lat, lon), "coordinates", ""

    entity_id = raw if "." in raw and " " not in raw else f"zone.{_slug(raw)}"
    state = get_state(entity_id)
    if state is None:
        return None, raw, f"no entity or zone named '{raw}'"
    coords = _coords_from_attributes(state)
    if coords is None:
        return None, entity_id, f"{entity_id} has no latitude/longitude"
    return coords, entity_id, ""


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def resolve_target(
    mode_state: Optional[dict[str, Any]],
    source_entity: str,
    fixed_entity: str,
    get_state: Callable[[str], Optional[dict[str, Any]]],
    max_gps_accuracy: int,
) -> Target:
    mode = normalize_mode((mode_state or {}).get("state"))
    if mode == MODE_OFF:
        return Target(MODE_OFF)

    if mode == MODE_MIRROR:
        state = get_state(source_entity)
        if state is None:
            return Target(mode, source=source_entity, problem=f"source {source_entity} not found")
        coords = _coords_from_attributes(state)
        if coords is None:
            return Target(mode, source=source_entity, problem=f"{source_entity} has no GPS coordinates")
        accuracy = (state.get("attributes") or {}).get("gps_accuracy")
        try:
            if max_gps_accuracy and accuracy is not None and float(accuracy) > max_gps_accuracy:
                return Target(
                    mode,
                    source=source_entity,
                    problem=f"{source_entity} accuracy {accuracy} m is worse than {max_gps_accuracy} m",
                )
        except (TypeError, ValueError):
            pass
        return Target(mode, coords[0], coords[1], source=source_entity)

    fixed_state = get_state(fixed_entity)
    coords, used, problem = parse_fixed((fixed_state or {}).get("state"), get_state)
    if coords is None:
        if fixed_state is None:
            problem = f"fixed helper {fixed_entity} not found"
        return Target(mode, source=used or fixed_entity, problem=problem)
    return Target(mode, coords[0], coords[1], source=used)


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in meters."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371000.0 * math.asin(math.sqrt(h))
