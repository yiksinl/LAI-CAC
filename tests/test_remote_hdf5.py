from __future__ import annotations

import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import h5py
import numpy as np
import pytest

import lai_cac.inference as inference
import lai_cac.goes as goes
from lai_cac.goes import ObservationRetrievalError, Scan, TransferMetrics, _decoded
from lai_cac.remote_hdf5 import HTTPRangeReader, RemoteRangeError


class FakeResponse:
    def __init__(self, data: bytes, start: int, end: int, size: int):
        self.status = 206
        self.data = data[start:end + 1]
        self.headers = {"Content-Range": f"bytes {start}-{end}/{size}"}


class FakePool:
    def __init__(self, data: bytes, delay: float = 0):
        self.data = data
        self.delay = delay
        self.calls = 0
        self._lock = threading.Lock()

    def request(self, method: str, url: str, headers: dict, **kwargs):
        match = re.fullmatch(r"bytes=(\d+)-(\d+)", headers["Range"])
        assert method == "GET" and match
        start, end = map(int, match.groups())
        with self._lock:
            self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return FakeResponse(self.data, start, end, len(self.data))


class FakeFullResponse:
    status = 200

    def __init__(self, data: bytes):
        self.data = data

    def stream(self, chunk_size: int):
        for offset in range(0, len(self.data), chunk_size):
            yield self.data[offset:offset + chunk_size]

    def release_conn(self):
        return None


class FakeFullPool:
    def __init__(self, data: bytes):
        self.data = data
        self.calls = 0
        self._lock = threading.Lock()

    def request(self, *args, **kwargs):
        with self._lock:
            self.calls += 1
        time.sleep(0.02)
        return FakeFullResponse(self.data)


def make_reader(data: bytes, cache: Path, pool: FakePool) -> HTTPRangeReader:
    return HTTPRangeReader(
        "https://example.invalid/object.nc",
        len(data),
        cache,
        object_identifier={"key": "object.nc", "etag": '"test"'},
        etag='"test"',
        block_size=64,
        pool=pool,
    )


def test_concurrent_remote_readers_share_checksummed_blocks(tmp_path: Path):
    data = bytes(range(256)) * 4
    pool = FakePool(data, delay=0.02)
    readers = [make_reader(data, tmp_path, pool) for _ in range(2)]

    def read(reader: HTTPRangeReader) -> bytes:
        reader.seek(12)
        return reader.read(100)

    with ThreadPoolExecutor(max_workers=2) as executor:
        values = list(executor.map(read, readers))

    assert values == [data[12:112], data[12:112]]
    assert pool.calls == 2
    assert sum(reader.request_count for reader in readers) == 2
    provenance = readers[0].provenance()
    assert provenance["whole_file_sha256"] is None
    assert provenance["partial_cache"]["partial_data_sha256"]
    assert "not a whole-file SHA-256" in provenance["partial_cache"]["checksum_scope"]


def test_corrupt_partial_block_is_refetched(tmp_path: Path):
    data = bytes(range(128))
    pool = FakePool(data)
    reader = make_reader(data, tmp_path, pool)
    assert reader.read(10) == data[:10]
    block = next(tmp_path.rglob("*.bin"))
    block.write_bytes(b"corrupt")

    replacement = make_reader(data, tmp_path, pool)
    assert replacement.read(10) == data[:10]
    assert pool.calls == 2


def test_concurrent_full_fallback_downloads_share_one_transfer(tmp_path: Path, monkeypatch):
    data = b"complete NOAA object"
    pool = FakeFullPool(data)
    monkeypatch.setattr(goes, "shared_http_pool", lambda: pool)
    scan = Scan(
        "ABI-L2-BRFF/2026/097/15/object.nc",
        datetime(2026, 4, 7, 15, tzinfo=timezone.utc),
        len(data),
    )
    destination = tmp_path / "object.nc"
    metrics = TransferMetrics()
    with ThreadPoolExecutor(max_workers=2) as executor:
        paths = list(executor.map(lambda _: goes.download(scan, destination, metrics), range(2)))
    assert paths == [destination, destination]
    assert destination.read_bytes() == data
    assert pool.calls == 1
    assert metrics.snapshot()["full_download_request_count"] == 1


