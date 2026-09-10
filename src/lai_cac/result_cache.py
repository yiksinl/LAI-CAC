from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

from .assets import ReferenceAssets
from .composites import COMPOSITE_DAYS, TARGET_HOURS_UTC, rolling_period

PIPELINE_VERSION = "leafview-notebook-compatible-v1"
WEB_COORDINATE_PRECISION = 6


def normalized_query(latitude: float, longitude: float, start: date) -> dict[str, object]:
    period_start, period_end = rolling_period(start)
    return {
        "latitude": round(float(latitude), WEB_COORDINATE_PRECISION),
        "longitude": round(float(longitude), WEB_COORDINATE_PRECISION),
        "start": period_start.isoformat(),
        "end": period_end.isoformat(),
    }


def cache_identity(
    latitude: float,
    longitude: float,
    start: date,
    root: Path,
    igbp_override: int | None = None,
) -> dict[str, object]:
    query = normalized_query(latitude, longitude, start)
    audit = ReferenceAssets(root / "artifacts/reference").audit()
    assets = {
        name: audit[name].get("sha256")
        for name in ("model", "igbp", "solar_geometry", "navigation")
    }
    inputs = {
        "pipeline_version": PIPELINE_VERSION,
        "query": query,
        "assets": assets,
        "composite_days": COMPOSITE_DAYS,
        "target_hours_utc": list(TARGET_HOURS_UTC),
        "strict_quality_filter": "notebook_dqf_and_solar_zenith_lt_67_and_view_zenith_lt_70",
    }
    if igbp_override is not None:
        inputs["igbp_override"] = igbp_override
    encoded = json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {**inputs, "key": hashlib.sha256(encoded).hexdigest()}


def result_matches_identity(result: dict, identity: dict[str, object]) -> bool:
    provenance = result.get("provenance", {})
    stored = provenance.get("cache_identity")
    return bool(
        stored
        and stored == identity
        and result.get("status") in {"verified", "insufficient_data"}
        and provenance.get("igbp", {}).get("source") != "explicit_argument"
        and provenance.get("navigation", {}).get("verified_for_exact_pixel") is True
    )


def runtime_cache_path(root: Path, key: str) -> Path:
    return root / "data/results/runtime" / f"{key}.json"


def load_compatible_result(
    root: Path, identity: dict[str, object]
) -> tuple[dict, Path] | None:
    key = str(identity["key"])
    runtime = runtime_cache_path(root, key)
    candidates = [runtime]
    candidates.extend(
        path
        for path in sorted((root / "data/results").glob("*.json"))
        if path != runtime
    )
    for path in candidates:
        if not path.is_file():
            continue
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if result_matches_identity(result, identity):
            return result, path
    return None
