# Required reference assets

The supplied scientific reference assets are stored here without renaming:

- `LAI_Machine_Learning_Project.ipynb`
- `lai_xgboost_model.json`
- `S-NPP_VIIRS_GST_IGBP_8-Year_30arcsec.nc`
- `geometry_goes19.py`
- `GOES_Navigation_2kmFD-GOES-East.nc` (735 MB, tracked with Git LFS)
- `navigation-source.json` (portable relative-path and checksum record)

Recorded SHA-256 values:

- Model: `815ed3b7cb92b95d598e24385527874864c5e6e282cf05d7dabbcca26391d4c6`
- Notebook: `d8944c0c38db645ba7dbc3dcfcbccd631074653d0edb5792a89af376d3ae91e2`
- IGBP grid: `59ed29f607a989e67c379cb572901aed84a0087b5f35ba5c619e0d777cd4e1c5`
- Solar helper: `1ebf9fe03be6de656554dbf5f094928b9f49265e291cf8cd53ecb9597e06e0bd`
- GOES-East navigation raster: `c08fa491793996ab204c936f4bb25f299f4cf18383b0bd3673db87f8bd780e96`

The application refuses verified inference if the model, IGBP grid, solar helper, or
configured navigation raster differs from its expected checksum. The navigation
source record resolves the Git LFS asset relative to this directory, so a complete
clone can run without a machine-specific path.
