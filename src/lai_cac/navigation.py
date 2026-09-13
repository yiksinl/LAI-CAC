from __future__ import annotations

import hashlib
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import netCDF4 as nc
import numpy as np

from .assets import NAVIGATION_NAME, sha256
from .geometry import view_geometry
from .navigation_asset import (
    LAND_MASK_ATTRIBUTES,
    NAVIGATION_LAYOUT,
    NAVIGATION_SHAPE,
    RemoteNavigationError,
    RemoteNavigationStore,
    get_remote_navigation_store,
)

REQUIRED_VARIABLES = ("Latitude", "Longitude", "LocalZenithAngle", "LandMask")


def _longitude_difference(a: float, b: float) -> float:
    difference = abs(a - b)
    return min(difference, 360.0 - difference)


def _scalar(variable: nc.Variable, indices: tuple[int, int], *, as_float32: bool) -> float:
    value = np.ma.filled(variable[indices], np.nan)
    scalar = np.asarray(value).reshape(-1)[0]
    return float(np.float32(scalar) if as_float32 else scalar)


def _candidate(dataset: nc.Dataset, indices: tuple[int, int], *, as_float32: bool) -> dict:
    return {
        "dataset_indices": list(indices),
        "latitude": _scalar(dataset["Latitude"], indices, as_float32=as_float32),
        "longitude": _scalar(dataset["Longitude"], indices, as_float32=as_float32),
        "local_zenith_angle": _scalar(
            dataset["LocalZenithAngle"], indices, as_float32=as_float32
        ),
        "land_mask": _scalar(dataset["LandMask"], indices, as_float32=as_float32),
    }


def _orientation_score(candidate: dict, latitude: float, longitude: float) -> float:
    if not math.isfinite(candidate["latitude"]) or not math.isfinite(candidate["longitude"]):
        return math.inf
    return abs(latitude - candidate["latitude"]) + _longitude_difference(
        longitude, candidate["longitude"]
    )


def _attribute(variable: nc.Variable, name: str):
    value = getattr(variable, name)
    array = np.asarray(value)
    if array.ndim == 0:
        return array.item()
    return array.tolist()


def _navigation_result(
    *,
    sample: dict,
    requested_latitude: float,
    requested_longitude: float,
    row_column_raw: dict,
    column_row_raw: dict,
    selected: dict,
    land_mask_attributes: dict,
    variable_details: dict,
    source_path: str,
    source_sha256: str,
    retrieval: dict | None = None,
) -> dict:
    row = int(sample["row"])
    column = int(sample["column"])
    requested_latitude = float(np.float32(requested_latitude))
    requested_longitude = float(np.float32(requested_longitude))
    row_column_score = _orientation_score(
        row_column_raw, requested_latitude, requested_longitude
    )
    column_row_score = _orientation_score(
        column_row_raw, requested_latitude, requested_longitude
    )
    orientation = "row_column" if row_column_score < column_row_score else "column_row"

    latitude = selected["latitude"]
    longitude = selected["longitude"]
    raster_view_zenith = selected["local_zenith_angle"]
    land_mask = selected["land_mask"]
    calculated_view_zenith, view_azimuth = view_geometry(
        latitude, longitude, **sample["view_geometry_parameters"]
    )
    projection_center = sample["sampled_pixel_center"]
    projection_view_zenith, projection_view_azimuth = view_geometry(
        projection_center["latitude"],
        projection_center["longitude"],
        **sample["view_geometry_parameters"],
    )
    latitude_error = abs(requested_latitude - latitude)
    longitude_error = _longitude_difference(requested_longitude, longitude)
    land_mask_valid_range = land_mask_attributes.get("valid_range")
    variables_match_notebook = all(
        name in variable_details for name in REQUIRED_VARIABLES
    ) and all(
        details["shape"] == list(NAVIGATION_SHAPE)
        for details in variable_details.values()
    )
    land_mask_matches_notebook = bool(
        land_mask_attributes.get("_FillValue") == -1
        and land_mask_valid_range == [0, 7]
    )
    verified = bool(
        variables_match_notebook
        and land_mask_matches_notebook
        and orientation == "row_column"
        and latitude_error <= 0.2
        and longitude_error <= 0.2
        and all(
            math.isfinite(value)
            for value in (latitude, longitude, raster_view_zenith, land_mask)
        )
        and 0 <= land_mask <= 7
    )
    result = {
        "verified_for_exact_pixel": verified,
        "source": NAVIGATION_NAME,
        "source_path": source_path,
        "source_sha256": source_sha256,
        "goes_row": row,
        "goes_column": column,
        "array_orientation": orientation,
        "notebook_reported_array_orientation": "row_column",
        "orientation_check": {
            "method": "notebook latitude-plus-wrapped-longitude error comparison at exact pixel",
            "row_column": {"score_degrees": row_column_score, "pixel": row_column_raw},
            "column_row": {"score_degrees": column_row_score, "pixel": column_row_raw},
        },
        "variables": variable_details,
        "land_mask_attributes": land_mask_attributes,
        "raster_pixel": {
            "latitude": latitude,
            "longitude": longitude,
            "local_zenith_angle": raster_view_zenith,
            "land_mask": land_mask,
            "dataset_indices": [row, column],
            "conversion": "raster scalars assigned to float32 arrays as in notebook Cell 2",
        },
        "projection_derived_pixel": {
            "latitude": projection_center["latitude"],
            "longitude": projection_center["longitude"],
            "calculated_view_zenith": projection_view_zenith,
            "calculated_view_azimuth": projection_view_azimuth,
        },
        "comparison": {
            "requested_latitude_error_degrees": latitude_error,
            "requested_longitude_error_degrees": longitude_error,
            "raster_minus_projection_latitude_degrees": latitude
            - projection_center["latitude"],
            "raster_minus_projection_longitude_degrees": longitude
            - projection_center["longitude"],
            "raster_minus_projection_view_zenith_degrees": raster_view_zenith
            - projection_view_zenith,
            "raster_minus_calculated_view_zenith_degrees": raster_view_zenith
            - calculated_view_zenith,
            "notebook_reported_median_absolute_view_zenith_difference_degrees": 0.1359,
        },
        "feature_geometry": {
            "latitude": latitude,
            "longitude": longitude,
            "view_zenith": raster_view_zenith,
            "view_azimuth": view_azimuth,
            "view_zenith_source": "navigation raster LocalZenithAngle",
            "view_azimuth_source": "notebook calculateViewGeometry scalar translation",
            "calculated_view_zenith_check": calculated_view_zenith,
        },
    }
    if retrieval is not None:
        result["retrieval"] = retrieval
    return result


