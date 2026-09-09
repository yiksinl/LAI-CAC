from __future__ import annotations

import json
import math
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

import h5py
import numpy as np

from .assets import ReferenceAssets
from .composites import TARGET_HOURS_UTC, aligned_period
from .geometry import angle_difference, solar_angles, view_geometry
from .goes import (
    ObservationRetrievalError,
    Scan,
    TransferMetrics,
    download,
    nearest_scan,
    passes_notebook_strict_quality,
    remote_object_metadata,
    sample_pixel,
    sample_remote_pixel,
)
from .model import LaiModel
from .navigation import read_navigation_pixel
from .remote_hdf5 import RemoteRangeError
from .result_cache import cache_identity

IGBP_CATEGORIES = (1, 10, 11, 12, 13, 14, 16, 2, 3, 4, 5, 6, 7, 8, 9)
ProgressCallback = Callable[[dict[str, object]], None]
MAX_OBSERVATION_WORKERS = 4


def _report(
    callback: ProgressCallback | None,
    percent: int,
    stage: str,
    detail: str,
    **extra: object,
) -> None:
    if callback is not None:
        callback({"percent": percent, "stage": stage, "detail": detail, **extra})


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


def _full_file_retrieval(
    scan: Scan,
    path: Path,
    route: str,
    *,
    partial_error: str | None = None,
) -> dict[str, object]:
    return {
        "route": route,
        "remote_object": remote_object_metadata(scan),
        "partial_cache": None,
        "whole_file": {
            "local_path": str(path),
            "size_bytes": path.stat().st_size,
            "sha256": None,
            "checksum_status": "whole-file SHA-256 not computed by the existing full-file route",
        },
        "whole_file_sha256": None,
        "whole_file_checksum_status": (
            "whole-file SHA-256 not computed by the existing full-file route"
        ),
        "partial_error": partial_error,
    }


def _retrieve_observation(
    scan: Scan,
    destination: Path,
    partial_cache_root: Path,
    latitude: float,
    longitude: float,
    policy: str,
    metrics: TransferMetrics,
) -> dict[str, object]:
    if policy not in {"auto", "full_only", "partial_required"}:
        raise ValueError(f"Unsupported observation retrieval policy {policy!r}")
    full_cached = destination.is_file() and destination.stat().st_size == scan.size
    if full_cached and policy != "partial_required":
        sample = sample_pixel(destination, latitude, longitude)
        sample["retrieval"] = _full_file_retrieval(scan, destination, "full_file_cache")
        return sample
    if policy == "full_only":
        path = download(scan, destination, metrics)
        sample = sample_pixel(path, latitude, longitude)
        sample["retrieval"] = _full_file_retrieval(scan, path, "full_file_download")
        return sample
    try:
        return sample_remote_pixel(scan, partial_cache_root, latitude, longitude)
    except (RemoteRangeError, OSError) as partial_error:
        if policy == "partial_required":
            raise ObservationRetrievalError(
                f"NOAA partial HDF5 retrieval failed for {scan.key}: {partial_error}"
            ) from partial_error
        try:
            path = download(scan, destination, metrics)
            sample = sample_pixel(path, latitude, longitude)
        except Exception as fallback_error:
            raise ObservationRetrievalError(
                f"NOAA observation retrieval failed for {scan.key}. "
                f"Partial read: {partial_error}. Full-file fallback: {fallback_error}"
            ) from fallback_error
        sample["retrieval"] = _full_file_retrieval(
            scan,
            path,
            "full_file_fallback",
            partial_error=str(partial_error),
        )
        return sample


