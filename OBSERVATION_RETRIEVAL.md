# Optimized GOES-19 observation retrieval

## Verdict: validated and adopted

LeafView now uses partial HTTP reads for a NOAA GOES-19 BRFF object when the
existing full-file cache does not already contain that object. The reader exposes a
seekable binary file object to h5py, which retrieves and checksums only the 128 KiB
blocks needed to open the HDF5 structure and read `x`, `y`, `t`, projection metadata,
`nominal_satellite_subpoint_lon`, `BRF2`, `BRF3`, `BRF5`, and `DQF` for the selected
pixel. Existing files in `data/cache` retain first priority and are not converted,
moved, or removed.

The production order is:

1. Reuse a size-matched existing full NetCDF file.
2. Read required HDF5 blocks from NOAA S3 with HTTP byte ranges, reusing the
   checksummed partial-block cache.
3. If the range/HDF5 route fails, download the complete object through the existing
   full-file cache route.
4. If both network routes fail, raise an observation-retrieval error. This does not
   become an `insufficient_data` result; that state is reserved for successfully
   decoded observations that fail the supplied quality and geometry filters.

The request pool reuses HTTP connections and is bounded to six connections. Scan
selection and observation sampling use at most four workers. Per-object locks make
simultaneous queries share each missing block; a two-reader cold test used nine
requests total rather than eighteen.

## Numerical consistency

The April 7–14 Montgomery composite was reread from an empty partial cache and
compared with `data/results/montgomery-md-2026-04-07.json`:

- all 24 attempt records matched exactly after excluding the newly added retrieval
  provenance, including fixed-grid indices and coordinates, BRF2/3/5, DQF,
  observation timestamps, geometry, and filtering decisions;
- an explicit missing-data pixel check returned NaN for BRF2/3/5 and DQF 255 through
  both the local and remote readers;
- all 56 features matched exactly;
- retained counts remained 4/5/6; and
- LAI remained exactly `1.20430588722229`.

The April 15–22 benchmark also reproduced all attempts, all features, counts 2/1/2,
and LAI `2.283097743988037` exactly.

## Cold-cache benchmark

The adopted complete-query benchmark used Montgomery County for April 15–22 with
an empty partial cache. The existing full-file observation cache remained in place
but was deliberately bypassed for the partial-reader measurement.

| Route | Observation bytes | HTTP requests | Pipeline time |
|---|---:|---:|---:|
| Existing cold full downloads | 778,474,187 | 24 object GETs + 24 listings | 99.262 s |
| Empty partial cache | 30,576,780 ranges + 61,104 listings | 244 ranges + 24 listings | 20.852 s |
| Warm partial cache | 0 range bytes + listings | 0 ranges + 24 listings | 1.812 s |

The cold partial route reduced observation-object bytes by 96.07% and completed
4.76 times faster. It did not invoke the full-file fallback. Results are recorded in
`benchmarks/observation-retrieval-2026-09-09.json`.

## Provenance and checksum scope

Every partial-read attempt records the NOAA bucket/key, S3 URI, HTTPS URL, object
size, ETag, and last-modified value. The ETag is explicitly described as an object
identifier, not a SHA-256 checksum.

Each cached block has its own SHA-256 sidecar. Result provenance includes the exact
byte ranges, each range checksum, and a deterministic `partial_data_sha256` over the
ordered offsets and bytes used for that sample. It also sets `whole_file_sha256` to
null and states that the partial checksum is not a whole-file checksum. Existing
full-file reads are identified separately and do not claim a whole-file SHA-256 when
one was not calculated.

## Implementation references

- [h5py file-object support](https://docs.h5py.org/en/3.15.0/high/file.html#python-file-like-objects)
- [Amazon S3 `GetObject` range semantics](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObject.html)
- [urllib3 connection pooling](https://urllib3.readthedocs.io/en/stable/reference/urllib3.poolmanager.html)
