# Implementation status

Last updated: 2026-09-10

## Plan

1. **Completed:** recover and audit the authoritative notebook and model.
2. **Completed:** verify GOES-19 retrieval, fixed-grid navigation, decoding, and scan inventory.
3. **Completed:** import and checksum the original IGBP grid and supplied solar helper.
4. **Completed:** rerun the April 7–14 composite and expose its revised result.
5. **Completed:** verify and use the notebook's separate GOES navigation grid.
6. **Completed:** build and browser-test the query-bound LeafView MVP and three-period trend.
7. **Completed:** add automatic latest-window selection and progressive query-bound history.
8. **Completed:** make preselected month-end eight-day snapshots the default history.

## LeafView MVP

- The main flow now starts from the map point and automatically chooses the latest
  completed eight-day window. LeafView waits until two hours after the final 21:00
  UTC observation group before including that ending date. Historical selection is
  optional.
- **Get latest estimate** starts the real supplied inference pipeline for the exact
  map point and UTC window. Work runs outside the request thread, and
  the browser polls concrete asset, land-cover, observation, feature, model, and save
  stages while remaining responsive.
- Derived results are keyed by normalized coordinates, exact rolling-window dates, pipeline
  version, filter definition, and the model, IGBP, solar-helper, and navigation
  checksums. Raw observations are still accepted from cache only when their byte size
  matches the selected immutable NOAA object.
- Changing coordinates or the optional historical window immediately clears the
  displayed LAI, footprint, calculation timestamp, and history; a late estimate or
  history response is not rendered unless its query still matches. The result panel
  shows usable observations and distinct usable days, 15/18/21 UTC counts,
  fixed-grid row/column, center, and all four footprint corners.
- New result files record a UTC calculation timestamp in processing provenance.
  Saved results retain and display that original value; older files without a
  trustworthy timestamp show `Calculation time unavailable.`
- The trend covers the available part of the previous year with an eight-day window
  ending on each completed month's final day, plus the latest rolling eight-day
  window when distinct. Snapshot windows are selected before any work is scheduled
  and processed newest first. Every observation remains after the April 6, 2026
  training cutoff. The UI distinguishes outside-scope dates, pending work,
  insufficient data, retrieval errors, and processing errors. Missing estimates
  remain gaps.
- A process-local memo shares scan selections and sampled observations across
  overlapping windows. Exact estimate jobs are also deduplicated, and observation
  retrieval remains bounded at four workers.
- All output is labelled **Research estimate**. Zero-usable-observation runs return
  `insufficient_data` with `lai: null`; the trend renderer leaves these periods as
  gaps and never connects a line across one.
- Browser validation covered the latest Montgomery result, all six current monthly
  snapshots, optional historical selection, stale estimate/history rejection, and
  desktop/narrow layouts. The fully cached monthly history loaded 55 ms after the
  latest card in the browser capture.
  Detailed evidence and timings are in [MVP_VALIDATION.md](MVP_VALIDATION.md).

## Verified findings

- The separate `LAI-CAC` repository contains all work in this project. GreenOrbit and
  `garden_ndvi_desktop` were not changed.
- The first post-training composite is 2026-04-07 through 2026-04-14. NOAA's public
  `noaa-goes19` bucket has all 24 selected `ABI-L2-BRFF` full-disk observations.
- BRF2/3/5 are packed `uint16`, scale factor 0.0001, offset 0, fill 65535, and valid
  packed range 0–20000. DQF is `uint8`, fill 255.
- The sampled Montgomery County pixel is row 797/column 2619. The navigation raster
  center, after the notebook's float32 assignment, is 39.1502686, -77.2435532. Its
  four computed fixed-grid corners are retained for display.
- The notebook SHA-256 is
  `d8944c0c38db645ba7dbc3dcfcbccd631074653d0edb5792a89af376d3ae91e2`.
  It was read without executing Drive-mount, training, cleanup, or deletion cells.
- The model checksum matches the supplied value. XGBoost 3.3.0 reports 600 trees
  and the expected 56 named features in notebook Cell 9 order.
