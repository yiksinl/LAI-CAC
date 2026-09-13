from __future__ import annotations

import hashlib
import json
import re
import struct
import threading
import time
from pathlib import Path

import pytest

from lai_cac.navigation import read_navigation_pixel, read_remote_navigation_pixel
from lai_cac.navigation_asset import (
    NAVIGATION_ETAG,
    NAVIGATION_LAYOUT,
    NAVIGATION_NAME,
    NAVIGATION_SHA256,
    NAVIGATION_SIZE_BYTES,
    NAVIGATION_URL,
    SOURCE,
    NavigationSource,
    RemoteNavigationError,
    RemoteNavigationStore,
)


class FakeResponse:
    def __init__(
        self,
        status: int,
        headers: dict[str, str],
        data: bytes = b"",
    ) -> None:
        self.status = status
        self.headers = headers
        self.data = data
        self.read_calls = 0
        self.closed = False
        self.released = False

    def read(self, amount: int) -> bytes:
        self.read_calls += 1
        return self.data[:amount]

    def close(self) -> None:
        self.closed = True

    def release_conn(self) -> None:
        self.released = True


class ObjectPool:
    def __init__(
        self,
        data: bytes,
        source: NavigationSource,
        *,
        range_status: int = 206,
        range_etag: str | None = None,
        short_body: bool = False,
        range_error: Exception | None = None,
        delay: float = 0.0,
        metadata_etag: str | None = None,
        metadata_size: int | None = None,
        accept_ranges: str | None = "bytes",
    ) -> None:
        self.data = data
        self.source = source
        self.range_status = range_status
        self.range_etag = range_etag or source.etag
        self.short_body = short_body
        self.range_error = range_error
        self.delay = delay
        self.metadata_etag = metadata_etag or source.etag
        self.metadata_size = metadata_size or source.size_bytes
        self.accept_ranges = accept_ranges
        self.calls: list[tuple[str, str | None]] = []
        self.responses: list[FakeResponse] = []
        self._lock = threading.Lock()

    def request(self, method: str, url: str, headers: dict[str, str], **kwargs):
        assert url == self.source.url
        assert kwargs["preload_content"] is False
        assert kwargs["decode_content"] is False
        requested_range = headers.get("Range")
        with self._lock:
            self.calls.append((method, requested_range))
        if method == "HEAD":
            response_headers = {
                "ETag": self.metadata_etag,
                "Content-Length": str(self.metadata_size),
            }
            if self.accept_ranges is not None:
                response_headers["Accept-Ranges"] = self.accept_ranges
            response = FakeResponse(200, response_headers)
        else:
            if self.range_error is not None:
                raise self.range_error
            match = re.fullmatch(r"bytes=(\d+)-(\d+)", requested_range or "")
            assert method == "GET" and match
            start, end = map(int, match.groups())
            if self.delay:
                time.sleep(self.delay)
            body = self.data[start : end + 1]
            if self.short_body:
                body = body[:-1]
            if self.range_status == 206:
                content_length = end - start + 1
                response_headers = {
                    "ETag": self.range_etag,
                    "Content-Length": str(content_length),
                    "Content-Range": (
                        f"bytes {start}-{end}/{self.source.size_bytes}"
                    ),
                }
            else:
                response_headers = {
                    "ETag": self.range_etag,
                    "Content-Length": str(self.source.size_bytes),
                }
            response = FakeResponse(self.range_status, response_headers, body)
        with self._lock:
            self.responses.append(response)
        return response


def small_source(data: bytes) -> NavigationSource:
    digest = hashlib.sha256(data).hexdigest()
    return NavigationSource(
        name="navigation.nc",
        url="https://example.invalid/navigation.nc",
        size_bytes=len(data),
        sha256=digest,
        etag=f'"{digest}"',
        git_commit="immutable-test-commit",
    )


def test_navigation_descriptor_pins_object_and_storage_layout():
    identity = SOURCE.identity()
    assert identity["url"] == NAVIGATION_URL
    assert identity["size_bytes"] == NAVIGATION_SIZE_BYTES
    assert identity["sha256"] == NAVIGATION_SHA256
    assert identity["etag"] == NAVIGATION_ETAG
    assert identity["shape"] == [5424, 5424]
    assert identity["array_order"] == "C"
    assert identity["variables"] == {
        name: {
            "offset": specification["offset"],
            "dtype": specification["dtype"],
            "itemsize": specification["itemsize"],
        }
        for name, specification in NAVIGATION_LAYOUT.items()
    }


def test_navigation_source_record_matches_runtime_descriptor():
    root = Path(__file__).parents[1]
    record = json.loads(
        (root / "artifacts/reference/navigation-source.json").read_text(
            encoding="utf-8"
        )
    )
    identity = SOURCE.identity()
    for key in (
        "name",
        "url",
        "git_commit",
        "size_bytes",
        "sha256",
        "etag",
        "shape",
        "array_order",
        "variables",
    ):
        record_key = "filename" if key == "name" else key
        assert record[record_key] == identity[key]
    assert record["mode"] == "remote_http_byte_ranges"
    assert record["full_file_fallback"] is False
    assert record["local_comparison_path"] == NAVIGATION_NAME


