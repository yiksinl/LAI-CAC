# Implementation status

Last updated: 2026-09-07

## Plan

1. **Completed:** recover and audit the authoritative notebook and model.
2. **Completed:** verify GOES-19 retrieval, navigation, decoding, and scan inventory.
3. **In progress:** a real composite and model prediction run; two exact auxiliary inputs remain missing.
4. **In progress:** the browser shell displays the clearly labeled cached research preview.

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
- The notebook was read completely without executing its training, Drive-mount,
  cleanup, or deletion cells. Its SHA-256 is `d8944c0c38db645ba7dbc3dcfcbccd631074653d0edb5792a89af376d3ae91e2`.
- The model checksum matches the supplied value. XGBoost 3.3.0 reports 600 trees
  and the expected 56 named features in notebook Cell 9 order.
- A complete real-observation run for 2026-04-07 through 2026-04-14 retained
  4, 5, and 6 strict observations at 15, 18, and 21 UTC. With an explicit
  deciduous-broadleaf IGBP class (4), it returned provisional LAI `1.2940457 m²/m²`.
- The browser displays that cached preview with its dependency overrides and keeps
  arbitrary live estimates disabled until exact auxiliary inputs are available.
- Verification: 7 automated tests pass; model loading, CLI inference, and the local
  HTTP API have been exercised.

## Exact blockers

The notebook imports two research inputs that were not included:

- `S-NPP_VIIRS_GST_IGBP_8-Year_30arcsec.nc` — exact `ST` grid used to assign the
  model's IGBP one-hot class. NOAA publishes related annual/climatology products,
  but none was silently substituted.
- `geometry_goes19.py` — defines the notebook's imported `calculate_solar_angles`.
  The provisional run uses a documented NOAA approximation, but exact numerical
  parity cannot be asserted without this helper or stored comparison rows.

Historical stored feature rows/predictions are absent, so historical numerical
reproduction also remains unverified.

## Dependency audit

| Dependency | Inference role | Status |
|---|---|---|
| `lai_xgboost_model.json` | Required prediction model and feature schema | Present, checksum verified, loads |
| `LAI_Machine_Learning_Project.ipynb` | Authoritative preprocessing extraction | Present and fully audited |
| GOES-19 ABI-L2-BRFF | Bands 2, 3, 5, DQF, fixed-grid metadata | Public source verified |
| `GOES_Navigation_2kmFD-GOES-East.nc` | Pixel navigation lookup | Missing; derivable from each ABI file's CF projection, equivalence must be notebook-checked |
| `S-NPP_VIIRS_GST_IGBP_8-Year_30arcsec.nc` | Exact IGBP class predictor | Missing; required for automatic faithful class assignment |
| `geometry_goes19.py` | Exact solar zenith/azimuth helper | Missing; required for numerical parity |
| `goes19_training_sites_geometry.csv` or Parquet | Training-site geometry | Not needed for arbitrary-location inference; notebook confirms it is derived training-site data |
| New MODIS/VIIRS LAI labels | None (labels, not predictors) | Correctly excluded |
