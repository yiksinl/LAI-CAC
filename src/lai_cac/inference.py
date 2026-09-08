from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

from .assets import ReferenceAssets
from .composites import TARGET_HOURS_UTC, aligned_period
from .geometry import angle_difference, solar_angles, view_geometry
from .goes import download, nearest_scan, passes_notebook_strict_quality, sample_pixel
from .model import LaiModel

IGBP_CATEGORIES = (1, 10, 11, 12, 13, 14, 16, 2, 3, 4, 5, 6, 7, 8, 9)


def _sample_std(values: list[float]) -> float | None:
    return float(np.std(values, ddof=1)) if len(values) >= 2 else None


def prepare_composite(latitude: float, longitude: float, start: date, igbp_class: int,
                      root: Path, solar_method: str = "noaa-approximation") -> tuple[dict[str, float | None], dict]:
    start, end = aligned_period(start)
    if igbp_class not in IGBP_CATEGORIES:
        raise ValueError(f"Unsupported IGBP class {igbp_class}; expected one of {IGBP_CATEGORIES}")
    view_zenith = view_azimuth = None
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
                sampled_center = sample["sampled_pixel_center"]
                view_zenith, view_azimuth = view_geometry(sampled_center["latitude"], sampled_center["longitude"])
            when = datetime.fromisoformat(sample["scan_time_utc"])
            solar_zenith, solar_azimuth = solar_angles(sample["sampled_pixel_center"]["latitude"], sample["sampled_pixel_center"]["longitude"], when)
            strict = passes_notebook_strict_quality(sample["dqf"]) and solar_zenith < 67 and view_zenith < 70 and all(math.isfinite(v) for v in sample["brf"].values())
            record = {"date": current.isoformat(), "target_hour": hour, "scan": scan.key,
                      "scan_time_utc": sample["scan_time_utc"], "dqf": sample["dqf"], "strict": strict,
                      "solar_zenith": solar_zenith, "solar_azimuth": solar_azimuth, **sample}
            attempts.append(record)
            if strict:
                grouped[hour].append(record)
        current += timedelta(days=1)
    if sampled_center is None or view_zenith is None or view_azimuth is None:
        raise ValueError("No GOES-19 scans were available for the requested composite")
    row: dict[str, float | None] = {name: None for name in LaiModel(ReferenceAssets(root / "artifacts/reference").require_model()).features}
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
        relative = [angle_difference(r["solar_azimuth"], view_azimuth) for r in records]
        row[f"relativeAzimuthSinMeanHour{hour}"] = float(np.mean([math.sin(math.radians(v)) for v in relative])) if relative else None
        row[f"relativeAzimuthCosMeanHour{hour}"] = float(np.mean([math.cos(math.radians(v)) for v in relative])) if relative else None
    for category in IGBP_CATEGORIES:
        row[f"igbpClass_{category}"] = float(category == igbp_class)
    provenance = {"requested_location": {"latitude": latitude, "longitude": longitude},
                  "sampled_pixel_center": sampled_center,
                  "composite": {"start": start.isoformat(), "end": end.isoformat()},
                  "igbp": {"class": igbp_class, "source": "explicit_argument_pending_exact_8_year_grid"},
                  "solar_geometry": solar_method, "attempts": attempts,
                  "usable_counts": {str(hour): len(grouped[hour]) for hour in TARGET_HOURS_UTC}}
    return row, provenance


def run_estimate(latitude: float, longitude: float, start: date, igbp_class: int, root: Path, output: Path) -> dict:
    row, provenance = prepare_composite(latitude, longitude, start, igbp_class, root)
    model = LaiModel(ReferenceAssets(root / "artifacts/reference").require_model())
    usable = sum(provenance["usable_counts"].values())
    prediction = model.predict(row, usable)
    result = {"status": "provisional_dependency_override", "lai": prediction, "units": "m² leaf area per m² ground area",
              "model_sha256": ReferenceAssets(root / "artifacts/reference").audit()["model"]["sha256"],
              "features": row, "provenance": provenance,
              "limitations": ["IGBP class was explicitly supplied because the exact eight-year grid is missing.",
                              "Solar angles use a NOAA approximation because geometry_goes19.py is missing."]}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
    return result
