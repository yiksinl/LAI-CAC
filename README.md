# LAI-CAC

A separate, local-first Congressional App Challenge project that will estimate leaf
area index (LAI) from post-cutoff GOES-19 observations using Yixuan Li's selected
CISESS/ESSIC XGBoost model. It does not modify or depend on GreenOrbit.

The project now runs a complete real GOES-19 composite through the checksum-verified
model. The cached result remains explicitly provisional because two notebook-imported
auxiliary inputs were not supplied; see [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md).

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

The interface includes a cached April 7–14 research preview and labels its two
remaining preprocessing substitutions. Arbitrary live estimates stay disabled.

The cached example is a separate, query-bound result card. Use **Load this example**
to synchronize the map, requested point, pixel footprint, and period selector. A
different map point or period never relabels the cached result.

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

Reproduce the provisional complete composite:

```bash
.venv/bin/lai-cac estimate --start 2026-04-07 \
  --lat 39.1547 --lon -77.2405 --igbp-class 4 \
  --output data/results/montgomery-md-2026-04-07.json
```

The explicit IGBP argument is an auditable override, not an automatic land-cover
classification. Do not present this run as exact notebook reproduction.

## Current dependency status

- Verified: authoritative notebook and checksum-matched 600-tree model.
- Missing exact input: `S-NPP_VIIRS_GST_IGBP_8-Year_30arcsec.nc`.
- Missing exact helper: `geometry_goes19.py`.
- Cached-example approximations: manually supplied IGBP class and the documented
  NOAA solar-angle approximation.

`/api/status` generates this state from the live dependency audit. HTML, JavaScript,
CSS, and API responses use `Cache-Control: no-store` to prevent a restarted local
server from being confused with browser caching.

## Scientific boundaries

LAI is an estimated leaf-area-per-ground-area ratio, not a direct field measurement.
The selected model learned from a quality-screened MODIS/VIIRS consensus and a fixed
10,000-site model-development sample. Its 600-tree export was fitted on the unseen-site
training split, not all 78,940 cleaned sites. The source observation period was
2025-04-07 through 2026-04-06; later GOES-19 results test temporal transfer and are
not forecasts.

## Attribution

Yixuan Li performed the underlying CISESS/ESSIC research and selected/trained the
model. The app implementation extracts that research pipeline into a reproducible
local tool. OpenAI coding assistance is being used for implementation and verification;
AI does not calculate or revise the deterministic satellite inputs or model output.