@pytest.mark.parametrize(
    ("pool_options", "message"),
    [
        ({"metadata_etag": '"other"'}, "ETag"),
        ({"metadata_size": 63}, "Content-Length"),
        ({"accept_ranges": None}, "does not advertise byte ranges"),
    ],
)
def test_metadata_identity_failures_stop_before_any_range_request(
    tmp_path: Path, pool_options: dict[str, object], message: str
):
    data = bytes(range(64))
    source = small_source(data)
    pool = ObjectPool(data, source, **pool_options)
    store = RemoteNavigationStore(tmp_path, source=source, pool=pool)

    with pytest.raises(RemoteNavigationError, match=message):
        store.read_range(2, 5)

    assert [method for method, _ in pool.calls] == ["HEAD"]
    assert pool.responses[-1].read_calls == 0
    assert pool.responses[-1].closed is True


def test_exact_range_is_cached_with_checksum_and_shared_by_concurrent_readers(
    tmp_path: Path,
):
    data = bytes(range(64))
    source = small_source(data)
    pool = ObjectPool(data, source, delay=0.02)
    stores = [
        RemoteNavigationStore(tmp_path, source=source, pool=pool),
        RemoteNavigationStore(tmp_path, source=source, pool=pool),
    ]
    results: list[tuple[bytes, dict[str, object]]] = []

    def read(store: RemoteNavigationStore):
        results.append(store.read_range(12, 19))

    threads = [threading.Thread(target=read, args=(store,)) for store in stores]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert [value for value, _ in results] == [data[12:20], data[12:20]]
    assert sum(method == "GET" for method, _ in pool.calls) == 1
    assert {record["source"] for _, record in results} == {"network", "cache"}
    cached = next(stores[0].cache_dir.glob("*.bin"))
    checksum = cached.with_suffix(".sha256").read_text(encoding="ascii").strip()
    assert checksum == hashlib.sha256(data[12:20]).hexdigest()


def test_corrupt_cached_range_is_refetched(tmp_path: Path):
    data = bytes(range(64))
    source = small_source(data)
    pool = ObjectPool(data, source)
    store = RemoteNavigationStore(tmp_path, source=source, pool=pool)
    assert store.read_range(2, 5)[0] == data[2:6]
    next(store.cache_dir.glob("*.bin")).write_bytes(b"bad!")

    repaired, record = store.read_range(2, 5)

    assert repaired == data[2:6]
    assert record["source"] == "network"
    assert sum(method == "GET" for method, _ in pool.calls) == 2


def test_ignored_range_response_is_rejected_before_reading_body(tmp_path: Path):
    data = bytes(range(64))
    source = small_source(data)
    pool = ObjectPool(data, source, range_status=200)
    store = RemoteNavigationStore(tmp_path, source=source, pool=pool)

    with pytest.raises(RemoteNavigationError, match="full-file fallback is disabled"):
        store.read_range(2, 5)

    response = pool.responses[-1]
    assert response.read_calls == 0
    assert response.closed is True


def test_wrong_range_etag_is_rejected_before_reading_body(tmp_path: Path):
    data = bytes(range(64))
    source = small_source(data)
    pool = ObjectPool(data, source, range_etag='"different-object"')
    store = RemoteNavigationStore(tmp_path, source=source, pool=pool)

    with pytest.raises(RemoteNavigationError, match="does not match"):
        store.read_range(2, 5)

    response = pool.responses[-1]
    assert response.read_calls == 0
    assert response.closed is True


def test_short_range_body_fails_closed_without_creating_cache(tmp_path: Path):
    data = bytes(range(64))
    source = small_source(data)
    pool = ObjectPool(data, source, short_body=True)
    store = RemoteNavigationStore(tmp_path, source=source, pool=pool)

    with pytest.raises(RemoteNavigationError, match="returned 3 bytes; expected 4"):
        store.read_range(2, 5)

    assert not list(store.cache_dir.glob("*.bin"))
    assert pool.responses[-1].released is True


def test_range_network_failure_has_no_complete_file_fallback(tmp_path: Path):
    data = bytes(range(64))
    source = small_source(data)
    pool = ObjectPool(data, source, range_error=OSError("offline"))
    store = RemoteNavigationStore(tmp_path, source=source, pool=pool)

    with pytest.raises(RemoteNavigationError, match="request failed: offline"):
        store.read_range(2, 5)

    assert [method for method, _ in pool.calls] == ["HEAD", "GET"]
    assert not list(store.cache_dir.glob("*.bin"))


