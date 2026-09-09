from __future__ import annotations

import math
from pathlib import Path

import netCDF4 as nc
import numpy as np

from .assets import NAVIGATION_NAME, sha256
from .geometry import view_geometry

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


def read_navigation_pixel(path: Path, sample: dict, requested_latitude: float,
                          requested_longitude: float) -> dict:
    """Read notebook navigation and calculate only view azimuth separately."""
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
            raise ValueError(f"Swapped orientation index column {column}, row {row} is outside {shape}")
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
    ) and all(details["shape"] == [5424, 5424] for details in variable_details.values())
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
        and all(math.isfinite(value) for value in (latitude, longitude, raster_view_zenith, land_mask))
        and 0 <= land_mask <= 7
    )
    return {
        "verified_for_exact_pixel": verified,
        "source": NAVIGATION_NAME,
        "source_path": str(path),
        "source_sha256": sha256(path),
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
            "raster_minus_projection_latitude_degrees": latitude - projection_center["latitude"],
            "raster_minus_projection_longitude_degrees": longitude - projection_center["longitude"],
            "raster_minus_projection_view_zenith_degrees": raster_view_zenith - projection_view_zenith,
            "raster_minus_calculated_view_zenith_degrees": raster_view_zenith - calculated_view_zenith,
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
