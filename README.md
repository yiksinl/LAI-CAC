# LAI-CAC

A separate, local-first Congressional App Challenge project that will estimate leaf
area index (LAI) from post-cutoff GOES-19 observations using Yixuan Li's selected
CISESS/ESSIC XGBoost model. It does not modify or depend on GreenOrbit.

The project runs a complete real GOES-19 composite through the checksum-verified
model using the original IGBP grid, checksum-matched notebook solar helper, and the
original GOES-East navigation raster. The cached result now matches the supplied
research preprocessing for its exact pixel; see
[IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md).

## Setup

Python 3.11 or newer is required. The environment has been verified with Homebrew
Python 3.14. XGBoost 3.3.0 requires the macOS OpenMP runtime, which is installed on
the current machine.

```bash
python3.14 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/lai-cac audit
```

Run the local browser shell at <http://127.0.0.1:8781>:

```bash
.venv/bin/lai-cac-web
```

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
- reuses a derived result only when its coordinates, period, pipeline version, and
  model/IGBP/solar/navigation checksums match.
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
solar helper at each actual GOES scan timestamp. It reads navigation-raster
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
  grid, supplied `geometry_goes19.py`, and original GOES-East navigation raster, all
  with recorded checksums.
- Compatibility concern: the supplied helper's documented azimuth convention does
  not match its implemented quadrant formula. The formula is deliberately preserved;
  see [SCIENTIFIC_NOTES.md](SCIENTIFIC_NOTES.md).

The navigation raster remains in Downloads and is read directly through
`artifacts/reference/navigation-source.json`; LAI-CAC does not create another full
copy. The configured source must remain at that path for future inference.

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
