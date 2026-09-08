from __future__ import annotations

import math
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import h5py
import numpy as np

BUCKET = "https://noaa-goes19.s3.amazonaws.com"
PRODUCT = "ABI-L2-BRFF"
SCAN_RE = re.compile(r"_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})(\d)_e")


@dataclass(frozen=True)
class Scan:
    key: str
    started_at: datetime
    size: int


def list_scans(day: date, hour: int) -> list[Scan]:
    prefix = f"{PRODUCT}/{day.year}/{day.timetuple().tm_yday:03d}/{hour:02d}/"
    query = urllib.parse.urlencode({"list-type": "2", "prefix": prefix})
    with urllib.request.urlopen(f"{BUCKET}/?{query}", timeout=30) as response:
        root = ET.fromstring(response.read())
    ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
    scans = []
    for item in root.findall("s3:Contents", ns):
        key = item.findtext("s3:Key", namespaces=ns) or ""
        match = SCAN_RE.search(key)
        if not match:
            continue
        year, doy, hh, mm, ss, tenth = map(int, match.groups())
        started = datetime.strptime(f"{year}-{doy:03d}", "%Y-%j").replace(
            hour=hh, minute=mm, second=ss, microsecond=tenth * 100_000, tzinfo=timezone.utc
        )
        scans.append(Scan(key, started, int(item.findtext("s3:Size", "0", ns))))
    return scans


def nearest_scan(day: date, target_hour: int) -> Scan | None:
    target = datetime(day.year, day.month, day.day, target_hour, tzinfo=timezone.utc)
    candidates = list_scans(day, target_hour)
    return min(candidates, key=lambda scan: abs(scan.started_at - target), default=None)


