from __future__ import annotations

import hashlib
import json
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows uses process-local locks.
    fcntl = None

import urllib3

NAVIGATION_NAME = "GOES_Navigation_2kmFD-GOES-East.nc"
NAVIGATION_SHA256 = "c08fa491793996ab204c936f4bb25f299f4cf18383b0bd3673db87f8bd780e96"
NAVIGATION_SIZE_BYTES = 735_508_871
NAVIGATION_GIT_COMMIT = "0d808c653be4cd45d0cd655b33e0874e9b326509"
NAVIGATION_URL = (
    "https://media.githubusercontent.com/media/yiksinl/LAI-CAC/"
    f"{NAVIGATION_GIT_COMMIT}/artifacts/reference/{NAVIGATION_NAME}"
)
NAVIGATION_ETAG = f'"{NAVIGATION_SHA256}"'
NAVIGATION_SHAPE = (5424, 5424)
NAVIGATION_LAYOUT = {
    "LandMask": {
        "offset": 10_116,
        "dtype": "<i1",
        "itemsize": 1,
        "fill_value": -1,
        "valid_range": (0, 7),
        "attributes": [
            "_FillValue",
            "long_name",
            "valid_range",
            "flag_meanings",
            "flag_values",
            "units",
        ],
    },
    "Latitude": {
        "offset": 29_429_892,
        "dtype": "<f8",
        "itemsize": 8,
        "fill_value": -999.0,
        "valid_range": (-81.15, 81.15),
        "attributes": ["_FillValue", "units", "long_name", "valid_range"],
    },
    "LocalZenithAngle": {
        "offset": 264_788_688,
        "dtype": "<f8",
        "itemsize": 8,
        "fill_value": -999.0,
        "valid_range": (0.0, 90.0),
        "attributes": ["_FillValue", "units", "long_name", "valid_range"],
    },
    "Longitude": {
        "offset": 500_147_480,
        "dtype": "<f8",
        "itemsize": 8,
        "fill_value": -999.0,
        "valid_range": (-158.2, 6.2),
        "attributes": ["_FillValue", "units", "long_name", "valid_range"],
    },
}
LAND_MASK_ATTRIBUTES = {
    "_FillValue": -1,
    "long_name": "ABI fixed grid land-sea mask classification",
    "valid_range": [0, 7],
    "flag_meanings": (
        "shallow oceanlandocean coastlines and lake shorelinesshallow inland water"
        "ephemeral waterdeep inland watermoderate or continental oceandeep ocean"
    ),
    "flag_values": [0, 1, 2, 3, 4, 5, 6, 7],
    "units": "1",
}


class RemoteNavigationError(OSError):
    """The exact navigation object could not be read safely with HTTP ranges."""


@dataclass(frozen=True)
class NavigationSource:
    name: str = NAVIGATION_NAME
    url: str = NAVIGATION_URL
    size_bytes: int = NAVIGATION_SIZE_BYTES
    sha256: str = NAVIGATION_SHA256
    etag: str = NAVIGATION_ETAG
    git_commit: str = NAVIGATION_GIT_COMMIT

    def identity(self) -> dict[str, object]:
        return {
            "descriptor_version": 1,
            "name": self.name,
            "url": self.url,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "etag": self.etag,
            "git_commit": self.git_commit,
            "format": "netCDF-4/HDF5; contiguous uncompressed arrays",
            "shape": list(NAVIGATION_SHAPE),
            "array_order": "C",
            "variables": {
                name: {
                    "offset": int(specification["offset"]),
                    "dtype": str(specification["dtype"]),
                    "itemsize": int(specification["itemsize"]),
                }
                for name, specification in NAVIGATION_LAYOUT.items()
            },
        }


SOURCE = NavigationSource()
_HTTP = urllib3.PoolManager(
    num_pools=2,
    maxsize=8,
    block=True,
    timeout=urllib3.Timeout(connect=10, read=30),
    retries=urllib3.Retry(
        total=2,
        connect=2,
        read=2,
        status=2,
        backoff_factor=0.25,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
    ),
)
_STORES_LOCK = threading.Lock()
_STORES: dict[str, "RemoteNavigationStore"] = {}
_RANGE_LOCKS_GUARD = threading.Lock()
_RANGE_LOCKS: dict[str, threading.Lock] = {}


def _range_lock(key: str) -> threading.Lock:
    with _RANGE_LOCKS_GUARD:
        return _RANGE_LOCKS.setdefault(key, threading.Lock())


@contextmanager
def _locked_range(key: str, lock_path: Path) -> Iterator[None]:
    with _range_lock(key):
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        if fcntl is None:
            yield
            return
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)


def _transport_attempts(response: object) -> int:
    retries = getattr(response, "retries", None)
    history = getattr(retries, "history", ()) if retries is not None else ()
    return 1 + len(history)


