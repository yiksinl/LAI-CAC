from __future__ import annotations

import math
import re
import threading
import urllib.parse
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback retains in-process locking.
    fcntl = None

import h5py
import netCDF4 as netcdf4
import numpy as np

from .remote_hdf5 import HTTPRangeReader, RemoteRangeError, shared_http_pool

BUCKET = "https://noaa-goes19.s3.amazonaws.com"
PRODUCT = "ABI-L2-BRFF"
SCAN_RE = re.compile(r"_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})(\d)_e")


@dataclass(frozen=True)
class Scan:
    key: str
    started_at: datetime
    size: int
    etag: str | None = None
    last_modified: str | None = None


@dataclass
class TransferMetrics:
    listing_request_count: int = 0
    listing_bytes: int = 0
    full_download_request_count: int = 0
    full_download_bytes: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, kind: str, byte_count: int) -> None:
        with self._lock:
            if kind == "listing":
                self.listing_request_count += 1
                self.listing_bytes += byte_count
            elif kind == "full_download":
                self.full_download_request_count += 1
                self.full_download_bytes += byte_count
            else:
                raise ValueError(f"Unknown transfer kind {kind}")

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "listing_request_count": self.listing_request_count,
                "listing_bytes": self.listing_bytes,
                "full_download_request_count": self.full_download_request_count,
                "full_download_bytes": self.full_download_bytes,
            }

    def merge(self, other: dict[str, int]) -> None:
        with self._lock:
            self.listing_request_count += int(other.get("listing_request_count", 0))
            self.listing_bytes += int(other.get("listing_bytes", 0))
            self.full_download_request_count += int(other.get("full_download_request_count", 0))
            self.full_download_bytes += int(other.get("full_download_bytes", 0))


class ObservationRetrievalError(RuntimeError):
    """A NOAA observation could not be retrieved by either supported route."""


_DOWNLOAD_LOCKS_GUARD = threading.Lock()
_DOWNLOAD_LOCKS: dict[str, threading.Lock] = {}


@contextmanager
def _download_lock(destination: Path) -> Iterator[None]:
    key = str(destination.resolve())
    with _DOWNLOAD_LOCKS_GUARD:
        thread_lock = _DOWNLOAD_LOCKS.setdefault(key, threading.Lock())
    with thread_lock:
        if fcntl is None:
            yield
            return
        lock_path = destination.with_suffix(destination.suffix + ".lock")
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)


def remote_object_metadata(scan: Scan) -> dict[str, object]:
    return {
        "provider": "NOAA public Amazon S3 archive",
        "bucket": "noaa-goes19",
        "key": scan.key,
        "s3_uri": f"s3://noaa-goes19/{scan.key}",
        "https_url": f"{BUCKET}/{urllib.parse.quote(scan.key, safe='/')}",
        "size_bytes": scan.size,
        "etag": scan.etag,
        "etag_note": "S3 object identifier; not represented as a SHA-256 checksum",
        "last_modified": scan.last_modified,
    }


def list_scans(day: date, hour: int, metrics: TransferMetrics | None = None) -> list[Scan]:
    prefix = f"{PRODUCT}/{day.year}/{day.timetuple().tm_yday:03d}/{hour:02d}/"
    query = urllib.parse.urlencode({"list-type": "2", "prefix": prefix})
    url = f"{BUCKET}/?{query}"
    try:
        response = shared_http_pool().request(
            "GET", url, headers={"Accept-Encoding": "identity", "User-Agent": "LeafView-LAI-CAC/0.1"}
        )
    except Exception as error:
        raise ObservationRetrievalError(f"NOAA scan listing failed for {day} {hour:02d} UTC: {error}") from error
    if response.status != 200:
        raise ObservationRetrievalError(
            f"NOAA scan listing returned HTTP {response.status} for {day} {hour:02d} UTC"
        )
    body = response.data
    if metrics is not None:
        metrics.add("listing", len(body))
    try:
        root = ET.fromstring(body)
    except ET.ParseError as error:
        raise ObservationRetrievalError(
            f"NOAA scan listing returned invalid XML for {day} {hour:02d} UTC"
        ) from error
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
        scans.append(Scan(
            key,
            started,
            int(item.findtext("s3:Size", "0", ns)),
            (item.findtext("s3:ETag", namespaces=ns) or "").strip() or None,
            item.findtext("s3:LastModified", namespaces=ns),
        ))
    return scans


