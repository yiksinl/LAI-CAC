# LeafView MVP validation

Validated on 2026-09-08 against the supplied 600-tree model, exact IGBP grid,
checksum-matched solar helper, and the navigation raster read directly from
`/Users/jianzhulee/Downloads/GOES_Navigation_2kmFD-GOES-East.nc`.

## Real Montgomery trend

| Period | LAI | Usable 15/18/21 UTC | Observation cache | Pipeline time | Wall time |
|---|---:|---:|---:|---:|---:|
| 2026-04-07–2026-04-14 | 1.2043058872 | 4 / 5 / 6 | 24 reused, 0 downloaded | 3.606 s | 4.01 s |
| 2026-04-15–2026-04-22 | 2.2830977440 | 2 / 1 / 2 | 0 reused, 24 downloaded | 99.262 s | 99.86 s |
| 2026-04-23–2026-04-30 | 2.6258995533 | 2 / 3 / 1 | 0 reused, 24 downloaded | 110.108 s | 110.71 s |

The latter two are cold raw-observation cache timings. A provenance-matched API read
of the first Montgomery result completed in 0.001 s server time and 0.005 s measured
client wall time.

## Query and cache checks

- College Park, Maryland (`38.9897, -76.9378`), April 7–14: IGBP class 13,
  GOES row 803/column 2631, counts 5/6/6, LAI `0.7712944746`. Recomputing from 24
  cached raw scans took 3.727 s; the repeated result-cache request took 0.002 s.
- Shenandoah test point (`38.5320, -78.4380`), April 15–22: the browser processed a
  real LAI `1.2730877399` result with counts 2/3/3 in 2.53 s from cached observations,
  rendered eight distinct progress updates, and remained responsive for 55
  independent 50 ms UI ticks. The immediate repeat was a provenance-matched cache hit.
- North Texas test point (`33.0000, -97.0000`), April 15–22: the location passed GOES
  coverage and IGBP class checks but no observations passed the supplied quality and
  geometry filters. The API returned `insufficient_data`, `lai: null`, counts 0/0/0,
  and the browser displayed a gap message. Processing 24 cached observations took
  2.187 s.
- Changing the selected period or point cleared the LAI, footprint polygon, support
  bars, and result metadata before any new response. The browser did not attach an
  older result to the new query.

The automated browser record is in `screenshots/layout-validation.json`; the desktop
and narrow captures are in the same directory. The OpenClaw-managed browser profile
could not start because this Mac has no supported Chrome/Brave/Edge/Chromium install,
so browser automation used the already-installed isolated Electron Chromium runtime.
No GreenOrbit source, configuration, cache, or result was changed.

## Validation state and remaining limits

- Estimate execution: ready.
- Supplied-pipeline preprocessing: verified.
- Historical numerical reproduction: unverified and non-blocking for runtime use.
- The supplied solar-azimuth calculation is intentionally unchanged for model
  compatibility; its convention concern remains documented in `SCIENTIFIC_NOTES.md`.
- Uncached periods require network access to NOAA's GOES-19 object store. The local
  development server is an MVP, not a hardened public deployment.