class RemoteNavigationStore:
    """Strict exact-range transport and checksummed partial cache for one object."""

    def __init__(
        self,
        cache_root: Path,
        *,
        source: NavigationSource = SOURCE,
        pool: urllib3.PoolManager | None = None,
    ) -> None:
        self.source = source
        self.pool = pool or _HTTP
        identity = json.dumps(
            source.identity(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self.cache_key = hashlib.sha256(identity).hexdigest()
        self.cache_dir = cache_root / self.cache_key
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._metadata_lock = threading.Lock()
        self._metadata: dict[str, object] | None = None
        self._metrics_lock = threading.Lock()
        self._metrics: dict[str, float | int] = {
            "metadata_request_count": 0,
            "metadata_payload_bytes": 0,
            "metadata_latency_seconds": 0.0,
            "range_request_count": 0,
            "range_payload_bytes": 0,
            "range_request_seconds": 0.0,
            "range_cache_hit_count": 0,
            "range_cache_hit_bytes": 0,
            "range_operation_count": 0,
            "range_operation_wall_seconds": 0.0,
        }
        self._ensure_manifest()

    def _ensure_manifest(self) -> None:
        manifest = {
            "format": 1,
            "source": self.source.identity(),
            "cache_policy": "Only exact requested scalar ranges are stored.",
            "fallback_policy": "Full-file download is disabled.",
            "integrity": "Every .bin range has a sibling SHA-256 file.",
        }
        path = self.cache_dir / "object.json"
        encoded = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        if path.is_file():
            try:
                if path.read_text(encoding="utf-8") == encoded:
                    return
            except (OSError, UnicodeError):
                pass
        temporary = path.with_suffix(".json.part")
        temporary.write_text(encoded, encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _close_without_body(response: object) -> None:
        close = getattr(response, "close", None)
        if close is not None:
            close()

    def _add_metrics(self, **changes: float | int) -> None:
        with self._metrics_lock:
            for key, value in changes.items():
                self._metrics[key] = self._metrics.get(key, 0) + value

    def validate_metadata(self) -> dict[str, object]:
        with self._metadata_lock:
            if self._metadata is not None:
                return {
                    **self._metadata,
                    "source": "process_cache",
                    "request_count": 0,
                    "payload_bytes": 0,
                    "latency_seconds": 0.0,
                }
            began = time.perf_counter()
            try:
                response = self.pool.request(
                    "HEAD",
                    self.source.url,
                    headers={
                        "Accept-Encoding": "identity",
                        "If-Match": self.source.etag,
                        "User-Agent": "LeafView-LAI-CAC/0.2 navigation-metadata",
                    },
                    preload_content=False,
                    decode_content=False,
                )
            except Exception as error:
                raise RemoteNavigationError(
                    f"Navigation metadata request failed: {error}"
                ) from error
            latency = time.perf_counter() - began
            attempts = _transport_attempts(response)
            status = int(response.status)
            etag = response.headers.get("ETag")
            content_length = response.headers.get("Content-Length")
            accept_ranges = response.headers.get("Accept-Ranges")
            if status != 200:
                self._close_without_body(response)
                raise RemoteNavigationError(
                    f"Navigation metadata returned HTTP {status}; expected 200"
                )
            if etag != self.source.etag:
                self._close_without_body(response)
                raise RemoteNavigationError(
                    f"Navigation metadata ETag {etag!r} does not match {self.source.etag!r}"
                )
            if content_length != str(self.source.size_bytes):
                self._close_without_body(response)
                raise RemoteNavigationError(
                    "Navigation metadata Content-Length "
                    f"{content_length!r} does not match {self.source.size_bytes}"
                )
            if accept_ranges is None or "bytes" not in accept_ranges.lower():
                self._close_without_body(response)
                raise RemoteNavigationError(
                    f"Navigation source does not advertise byte ranges: {accept_ranges!r}"
                )
            self._close_without_body(response)
            self._metadata = {
                "http_status": status,
                "etag": etag,
                "content_length": int(content_length),
                "accept_ranges": accept_ranges,
                "identity_verified": True,
            }
            self._add_metrics(
                metadata_request_count=attempts,
                metadata_payload_bytes=0,
                metadata_latency_seconds=latency,
            )
            return {
                **self._metadata,
                "source": "network",
                "request_count": attempts,
                "payload_bytes": 0,
                "latency_seconds": round(latency, 6),
            }

    def cached_metadata(self) -> dict[str, object] | None:
        with self._metadata_lock:
            return dict(self._metadata) if self._metadata is not None else None

    def _paths(self, start: int, end: int) -> tuple[Path, Path, Path]:
        stem = f"{start}-{end}"
        return (
            self.cache_dir / f"{stem}.bin",
            self.cache_dir / f"{stem}.sha256",
            self.cache_dir / f"{stem}.lock",
        )

    @staticmethod
    def _cached_range(
        data_path: Path, checksum_path: Path, expected_length: int
    ) -> bytes | None:
        if not data_path.is_file() or not checksum_path.is_file():
            return None
        try:
            data = data_path.read_bytes()
            expected_checksum = checksum_path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            return None
        if len(data) != expected_length:
            return None
        if hashlib.sha256(data).hexdigest() != expected_checksum:
            return None
        return data

    def read_range(self, start: int, end: int) -> tuple[bytes, dict[str, object]]:
        if start < 0 or end < start or end >= self.source.size_bytes:
            raise RemoteNavigationError(
                f"Invalid navigation range {start}-{end} for {self.source.size_bytes}-byte object"
            )
        self.validate_metadata()
        expected_length = end - start + 1
        data_path, checksum_path, lock_path = self._paths(start, end)
        lock_key = f"{self.cache_key}:{start}:{end}"
        with _locked_range(lock_key, lock_path):
            data = self._cached_range(data_path, checksum_path, expected_length)
            if data is not None:
                self._add_metrics(
                    range_cache_hit_count=1,
                    range_cache_hit_bytes=len(data),
                )
                return data, {
                    "start": start,
                    "end": end,
                    "bytes": len(data),
                    "source": "cache",
                    "latency_seconds": 0.0,
                    "sha256": hashlib.sha256(data).hexdigest(),
                }

            began = time.perf_counter()
            try:
                response = self.pool.request(
                    "GET",
                    self.source.url,
                    headers={
                        "Range": f"bytes={start}-{end}",
                        "Accept-Encoding": "identity",
                        "If-Match": self.source.etag,
                        "User-Agent": "LeafView-LAI-CAC/0.2 navigation-range",
                    },
                    preload_content=False,
                    decode_content=False,
                )
            except Exception as error:
                raise RemoteNavigationError(
                    f"Navigation range {start}-{end} request failed: {error}"
                ) from error
            expected_content_range = (
                f"bytes {start}-{end}/{self.source.size_bytes}"
            )
            status = int(response.status)
            content_range = response.headers.get("Content-Range")
            etag = response.headers.get("ETag")
            content_length = response.headers.get("Content-Length")
            if status != 206:
                self._close_without_body(response)
                raise RemoteNavigationError(
                    f"Navigation range {start}-{end} returned HTTP {status}; "
                    "full-file fallback is disabled"
                )
            if content_range != expected_content_range:
                self._close_without_body(response)
                raise RemoteNavigationError(
                    f"Navigation range response identified {content_range!r}; "
                    f"expected {expected_content_range!r}"
                )
            if etag != self.source.etag:
                self._close_without_body(response)
                raise RemoteNavigationError(
                    f"Navigation range ETag {etag!r} does not match {self.source.etag!r}"
                )
            if content_length != str(expected_length):
                self._close_without_body(response)
                raise RemoteNavigationError(
                    f"Navigation range Content-Length {content_length!r}; "
                    f"expected {expected_length}"
                )
            try:
                data = bytes(response.read(expected_length))
            finally:
                response.release_conn()
            latency = time.perf_counter() - began
            if len(data) != expected_length:
                raise RemoteNavigationError(
                    f"Navigation range {start}-{end} returned {len(data)} bytes; "
                    f"expected {expected_length}"
                )
            checksum = hashlib.sha256(data).hexdigest()
            data_temporary = data_path.with_suffix(".bin.part")
            checksum_temporary = checksum_path.with_suffix(".sha256.part")
            data_temporary.write_bytes(data)
            checksum_temporary.write_text(checksum + "\n", encoding="ascii")
            data_temporary.replace(data_path)
            checksum_temporary.replace(checksum_path)
            attempts = _transport_attempts(response)
            self._add_metrics(
                range_request_count=attempts,
                range_payload_bytes=len(data),
                range_request_seconds=latency,
            )
            return data, {
                "start": start,
                "end": end,
                "bytes": len(data),
                "source": "network",
                "request_count": attempts,
                "latency_seconds": round(latency, 6),
                "sha256": checksum,
            }

    def record_range_operation(self, wall_seconds: float) -> None:
        self._add_metrics(
            range_operation_count=1,
            range_operation_wall_seconds=wall_seconds,
        )

    def session_metrics(self) -> dict[str, object]:
        with self._metrics_lock:
            values = dict(self._metrics)
        metadata_seconds = float(values["metadata_latency_seconds"])
        operation_seconds = float(values["range_operation_wall_seconds"])
        metadata_requests = int(values["metadata_request_count"])
        range_requests = int(values["range_request_count"])
        metadata_bytes = int(values["metadata_payload_bytes"])
        range_bytes = int(values["range_payload_bytes"])
        return {
            **{
                key: round(value, 6) if isinstance(value, float) else value
                for key, value in values.items()
            },
            "total_http_request_count": metadata_requests + range_requests,
            "total_http_payload_bytes": metadata_bytes + range_bytes,
            "navigation_latency_seconds": round(
                metadata_seconds + operation_seconds, 6
            ),
            "full_file_fallbacks": 0,
        }


def get_remote_navigation_store(cache_root: Path) -> RemoteNavigationStore:
    key = str(cache_root.resolve())
    with _STORES_LOCK:
        return _STORES.setdefault(key, RemoteNavigationStore(cache_root))


def clear_remote_navigation_stores() -> None:
    """Forget process-local metadata and metrics; intended for isolated validation."""
    with _STORES_LOCK:
        _STORES.clear()