def nearest_scan(
    day: date, target_hour: int, metrics: TransferMetrics | None = None
) -> Scan | None:
    target = datetime(day.year, day.month, day.day, target_hour, tzinfo=timezone.utc)
    candidates = list_scans(day, target_hour, metrics)
    selected = min(candidates, key=lambda scan: (abs(scan.started_at - target), scan.started_at), default=None)
    if selected is not None and abs((selected.started_at - target).total_seconds()) <= 20 * 60:
        return selected
    candidates = []
    for offset in (-1, 1):
        adjacent = target.fromtimestamp(target.timestamp() + offset * 3600, timezone.utc)
        candidates.extend(list_scans(adjacent.date(), adjacent.hour, metrics))
    selected = min(candidates, key=lambda scan: (abs(scan.started_at - target), scan.started_at), default=None)
    if selected is None or abs((selected.started_at - target).total_seconds()) > 20 * 60:
        return None
    return selected


def download(
    scan: Scan, destination: Path, metrics: TransferMetrics | None = None
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with _download_lock(destination):
        if destination.is_file() and destination.stat().st_size == scan.size:
            return destination
        temporary = destination.with_suffix(destination.suffix + ".part")
        url = f"{BUCKET}/{urllib.parse.quote(scan.key, safe='/')}"
        try:
            response = shared_http_pool().request(
                "GET",
                url,
                headers={"Accept-Encoding": "identity", "User-Agent": "LeafView-LAI-CAC/0.1"},
                preload_content=False,
            )
            if response.status != 200:
                raise ObservationRetrievalError(
                    f"NOAA full-file fallback returned HTTP {response.status} for {scan.key}"
                )
            downloaded = 0
            with temporary.open("wb") as target:
                for chunk in response.stream(1024 * 1024):
                    target.write(chunk)
                    downloaded += len(chunk)
        except Exception as error:
            raise ObservationRetrievalError(
                f"NOAA full-file fallback failed for {scan.key}: {error}"
            ) from error
        finally:
            if "response" in locals():
                response.release_conn()
        if downloaded != scan.size:
            raise ObservationRetrievalError(
                f"NOAA full-file fallback was short for {scan.key}: {downloaded} of {scan.size} bytes"
            )
        if metrics is not None:
            metrics.add("full_download", downloaded)
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


def _attribute_text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _scan_time_utc(time_dataset: h5py.Dataset) -> datetime:
    numeric_time = float(np.asarray(time_dataset[...]).squeeze())
    units = _attribute_text(time_dataset.attrs.get("units", "seconds since 2000-01-01 12:00:00"))
    calendar = _attribute_text(time_dataset.attrs.get("calendar", "standard"))
    scan_time = netcdf4.num2date(
        numeric_time, units, calendar=calendar, only_use_cftime_datetimes=False
    )
    if scan_time.tzinfo is None:
        scan_time = scan_time.replace(tzinfo=timezone.utc)
    return scan_time.astimezone(timezone.utc)


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


def _sample_open_hdf5(
    nc: h5py.File, latitude: float, longitude: float
) -> dict[str, object]:
    x_rad, y_rad = fixed_grid_xy(latitude, longitude, nc["goes_imager_projection"])
    x_values = nc["x"][:] * nc["x"].attrs["scale_factor"][0] + nc["x"].attrs["add_offset"][0]
    y_values = nc["y"][:] * nc["y"].attrs["scale_factor"][0] + nc["y"].attrs["add_offset"][0]
    col = int(np.abs(x_values - x_rad).argmin())
    row = int(np.abs(y_values - y_rad).argmin())
    dqf = int(nc["DQF"][row, col])
    center_lat, center_lon = fixed_grid_latlon(
        float(x_values[col]), float(y_values[row]), nc["goes_imager_projection"]
    )
    x_step = abs(
        float(x_values[min(col + 1, len(x_values) - 1)] - x_values[max(col - 1, 0)])
    ) / 2
    y_step = abs(
        float(y_values[min(row + 1, len(y_values) - 1)] - y_values[max(row - 1, 0)])
    ) / 2
    footprint = []
    for yy in (float(y_values[row]) - y_step / 2, float(y_values[row]) + y_step / 2):
        for xx in (float(x_values[col]) - x_step / 2, float(x_values[col]) + x_step / 2):
            corner_lat, corner_lon = fixed_grid_latlon(xx, yy, nc["goes_imager_projection"])
            footprint.append({"latitude": corner_lat, "longitude": corner_lon})
    scan_time = _scan_time_utc(nc["t"])
    projection = nc["goes_imager_projection"].attrs
    satellite_longitude = float(
        np.asarray(nc["nominal_satellite_subpoint_lon"][...]).reshape(-1)[0]
    )
    return {
        "row": row,
        "column": col,
        "fixed_grid_x_rad": float(x_values[col]),
        "fixed_grid_y_rad": float(y_values[row]),
        "dqf": dqf,
        "sampled_pixel_center": {"latitude": center_lat, "longitude": center_lon},
        "sampled_pixel_corners": footprint,
        "brf": {str(b): _decoded(nc[f"BRF{b}"], (row, col)) for b in (2, 3, 5)},
        "time_coverage_start": _attribute_text(nc.attrs["time_coverage_start"]),
        "time_coverage_end": _attribute_text(nc.attrs["time_coverage_end"]),
        "scan_time_utc": scan_time.isoformat(),
        "view_geometry_parameters": {
            "satellite_longitude": satellite_longitude,
            "semi_major": float(np.asarray(projection["semi_major_axis"]).reshape(-1)[0]),
            "semi_minor": float(np.asarray(projection["semi_minor_axis"]).reshape(-1)[0]),
            "perspective_height": float(
                np.asarray(projection["perspective_point_height"]).reshape(-1)[0]
            ),
        },
        "dataset_name": _attribute_text(nc.attrs["dataset_name"]),
    }


def sample_pixel(path: Path, latitude: float, longitude: float) -> dict[str, object]:
    with h5py.File(path, "r") as nc:
        return _sample_open_hdf5(nc, latitude, longitude)


def sample_remote_pixel(
    scan: Scan,
    cache_root: Path,
    latitude: float,
    longitude: float,
) -> dict[str, object]:
    remote = remote_object_metadata(scan)
    reader = HTTPRangeReader(
        str(remote["https_url"]),
        scan.size,
        cache_root,
        object_identifier=remote,
        etag=scan.etag,
    )
    try:
        with h5py.File(reader, "r") as nc:
            sample = _sample_open_hdf5(nc, latitude, longitude)
    except ValueError:
        raise
    except Exception as error:
        if isinstance(error, RemoteRangeError):
            raise
        raise RemoteRangeError(f"Remote HDF5 read failed for {scan.key}: {error}") from error
    sample["retrieval"] = reader.provenance()
    return sample


def decode_dqf(dqf: int) -> dict[str, bool | int]:
    """Decode product-defined bits; acceptance remains notebook-gated."""
    return {
        "overall_quality": dqf & 0b111,
        "retrieval_path": (dqf & 0b11000) >> 3,
        "small_scattering_angle": bool(dqf & 0b100000),
        "not_absolutely_clear": bool(dqf & 0b1000000),
        "invalid_aerosol_climatology": bool(dqf & 0b10000000),
    }


def passes_notebook_strict_quality(dqf: int) -> bool:
    decoded = decode_dqf(dqf)
    return bool(
        decoded["overall_quality"] == 0
        and decoded["retrieval_path"] == 2
        and not decoded["small_scattering_angle"]
        and not decoded["not_absolutely_clear"]
        and not decoded["invalid_aerosol_climatology"]
    )