def download(scan: Scan, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and destination.stat().st_size == scan.size:
        return destination
    temporary = destination.with_suffix(destination.suffix + ".part")
    urllib.request.urlretrieve(f"{BUCKET}/{scan.key}", temporary)
    temporary.replace(destination)
    return destination


def _decoded(dataset: h5py.Dataset, index: tuple[int, ...]) -> float:
    raw = dataset[index].item()
    fill = int(np.asarray(dataset.attrs.get("_FillValue", [-1])).flat[0])
    valid = np.asarray(dataset.attrs.get("valid_range", [-math.inf, math.inf])).astype(float)
    if raw == fill or not valid[0] <= raw <= valid[1]:
        return math.nan
    scale = float(np.asarray(dataset.attrs.get("scale_factor", [1.0])).flat[0])
    offset = float(np.asarray(dataset.attrs.get("add_offset", [0.0])).flat[0])
    return raw * scale + offset


def fixed_grid_xy(latitude: float, longitude: float, projection: h5py.Dataset) -> tuple[float, float]:
    """NOAA GOES-R geostationary navigation equations, returning scan radians."""
    attrs = projection.attrs
    req = float(np.asarray(attrs["semi_major_axis"]).flat[0])
    rpol = float(np.asarray(attrs["semi_minor_axis"]).flat[0])
    height = float(np.asarray(attrs["perspective_point_height"]).flat[0]) + req
    lon0 = math.radians(float(np.asarray(attrs["longitude_of_projection_origin"]).flat[0]))
    lat = math.radians(latitude)
    lon = math.radians(longitude)
    phi_c = math.atan((rpol * rpol / (req * req)) * math.tan(lat))
    rc = rpol / math.sqrt(1 - ((req * req - rpol * rpol) / (req * req)) * math.cos(phi_c) ** 2)
    sx = height - rc * math.cos(phi_c) * math.cos(lon - lon0)
    sy = -rc * math.cos(phi_c) * math.sin(lon - lon0)
    sz = rc * math.sin(phi_c)
    if height * (height - sx) < sy * sy + (req * req / (rpol * rpol)) * sz * sz:
        raise ValueError("Location is outside the GOES-19 visible disk")
    return math.asin(-sy / math.sqrt(sx * sx + sy * sy + sz * sz)), math.atan(sz / sx)


def fixed_grid_latlon(x: float, y: float, projection: h5py.Dataset) -> tuple[float, float]:
    """Invert a GOES-R fixed-grid coordinate using the product projection metadata."""
    attrs = projection.attrs
    req = float(np.asarray(attrs["semi_major_axis"]).flat[0])
    rpol = float(np.asarray(attrs["semi_minor_axis"]).flat[0])
    h = float(np.asarray(attrs["perspective_point_height"]).flat[0]) + req
    lon0 = math.radians(float(np.asarray(attrs["longitude_of_projection_origin"]).flat[0]))
    ratio = req * req / (rpol * rpol)
    a = math.sin(x) ** 2 + math.cos(x) ** 2 * (math.cos(y) ** 2 + ratio * math.sin(y) ** 2)
    b = -2 * h * math.cos(x) * math.cos(y)
    c = h * h - req * req
    discriminant = b * b - 4 * a * c
    if discriminant < 0:
        raise ValueError("Fixed-grid coordinate is outside the Earth disk")
    rs = (-b - math.sqrt(discriminant)) / (2 * a)
    sx = rs * math.cos(x) * math.cos(y)
    sy = -rs * math.sin(x)
    sz = rs * math.cos(x) * math.sin(y)
    latitude = math.atan(ratio * sz / math.sqrt((h - sx) ** 2 + sy * sy))
    longitude = lon0 - math.atan2(sy, h - sx)
    return math.degrees(latitude), math.degrees(longitude)


def sample_pixel(path: Path, latitude: float, longitude: float) -> dict[str, object]:
    with h5py.File(path, "r") as nc:
        x_rad, y_rad = fixed_grid_xy(latitude, longitude, nc["goes_imager_projection"])
        x_values = nc["x"][:] * nc["x"].attrs["scale_factor"][0] + nc["x"].attrs["add_offset"][0]
        y_values = nc["y"][:] * nc["y"].attrs["scale_factor"][0] + nc["y"].attrs["add_offset"][0]
        col = int(np.abs(x_values - x_rad).argmin())
        row = int(np.abs(y_values - y_rad).argmin())
        dqf = int(nc["DQF"][row, col])
        center_lat, center_lon = fixed_grid_latlon(float(x_values[col]), float(y_values[row]), nc["goes_imager_projection"])
        x_step = abs(float(x_values[min(col + 1, len(x_values) - 1)] - x_values[max(col - 1, 0)])) / 2
        y_step = abs(float(y_values[min(row + 1, len(y_values) - 1)] - y_values[max(row - 1, 0)])) / 2
        footprint = []
        for yy in (float(y_values[row]) - y_step / 2, float(y_values[row]) + y_step / 2):
            for xx in (float(x_values[col]) - x_step / 2, float(x_values[col]) + x_step / 2):
                corner_lat, corner_lon = fixed_grid_latlon(xx, yy, nc["goes_imager_projection"])
                footprint.append({"latitude": corner_lat, "longitude": corner_lon})
        return {
            "row": row, "column": col, "fixed_grid_x_rad": float(x_values[col]),
            "fixed_grid_y_rad": float(y_values[row]), "dqf": dqf,
            "sampled_pixel_center": {"latitude": center_lat, "longitude": center_lon},
            "sampled_pixel_corners": footprint,
            "brf": {str(b): _decoded(nc[f"BRF{b}"], (row, col)) for b in (2, 3, 5)},
            "time_coverage_start": nc.attrs["time_coverage_start"].decode(),
            "time_coverage_end": nc.attrs["time_coverage_end"].decode(),
            "dataset_name": nc.attrs["dataset_name"].decode(),
        }


def decode_dqf(dqf: int) -> dict[str, bool | int]:
    """Decode product-defined bits; acceptance remains notebook-gated."""
    return {
        "overall_quality": dqf & 0b111,
        "retrieval_path": (dqf & 0b11000) >> 3,
        "small_scattering_angle": bool(dqf & 0b100000),
        "not_absolutely_clear": bool(dqf & 0b1000000),
        "invalid_aerosol_climatology": bool(dqf & 0b10000000),
    }