- Rolling windows preserve those 56 features and the 15, 18, and 21 UTC groups.
  Seasonality uses the notebook formula evaluated from each exact window start.
- Direct recomputation of April 7–14 produced the same 56 feature names and values,
  missing-value pattern, retained counts, and LAI `1.2043058872` (maximum feature
  delta `0.0`; LAI delta `0.0`). Rolling-window prediction accuracy remains
  separately unevaluated.
- The original IGBP grid SHA-256 is
  `59ed29f607a989e67c379cb572901aed84a0087b5f35ba5c619e0d777cd4e1c5`.
  Notebook-equivalent geographic indexing assigns class 4, deciduous broadleaf
  forest, to the requested point; this matches the earlier manual override.
- `geometry_goes19.py` was copied byte-for-byte from Downloads while preserving the
  original. Both copies have the expected SHA-256
  `1ebf9fe03be6de656554dbf5f094928b9f49265e291cf8cd53ecb9597e06e0bd`.
- The helper is imported under a non-`__main__` module name, so its demonstration
  block is not executed. The runtime uses the notebook's conversions: GOES pixel
  coordinates are quantized to float32 and passed as float64 arrays; `t` is decoded
  using its CF units/calendar into the actual UTC scan datetime; outputs are cast to
  float32; and azimuth is reduced modulo 360.
- The complete rerun recomputed filtering and all 56 features. Retained strict counts
  remained 4, 5, and 6 at 15, 18, and 21 UTC; none of the 24 strict decisions changed.
- The selected scan keys, decoded UTC timestamps, DQF values, BRFs, and all spectral
  aggregate features are identical to the provisional run.
- LAI changed from provisional `1.2940456867` to `1.2043058872 m²/m²`, a difference
  of `-0.0897397995` or `-6.9348%`.
- Mean solar zenith changed from 42.115575, 33.717952, and 61.001362 degrees to
  41.747422, 33.364841, and 60.799428 degrees at 15, 18, and 21 UTC. The helper's
  different azimuth convention produces the much larger changes in azimuth and
  relative-azimuth cosine features.
- The nominal satellite subpoint longitude now comes from the BRFF metadata (-75.2°)
  and calculated view-angle outputs are cast to float32 as in the notebook.
- The original `GOES_Navigation_2kmFD-GOES-East.nc` is readable NetCDF4, 735,508,871
  bytes, with SHA-256
  `c08fa491793996ab204c936f4bb25f299f4cf18383b0bd3673db87f8bd780e96`.
  LAI-CAC reads the completed Downloads copy directly and leaves the mounted Drive
  original untouched.
- All four notebook variables are present as 5424×5424 arrays. The exact-pixel
  orientation check strongly selects `[row, column]`: combined coordinate error
  0.007481° versus 74.078534° for `[column, row]`.
- At row 797/column 2619 the float32 notebook values are Latitude 39.1502686,
  Longitude -77.2435532, LocalZenithAngle 45.3858604°, and LandMask 1. The projection
  center differs by +0.00000278° latitude and -0.00000827° longitude.
- The notebook's separate calculation gives view zenith 45.3438301° and view azimuth
  176.7627563°. LAI-CAC uses raster LocalZenithAngle for the zenith feature and uses
  the calculation only for view azimuth. The exact-pixel zenith difference is
  0.0420303°, not zero; the notebook's reported multi-site median is 0.1359°.
- The navigation rerun changed 18 geometry-derived features but no strict filtering
  decisions. View zenith changed from 45.3438263° to 45.3858604°; solar and relative
  azimuth aggregates changed only at float32-scale because the raster and projection
  coordinates are extremely close.
- Retained counts remain 4, 5, and 6 and LAI remains exactly `1.2043058872 m²/m²`.
- The cached result is now `verified`. Operational readiness and supplied
  preprocessing validation are ready/verified; historical numerical reproduction
  remains explicitly unverified and is not a runtime dependency.

### Navigation rerun feature comparison

