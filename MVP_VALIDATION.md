# LeafView MVP validation

Validated through 2026-09-10 against the supplied 600-tree model, exact IGBP grid,
checksum-matched solar helper, and the navigation raster read directly from
`/Users/jianzhulee/Downloads/GOES_Navigation_2kmFD-GOES-East.nc`.

## Latest result and monthly snapshots

At 2026-09-10 04:03 UTC, the latest completed Montgomery County window was
September 2–9, 2026. Its explicit boundaries are `2026-09-02T00:00:00Z` through
`2026-09-10T00:00:00Z` (end exclusive); the final archive allowance ended at
`2026-09-09T23:00:00Z`.

- The latest result is LAI `3.5869650841 m²/m²`, with 5/1/3 usable observations at
  15/18/21 UTC: 9 of 24 observations across 6 of 8 days.
- The first latest-result request took 15.406 s backend and 15.975 s client wall.
  It used 24 ranged observation reads at concurrency 4, downloaded 36,830,588 HTTP
  bytes including listings, and made no full-file download or fallback. A repeated
  provenance-matched result took 0.003 s backend and 0.021 s client wall.
- The default available history contains six preselected windows: April through
  August month-end snapshots plus the September 2–9 latest window. LeafView schedules
  only those six windows; it does not generate the former 149 daily windows and then
  downsample them. The latest result completes before history starts, and uncached
  snapshots are processed newest first.
- With all six provenance-matched results cached, 10 fresh-app API measurements took
  a median 0.009 s inside the history loader and 0.010 s request wall time after the
  first dependency-audit request. The browser capture completed history 55 ms after
  the latest card (583 ms versus 528 ms from navigation). The former fully populated
  daily history took 0.263 s backend and 0.278 s client wall, so the comparable warm
  loader is about 29 times faster.
- The earlier full daily pass remains a useful baseline: 149 windows with an empty
  derived-result cache and existing observation caches took 119.499 s backend and
  120.013 s client wall, producing 147 estimates and 2 insufficient-data gaps. That
  pass reused 3,059 sampled observations in memory, used 356 partial-cache and 105
  full-file-cache observations, downloaded no new observation bytes, and kept the
  four-worker bound. Monthly mode preserves those cache paths but never schedules
  the unselected daily windows.
- The first history population exposed a reuse bug for a cached invalid reflectance:
  strict JSON had stored its non-finite value as `null`. Restoring that value to its
  in-memory non-finite representation before reuse removed all seven affected-window
  errors without changing valid reflectances or filtering rules. A focused regression
  covers this case.
- Directly recomputing April 7–14 with the rolling implementation reproduced all 56
  feature names and values, the missing-value pattern, retained counts, and LAI
  `1.2043058872` exactly (maximum feature delta `0.0`, LAI delta `0.0`). This checks
  construction and same-boundary consistency, not rolling-window prediction accuracy.
- For Montgomery County, each monthly point was compared with the existing daily
  result at the identical coordinates and dates. Status, exact start/end boundaries,
  usable-observation support, and model output matched. The LAI values were
  `2.6258995533`, `4.8116402626`, `4.3954725266`, `4.5310139656`,
  `4.5296845436`, and `3.5869650841` for April through the September latest point.

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
- The July 4–11 custom-location result (`39.165474, -77.325871`) remained
  `5.2732105255 m²/m²`, with 5 of 24 usable observations on 2 of 8 distinct days.
  A fresh result calculation reused 23 partial-cache ranges and displayed the
  metadata-derived cached-observation explanation.
- The selected-location trend is no longer a three-period view. It follows the exact
  selected point across the preselected monthly snapshots and reports each window's
  exact UTC dates and per-date usable-observation support. The fixed Montgomery
  demonstration is not used by the chart.
- The displayed source corner order is southwest, southeast, northwest, northeast.
  The renderer's perimeter order is southwest, southeast, northeast, northwest; the
  browser polygon did not cross and contained both the requested point and sampled
  center. No footprint-coordinate or feature-construction change was required.

The automated browser record is in `screenshots/layout-validation.json`; the desktop
and 430-pixel captures are in the same directory. Both layouts had no horizontal
overflow. The browser rendered six monthly points, their month labels, exact dates,
and observation support; bound the result and history to the same point; and rejected delayed estimate and history
responses after a location change. The OpenClaw-managed browser profile could not
start because this Mac has no supported Chrome/Brave/Edge/Chromium install, so the
checks used the already-installed isolated Electron Chromium runtime. No GreenOrbit
source, configuration, cache, or result was changed.

## Validation state and remaining limits

- Estimate execution: ready.
- Supplied-pipeline preprocessing: verified.
- Historical numerical reproduction: unverified and non-blocking for runtime use.
- Rolling eight-day-window prediction accuracy: separately unevaluated. Construction and
  same-boundary checks do not establish accuracy.
- The supplied solar-azimuth calculation is intentionally unchanged for model
  compatibility; its convention concern remains documented in `SCIENTIFIC_NOTES.md`.
- Uncached periods require network access to NOAA's GOES-19 object store. The local
  development server is an MVP, not a hardened public deployment.