def prepare_composite(
    latitude: float,
    longitude: float,
    start: date,
    igbp_class: int | None,
    root: Path,
    progress: ProgressCallback | None = None,
    *,
    retrieval_policy: str = "auto",
    partial_cache_root: Path | None = None,
    max_observation_workers: int = MAX_OBSERVATION_WORKERS,
) -> tuple[dict[str, float | None], dict]:
    start, end = aligned_period(start)
    assets = ReferenceAssets(root / "artifacts/reference")
    _report(progress, 2, "validating", "Verifying the supplied preprocessing assets")
    solar_geometry_path = assets.require_solar_geometry()
    navigation_path = assets.require_navigation() if assets.navigation.is_file() else None
    igbp_source = "explicit_argument"
    _report(progress, 5, "land_cover", "Checking the requested location against the IGBP grid")
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
    observation_cache_hits = 0
    observation_downloads = 0
    partial_remote_reads = 0
    partial_cache_reads = 0
    partial_range_requests = 0
    partial_downloaded_bytes = 0
    partial_cache_hit_bytes = 0
    observation_total = len(TARGET_HOURS_UTC) * 8
    partial_cache_root = partial_cache_root or root / "data/partial-cache"
    workers = max(1, min(int(max_observation_workers), MAX_OBSERVATION_WORKERS))
    transfer_metrics = TransferMetrics()
    slots: list[tuple[int, date, int]] = []
    current = start
    while current <= end:
        for hour in TARGET_HOURS_UTC:
            slots.append((len(slots), current, hour))
        current += timedelta(days=1)
    _report(
        progress,
        8,
        "observations",
        f"Finding {observation_total} NOAA observations",
        completed=0,
        total=observation_total,
    )
    selected_scans: dict[int, Scan | None] = {}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="goes-list") as executor:
        futures = {
            executor.submit(nearest_scan, day, hour, transfer_metrics): ordinal
            for ordinal, day, hour in slots
        }
        selected_count = 0
        for future in as_completed(futures):
            ordinal = futures[future]
            selected_scans[ordinal] = future.result()
            selected_count += 1
            _report(
                progress,
                8 + int(8 * selected_count / observation_total),
                "observations",
                f"Selected observation {selected_count} of {observation_total}",
                completed=selected_count,
                total=observation_total,
            )
    samples: dict[int, dict[str, object]] = {}
    available_slots = [slot for slot in slots if selected_scans[slot[0]] is not None]
    retrieved_count = 0
    remaining_slots = available_slots
    if available_slots:
        first_ordinal = available_slots[0][0]
        first_scan = selected_scans[first_ordinal]
        assert first_scan is not None
        first_destination = root / "data/cache" / Path(first_scan.key).name
        samples[first_ordinal] = _retrieve_observation(
            first_scan,
            first_destination,
            partial_cache_root,
            latitude,
            longitude,
            retrieval_policy,
            transfer_metrics,
        )
        retrieved_count = 1
        remaining_slots = available_slots[1:]
        _report(
            progress,
            16 + int(56 * retrieved_count / len(available_slots)),
            "observations",
            f"Retrieved observation {retrieved_count} of {len(available_slots)}",
            completed=retrieved_count,
            total=len(available_slots),
        )
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="goes-sample") as executor:
        futures = {}
        for ordinal, _, _ in remaining_slots:
            scan = selected_scans[ordinal]
            assert scan is not None
            destination = root / "data/cache" / Path(scan.key).name
            future = executor.submit(
                _retrieve_observation,
                scan,
                destination,
                partial_cache_root,
                latitude,
                longitude,
                retrieval_policy,
                transfer_metrics,
            )
            futures[future] = ordinal
        for future in as_completed(futures):
            ordinal = futures[future]
            samples[ordinal] = future.result()
            retrieved_count += 1
            _report(
                progress,
                16 + int(56 * retrieved_count / max(len(available_slots), 1)),
                "observations",
                f"Retrieved observation {retrieved_count} of {len(available_slots)}",
                completed=retrieved_count,
                total=len(available_slots),
            )
    completed = 0
    for ordinal, current, hour in slots:
        scan = selected_scans[ordinal]
        if scan is None:
            attempts.append({
                "date": current.isoformat(),
                "target_hour": hour,
                "status": "missing_scan",
            })
            completed += 1
            continue
        sample = samples[ordinal]
        retrieval = sample["retrieval"]
        route = str(retrieval["route"])
        if route == "full_file_cache":
            observation_cache_hits += 1
        elif route in {"full_file_download", "full_file_fallback"}:
            observation_downloads += 1
        elif route == "http_byte_ranges":
            partial = retrieval["partial_cache"]
            assert isinstance(partial, dict)
            request_count = int(partial["request_count"])
            if request_count:
                partial_remote_reads += 1
            else:
                partial_cache_reads += 1
            partial_range_requests += request_count
            partial_downloaded_bytes += int(partial["downloaded_bytes"])
            partial_cache_hit_bytes += int(partial["cache_hit_bytes"])
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
        strict = (
            passes_notebook_strict_quality(sample["dqf"])
            and solar_zenith < 67
            and view_zenith < 70
            and all(math.isfinite(v) for v in sample["brf"].values())
        )
        record = {
            **sample,
            "date": current.isoformat(),
            "target_hour": hour,
            "scan": scan.key,
            "scan_time_utc": sample["scan_time_utc"],
            "dqf": sample["dqf"],
            "strict": strict,
            "solar_zenith": solar_zenith,
            "solar_azimuth": solar_azimuth,
            "projection_derived_pixel_center": sample["sampled_pixel_center"],
            "sampled_pixel_center": sampled_center,
        }
        attempts.append(record)
        if strict:
            grouped[hour].append(record)
        completed += 1
        _report(
            progress,
            72 + int(8 * completed / observation_total),
            "observations",
            f"Processed observation {completed} of {observation_total}",
            completed=completed,
            total=observation_total,
            usable=sum(len(records) for records in grouped.values()),
        )
    if sampled_center is None or view_zenith is None or view_azimuth is None or navigation is None:
        raise ValueError("No GOES-19 scans were available for the requested composite")
    _report(progress, 82, "features", "Constructing the notebook-compatible feature row")
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
    transfer = transfer_metrics.snapshot()
    full_file_fallbacks = sum(
        1
        for attempt in attempts
        if attempt.get("retrieval", {}).get("route") == "full_file_fallback"
    )
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
                  "usable_counts": {str(hour): len(grouped[hour]) for hour in TARGET_HOURS_UTC},
                  "observation_cache": {
                      "reused": observation_cache_hits,
                      "downloaded": observation_downloads,
                      "partial_remote": partial_remote_reads,
                      "partial_reused": partial_cache_reads,
                      "total_selected": (
                          observation_cache_hits
                          + observation_downloads
                          + partial_remote_reads
                          + partial_cache_reads
                      ),
                  },
                  "observation_retrieval": {
                      "policy": retrieval_policy,
                      "strategy": "existing full-file cache, then HTTP byte ranges, then full-file fallback",
                      "max_concurrency": workers,
                      "connection_reuse": "shared bounded urllib3.PoolManager",
                      **transfer,
                      "range_request_count": partial_range_requests,
                      "range_downloaded_bytes": partial_downloaded_bytes,
                      "range_cache_hit_bytes": partial_cache_hit_bytes,
                      "full_file_fallbacks": full_file_fallbacks,
                      "total_http_request_count": (
                          transfer["listing_request_count"]
                          + transfer["full_download_request_count"]
                          + partial_range_requests
                      ),
                      "total_http_downloaded_bytes": (
                          transfer["listing_bytes"]
                          + transfer["full_download_bytes"]
                          + partial_downloaded_bytes
                      ),
                      "failure_semantics": (
                          "Network or remote-HDF5 failures raise an observation retrieval error; "
                          "only successfully decoded observations can be classified as insufficient data."
                      ),
                      "checksum_semantics": (
                          "Remote S3 keys and ETags identify objects. Partial-data SHA-256 values cover only "
                          "the listed byte ranges and are never represented as whole-file SHA-256 values."
                      ),
                  }}
    return row, provenance


