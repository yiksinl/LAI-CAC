# LAI-CAC

A separate, local-first Congressional App Challenge project that will estimate leaf
area index (LAI) from post-cutoff GOES-19 observations using Yixuan Li's selected
CISESS/ESSIC XGBoost model. It does not modify or depend on GreenOrbit.

The project is currently at the real-data preprocessing milestone, **not yet a
validated LAI app**. NOAA observation discovery, download, packed-value decoding,
DQF decoding, and geostationary pixel navigation are implemented. Prediction is
intentionally blocked until the two supplied reference assets are placed in
`artifacts/reference/`; see [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md).

## Setup

Python 3.11 or newer is required. The environment has been verified with Homebrew
Python 3.14. XGBoost 3.3.0 also requires the macOS OpenMP runtime
(`brew install libomp`) before model loading; that system-wide dependency was not
installed automatically.

```bash
python3.14 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/lai-cac audit
```

Run the local browser shell at <http://127.0.0.1:8781>:

```bash
.venv/bin/lai-cac-web
```

Until both reference assets pass the audit, the interface intentionally disables
the estimate action rather than presenting synthetic data as a real result.

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
the model or label the output as LAI. Exact notebook quality filters must be audited
before sampled observations feed a composite.

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