Eighteen of 56 features changed; the other 38 spectral, seasonal, and IGBP features
are identical. Values below compare the solar-helper/projection-navigation run with
the final raster-navigation run.

| Feature | Before | After | Delta |
|---|---:|---:|---:|
| `viewZenithDeg` | 45.343826294 | 45.385860443 | +0.042034149 |
| `viewAzimuthSin` | 0.056470238 | 0.056470504 | +0.000000266 |
| `viewAzimuthCos` | -0.998404283 | -0.998404268 | +0.000000015 |
| `solarZenithDegMeanHour15` | 41.747422218 | 41.747428894 | +0.000006676 |
| `solarZenithDegStdHour15` | 0.570967811 | 0.570968371 | +0.000000560 |
| `solarAzimuthSinMeanHour15` | 0.771552458 | 0.771552511 | +0.000000053 |
| `solarAzimuthCosMeanHour15` | 0.636131833 | 0.636131769 | -0.000000064 |
| `relativeAzimuthSinMeanHour15` | 0.806243824 | 0.806243982 | +0.000000158 |
| `relativeAzimuthCosMeanHour15` | -0.591546955 | -0.591546741 | +0.000000215 |
| `solarZenithDegMeanHour18` | 33.364840698 | 33.364842224 | +0.000001526 |
| `solarZenithDegStdHour18` | 0.887791885 | 0.887791553 | -0.000000332 |
| `solarAzimuthSinMeanHour18` | -0.427200513 | -0.427200224 | +0.000000289 |
| `solarAzimuthCosMeanHour18` | 0.904042523 | 0.904042659 | +0.000000135 |
| `relativeAzimuthSinMeanHour18` | 0.375467326 | 0.375466782 | -0.000000543 |
| `relativeAzimuthCosMeanHour18` | -0.926724042 | -0.926724261 | -0.000000219 |
| `solarZenithDegMeanHour21` | 60.799427668 | 60.799423854 | -0.000003815 |
| `relativeAzimuthSinMeanHour21` | 0.953706908 | 0.953706828 | -0.000000080 |
| `relativeAzimuthCosMeanHour21` | -0.300385504 | -0.300385758 | -0.000000254 |

## Solar-azimuth compatibility concern

The supplied helper says its azimuth is clockwise from north, but its implemented
quadrant correction does not produce that convention. LAI-CAC does not silently
correct the formula because the model's training features were produced with this
helper. Exact examples and rationale are in [SCIENTIFIC_NOTES.md](SCIENTIFIC_NOTES.md).

## Validation status

- Remaining preprocessing discrepancies for the supplied runtime workflow: none
  identified after the exact-pixel navigation and full feature rerun.
- Historical feature-row and prediction reproduction has not been completed. It
  remains explicit validation work, but those rows are not predictors and their
  absence does not block normal inference.
- The solar-azimuth convention concern remains a scientific compatibility note, not
  an unannounced formula correction or missing runtime input.

## Dependency audit

| Dependency | Inference role | Status |
|---|---|---|
| `lai_xgboost_model.json` | Required prediction model and feature schema | Present, checksum verified, loads |
| `LAI_Machine_Learning_Project.ipynb` | Authoritative preprocessing extraction | Present and fully audited |
| GOES-19 ABI-L2-BRFF | Bands 2, 3, 5, DQF, fixed-grid metadata, actual scan time | Public source verified |
| `S-NPP_VIIRS_GST_IGBP_8-Year_30arcsec.nc` | Exact IGBP class predictor | Present, checksum recorded, class 4 verified |
| `geometry_goes19.py` | Notebook solar zenith/azimuth helper | Present, expected checksum verified, imported without demo |
| `GOES_Navigation_2kmFD-GOES-East.nc` | Notebook pixel coordinates, view zenith, and land mask | Downloads copy read directly; checksum and exact pixel verified |
| `goes19_training_sites_geometry.csv` or Parquet | Training-site geometry | Not needed for arbitrary-location inference; derived training-site data |
| New MODIS/VIIRS LAI labels | None (labels, not predictors) | Correctly excluded |
