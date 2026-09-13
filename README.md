# LAI-CAC

A separate, local-first Congressional App Challenge project that will estimate leaf
area index (LAI) from post-cutoff GOES-19 observations using Yixuan Li's selected
CISESS/ESSIC XGBoost model. It does not modify or depend on GreenOrbit.

The project runs a complete real GOES-19 composite through the checksum-verified
model using the original IGBP grid, checksum-matched notebook solar helper, and the
original GOES-East navigation raster through checksum-pinned exact HTTP ranges. The
cached result matches the supplied research preprocessing for its exact pixel; see
[IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md).

## Setup

Python 3.11 or newer is required. The 735 MB navigation raster is not downloaded by
normal clones or installers; LeafView reads and caches only the exact scalar byte
ranges needed for the selected location.

### macOS

The environment has been verified with Homebrew Python 3.14. XGBoost 3.3.0 requires
the macOS OpenMP runtime.

```bash
brew install python@3.14 libomp
git clone https://github.com/yiksinl/LAI-CAC.git
cd LAI-CAC
python3.14 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/lai-cac audit
.venv/bin/lai-cac-web
```

### Windows

Install 64-bit Python 3.11 or newer and Git for Windows. Then run these commands in
PowerShell. Replace `3.11` with your installed Python version if needed.

```powershell
git clone https://github.com/yiksinl/LAI-CAC.git
Set-Location LAI-CAC
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\lai-cac.exe audit
.\.venv\Scripts\lai-cac-web.exe
```

On either platform, open <http://127.0.0.1:8781> after starting the server. Keep the
terminal or PowerShell window open while using LeafView, and press `Ctrl+C` there to
stop it. Startup validation and first-time estimates require internet access for the
pinned navigation source and GOES-19 observations.

The main flow is location-first: choose a point, see its latest completed eight-day
estimate, then explore monthly snapshots for that same point. Each completed month
uses the eight-day window ending on its final calendar day, followed by the latest
rolling eight-day window when it is not already that month-end snapshot. The
latest window is selected automatically after a two-hour publication allowance for
the final 21:00 UTC observation group. Choosing an older window remains optional.
The app then:

- starts the real inference pipeline in a background job and reports progress as it
  selects, downloads or reuses, samples, filters, and models 24 observations;
- applies GOES coverage, exact IGBP class, DQF, reflectance, solar-zenith, and
  view-zenith checks to the selected query;
- displays the query-bound LAI, usable dates and per-hour counts, sampled pixel
  center/index, and all four footprint corners; and
- reuses a derived result only when its coordinates, period, pipeline version,
  model/IGBP/solar checksums, and full immutable navigation descriptor match.
- selects the monthly snapshot windows before starting work, reuses the completed
  latest result as the chart's newest point, then loads the newest missing monthly
  snapshot first and the rest with at most two windows in flight. Matching result
  and observation caches are reused, and a location change stops additional work
  from being scheduled for the old history request.

For a first-time observation, LeafView opens the NOAA GOES-19 HDF5 object through
checksummed HTTP byte ranges and caches only the blocks needed for the sampled pixel.
Existing full NetCDF files remain preferred and untouched, and a failed partial read
falls back to the complete-file download route. See
[OBSERVATION_RETRIEVAL.md](OBSERVATION_RETRIEVAL.md) for numerical comparisons,
checksum scope, concurrency behavior, and the cold-cache benchmark.

Navigation is stricter: LeafView accepts only the immutable 735,508,871-byte object
at commit `0d808c6`, with its known SHA-256/ETag and storage layout. One new location
uses eight independent scalar ranges (50 payload bytes total); each range is cached
atomically with its own SHA-256. HTTP 200, wrong identity or range headers, short
bodies, and network failures abort navigation. There is no complete-file fallback.
See [NAVIGATION_RETRIEVAL.md](NAVIGATION_RETRIEVAL.md).

