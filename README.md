# LeafView

LeafView is a local web application that estimates leaf area index (LAI) from
GOES-19 satellite observations. It uses the XGBoost model selected and trained by
Yixuan Li through CISESS/ESSIC research.

LeafView labels every result as a research estimate. It is not a field
measurement, health rating, or forecast.

## What it does

- Estimates LAI for a selected U.S. location using a complete eight-day window.
- Filters observations by satellite coverage, land class, data quality, reflectance,
  solar zenith, and view zenith.
- Shows the latest estimate and monthly snapshots for the same location.
- Caches verified inputs and results so repeated requests finish faster.

## Requirements

- Python 3.11 or newer
- Git
- Internet access for GOES-19 observations, navigation data, and place search
- macOS only: the OpenMP runtime required by XGBoost

The repository includes the trained model, IGBP land-cover grid, solar-geometry
helper, and verification notebook. The 735 MB navigation raster is not downloaded.
LeafView reads only the exact byte ranges needed for the selected location.

## Install and run

### macOS

```bash
brew install python@3.14 libomp
git clone https://github.com/yiksinl/LAI-CAC.git
cd LAI-CAC
python3.14 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/lai-cac audit
.venv/bin/lai-cac-web
```

### Windows

Install 64-bit Python 3.11 or newer and Git for Windows, then run:

```powershell
git clone https://github.com/yiksinl/LAI-CAC.git
Set-Location LAI-CAC
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\lai-cac.exe audit
.\.venv\Scripts\lai-cac-web.exe
```

Open <http://127.0.0.1:8781>. Keep the terminal open while LeafView is running.
Press `Ctrl+C` to stop it.

## Data retrieval

LeafView normally reads only the required blocks from each NOAA GOES-19 observation
file. If a partial observation read fails, it can download that complete observation
file instead.

Navigation is stricter. LeafView accepts only the pinned GOES-East navigation object
and reads eight checksum-verified ranges for each new location. It never falls back
to downloading the complete navigation raster.

Downloaded observations, navigation ranges, and calculated results are stored in
ignored local cache directories.

## Command line

Check the bundled assets and remote navigation source:

```bash
.venv/bin/lai-cac audit
```

Run an estimate directly:

```bash
.venv/bin/lai-cac estimate --start 2026-04-07 \
  --lat 39.1547 --lon -77.2405 \
  --location-name "Montgomery County, Maryland"
```

## Tests

```bash
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest -q
```

## Scientific limits

The model's source observation period was April 7, 2025 through April 6, 2026.
Estimates after that period test temporal transfer. Rolling-window construction and
numerical consistency have been checked, but prediction accuracy for those windows
has not been evaluated.

## Attribution

Yixuan Li performed the underlying CISESS/ESSIC research and selected and trained
the model. OpenAI coding assistance was used to implement and verify the application.
Satellite inputs and model predictions remain deterministic.
