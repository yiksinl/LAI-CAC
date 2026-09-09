from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import h5py
import numpy as np

from .assets import ReferenceAssets
from .composites import TARGET_HOURS_UTC, aligned_period
from .geometry import angle_difference, solar_angles, view_geometry
from .goes import download, nearest_scan, passes_notebook_strict_quality, sample_pixel
from .model import LaiModel
from .navigation import read_navigation_pixel

IGBP_CATEGORIES = (1, 10, 11, 12, 13, 14, 16, 2, 3, 4, 5, 6, 7, 8, 9)


def _sample_std(values: list[float]) -> float | None:
    return float(np.std(values, ddof=1)) if len(values) >= 2 else None


def read_igbp_class(path: Path, latitude: float, longitude: float) -> int:
    """Exact scalar translation of notebook Cell 5 geographic-grid indexing."""
    if not path.is_file():
        raise FileNotFoundError(f"Missing exact IGBP grid: {path}")
    with h5py.File(path, "r") as source:
        if "ST" not in source:
            raise KeyError("IGBP file does not contain the notebook's expected ST variable")
        grid = source["ST"]
        row = int(np.floor((90.0 - latitude) * (grid.shape[0] / 180.0)))
        column = int(np.floor((longitude + 180.0) * (grid.shape[1] / 360.0)))
        row = int(np.clip(row, 0, grid.shape[0] - 1))
        column = int(np.clip(column, 0, grid.shape[1] - 1))
        value = int(grid[row, column])
        fill = grid.attrs.get("_FillValue")
        if fill is not None and value == int(np.asarray(fill).reshape(-1)[0]):
            raise ValueError("The exact IGBP grid is missing at the requested location")
    if value not in IGBP_CATEGORIES:
        raise ValueError(f"Unsupported IGBP class {value}; expected one of {IGBP_CATEGORIES}")
    return value