MONTGOMERY_RANGES = {
    (4_335_663, 4_335_663): struct.pack("<b", 1),
    (64_034_268, 64_034_275): struct.pack("<d", 39.15026956927703),
    (299_393_064, 299_393_071): struct.pack("<d", 45.385858840356114),
    (534_751_856, 534_751_863): struct.pack("<d", -77.24355094783465),
    (14_216_369, 14_216_369): struct.pack("<b", 7),
    (143_079_916, 143_079_923): struct.pack("<d", 1.7431137536994001),
    (378_438_712, 378_438_719): struct.pack("<d", 45.09029333558558),
    (613_797_504, 613_797_511): struct.pack("<d", -113.90744806478645),
}


class NavigationPool:
    def __init__(self, *, local_path: Path | None = None) -> None:
        self.local_path = local_path
        self.calls: list[tuple[str, str | None]] = []
        self._lock = threading.Lock()

    def request(self, method: str, url: str, headers: dict[str, str], **kwargs):
        assert url == NAVIGATION_URL
        requested_range = headers.get("Range")
        with self._lock:
            self.calls.append((method, requested_range))
        if method == "HEAD":
            return FakeResponse(200, {
                "ETag": NAVIGATION_ETAG,
                "Content-Length": str(NAVIGATION_SIZE_BYTES),
                "Accept-Ranges": "bytes",
            })
        match = re.fullmatch(r"bytes=(\d+)-(\d+)", requested_range or "")
        assert method == "GET" and match
        start, end = map(int, match.groups())
        if self.local_path is None:
            data = MONTGOMERY_RANGES[(start, end)]
        else:
            with self.local_path.open("rb") as stream:
                stream.seek(start)
                data = stream.read(end - start + 1)
        return FakeResponse(206, {
            "ETag": NAVIGATION_ETAG,
            "Content-Length": str(end - start + 1),
            "Content-Range": f"bytes {start}-{end}/{NAVIGATION_SIZE_BYTES}",
        }, data)


def montgomery_sample() -> dict[str, object]:
    return {
        "row": 797,
        "column": 2619,
        "sampled_pixel_center": {
            "latitude": 39.15026577665899,
            "longitude": -77.24354489354585,
        },
        "view_geometry_parameters": {
            "satellite_longitude": -75.19999694824219,
            "semi_major": 6_378_137.0,
            "semi_minor": 6_356_752.31414,
            "perspective_height": 35_786_023.0,
        },
    }


def test_remote_pixel_matches_checksum_pinned_montgomery_golden(tmp_path: Path):
    pool = NavigationPool()
    store = RemoteNavigationStore(tmp_path, pool=pool)

    navigation = read_remote_navigation_pixel(
        montgomery_sample(), 39.1547, -77.2405, tmp_path, store=store
    )

    assert navigation["verified_for_exact_pixel"] is True
    assert navigation["array_orientation"] == "row_column"
    assert navigation["raster_pixel"] == {
        "latitude": pytest.approx(39.1502685546875),
        "longitude": pytest.approx(-77.2435531616211),
        "local_zenith_angle": pytest.approx(45.385860443115234),
        "land_mask": 1.0,
        "dataset_indices": [797, 2619],
        "conversion": (
            "raster scalars assigned to float32 arrays as in notebook Cell 2"
        ),
    }
    retrieval = navigation["retrieval"]
    assert retrieval["range_cache"]["request_count"] == 8
    assert retrieval["range_cache"]["downloaded_payload_bytes"] == 50
    assert retrieval["range_cache"]["cache_hit_count"] == 0
    assert retrieval["full_file_fallbacks"] == 0
    assert retrieval["startup_through_result"]["total_http_request_count"] == 9

    warm = read_remote_navigation_pixel(
        montgomery_sample(), 39.1547, -77.2405, tmp_path, store=store
    )
    assert warm["raster_pixel"] == navigation["raster_pixel"]
    assert warm["retrieval"]["range_cache"]["request_count"] == 0
    assert warm["retrieval"]["range_cache"]["cache_hit_bytes"] == 50
    assert sum(method == "GET" for method, _ in pool.calls) == 8


def test_remote_backend_matches_optional_local_reference_raster(tmp_path: Path):
    root = Path(__file__).parents[1]
    path = root / "artifacts/reference" / NAVIGATION_NAME
    if not path.is_file() or path.stat().st_size != NAVIGATION_SIZE_BYTES:
        pytest.skip("Optional local navigation comparison raster is not installed")
    pool = NavigationPool(local_path=path)
    store = RemoteNavigationStore(tmp_path, pool=pool)
    sample = montgomery_sample()

    local = read_navigation_pixel(path, sample, 39.1547, -77.2405)
    remote = read_remote_navigation_pixel(
        sample, 39.1547, -77.2405, tmp_path, store=store
    )

    for key in (
        "verified_for_exact_pixel",
        "array_orientation",
        "orientation_check",
        "raster_pixel",
        "projection_derived_pixel",
        "comparison",
        "feature_geometry",
    ):
        assert remote[key] == local[key]
