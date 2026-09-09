from __future__ import annotations

import hashlib
import io
import json
import threading
from pathlib import Path

import urllib3

BLOCK_SIZE = 128 * 1024
MAX_HTTP_CONNECTIONS = 6


class RemoteRangeError(OSError):
    """A remote object could not be read safely with HTTP byte ranges."""


_HTTP = urllib3.PoolManager(
    num_pools=2,
    maxsize=MAX_HTTP_CONNECTIONS,
    block=True,
    timeout=urllib3.Timeout(connect=10, read=45),
    retries=urllib3.Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.25,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    ),
)
_LOCKS_GUARD = threading.Lock()
_OBJECT_LOCKS: dict[str, threading.Lock] = {}


def shared_http_pool() -> urllib3.PoolManager:
    return _HTTP


def _object_lock(cache_key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _OBJECT_LOCKS.setdefault(cache_key, threading.Lock())


class HTTPRangeReader(io.RawIOBase):
    """Seekable remote object reader for h5py with checksummed block caching."""

    def __init__(
        self,
        url: str,
        size: int,
        cache_root: Path,
        *,
        object_identifier: dict[str, object],
        etag: str | None = None,
        block_size: int = BLOCK_SIZE,
        pool: urllib3.PoolManager | None = None,
    ) -> None:
        super().__init__()
        if size <= 0:
            raise ValueError("Remote object size must be positive")
        if block_size <= 0:
            raise ValueError("Remote block size must be positive")
        self.url = url
        self.size = int(size)
        self.block_size = int(block_size)
        self.object_identifier = object_identifier
        self.etag = etag
        self.position = 0
        self.pool = pool or shared_http_pool()
        identity = json.dumps(
            {
                "format": 1,
                "url": url,
                "size": self.size,
                "etag": etag,
                "object_identifier": object_identifier,
                "block_size": self.block_size,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.cache_key = hashlib.sha256(identity).hexdigest()
        self.cache_dir = cache_root / self.cache_key
        self._memory_blocks: dict[int, bytes] = {}
        self._used_blocks: set[int] = set()
        self.request_count = 0
        self.downloaded_bytes = 0
        self.cache_hit_bytes = 0
        self.cache_hit_blocks = 0
        self._ensure_manifest()

    def _ensure_manifest(self) -> None:
        manifest = {
            "format": 1,
            "remote_object": self.object_identifier,
            "block_size_bytes": self.block_size,
            "integrity": (
                "Each .bin block has a sibling .sha256 covering that exact byte range; "
                "these are partial-data checksums, not a whole-file SHA-256."
            ),
        }
        with _object_lock(self.cache_key):
            self.cache_dir.mkdir(parents=True, exist_ok=True)
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

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        self._checkClosed()
        if whence == io.SEEK_SET:
            position = offset
        elif whence == io.SEEK_CUR:
            position = self.position + offset
        elif whence == io.SEEK_END:
            position = self.size + offset
        else:
            raise ValueError(f"Unsupported seek origin {whence}")
        if position < 0:
            raise ValueError("Negative seek position")
        self.position = int(position)
        return self.position

    def tell(self) -> int:
        return self.position

    def readinto(self, target: bytearray | memoryview) -> int:
        self._checkClosed()
        if self.position >= self.size:
            return 0
        length = min(len(target), self.size - self.position)
        copied = 0
        while copied < length:
            block_number, offset = divmod(self.position, self.block_size)
            block = self._get_block(block_number)
            amount = min(length - copied, len(block) - offset)
            target[copied:copied + amount] = block[offset:offset + amount]
            copied += amount
            self.position += amount
        return copied

    def _paths(self, block_number: int) -> tuple[Path, Path]:
        stem = f"{block_number:08d}"
        return self.cache_dir / f"{stem}.bin", self.cache_dir / f"{stem}.sha256"

    def _expected_length(self, block_number: int) -> int:
        start = block_number * self.block_size
        return min(self.block_size, self.size - start)

    def _read_cached_block(self, block_number: int) -> bytes | None:
        data_path, checksum_path = self._paths(block_number)
        if not data_path.is_file() or not checksum_path.is_file():
            return None
        try:
            data = data_path.read_bytes()
            expected_checksum = checksum_path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            return None
        if len(data) != self._expected_length(block_number):
            return None
        if hashlib.sha256(data).hexdigest() != expected_checksum:
            return None
        return data

    def _fetch_block(self, block_number: int) -> bytes:
        start = block_number * self.block_size
        end = min(self.size, start + self.block_size) - 1
        headers = {
            "Range": f"bytes={start}-{end}",
            "Accept-Encoding": "identity",
            "User-Agent": "LeafView-LAI-CAC/0.1 range-reader",
        }
        if self.etag:
            headers["If-Match"] = self.etag
        try:
            response = self.pool.request(
                "GET", self.url, headers=headers, preload_content=True, decode_content=False
            )
        except Exception as error:
            raise RemoteRangeError(f"NOAA range request failed for bytes {start}-{end}: {error}") from error
        if response.status != 206:
            raise RemoteRangeError(
                f"NOAA range request returned HTTP {response.status} for bytes {start}-{end}"
            )
        expected_range = f"bytes {start}-{end}/{self.size}"
        if response.headers.get("Content-Range") != expected_range:
            raise RemoteRangeError(
                f"NOAA range response identified {response.headers.get('Content-Range')!r}; "
                f"expected {expected_range!r}"
            )
        data = response.data
        if len(data) != end - start + 1:
            raise RemoteRangeError(
                f"NOAA range response was short for bytes {start}-{end}: received {len(data)}"
            )
        self.request_count += 1
        self.downloaded_bytes += len(data)
        return data

    def _write_block(self, block_number: int, data: bytes) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        data_path, checksum_path = self._paths(block_number)
        checksum = hashlib.sha256(data).hexdigest()
        data_temporary = data_path.with_suffix(".bin.part")
        checksum_temporary = checksum_path.with_suffix(".sha256.part")
        data_temporary.write_bytes(data)
        checksum_temporary.write_text(checksum + "\n", encoding="ascii")
        data_temporary.replace(data_path)
        checksum_temporary.replace(checksum_path)

    def _get_block(self, block_number: int) -> bytes:
        if block_number in self._memory_blocks:
            return self._memory_blocks[block_number]
        if block_number < 0 or block_number * self.block_size >= self.size:
            raise RemoteRangeError(f"HDF5 requested invalid remote block {block_number}")
        with _object_lock(self.cache_key):
            data = self._read_cached_block(block_number)
            if data is None:
                data = self._fetch_block(block_number)
                self._write_block(block_number, data)
            else:
                self.cache_hit_blocks += 1
                self.cache_hit_bytes += len(data)
        self._memory_blocks[block_number] = data
        self._used_blocks.add(block_number)
        return data

    def provenance(self) -> dict[str, object]:
        ranges = []
        partial_digest = hashlib.sha256()
        for number in sorted(self._used_blocks):
            data = self._memory_blocks[number]
            start = number * self.block_size
            end = start + len(data) - 1
            checksum = hashlib.sha256(data).hexdigest()
            partial_digest.update(f"{start}:{end}:".encode("ascii"))
            partial_digest.update(data)
            ranges.append({
                "start": start,
                "end": end,
                "sha256": checksum,
            })
        return {
            "route": "http_byte_ranges",
            "remote_object": self.object_identifier,
            "partial_cache": {
                "cache_key": self.cache_key,
                "block_size_bytes": self.block_size,
                "blocks_used": len(ranges),
                "cache_hit_blocks": self.cache_hit_blocks,
                "cache_hit_bytes": self.cache_hit_bytes,
                "request_count": self.request_count,
                "downloaded_bytes": self.downloaded_bytes,
                "ranges": ranges,
                "partial_data_sha256": partial_digest.hexdigest(),
                "checksum_scope": (
                    "SHA-256 over ordered byte-range offsets and bytes used by this HDF5 sample; "
                    "this is not a whole-file SHA-256"
                ),
            },
            "whole_file_sha256": None,
            "whole_file_checksum_status": "not downloaded; whole-file SHA-256 unavailable",
        }
