from __future__ import annotations

import importlib.util
import math
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Callable

import numpy as np

from .assets import SOLAR_GEOMETRY_SHA256, sha256
from .errors import MissingDependencyError


def view_geometry(latitude: float, longitude: float, satellite_longitude: float = -75.0,
                  semi_major: float = 6378137.0, semi_minor: float = 6356752.31414,
                  perspective_height: float = 35786023.0) -> tuple[float, float]:
    """Scalar translation of notebook Cell 2 calculateViewGeometry."""
    lat, lon, sat_lon = map(math.radians, (latitude, longitude, satellite_longitude))
    e2 = 1 - semi_minor**2 / semi_major**2
    n = semi_major / math.sqrt(1 - e2 * math.sin(lat)**2)
    site = (n*math.cos(lat)*math.cos(lon), n*math.cos(lat)*math.sin(lon), n*(1-e2)*math.sin(lat))
    distance = semi_major + perspective_height
    delta = (distance*math.cos(sat_lon)-site[0], distance*math.sin(sat_lon)-site[1], -site[2])
    east = -math.sin(lon)*delta[0] + math.cos(lon)*delta[1]
    north = -math.sin(lat)*math.cos(lon)*delta[0] - math.sin(lat)*math.sin(lon)*delta[1] + math.cos(lat)*delta[2]
    up = math.cos(lat)*math.cos(lon)*delta[0] + math.cos(lat)*math.sin(lon)*delta[1] + math.sin(lat)*delta[2]
    slant = math.sqrt(sum(value*value for value in delta))
    view_zenith = math.degrees(math.acos(max(-1, min(1, up/slant))))
    view_azimuth = (math.degrees(math.atan2(east, north))+360)%360
    return float(np.float32(view_zenith)), float(np.float32(view_azimuth))


@lru_cache(maxsize=4)
def _load_solar_calculator(path_text: str) -> Callable:
    """Import the checksum-verified research module under a non-main module name."""
    path = Path(path_text)
    if not path.is_file():
        raise MissingDependencyError(f"Missing required solar-geometry helper: {path}")
    actual = sha256(path)
    if actual != SOLAR_GEOMETRY_SHA256:
        raise MissingDependencyError(
            f"Solar-geometry checksum mismatch for {path}: "
            f"expected {SOLAR_GEOMETRY_SHA256}, got {actual}"
        )
    spec = importlib.util.spec_from_file_location("_lai_cac_reference_geometry_goes19", path)
    if spec is None or spec.loader is None:
        raise MissingDependencyError(f"Could not create an import specification for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calculator = getattr(module, "calculate_solar_angles", None)
    if not callable(calculator):
        raise MissingDependencyError(f"Solar-geometry helper has no calculate_solar_angles: {path}")
    return calculator


def solar_angles(latitude: float, longitude: float, when: datetime, helper_path: Path) -> tuple[float, float]:
    """Call the supplied helper with the notebook's coordinate/output conversions."""
    calculator = _load_solar_calculator(str(helper_path.resolve()))
    latitude_values = np.asarray([latitude], dtype=np.float32).astype(float)
    longitude_values = np.asarray([longitude], dtype=np.float32).astype(float)
    zenith, azimuth = calculator(latitude_values, longitude_values, when)
    zenith_values = np.asarray(zenith, dtype=np.float32)
    azimuth_values = np.asarray(azimuth, dtype=np.float32) % np.float32(360.0)
    return float(zenith_values.reshape(-1)[0]), float(azimuth_values.reshape(-1)[0])


def angle_difference(a: float, b: float) -> float:
    return abs((a-b+180)%360-180)
