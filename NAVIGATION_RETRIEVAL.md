# Exact-range navigation retrieval

LeafView uses the notebook's original 5,424 × 5,424 GOES-East navigation raster
without installing or downloading its complete 735 MB file. The runtime source is
the existing Git LFS object at immutable repository commit
`0d808c653be4cd45d0cd655b33e0874e9b326509`. Its URL, 735,508,871-byte size,
SHA-256/ETag, shape, array order, variable offsets, data types, and item sizes are
pinned in `src/lai_cac/navigation_asset.py` and recorded in
`artifacts/reference/navigation-source.json`.

## Read contract

The four required arrays are contiguous and uncompressed in the NetCDF-4/HDF5
container. For both `(row, column)` and the notebook orientation-check candidate
`(column, row)`, LeafView reads:

- one byte from `LandMask`;
- eight bytes each from `Latitude`, `LocalZenithAngle`, and `Longitude`.

That is eight independent HTTP range requests and 50 payload bytes for a cold
location. GitHub's media endpoint does not safely support the disjoint multi-range
form for this object, so ranges are deliberately kept separate and run concurrently.

Before any range request, a metadata request must return HTTP 200 with the exact
ETag, content length, and byte-range advertisement. Every scalar response must then
return HTTP 206 with the exact ETag, `Content-Range`, `Content-Length`, and body
length. Response headers are checked before any body bytes are consumed. An ignored
range (HTTP 200), wrong object identity, wrong range, short body, invalid decoded
value, or network failure aborts navigation. A complete-file fallback does not
exist.

## Partial cache and provenance

Each accepted scalar range is stored under `data/navigation-cache/`, keyed by the
complete immutable source descriptor. A `.bin` file has a sibling SHA-256 file and
is written through atomic temporary files under a per-range process/file lock.
Length or checksum corruption causes only that exact range to be fetched again.

Result provenance records the immutable remote descriptor, metadata validation,
each used range, the ordered partial-data SHA-256, network and cache-hit byte counts,
latency, and a zero full-file-fallback count. Derived-result identity includes the
whole remote descriptor, so source, layout, or version changes cannot reuse an older
result silently.

## Validation

The production regression suite covers ignored-range, wrong-ETag, short-body,
network-failure, corrupt-cache, concurrent-reader, warm-cache, pinned-layout, and
scientific-golden cases. When the optional complete raster is present, an additional
test compares the local NetCDF reader with the exact-range backend at Montgomery
County's row 797/column 2619.

The 2026-09-13 live validation reproduced float32 Latitude 39.1502686, Longitude
-77.2435532, LocalZenithAngle 45.3858604°, LandMask 1, and the row-column
orientation. The cold navigation pass used 8 range requests and 50 payload bytes;
all three checked-in Montgomery composites retained byte-for-byte equal feature
dictionaries and unchanged LAI values of 1.2043058872, 2.2830977440, and
2.6258995533.