def prepare_composite(latitude: float, longitude: float, start: date, igbp_class: int | None,
                      root: Path) -> tuple[dict[str, float | None], dict]:
    start, end = aligned_period(start)
    assets = ReferenceAssets(root / "artifacts/reference")
    solar_geometry_path = assets.require_solar_geometry()
    navigation_path = assets.require_navigation() if assets.navigation.is_file() else None
    igbp_source = "explicit_argument"
    if igbp_class is None:
        igbp_class = read_igbp_class(assets.require_igbp(), latitude, longitude)
        igbp_source = "S-NPP_VIIRS_GST_IGBP_8-Year_30arcsec.nc"
    if igbp_class not in IGBP_CATEGORIES:
        raise ValueError(f"Unsupported IGBP class {igbp_class}; expected one of {IGBP_CATEGORIES}")
    view_zenith = view_azimuth = None
    navigation = None
    sampled_center = None
    grouped: dict[int, list[dict]] = defaultdict(list)
    attempts = []
    current = start
    while current <= end:
        for hour in TARGET_HOURS_UTC:
            scan = nearest_scan(current, hour)
            if scan is None:
                attempts.append({"date": current.isoformat(), "target_hour": hour, "status": "missing_scan"})
                continue
            path = download(scan, root / "data/cache" / Path(scan.key).name)
            sample = sample_pixel(path, latitude, longitude)
            if sampled_center is None:
                if navigation_path:
                    navigation = read_navigation_pixel(
                        navigation_path, sample, latitude, longitude
                    )
                    feature_geometry = navigation["feature_geometry"]
                    sampled_center = {
                        "latitude": feature_geometry["latitude"],
                        "longitude": feature_geometry["longitude"],
                    }
                    view_zenith = feature_geometry["view_zenith"]
                    view_azimuth = feature_geometry["view_azimuth"]
                else:
                    sampled_center = sample["sampled_pixel_center"]
                    view_zenith, view_azimuth = view_geometry(
                        sampled_center["latitude"], sampled_center["longitude"],
                        **sample["view_geometry_parameters"],
                    )
                    navigation = {
                        "verified_for_exact_pixel": False,
                        "source": None,
                        "source_path": str(assets.navigation),
                        "source_sha256": None,
                        "goes_row": sample["row"],
                        "goes_column": sample["column"],
                        "array_orientation": "unverified",
                        "raster_pixel": None,
                        "projection_derived_pixel": sample["sampled_pixel_center"],
                        "comparison": {
                            "notebook_reported_median_absolute_view_zenith_difference_degrees": 0.1359,
                        },
                        "feature_geometry": {
                            "latitude": sampled_center["latitude"],
                            "longitude": sampled_center["longitude"],
                            "view_zenith": view_zenith,
                            "view_azimuth": view_azimuth,
                            "view_zenith_source": "BRFF CF projection fallback",
                            "view_azimuth_source": "notebook calculateViewGeometry scalar translation",
                        },
                    }
            elif (sample["row"], sample["column"]) != (
                navigation["goes_row"], navigation["goes_column"]
            ):
                raise ValueError(
                    "The composite selected more than one GOES fixed-grid pixel; "
                    "notebook site geometry is fixed per location."
                )
            when = datetime.fromisoformat(sample["scan_time_utc"])
            solar_zenith, solar_azimuth = solar_angles(
                sampled_center["latitude"],
                sampled_center["longitude"],
                when,
                solar_geometry_path,
            )
            strict = passes_notebook_strict_quality(sample["dqf"]) and solar_zenith < 67 and view_zenith < 70 and all(math.isfinite(v) for v in sample["brf"].values())
            record = {**sample, "date": current.isoformat(), "target_hour": hour,
                      "scan": scan.key, "scan_time_utc": sample["scan_time_utc"],
                      "dqf": sample["dqf"], "strict": strict,
                      "solar_zenith": solar_zenith, "solar_azimuth": solar_azimuth,
                      "projection_derived_pixel_center": sample["sampled_pixel_center"],
                      "sampled_pixel_center": sampled_center}
            attempts.append(record)
            if strict:
                grouped[hour].append(record)
        current += timedelta(days=1)
    if sampled_center is None or view_zenith is None or view_azimuth is None or navigation is None:
        raise ValueError("No GOES-19 scans were available for the requested composite")
    row: dict[str, float | None] = {name: None for name in LaiModel(assets.require_model()).features}
    row.update({"viewZenithDeg": view_zenith, "viewAzimuthSin": math.sin(math.radians(view_azimuth)),
                "viewAzimuthCos": math.cos(math.radians(view_azimuth)),
                "dayOfYearSin": math.sin(2*math.pi*start.timetuple().tm_yday/365.25),
                "dayOfYearCos": math.cos(2*math.pi*start.timetuple().tm_yday/365.25)})
    for hour in TARGET_HOURS_UTC:
        records = grouped[hour]
        for band, stem in (("2", "brfBand2Red"), ("3", "brfBand3Nir"), ("5", "brfBand5Swir")):
            values = [r["brf"][band] for r in records]
            row[f"{stem}MeanHour{hour}"] = float(np.mean(values)) if values else None
            row[f"{stem}StdHour{hour}"] = _sample_std(values)
        sz = [r["solar_zenith"] for r in records]
        row[f"solarZenithDegMeanHour{hour}"] = float(np.mean(sz)) if sz else None
        row[f"solarZenithDegStdHour{hour}"] = _sample_std(sz)
        for name, fn in (("solarAzimuthSin", math.sin), ("solarAzimuthCos", math.cos)):
            values = [fn(math.radians(r["solar_azimuth"])) for r in records]
            row[f"{name}MeanHour{hour}"] = float(np.mean(values)) if values else None
        relative = [float(np.float32(angle_difference(r["solar_azimuth"], view_azimuth))) for r in records]
        row[f"relativeAzimuthSinMeanHour{hour}"] = float(np.mean([math.sin(math.radians(v)) for v in relative])) if relative else None
        row[f"relativeAzimuthCosMeanHour{hour}"] = float(np.mean([math.cos(math.radians(v)) for v in relative])) if relative else None
    for category in IGBP_CATEGORIES:
        row[f"igbpClass_{category}"] = float(category == igbp_class)
    provenance = {"requested_location": {"latitude": latitude, "longitude": longitude},
                  "sampled_pixel_center": sampled_center,
                  "composite": {"start": start.isoformat(), "end": end.isoformat()},
                  "igbp": {"class": igbp_class, "source": igbp_source},
                  "navigation": navigation,
                  "solar_geometry": {
                      "method": "geometry_goes19.calculate_solar_angles",
                      "source": solar_geometry_path.name,
                      "sha256": assets.audit()["solar_geometry"]["sha256"],
                      "coordinate_source": (
                          "navigation-raster Latitude/Longitude at the exact sampled GOES row and column"
                          if navigation["verified_for_exact_pixel"] else
                          "sampled GOES-19 pixel center derived from each BRFF file's CF projection"
                      ),
                      "coordinate_conversion": "navigation scalars assigned to float32, then passed as float64 helper arrays",
                      "timestamp_source": "GOES t variable decoded with its CF units and calendar as UTC",
                      "output_conversion": "solar zenith and azimuth cast to float32; azimuth modulo 360",
                  }, "attempts": attempts,
                  "usable_counts": {str(hour): len(grouped[hour]) for hour in TARGET_HOURS_UTC}}
    return row, provenance


