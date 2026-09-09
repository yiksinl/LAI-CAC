# Scientific compatibility notes

## Solar-azimuth convention concern

The checksum-verified `geometry_goes19.py` helper documents solar azimuth as
clockwise from north: north 0°, east 90°, south 180°, and west 270°. Its implemented
quadrant correction does not produce that convention. At the Montgomery County
sampled pixel on 2026-04-07, for example, the helper returns approximately 49.7° at
15:05 UTC and 335.7° at 18:05 UTC, whereas a conventional north-clockwise NOAA
calculation is approximately 130.3° and 204.3°.

NOAA defines solar azimuth as degrees clockwise from north and publishes the
corresponding equation in its
[General Solar Position Calculations](https://www.gml.noaa.gov/grad/solcalc/solareqns.PDF)
reference.

LAI-CAC intentionally retains the supplied helper's formula and output unchanged.
The trained model was built from features created with this helper, so silently
changing the convention would create a training/inference mismatch. This concern is
reported separately from missing-input or navigation-equivalence limitations.

## Navigation-raster verification

The notebook selected GOES pixels using each BRFF file's fixed grid, then read the
matched pixel latitude, longitude, and `LocalZenithAngle` from
`GOES_Navigation_2kmFD-GOES-East.nc`. It calculated view azimuth from those navigation
coordinates and the nominal satellite longitude.

LAI-CAC now reads the checksum-recorded original raster at the exact matched row and
column. It uses raster `Latitude`, `Longitude`, and `LocalZenithAngle`, retains
`LandMask` in provenance, and calculates view azimuth separately with the scalar
translation of notebook `calculateViewGeometry`. It does not call the viewing-angle
function from `geometry_goes19.py`.

For Montgomery County row 797/column 2619, raster LocalZenithAngle is 45.3858604°
after the notebook's float32 assignment. The separately calculated view zenith is
45.3438301°, an absolute difference of 0.0420303°. This is reported rather than
collapsed into an equivalence claim; the notebook's multi-site median difference is
0.1359°.
