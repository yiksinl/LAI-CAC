# Implementation status

Last updated: 2026-09-07

## Plan

1. **In progress:** recover and audit the authoritative notebook and model.
2. **In progress:** verify GOES-19 retrieval, navigation, decoding, and scan inventory.
3. **Blocked:** reproduce the notebook's exact 56-feature composite and one real prediction.
4. **In progress:** a map/results browser shell exists; connect it only after Phase 1 verification.

## Verified findings

- The separate `LAI-CAC` Git repository was created; `garden_ndvi_desktop` was not changed.
- The prompt identifies the first post-training composite as 2026-04-07 through
  2026-04-14. The post-cutoff calendar is anchored there; the notebook is still
  needed to verify how its earlier annual/composite boundaries were constructed.
- NOAA's public `noaa-goes19` bucket has `ABI-L2-BRFF` full-disk observations for that period.
- A real 2026-04-07 15:00:21.1 UTC file was downloaded and inspected. It is GOES-19,
  ABI Mode 6, 2 km nadir resolution, with BRF bands 1, 2, 3, 5, and 6 plus DQF.
- BRF2/3/5 are packed `uint16`, scale factor 0.0001, offset 0, fill 65535,
  and valid packed range 0–20000. DQF is `uint8`, fill 255.
- The product projection metadata supplies the standard GOES fixed-grid navigation parameters.
- Montgomery County trial `39.1547, -77.2405`, 2026-04-07: the 15 UTC scan
  sampled row 797/column 2619 but had DQF 147 and is unusable; the 18 and 21 UTC
  scans sampled the same pixel with DQF 16 and decoded BRF2/3/5 values of
  (0.0711, 0.2697, 0.2436) and (0.0535, 0.2220, 0.1788), respectively.
- The sampled pixel center is 39.150266, -77.243545; its four computed fixed-grid
  corners are retained by the sampler for footprint display.
- A local map/results shell runs and hard-disables LAI output while authoritative
  assets are missing. This is scaffolding, not a completed Phase 2 app.
- Verification: 5 automated tests pass; the local HTTP status endpoint was exercised
  and returned the expected missing-asset state.

## Exact blockers

Neither supplied reference asset exists in the workspace, Downloads, Documents,
Desktop, Google Drive mount, or the OpenClaw data tree:

- `artifacts/reference/LAI_Machine_Learning_Project.ipynb` — authoritative feature
  construction, time-offset selection, strict quality acceptance, geometry, and IGBP mapping.
- `artifacts/reference/lai_xgboost_model.json` — the selected 600-tree model and the
  authoritative 56 feature names/order. Expected SHA-256:
  `815ed3b7cb92b95d598e24385527874864c5e6e282cf05d7dabbcca26391d4c6`.

Without these, producing an LAI number would be fabricated. Historical stored feature
rows/predictions are also absent, so numerical reproduction cannot yet be checked.
The isolated environment installs XGBoost 3.3.0, but this Mac needs Homebrew
`libomp` before its native library can load.

## Dependency audit (provisional until notebook is available)

| Dependency | Inference role | Status |
|---|---|---|
| `lai_xgboost_model.json` | Required prediction model and feature schema | Missing; required |
| `LAI_Machine_Learning_Project.ipynb` | Authoritative preprocessing extraction | Missing; required for verification |
| GOES-19 ABI-L2-BRFF | Bands 2, 3, 5, DQF, fixed-grid metadata | Public source verified |
| `GOES_Navigation_2kmFD-GOES-East.nc` | Pixel navigation lookup | Missing; derivable from each ABI file's CF projection, equivalence must be notebook-checked |
| `S-NPP_VIIRS_GST_IGBP_8-Year_30arcsec.nc` | Exact IGBP class predictor | Missing; source/format must be established from notebook |
| `goes19_training_sites_geometry.csv` or Parquet | Training-site geometry / possibly documented demo site | Missing; likely not needed for arbitrary-location inference, notebook must confirm |
| New MODIS/VIIRS LAI labels | None (labels, not predictors) | Correctly excluded |