def read_navigation_pixel(
    path: Path,
    sample: dict,
    requested_latitude: float,
    requested_longitude: float,
) -> dict:
    """Read the local reference raster for explicit comparison and validation."""
    row = int(sample["row"])
    column = int(sample["column"])
    with nc.Dataset(path) as dataset:
        missing = [name for name in REQUIRED_VARIABLES if name not in dataset.variables]
        if missing:
            raise ValueError(f"Navigation raster is missing variables: {', '.join(missing)}")
        shapes = {name: list(dataset[name].shape) for name in REQUIRED_VARIABLES}
        if any(len(shape) != 2 for shape in shapes.values()) or len(
            {tuple(shape) for shape in shapes.values()}
        ) != 1:
            raise ValueError(f"Navigation variables do not share one 2-D shape: {shapes}")
        shape = tuple(shapes["Latitude"])
        if not (0 <= row < shape[0] and 0 <= column < shape[1]):
            raise ValueError(f"GOES pixel row {row}, column {column} is outside {shape}")
        if not (0 <= column < shape[0] and 0 <= row < shape[1]):
            raise ValueError(
                f"Swapped orientation index column {column}, row {row} is outside {shape}"
            )
        row_column_raw = _candidate(dataset, (row, column), as_float32=False)
        column_row_raw = _candidate(dataset, (column, row), as_float32=False)
        selected = _candidate(dataset, (row, column), as_float32=True)
        land_mask_attributes = {
            name: _attribute(dataset["LandMask"], name)
            for name in dataset["LandMask"].ncattrs()
        }
        variable_details = {
            name: {
                "shape": list(dataset[name].shape),
                "dtype": str(dataset[name].dtype),
                "attributes": list(dataset[name].ncattrs()),
            }
            for name in REQUIRED_VARIABLES
        }
    return _navigation_result(
        sample=sample,
        requested_latitude=requested_latitude,
        requested_longitude=requested_longitude,
        row_column_raw=row_column_raw,
        column_row_raw=column_row_raw,
        selected=selected,
        land_mask_attributes=land_mask_attributes,
        variable_details=variable_details,
        source_path=str(path),
        source_sha256=sha256(path),
    )


def _remote_offset(variable: str, row: int, column: int) -> tuple[int, int]:
    specification = NAVIGATION_LAYOUT[variable]
    flat_index = row * NAVIGATION_SHAPE[1] + column
    start = int(specification["offset"]) + flat_index * int(
        specification["itemsize"]
    )
    return start, start + int(specification["itemsize"]) - 1


def _decode_remote_value(variable: str, data: bytes) -> float:
    specification = NAVIGATION_LAYOUT[variable]
    value = float(
        np.frombuffer(data, dtype=str(specification["dtype"]), count=1)[0]
    )
    minimum, maximum = specification["valid_range"]
    if value == specification["fill_value"] or not minimum <= value <= maximum:
        return math.nan
    return value