def test_transfer_metrics_merge_process_local_counts():
    metrics = TransferMetrics()
    metrics.add("listing", 11)
    metrics.merge({
        "listing_request_count": 0,
        "listing_bytes": 0,
        "full_download_request_count": 1,
        "full_download_bytes": 23,
    })
    assert metrics.snapshot() == {
        "listing_request_count": 1,
        "listing_bytes": 11,
        "full_download_request_count": 1,
        "full_download_bytes": 23,
    }


def test_fill_and_out_of_range_values_decode_to_nan(tmp_path: Path):
    path = tmp_path / "missing.h5"
    with h5py.File(path, "w") as target:
        dataset = target.create_dataset("value", data=np.array([65535, 101, 50], dtype="u2"))
        dataset.attrs["_FillValue"] = np.array([65535], dtype="u2")
        dataset.attrs["valid_range"] = np.array([0, 100], dtype="u2")
        dataset.attrs["scale_factor"] = np.array([0.1], dtype="f4")
        dataset.attrs["add_offset"] = np.array([0.2], dtype="f4")
    with h5py.File(path, "r") as source:
        assert math.isnan(_decoded(source["value"], (0,)))
        assert math.isnan(_decoded(source["value"], (1,)))
        assert _decoded(source["value"], (2,)) == pytest.approx(5.2)


def test_partial_failure_uses_existing_full_download_fallback(tmp_path: Path, monkeypatch):
    scan = Scan(
        "ABI-L2-BRFF/2026/097/15/object.nc",
        datetime(2026, 4, 7, 15, tzinfo=timezone.utc),
        4,
        '"etag"',
    )
    monkeypatch.setattr(
        inference,
        "sample_remote_pixel",
        lambda *args, **kwargs: (_ for _ in ()).throw(RemoteRangeError("range failed")),
    )

    def fake_download(scan, destination, metrics):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"full")
        metrics.add("full_download", 4)
        return destination

    monkeypatch.setattr(inference, "download", fake_download)
    monkeypatch.setattr(inference, "sample_pixel", lambda *args: {"dqf": 16})
    metrics = TransferMetrics()
    sample = inference._retrieve_observation(
        scan,
        tmp_path / "full" / "object.nc",
        tmp_path / "partial",
        39.0,
        -77.0,
        "auto",
        metrics,
    )
    assert sample["retrieval"]["route"] == "full_file_fallback"
    assert sample["retrieval"]["partial_error"] == "range failed"
    assert metrics.snapshot()["full_download_bytes"] == 4


def test_network_failure_is_not_an_insufficient_observation(tmp_path: Path, monkeypatch):
    scan = Scan(
        "ABI-L2-BRFF/2026/097/15/object.nc",
        datetime(2026, 4, 7, 15, tzinfo=timezone.utc),
        4,
    )
    monkeypatch.setattr(
        inference,
        "sample_remote_pixel",
        lambda *args, **kwargs: (_ for _ in ()).throw(RemoteRangeError("range failed")),
    )
    monkeypatch.setattr(
        inference,
        "download",
        lambda *args, **kwargs: (_ for _ in ()).throw(ObservationRetrievalError("full failed")),
    )
    with pytest.raises(ObservationRetrievalError, match="Partial read.*Full-file fallback"):
        inference._retrieve_observation(
            scan,
            tmp_path / "full" / "object.nc",
            tmp_path / "partial",
            39.0,
            -77.0,
            "auto",
            TransferMetrics(),
        )


def test_partial_checksum_changes_with_range_bytes(tmp_path: Path):
    first_data = b"a" * 128
    first = make_reader(first_data, tmp_path / "first", FakePool(first_data))
    first.read(5)
    second_data = b"b" * 128
    second = make_reader(second_data, tmp_path / "second", FakePool(second_data))
    second.read(5)
    assert (
        first.provenance()["partial_cache"]["partial_data_sha256"]
        != second.provenance()["partial_cache"]["partial_data_sha256"]
    )