def run_estimate(latitude: float, longitude: float, start: date, igbp_class: int | None,
                 root: Path, output: Path, location_name: str | None = None) -> dict:
    row, provenance = prepare_composite(latitude, longitude, start, igbp_class, root)
    if location_name:
        provenance["location_name"] = location_name
    model = LaiModel(ReferenceAssets(root / "artifacts/reference").require_model())
    usable = sum(provenance["usable_counts"].values())
    prediction = model.predict(row, usable)
    limitations = []
    if provenance["igbp"]["source"] == "explicit_argument":
        limitations.append("IGBP class was explicitly supplied instead of read from the exact eight-year grid.")
    if not provenance["navigation"]["verified_for_exact_pixel"]:
        limitations.append(
            "GOES pixel centers and view zenith are derived from each BRFF file because the notebook's "
            "separate navigation grid is unavailable; numerical equivalence remains unverified."
        )
    preprocessing_verified = not limitations
    result = {"status": "provisional_dependency_override" if limitations else "verified", "lai": prediction, "units": "m² leaf area per m² ground area",
              "model_sha256": ReferenceAssets(root / "artifacts/reference").audit()["model"]["sha256"],
              "features": row, "provenance": provenance,
              "limitations": limitations,
              "readiness": {
                  "estimate_execution": {
                      "ready": True,
                      "status": "ready",
                      "detail": "The supplied model and current preprocessing inputs can execute inference.",
                  },
                  "preprocessing_validation": {
                      "verified": preprocessing_verified,
                      "status": "verified" if preprocessing_verified else "provisional",
                      "detail": (
                          "Feature construction matches the supplied notebook for the exact sampled pixel."
                          if preprocessing_verified else
                          "At least one supplied preprocessing dependency or equivalence check remains unresolved."
                      ),
                  },
                  "historical_numerical_reproduction": {
                      "completed": False,
                      "status": "unverified",
                      "required_for_runtime": False,
                      "detail": "Historical feature-row and prediction reproduction has not been completed.",
                  },
              },
              "scientific_concerns": [
                  "The supplied helper labels its solar azimuth as clockwise from north, but its quadrant "
                  "formula produces a different convention. The formula is intentionally preserved for "
                  "model compatibility; see SCIENTIFIC_NOTES.md."
              ]}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
    return result