def read_remote_navigation_pixel(
    sample: dict,
    requested_latitude: float,
    requested_longitude: float,
    cache_root: Path,
    *,
    store: RemoteNavigationStore | None = None,
) -> dict:
    """Read only exact scalar ranges from the immutable navigation object."""
    row = int(sample["row"])
    column = int(sample["column"])
    if not (0 <= row < NAVIGATION_SHAPE[0] and 0 <= column < NAVIGATION_SHAPE[1]):
        raise ValueError(
            f"GOES pixel row {row}, column {column} is outside {NAVIGATION_SHAPE}"
        )
    if not (0 <= column < NAVIGATION_SHAPE[0] and 0 <= row < NAVIGATION_SHAPE[1]):
        raise ValueError(
            f"Swapped orientation index column {column}, row {row} is outside "
            f"{NAVIGATION_SHAPE}"
        )
    store = store or get_remote_navigation_store(cache_root)
    metadata = store.validate_metadata()
    jobs: list[tuple[str, str, tuple[int, int], int, int]] = []
    for orientation, indices in (
        ("row_column", (row, column)),
        ("column_row", (column, row)),
    ):
        for variable in REQUIRED_VARIABLES:
            start, end = _remote_offset(variable, *indices)
            jobs.append((orientation, variable, indices, start, end))

    began = time.perf_counter()
    values: dict[tuple[str, str], float] = {}
    range_records: list[dict[str, object]] = []
    partial_values: list[tuple[int, int, bytes]] = []
    with ThreadPoolExecutor(
        max_workers=len(jobs), thread_name_prefix="navigation-range"
    ) as executor:
        futures = {
            executor.submit(store.read_range, start, end): (
                orientation,
                variable,
                start,
                end,
            )
            for orientation, variable, _, start, end in jobs
        }
        for future in as_completed(futures):
            orientation, variable, start, end = futures[future]
            data, record = future.result()
            value = _decode_remote_value(variable, data)
            if not math.isfinite(value):
                raise RemoteNavigationError(
                    f"Remote navigation {variable} is fill or outside its pinned valid "
                    f"range at byte offset {start}"
                )
            values[(orientation, variable)] = value
            range_records.append(record)
            partial_values.append((start, end, data))
    operation_seconds = time.perf_counter() - began
    store.record_range_operation(operation_seconds)

    def candidate(
        orientation: str, indices: tuple[int, int], *, as_float32: bool
    ) -> dict:
        def value(name: str) -> float:
            raw = values[(orientation, name)]
            return float(np.float32(raw)) if as_float32 else raw

        return {
            "dataset_indices": list(indices),
            "latitude": value("Latitude"),
            "longitude": value("Longitude"),
            "local_zenith_angle": value("LocalZenithAngle"),
            "land_mask": value("LandMask"),
        }

    row_column_raw = candidate("row_column", (row, column), as_float32=False)
    column_row_raw = candidate("column_row", (column, row), as_float32=False)
    selected = candidate("row_column", (row, column), as_float32=True)
    variable_details = {
        name: {
            "shape": list(NAVIGATION_SHAPE),
            "dtype": "int8" if name == "LandMask" else "float64",
            "attributes": list(NAVIGATION_LAYOUT[name]["attributes"]),
        }
        for name in REQUIRED_VARIABLES
    }
    partial_digest = hashlib.sha256()
    for start, end, data in sorted(partial_values):
        partial_digest.update(f"{start}:{end}:".encode("ascii"))
        partial_digest.update(data)
    network_records = [
        record for record in range_records if record["source"] == "network"
    ]
    cache_records = [
        record for record in range_records if record["source"] == "cache"
    ]
    retrieval = {
        "route": "remote_http_byte_ranges",
        "remote_object": store.source.identity(),
        "metadata": metadata,
        "range_cache": {
            "cache_key": store.cache_key,
            "path": str(Path(cache_root.name) / store.cache_key),
            "request_count": sum(
                int(record.get("request_count", 1)) for record in network_records
            ),
            "downloaded_payload_bytes": sum(
                int(record["bytes"]) for record in network_records
            ),
            "cache_hit_count": len(cache_records),
            "cache_hit_bytes": sum(int(record["bytes"]) for record in cache_records),
            "operation_wall_seconds": round(operation_seconds, 6),
            "ranges": sorted(range_records, key=lambda record: int(record["start"])),
            "partial_data_sha256": partial_digest.hexdigest(),
            "checksum_scope": (
                "SHA-256 over the ordered exact offsets and bytes used for this "
                "navigation sample; not a whole-file checksum"
            ),
        },
        "startup_through_result": store.session_metrics(),
        "full_file_fallbacks": 0,
        "failure_semantics": (
            "Any metadata, identity, range, or response-size failure aborts "
            "navigation; a complete-file download is never attempted."
        ),
    }
    return _navigation_result(
        sample=sample,
        requested_latitude=requested_latitude,
        requested_longitude=requested_longitude,
        row_column_raw=row_column_raw,
        column_row_raw=column_row_raw,
        selected=selected,
        land_mask_attributes=dict(LAND_MASK_ATTRIBUTES),
        variable_details=variable_details,
        source_path=store.source.url,
        source_sha256=store.source.sha256,
        retrieval=retrieval,
    )