def run_estimate(latitude: float, longitude: float, start: date, igbp_class: int | None,
                 root: Path, output: Path, location_name: str | None = None,
                 progress: ProgressCallback | None = None,
                 identity: dict[str, object] | None = None, *,
                 retrieval_policy: str = "auto",
                 partial_cache_root: Path | None = None,
                 max_observation_workers: int = MAX_OBSERVATION_WORKERS) -> dict:
    started = time.perf_counter()
    identity = identity or cache_identity(
        latitude, longitude, start, root, igbp_override=igbp_class
    )
    row, provenance = prepare_composite(
        latitude,
        longitude,
        start,
        igbp_class,
        root,
        progress=progress,
        retrieval_policy=retrieval_policy,
        partial_cache_root=partial_cache_root,
        max_observation_workers=max_observation_workers,
    )
    if location_name:
        provenance["location_name"] = location_name
    provenance["cache_identity"] = identity
    usable = sum(provenance["usable_counts"].values())
    prediction = None
    if usable:
        _report(progress, 91, "model", "Applying the supplied 600-tree LAI model")
        model = LaiModel(ReferenceAssets(root / "artifacts/reference").require_model())
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
    if not usable:
        status = "insufficient_data"
    else:
        status = "provisional_dependency_override" if limitations else "verified"
    provenance["processing"] = {
        "duration_seconds": round(time.perf_counter() - started, 3),
        "result_cache_hit": False,
    }
    result = {"status": status, "label": "Research estimate", "lai": prediction, "units": "m² leaf area per m² ground area",
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
    if not usable:
        result["data_message"] = (
            "No observations passed the supplied quality and geometry filters; "
            "this period is shown as a gap."
        )
    _report(progress, 97, "saving", "Saving result and provenance")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".part")
    temporary.write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(output)
    _report(progress, 100, "complete", "Research estimate complete")
    return result