Changing the map point or selected window clears the displayed result and history.
Late estimate and history responses are discarded unless their coordinates and UTC
dates still match the selection. Every output is labelled **Research estimate**; a
window with no usable observations is displayed as an explicit gap rather than a
model prediction. Newly calculated results record their UTC calculation time, and
cache hits retain that original timestamp. Older results without a recorded time are
explicitly labelled as unavailable rather than dated from the request or file. See
[MVP_VALIDATION.md](MVP_VALIDATION.md) for direct numerical
checks, browser evidence, and separate latest/history timings.

Inspect the real scan selection for the first post-cutoff composite:

```bash
.venv/bin/lai-cac scans --start 2026-04-07
```

Download and sample one real scan near Montgomery County, Maryland:

```bash
.venv/bin/lai-cac sample --date 2026-04-07 --hour 15 \
  --lat 39.1547 --lon -77.2405
```

This command reports decoded BRF and DQF metadata; it deliberately does not call
the model or label the output as LAI.

Reproduce the verified complete composite:

```bash
.venv/bin/lai-cac estimate --start 2026-04-07 \
  --lat 39.1547 --lon -77.2405 \
  --location-name "Montgomery County, Maryland" \
  --output data/results/montgomery-md-2026-04-07.json
```

This run reads IGBP class 4 from the supplied eight-year grid and calls the supplied
solar helper at each actual GOES scan timestamp. It range-reads navigation-raster
Latitude, Longitude, LocalZenithAngle, and LandMask at row 797/column 2619; calculates
view azimuth separately with the notebook's `calculateViewGeometry` translation; retains
4, 5, and 6 observations at 15, 18, and 21 UTC; and returns LAI `1.2043058872`.

## Current dependency status

- Operational readiness: ready. The installed runtime inputs can execute estimates.
- Supplied preprocessing validation: verified for the exact navigation and feature
  construction used by this workflow.
- Historical numerical reproduction: unverified and tracked separately. Historical
  feature rows are validation evidence, not runtime inputs.
- Verified assets: authoritative notebook, 600-tree model, original eight-year IGBP
  grid, supplied `geometry_goes19.py`, and the immutable original GOES-East navigation
  object and layout, all with recorded checksums or identity fields.
- Compatibility concern: the supplied helper's documented azimuth convention does
  not match its implemented quadrant formula. The formula is deliberately preserved;
  see [SCIENTIFIC_NOTES.md](SCIENTIFIC_NOTES.md).

The full navigation raster is intentionally excluded from normal clones and release
artifacts. Developers who already possess the checksum-matched file may place it at
`artifacts/reference/GOES_Navigation_2kmFD-GOES-East.nc` for explicit local/remote
comparison; runtime inference never opens that optional file.

`/api/status` generates this state and the latest UTC window from the live clock and
dependency audit. `/api/estimate` starts or reuses a query-bound latest or historical
job; `/api/jobs/<id>` reports progress. `/api/history` builds the available past-year
monthly snapshots, and `/api/history/<id>` reports their progressive state. The
separate `/api/trends/montgomery` endpoint remains explicitly marked as a fixed legacy
demonstration and is not used by the interface. HTML, JavaScript, CSS, and API
responses use `Cache-Control: no-store`; scientific result reuse is controlled
separately by explicit provenance identities.

## Scientific boundaries

LAI is an estimated leaf-area-per-ground-area ratio, not a direct field measurement.
The selected model learned from a quality-screened MODIS/VIIRS consensus and a fixed
10,000-site model-development sample. Its 600-tree export was fitted on the unseen-site
training split, not all 78,940 cleaned sites. The source observation period was
2025-04-07 through 2026-04-06; later GOES-19 results test temporal transfer and are
not forecasts.

Eight-day rolling-window construction and same-boundary numerical consistency have been
checked. Prediction accuracy for rolling windows has not been evaluated and is not
implied by those implementation checks.

## Attribution

Yixuan Li performed the underlying CISESS/ESSIC research and selected/trained the
model. The app implementation extracts that research pipeline into a reproducible
local tool. OpenAI coding assistance is being used for implementation and verification;
AI does not calculate or revise the deterministic satellite inputs or model output.
