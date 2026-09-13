from __future__ import annotations

import json
import hashlib
import math
import os
import threading
import time
import uuid
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import urllib3
from flask import Flask, jsonify, render_template, request

from .assets import ReferenceAssets
from .composites import (
    ARCHIVE_PUBLICATION_DELAY,
    HISTORY_DAYS,
    POST_CUTOFF_ANCHOR,
    TRAINING_END,
    archive_ready_after,
    is_month_end,
    latest_completed_rolling_period,
    monthly_snapshot_periods,
    rolling_period,
    utc_window_boundaries,
)
from .goes import ObservationRetrievalError
from .inference import ObservationMemo, run_estimate
from .result_cache import (
    cache_identity,
    load_compatible_result,
    normalized_query,
    result_matches_identity,
    runtime_cache_path,
)
from .serialization import StrictJSONProvider

ROOT = Path(__file__).resolve().parents[2]
MONTGOMERY = {
    "latitude": 39.1547,
    "longitude": -77.2405,
    "name": "Montgomery County, Maryland",
}
TREND_STARTS = (date(2026, 4, 7), date(2026, 4, 15), date(2026, 4, 23))
HISTORY_MAX_CONCURRENT_WINDOWS = 2
NOMINATIM_REVERSE_URL = os.environ.get(
    "LEAFVIEW_PLACE_LOOKUP_URL",
    "https://nominatim.openstreetmap.org/reverse",
)
NOMINATIM_SEARCH_URL = os.environ.get(
    "LEAFVIEW_PLACE_SEARCH_URL",
    "https://nominatim.openstreetmap.org/search",
)
PLACE_AREA_FIELDS = (
    "neighbourhood",
    "suburb",
    "quarter",
    "borough",
    "city_district",
    "town",
    "city",
    "village",
    "hamlet",
    "municipality",
    "locality",
)

NAVIGATION_DISCREPANCY = {
    "key": "navigation_equivalence",
    "name": "Notebook navigation-grid equivalence",
    "detail": (
        "LeafView reads the exact notebook navigation values from a checksum-pinned "
        "remote GOES_Navigation_2kmFD-GOES-East.nc object. Its HTTP metadata and "
        "immutable asset identity must validate before inference."
    ),
}


class PlaceLookupError(RuntimeError):
    """A place-name service could not answer the selected-coordinate lookup."""


def _place_component(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split()).strip(" ,")
    return cleaned or None


def place_name_from_nominatim(payload: object) -> dict[str, str] | None:
    """Select only response-backed U.S. area, county, and state fields."""
    if not isinstance(payload, dict) or not isinstance(payload.get("address"), dict):
        return None
    address = payload["address"]
    country_code = _place_component(address.get("country_code"))
    if country_code is None or country_code.casefold() != "us":
        return None
    state = _place_component(address.get("state"))
    county = _place_component(address.get("county"))
    area = next(
        (
            component
            for field in PLACE_AREA_FIELDS
            if (component := _place_component(address.get(field))) is not None
        ),
        None,
    )
    primary = area or county
    if primary is None or state is None:
        return None
    return {
        "display_name": f"{primary}, {state}"[:100],
        "county": county[:100] if county else "",
    }


def places_from_nominatim_search(payload: object) -> list[dict[str, object]]:
    """Return response-backed U.S. search suggestions without country/province text."""

    if not isinstance(payload, list):
        return []
    suggestions: list[dict[str, object]] = []
    seen: set[tuple[float, float]] = set()
    for raw in payload:
        if not isinstance(raw, dict) or not isinstance(raw.get("address"), dict):
            continue
        address = raw["address"]
        country_code = _place_component(address.get("country_code"))
        state = _place_component(address.get("state"))
        if country_code is None or country_code.casefold() != "us" or state is None:
            continue
        try:
            latitude = round(float(raw["lat"]), 6)
            longitude = round(float(raw["lon"]), 6)
        except (KeyError, TypeError, ValueError):
            continue
        if not (
            math.isfinite(latitude)
            and math.isfinite(longitude)
            and -90 <= latitude <= 90
            and -180 <= longitude <= 180
        ):
            continue
        coordinate_key = (latitude, longitude)
        if coordinate_key in seen:
            continue

        area = next(
            (
                component
                for field in PLACE_AREA_FIELDS
                if (component := _place_component(address.get(field))) is not None
            ),
            None,
        )
        road = _place_component(address.get("road"))
        house_number = _place_component(address.get("house_number"))
        namedetails = raw.get("namedetails")
        named_name = (
            _place_component(namedetails.get("name"))
            if isinstance(namedetails, dict)
            else None
        )
        name = (
            _place_component(raw.get("name"))
            or named_name
            or (f"{house_number} {road}" if house_number and road else road)
            or area
            or _place_component(address.get("county"))
        )
        if name is None:
            continue
        label_parts = [name]
        if area and area.casefold() != name.casefold() and (road or named_name):
            label_parts.append(area)
        if state.casefold() not in {part.casefold() for part in label_parts}:
            label_parts.append(state)
        county = _place_component(address.get("county"))
        suggestions.append({
            "display_name": ", ".join(label_parts)[:100],
            "county": county[:100] if county else "",
            "latitude": latitude,
            "longitude": longitude,
        })
        seen.add(coordinate_key)
        if len(suggestions) == 5:
            break
    return suggestions


class NominatimPlaceLookup:
    """Low-rate, process-local forward and reverse geocoder."""

    def __init__(
        self,
        pool: urllib3.PoolManager | None = None,
        *,
        minimum_interval_seconds: float = 1.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.pool = pool or urllib3.PoolManager(num_pools=1, maxsize=1, block=True)
        self.minimum_interval_seconds = minimum_interval_seconds
        self.monotonic = monotonic
        self.sleeper = sleeper
        self._lock = threading.Lock()
        self._last_request_started: float | None = None
        self._cache: dict[tuple[float, float], dict[str, str] | None] = {}
        self._search_cache: dict[str, list[dict[str, object]]] = {}

    def _request_locked(self, url: str, fields: dict[str, str]) -> object | None:
        if self._last_request_started is not None:
            remaining = (
                self.minimum_interval_seconds
                - (self.monotonic() - self._last_request_started)
            )
            if remaining > 0:
                self.sleeper(remaining)
        self._last_request_started = self.monotonic()
        try:
            response = self.pool.request(
                "GET",
                url,
                fields=fields,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "LeafView/0.1 (CISESS LAI research app)",
                },
                timeout=urllib3.Timeout(connect=2, read=4),
                retries=False,
            )
        except urllib3.exceptions.HTTPError as error:
            raise PlaceLookupError("Place service request failed") from error
        if response.status == 404:
            return None
        if response.status != 200:
            raise PlaceLookupError(f"Place service returned HTTP {response.status}")
        try:
            return json.loads(response.data)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PlaceLookupError("Place service returned invalid JSON") from error

    def __call__(self, latitude: float, longitude: float) -> dict[str, str] | None:
        key = (round(latitude, 6), round(longitude, 6))
        with self._lock:
            if key in self._cache:
                cached = self._cache[key]
                return deepcopy(cached) if cached is not None else None
            payload = self._request_locked(NOMINATIM_REVERSE_URL, {
                "format": "jsonv2",
                "lat": f"{key[0]:.6f}",
                "lon": f"{key[1]:.6f}",
                "zoom": "14",
                "addressdetails": "1",
                "layer": "address",
                "accept-language": "en-US",
            })
            result = place_name_from_nominatim(payload) if payload is not None else None
            if len(self._cache) >= 512:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = deepcopy(result)
            return result

    def search(self, query: str) -> list[dict[str, object]]:
        normalized = " ".join(query.split()).strip()
        cache_key = normalized.casefold()
        with self._lock:
            if cache_key in self._search_cache:
                return deepcopy(self._search_cache[cache_key])
            payload = self._request_locked(NOMINATIM_SEARCH_URL, {
                "format": "jsonv2",
                "q": normalized,
                "countrycodes": "us",
                "addressdetails": "1",
                "namedetails": "1",
                "accept-language": "en-US",
                "dedupe": "1",
                "limit": "8",
            })
            results = places_from_nominatim_search(payload)
            if len(self._search_cache) >= 256:
                self._search_cache.pop(next(iter(self._search_cache)))
            self._search_cache[cache_key] = deepcopy(results)
            return results


def dependency_status(assets: dict) -> dict:
    verified = []
    missing = []
    approximations = []
    if assets["notebook"]["valid"]:
        verified.append("Authoritative notebook")
    elif assets["notebook"]["present"]:
        missing.append({"key": "notebook", "name": "Authoritative notebook with expected checksum", "filename": "LAI_Machine_Learning_Project.ipynb"})
    else:
        missing.append({"key": "notebook", "name": "Authoritative notebook", "filename": "LAI_Machine_Learning_Project.ipynb"})
    if assets["model"]["valid"]:
        verified.append("Checksum-verified 600-tree model")
    elif assets["model"]["present"]:
        missing.append({"key": "model", "name": "Selected model with expected checksum", "filename": "lai_xgboost_model.json"})
    else:
        missing.append({"key": "model", "name": "Selected trained model", "filename": "lai_xgboost_model.json"})
    if not assets["igbp"]["valid"]:
        missing.append({"key": "igbp", "name": "Exact eight-year VIIRS IGBP grid", "filename": "S-NPP_VIIRS_GST_IGBP_8-Year_30arcsec.nc"})
        approximations.append("Cached example uses a manually supplied IGBP land-cover class.")
    else:
        verified.append("Checksum-verified eight-year VIIRS IGBP grid")
    if not assets["solar_geometry"]["valid"]:
        missing.append({"key": "solar_geometry", "name": "Notebook solar-geometry helper", "filename": "geometry_goes19.py"})
        approximations.append("Verified inference requires the checksum-matched notebook solar helper.")
    else:
        verified.append("Checksum-verified notebook solar-geometry helper")
    discrepancies = []
    if assets["navigation"]["valid"]:
        verified.append("Checksum-pinned GOES-East navigation via exact HTTP ranges")
    else:
        discrepancies.append(NAVIGATION_DISCREPANCY)
    operational_ready = bool(
        assets["model"]["valid"]
        and assets["igbp"]["valid"]
        and assets["solar_geometry"]["valid"]
    )
    preprocessing_verified = bool(
        operational_ready
        and assets["notebook"]["valid"]
        and assets["navigation"]["valid"]
        and not discrepancies
    )
    if preprocessing_verified:
        headline = (
            "Research estimates are ready with the supplied preprocessing verified. "
            "Historical numerical reproduction remains a separate, non-blocking validation task."
        )
    elif operational_ready:
        headline = "Estimate execution is ready; supplied preprocessing validation remains provisional."
    elif assets["notebook"]["valid"] and assets["model"]["valid"]:
        count = len(missing)
        noun = "input" if count == 1 else "inputs"
        headline = f"Notebook and model verified; estimate execution is blocked pending {count} runtime {noun}."
    else:
        headline = "Required runtime inputs are missing or invalid; estimate execution is blocked."
    return {
        "ready_for_estimate_execution": operational_ready,
        "ready_for_verified_inference": preprocessing_verified,
        "headline": headline,
        "verified": verified,
        "missing": missing,
        "approximations": approximations,
        "preprocessing_discrepancies": discrepancies,
        "operational_readiness": {
            "ready": operational_ready,
            "status": "ready" if operational_ready else "blocked",
            "detail": "The installed runtime assets can execute a research estimate." if operational_ready else
                      "One or more runtime assets are missing or invalid.",
        },
        "preprocessing_validation": {
            "verified": preprocessing_verified,
            "status": "verified" if preprocessing_verified else "provisional",
            "detail": (
                "Processing checks passed. LeafView uses the supplied model and reference files."
                if preprocessing_verified else
                "A difference from the supplied preprocessing remains unresolved."
            ),
        },
        "historical_numerical_reproduction": {
            "completed": False,
            "status": "unverified",
            "required_for_runtime": False,
            "detail": (
                "Reproduction of the original training-period results remains unverified because "
                "historical feature rows are unavailable."
            ),
        },
    }


def _usable_dates(provenance: dict) -> list[dict[str, object]]:
    counts: dict[str, int] = {}
    hours: dict[str, set[int]] = {}
    for attempt in provenance.get("attempts", []):
        if not attempt.get("strict"):
            continue
        day = str(attempt["date"])
        counts[day] = counts.get(day, 0) + 1
        hours.setdefault(day, set()).add(int(attempt["target_hour"]))
    return [
        {"date": day, "count": counts[day], "hours_utc": sorted(hours[day])}
        for day in sorted(counts)
    ]


def _observation_dates(provenance: dict) -> list[dict[str, object]]:
    dates: dict[str, dict[str, object]] = {}
    for attempt in provenance.get("attempts", []):
        if not attempt.get("scan"):
            continue
        day = str(attempt["date"])
        item = dates.setdefault(day, {
            "date": day,
            "observations": 0,
            "usable": 0,
            "hours_utc": [],
            "usable_hours_utc": [],
        })
        hour = int(attempt["target_hour"])
        item["observations"] = int(item["observations"]) + 1
        item["hours_utc"].append(hour)
        if attempt.get("strict"):
            item["usable"] = int(item["usable"]) + 1
            item["usable_hours_utc"].append(hour)
    return [dates[day] for day in sorted(dates)]


def _observation_support(provenance: dict) -> dict[str, object]:
    passed = provenance["usable_counts"]
    usable_dates = _usable_dates(provenance)
    composite = provenance["composite"]
    start = date.fromisoformat(str(composite["start"]))
    end = date.fromisoformat(str(composite["end"]))
    possible_days = (end - start).days + 1
    return {
        "passed_by_hour": passed,
        "passed_total": sum(passed.values()),
        "possible_per_hour": possible_days,
        "possible_total": possible_days * len(passed),
        "usable_days": len(usable_dates),
        "possible_days": possible_days,
        "usable_dates": usable_dates,
        "observation_dates": _observation_dates(provenance),
    }


def _recorded_calculation_timestamp(result: dict) -> str | None:
    provenance = result.get("provenance", {})
    processing = provenance.get("processing", {})
    candidates = (
        processing.get("calculated_at_utc"),
        result.get("calculated_at_utc"),
    )
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        try:
            parsed = datetime.fromisoformat(candidate.strip().replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            continue
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return None


def _delivery_details(
    provenance: dict, delivery: dict[str, object] | None
) -> dict[str, object]:
    details = dict(delivery or {
        "cache_hit": False,
        "request_seconds": provenance.get("processing", {}).get("duration_seconds"),
        "observation_cache": provenance.get("observation_cache", {}),
    })
    observation_cache = dict(details.get("observation_cache") or {})
    details["observation_cache"] = observation_cache
    if details.get("cache_hit"):
        calculation = {
            "kind": "saved_result",
            "summary": "Showing a saved estimate for this location and period.",
        }
    else:
        calculation = {
            "kind": "new_result",
            "summary": "New estimate calculated.",
        }
    details["calculation"] = calculation
    return details


def public_result(
    result: dict,
    identifier: str,
    display_location: str,
    delivery: dict[str, object] | None = None,
) -> dict:
    provenance = result["provenance"]
    period_start = date.fromisoformat(str(provenance["composite"]["start"]))
    first_footprint = next(
        (
            attempt.get("sampled_pixel_corners")
            for attempt in provenance.get("attempts", [])
            if attempt.get("sampled_pixel_corners")
        ),
        None,
    )
    return {
        "id": identifier,
        "label": "Research estimate",
        "experience_label": "An eight-day estimate updated daily.",
        "display_location": display_location,
        "status": result["status"],
        "lai": result["lai"],
        "units": result["units"],
        "data_message": result.get("data_message"),
        "calculated_at_utc": _recorded_calculation_timestamp(result),
        "limitations": result["limitations"],
        "readiness": result.get("readiness", {}),
        "scientific_concerns": result.get("scientific_concerns", []),
        "query": {
            "location": provenance["requested_location"],
            "period": utc_window_boundaries(period_start),
            "key": provenance.get("cache_identity", {}).get("key"),
        },
        "sampled_pixel": {
            "center": provenance.get("sampled_pixel_center"),
            "footprint": first_footprint,
            "row": provenance.get("navigation", {}).get("goes_row"),
            "column": provenance.get("navigation", {}).get("goes_column"),
        },
        "observation_support": _observation_support(provenance),
        "delivery": _delivery_details(provenance, delivery),
        "provenance": provenance,
    }


def _history_scope(latest_start: date) -> dict[str, object]:
    latest_start, latest_end = rolling_period(latest_start)
    snapshots = monthly_snapshot_periods(latest_start)
    requested_start = latest_end - timedelta(days=HISTORY_DAYS - 1)
    first_start = max(
        POST_CUTOFF_ANCHOR,
        latest_start - timedelta(days=HISTORY_DAYS - 1),
    )
    _, first_end = rolling_period(first_start)
    outside_scope = None
    first_eligible_end = first_end
    if requested_start < first_eligible_end:
        outside_scope = {
            "start": requested_start.isoformat(),
            "end": (first_eligible_end - timedelta(days=1)).isoformat(),
            "status": "outside_scope",
            "message": (
                "These ending dates would require a window containing observations "
                "from the model-development period."
            ),
        }
    return {
        "requested_days": HISTORY_DAYS,
        "requested_start": requested_start.isoformat(),
        "requested_end": latest_end.isoformat(),
        "available_start": first_start.isoformat(),
        "available_first_end": first_end.isoformat(),
        "available_latest_start": latest_start.isoformat(),
        "available_end": latest_end.isoformat(),
        "training_cutoff": TRAINING_END.isoformat(),
        "outside_scope": outside_scope,
        "mode": "monthly_snapshots",
        "label": "Monthly snapshots",
        "explanation": "Each point summarizes eight days of satellite observations.",
        "window_count": len(snapshots),
    }


def _history_item(
    identity: dict[str, object],
    period_start: date,
    result: dict | None = None,
    *,
    latest_start: date,
    state: str = "loading",
    message: str | None = None,
) -> dict[str, object]:
    _, period_end = rolling_period(period_start)
    latest = period_start == latest_start
    base = {
        "period": utc_window_boundaries(period_start),
        "snapshot": {
            "label": period_end.strftime("%B %Y") + (" · latest" if latest else ""),
            "month": period_end.strftime("%Y-%m"),
            "is_month_end": is_month_end(period_end),
            "is_latest": latest,
        },
        "query_key": identity["key"],
        "state": state,
        "status": state,
        "lai": None,
        "usable_total": None,
        "possible_total": 24,
        "usable_days": None,
        "possible_days": 8,
        "gap": True,
        "message": message,
    }
    if result is None:
        return base
    support = _observation_support(result["provenance"])
    item_state = "insufficient_data" if result["lai"] is None else "available"
    return {
        **base,
        "state": item_state,
        "status": result["status"],
        "lai": result["lai"],
        "usable_total": support["passed_total"],
        "possible_total": support["possible_total"],
        "usable_days": support["usable_days"],
        "possible_days": support["possible_days"],
        "usable_counts": support["passed_by_hour"],
        "observation_dates": support["observation_dates"],
        "gap": result["lai"] is None,
        "message": result.get("data_message"),
    }


class EstimateJobs:
    def __init__(self, root: Path, observation_memo: ObservationMemo | None = None):
        self.root = root
        self.observation_memo = observation_memo or ObservationMemo()
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._running_by_query: dict[str, str] = {}
        self._completed_by_query: dict[str, tuple[dict, Path]] = {}
        self._timing: dict[str, dict[str, object]] = {}

    def _set(self, job_id: str, **changes: object) -> None:
        with self._lock:
            self._jobs[job_id].update(changes)

    def _progress(self, job_id: str, update: dict[str, object]) -> None:
        now = time.perf_counter()
        stage = str(update.get("stage") or "unknown")
        with self._lock:
            timing = self._timing.get(job_id)
            if timing is not None:
                current_stage = timing.get("current_stage")
                stage_started = float(timing.get("stage_started", now))
                if current_stage and current_stage != stage:
                    phase_seconds = timing["phase_seconds"]
                    assert isinstance(phase_seconds, dict)
                    phase_seconds[current_stage] = float(phase_seconds.get(current_stage, 0)) + (
                        now - stage_started
                    )
                    timing["stage_started"] = now
                timing["current_stage"] = stage
            self._jobs[job_id].update(progress=update)

    def _finish_timing(self, job_id: str, started: float) -> dict[str, object]:
        now = time.perf_counter()
        with self._lock:
            timing = self._timing.pop(job_id, {})
            current_stage = timing.get("current_stage")
            if current_stage:
                phase_seconds = timing["phase_seconds"]
                assert isinstance(phase_seconds, dict)
                phase_seconds[str(current_stage)] = float(
                    phase_seconds.get(str(current_stage), 0)
                ) + now - float(timing.get("stage_started", now))
            phases = {
                str(name): round(float(seconds), 3)
                for name, seconds in dict(timing.get("phase_seconds", {})).items()
            }
            return {
                "queue_wait_seconds": round(float(timing.get("queue_wait_seconds", 0)), 3),
                "phase_seconds": phases,
                "total_seconds": round(now - started, 3),
            }

    def snapshot(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return deepcopy(job) if job else None

    def active_count(self) -> int:
        with self._lock:
            return len(self._running_by_query)

    def compatible_result(
        self, identity: dict[str, object]
    ) -> tuple[dict, Path] | None:
        """Return a provenance-compatible result without recalculating it."""

        key = str(identity["key"])
        with self._lock:
            completed = self._completed_by_query.get(key)
            if completed is not None:
                result, source = completed
                return deepcopy(result), source
        cached = load_compatible_result(self.root, identity)
        if cached is None:
            return None
        result, source = cached
        self.observation_memo.seed_result(result)
        with self._lock:
            self._completed_by_query[key] = (deepcopy(result), source)
        return result, source

    def start(
        self, latitude: float, longitude: float, period_start: date, display_location: str
    ) -> dict:
        request_started = time.perf_counter()
        identity = cache_identity(latitude, longitude, period_start, self.root)
        key = str(identity["key"])
        cached = self.compatible_result(identity)
        if cached:
            result, source = cached
            self.observation_memo.seed_result(result)
            job_id = uuid.uuid4().hex
            delivery = {
                "cache_hit": True,
                "request_seconds": round(time.perf_counter() - request_started, 3),
                "source": str(source.relative_to(self.root)),
                "original_processing_seconds": result.get("provenance", {}).get("processing", {}).get("duration_seconds"),
                "observation_cache": result.get("provenance", {}).get("observation_cache", {}),
                "timing": {
                    "queue_wait_seconds": 0.0,
                    "phase_seconds": {"cache_lookup": round(time.perf_counter() - request_started, 3)},
                    "total_seconds": round(time.perf_counter() - request_started, 3),
                },
            }
            job = {
                "job_id": job_id,
                "state": "complete",
                "query_key": key,
                "progress": {
                    "percent": 100,
                    "stage": "cache",
                    "detail": "Showing a saved estimate for this location and period.",
                },
                "result": public_result(result, key[:16], display_location, delivery),
            }
            with self._lock:
                self._jobs[job_id] = job
            return deepcopy(job)

        with self._lock:
            existing_id = self._running_by_query.get(key)
            if existing_id:
                return deepcopy(self._jobs[existing_id])
            job_id = uuid.uuid4().hex
            job = {
                "job_id": job_id,
                "state": "running",
                "query_key": key,
                "progress": {
                    "percent": 0,
                    "stage": "queued",
                    "detail": "Queued the selected eight-day research estimate",
                },
            }
            self._jobs[job_id] = job
            self._running_by_query[key] = job_id
            queued_at = time.perf_counter()
            self._timing[job_id] = {
                "queued_at": queued_at,
                "queue_wait_seconds": 0.0,
                "phase_seconds": {},
                "current_stage": None,
                "stage_started": queued_at,
            }
        worker = threading.Thread(
            target=self._run,
            args=(
                job_id, key, identity, latitude, longitude, period_start,
                display_location, request_started,
            ),
            daemon=True,
            name=f"leafview-{job_id[:8]}",
        )
        worker.start()
        return deepcopy(job)

    def _run(
        self,
        job_id: str,
        key: str,
        identity: dict[str, object],
        latitude: float,
        longitude: float,
        period_start: date,
        display_location: str,
        request_started: float,
    ) -> None:
        started = time.perf_counter()
        with self._lock:
            timing = self._timing[job_id]
            timing["queue_wait_seconds"] = started - float(timing["queued_at"])
            timing["stage_started"] = started
        try:
            output = runtime_cache_path(self.root, key)
            result = run_estimate(
                latitude,
                longitude,
                period_start,
                None,
                self.root,
                output,
                display_location,
                progress=lambda update: self._progress(job_id, update),
                identity=identity,
                observation_memo=self.observation_memo,
            )
            delivery = {
                "cache_hit": False,
                "request_seconds": round(time.perf_counter() - request_started, 3),
                "source": str(output.relative_to(self.root)),
                "observation_cache": result.get("provenance", {}).get("observation_cache", {}),
            }
            delivery["timing"] = self._finish_timing(job_id, request_started)
            with self._lock:
                self._completed_by_query[key] = (deepcopy(result), output)
            self._set(
                job_id,
                state="complete",
                progress={
                    "percent": 100,
                    "stage": "complete",
                    "detail": (
                        "No usable observations; the period is shown as a gap"
                        if result["status"] == "insufficient_data"
                        else "Estimate ready"
                    ),
                },
                result=public_result(result, key[:16], display_location, delivery),
            )
        except Exception as error:
            timing = self._finish_timing(job_id, request_started)
            self._set(
                job_id,
                state="error",
                progress={"percent": 100, "stage": "error", "detail": "Estimate could not be completed"},
                error=str(error),
                error_kind=(
                    "retrieval_error"
                    if isinstance(error, ObservationRetrievalError)
                    else "processing_error"
                ),
                timing=timing,
            )
        finally:
            with self._lock:
                self._running_by_query.pop(key, None)


class HistoryJobs:
    def __init__(
        self,
        root: Path,
        observation_memo: ObservationMemo,
        estimate_jobs: EstimateJobs,
    ):
        self.root = root
        self.observation_memo = observation_memo
        self.estimate_jobs = estimate_jobs
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._running_by_query: dict[str, str] = {}
        self._jobs_by_query: dict[str, str] = {}

    def snapshot(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return deepcopy(job) if job else None

    def active_count(self) -> int:
        with self._lock:
            return len(self._running_by_query)

    def _set(self, job_id: str, **changes: object) -> None:
        with self._lock:
            self._jobs[job_id].update(changes)

    def _set_item(self, job_id: str, index: int, item: dict, **changes: object) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job["periods"][index] = item
            job.update(changes)

    @staticmethod
    def _progress(
        periods: list[dict],
        *,
        detail: str,
        window_progress: dict | None = None,
    ) -> dict[str, object]:
        completed = sum(item["state"] != "loading" for item in periods)
        monthly = [item for item in periods if item["snapshot"]["is_month_end"]]
        monthly_completed = sum(
            item["state"] in {"available", "insufficient_data"} for item in monthly
        )
        progress: dict[str, object] = {
            "completed": completed,
            "total": len(periods),
            "monthly_completed": monthly_completed,
            "monthly_finished": sum(item["state"] != "loading" for item in monthly),
            "monthly_total": len(monthly),
            "percent": round(100 * completed / len(periods), 1),
            "detail": detail,
        }
        if window_progress:
            progress["active_windows"] = window_progress
        return progress

    def cancel(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job["state"] == "running":
                job["cancel_requested"] = True
                job["progress"]["detail"] = "Stopping history work for the previous location."
            return deepcopy(job)

    def start(
        self,
        latitude: float,
        longitude: float,
        latest_start: date,
        display_location: str,
        expected_latest_key: str | None = None,
    ) -> dict:
        started = time.perf_counter()
        windows = monthly_snapshot_periods(latest_start)
        identities = [
            cache_identity(latitude, longitude, period_start, self.root)
            for period_start, _ in windows
        ]
        query_document = {
            "version": "monthly-snapshot-history-v1",
            "latitude": latitude,
            "longitude": longitude,
            "latest_start": latest_start.isoformat(),
            "first_start": windows[0][0].isoformat(),
        }
        query_key = hashlib.sha256(
            json.dumps(query_document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if expected_latest_key and str(identities[-1]["key"]) != expected_latest_key:
            raise ValueError("The latest estimate does not match the requested history")
        with self._lock:
            running_id = self._running_by_query.get(query_key)
            if running_id:
                return deepcopy(self._jobs[running_id])
            previous_id = self._jobs_by_query.get(query_key)
            if previous_id:
                previous = self._jobs[previous_id]
                if previous["state"] == "complete" and not previous.get("error_count"):
                    return deepcopy(previous)

        items = []
        completed = 0
        for (period_start, _), identity in zip(windows, identities, strict=True):
            compatible_result = getattr(self.estimate_jobs, "compatible_result", None)
            cached = (
                compatible_result(identity)
                if compatible_result is not None
                else load_compatible_result(self.root, identity)
            )
            if cached:
                result, _ = cached
                self.observation_memo.seed_result(result)
                items.append(_history_item(
                    identity,
                    period_start,
                    result,
                    latest_start=latest_start,
                ))
                completed += 1
            else:
                items.append(_history_item(
                    identity,
                    period_start,
                    latest_start=latest_start,
                    message="This monthly snapshot is waiting to be calculated.",
                ))
        job_id = uuid.uuid4().hex
        state = "complete" if completed == len(items) else "running"
        job = {
            "job_id": job_id,
            "state": state,
            "query_key": query_key,
            "query": {
                "location": {
                    "latitude": latitude,
                    "longitude": longitude,
                    "name": display_location,
                },
                "latest_period": utc_window_boundaries(latest_start),
            },
            "scope": _history_scope(latest_start),
            "periods": items,
            "progress": self._progress(
                items,
                detail=(
                    "Monthly snapshots loaded from saved estimates."
                    if state == "complete" else
                    "Loading the newest monthly snapshots first."
                ),
            ),
            "elapsed_seconds": round(time.perf_counter() - started, 3) if state == "complete" else None,
            "error_count": 0,
            "latest_reused": items[-1]["state"] != "loading",
            "max_concurrent_windows": HISTORY_MAX_CONCURRENT_WINDOWS,
            "cancel_requested": False,
        }
        with self._lock:
            running_id = self._running_by_query.get(query_key)
            if running_id:
                return deepcopy(self._jobs[running_id])
            previous_id = self._jobs_by_query.get(query_key)
            if previous_id:
                previous = self._jobs[previous_id]
                if previous["state"] == "complete" and not previous.get("error_count"):
                    return deepcopy(previous)
            self._jobs[job_id] = job
            self._jobs_by_query[query_key] = job_id
            if state == "running":
                self._running_by_query[query_key] = job_id
        if state == "running":
            worker = threading.Thread(
                target=self._run,
                args=(
                    job_id,
                    query_key,
                    windows,
                    identities,
                    latest_start,
                    latitude,
                    longitude,
                    display_location,
                    started,
                ),
                daemon=True,
                name=f"leafview-history-{job_id[:8]}",
            )
            worker.start()
        return deepcopy(job)

    def _run(
        self,
        job_id: str,
        query_key: str,
        windows: list[tuple[date, date]],
        identities: list[dict[str, object]],
        latest_start: date,
        latitude: float,
        longitude: float,
        display_location: str,
        started: float,
    ) -> None:
        errors = 0
        total = len(windows)
        pending: list[int] = []
        active: dict[int, dict] = {}
        prioritize_newest = True
        try:
            with self._lock:
                pending = [
                    index for index in range(total - 1, -1, -1)
                    if self._jobs[job_id]["periods"][index]["state"] == "loading"
                ]
            while pending or active:
                with self._lock:
                    if self._jobs[job_id].get("cancel_requested"):
                        self._jobs[job_id].update(
                            state="cancelled",
                            elapsed_seconds=round(time.perf_counter() - started, 3),
                        )
                        return

                concurrency_limit = 1 if prioritize_newest else HISTORY_MAX_CONCURRENT_WINDOWS
                while pending and len(active) < concurrency_limit:
                    index = pending.pop(0)
                    period_start = windows[index][0]
                    estimate_job = self.estimate_jobs.start(
                        latitude, longitude, period_start, display_location
                    )
                    active[index] = estimate_job

                active_progress: dict[str, object] = {}
                finished: list[int] = []
                for index, estimate_job in list(active.items()):
                    if estimate_job["state"] == "running":
                        estimate_job = self.estimate_jobs.snapshot(
                            str(estimate_job["job_id"])
                        )
                        if estimate_job is not None:
                            active[index] = estimate_job
                    if estimate_job is None:
                        errors += 1
                        item = _history_item(
                            identities[index], windows[index][0], latest_start=latest_start,
                            state="error",
                            message="The shared estimate job disappeared before completion.",
                        )
                    elif estimate_job["state"] == "running":
                        active_progress[windows[index][0].isoformat()] = estimate_job.get(
                            "progress", {}
                        )
                        continue
                    elif estimate_job["state"] == "error":
                        errors += 1
                        item = _history_item(
                            identities[index], windows[index][0], latest_start=latest_start,
                            state=str(estimate_job.get("error_kind") or "error"),
                            message=str(estimate_job.get("error") or "Estimate failed"),
                        )
                    else:
                        item = _history_item(
                            identities[index], windows[index][0], estimate_job["result"],
                            latest_start=latest_start,
                        )
                    finished.append(index)
                    with self._lock:
                        periods = self._jobs[job_id]["periods"]
                        periods[index] = item
                        self._jobs[job_id]["error_count"] = errors
                        self._jobs[job_id]["progress"] = self._progress(
                            periods,
                            detail="Loading the remaining monthly snapshots.",
                            window_progress=active_progress,
                        )
                for index in finished:
                    active.pop(index, None)
                if finished:
                    prioritize_newest = False
                if active and not finished:
                    with self._lock:
                        periods = self._jobs[job_id]["periods"]
                        self._jobs[job_id]["progress"] = self._progress(
                            periods,
                            detail="Loading the newest monthly snapshots first.",
                            window_progress=active_progress,
                        )
                    time.sleep(0.1)
            with self._lock:
                periods = deepcopy(self._jobs[job_id]["periods"])
            self._set(
                job_id,
                state="complete",
                elapsed_seconds=round(time.perf_counter() - started, 3),
                progress=self._progress(
                    periods, detail="Monthly snapshots finished loading."
                ),
                error_count=errors,
            )
        except Exception as error:
            with self._lock:
                periods = deepcopy(self._jobs[job_id]["periods"])
            self._set(
                job_id,
                state="error",
                elapsed_seconds=round(time.perf_counter() - started, 3),
                progress=self._progress(
                    periods, detail="Monthly snapshots stopped before they finished."
                ),
                error_count=errors + 1,
                error=str(error),
            )
        finally:
            with self._lock:
                self._running_by_query.pop(query_key, None)


def _parse_location(payload: dict) -> tuple[float, float, str]:
    try:
        latitude = round(float(payload["latitude"]), 6)
        longitude = round(float(payload["longitude"]), 6)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Latitude and longitude are required") from error
    if not math.isfinite(latitude) or not math.isfinite(longitude):
        raise ValueError("Latitude and longitude must be finite")
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("Latitude or longitude is outside its valid range")
    display_location = str(payload.get("display_location") or "Selected location").strip()
    if not display_location:
        display_location = "Selected location"
    return latitude, longitude, display_location[:100]


def _parse_estimate_payload(
    payload: dict,
    now: datetime,
) -> tuple[float, float, date, str]:
    latitude, longitude, display_location = _parse_location(payload)
    raw_start = payload.get("start")
    if raw_start:
        try:
            period_start = date.fromisoformat(str(raw_start))
        except ValueError as error:
            raise ValueError("Historical window start must be an ISO date") from error
        _, period_end = rolling_period(period_start)
    else:
        period_start, period_end = latest_completed_rolling_period(now)
    if now.astimezone(timezone.utc) < archive_ready_after(period_end):
        raise ValueError(
            "This eight-day window is still within the archive publication delay"
        )
    return latitude, longitude, period_start, display_location


def create_app(
    root: Path = ROOT,
    clock: Callable[[], datetime] | None = None,
    place_lookup: Callable[[float, float], dict[str, str] | None] | None = None,
    place_search: Callable[[str], list[dict[str, object]]] | None = None,
    validate_remote_navigation: bool = False,
) -> Flask:
    app = Flask(__name__, template_folder=str(root / "templates"), static_folder=str(root / "static"))
    app.json = StrictJSONProvider(app)
    clock = clock or (lambda: datetime.now(timezone.utc))
    if place_lookup is None:
        provider = NominatimPlaceLookup()
        place_lookup = provider
        place_search = place_search or provider.search
    elif place_search is None:
        place_search = getattr(place_lookup, "search", None)
    observation_memo = ObservationMemo()
    jobs = EstimateJobs(root, observation_memo)
    history_jobs = HistoryJobs(root, observation_memo, jobs)
    reference_assets = ReferenceAssets(root / "artifacts/reference")

    def asset_audit() -> dict[str, object]:
        return reference_assets.audit(
            validate_remote_navigation=validate_remote_navigation
        )

    if validate_remote_navigation:
        asset_audit()

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.after_request
    def prevent_stale_local_ui(response):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response

    @app.get("/api/status")
    def status():
        now = clock().astimezone(timezone.utc)
        latest_start, latest_end = latest_completed_rolling_period(now)
        assets = asset_audit()
        dependencies = dependency_status(assets)
        navigation = assets["navigation"]
        return jsonify({
            **dependencies,
            "result_label": "Research estimate",
            "experience_label": "An eight-day estimate updated daily.",
            "latest_window": utc_window_boundaries(latest_start),
            "archive_publication_delay_hours": (
                ARCHIVE_PUBLICATION_DELAY.total_seconds() / 3600
            ),
            "archive_note": (
                "LeafView waits two hours after the final 21:00 UTC observation group "
                "before choosing the newest complete day."
            ),
            "historical_date_range": {
                "minimum_end": (POST_CUTOFF_ANCHOR + timedelta(days=7)).isoformat(),
                "maximum_end": latest_end.isoformat(),
            },
            "history_scope": _history_scope(latest_start),
            "navigation_source": {
                "mode": navigation["mode"],
                "remote_object": navigation["remote_object"],
                "metadata": navigation["metadata"],
                "metadata_error": navigation["metadata_error"],
                "metadata_validation": navigation["metadata_validation"],
                "cache_policy": "Only checksummed exact scalar ranges are retained.",
                "full_file_fallbacks": navigation["full_file_fallbacks"],
            },
            "rolling_window_accuracy": {
                "status": "separately_unevaluated",
                "detail": (
                    "Rolling-window construction and numerical consistency are checked, "
                    "but rolling-window prediction accuracy has not been evaluated."
                ),
            },
        })

    @app.post("/api/place-name")
    def place_name():
        payload = request.get_json(silent=True) or {}
        try:
            latitude, longitude, _ = _parse_location(payload)
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
        try:
            place = place_lookup(latitude, longitude)
        except PlaceLookupError:
            return jsonify({"error": "Place name lookup unavailable"}), 502
        if place is None:
            return jsonify({"error": "No supported area name was returned"}), 404
        return jsonify({
            **place,
            "requested_location": {
                "latitude": latitude,
                "longitude": longitude,
            },
            "attribution": "© OpenStreetMap contributors",
        })

    @app.get("/api/place-search")
    def search_places():
        query = " ".join(str(request.args.get("q") or "").split()).strip()
        if len(query) < 2:
            return jsonify({"error": "Enter at least two characters to search"}), 400
        if len(query) > 160:
            return jsonify({"error": "Place search is too long"}), 400
        if place_search is None:
            return jsonify({"error": "Place search is unavailable"}), 503
        try:
            results = place_search(query)
        except PlaceLookupError:
            return jsonify({"error": "Place search is unavailable"}), 502
        return jsonify({
            "query": query,
            "results": results,
            "attribution": "© OpenStreetMap contributors",
        })

    @app.get("/api/examples/montgomery-2026-04-07")
    def cached_example():
        path = root / "data/results/montgomery-md-2026-04-07.json"
        if not path.is_file():
            return jsonify({"status": "unavailable"}), 404
        result = json.loads(path.read_text(encoding="utf-8"))
        identity = cache_identity(
            MONTGOMERY["latitude"], MONTGOMERY["longitude"], TREND_STARTS[0], root
        )
        if not result_matches_identity(result, identity):
            return jsonify({"status": "stale", "error": "The cached example provenance is stale"}), 409
        return jsonify(public_result(
            result,
            "montgomery-md-2026-04-07",
            MONTGOMERY["name"],
            {
                "cache_hit": True,
                "request_seconds": 0.0,
                "source": str(path.relative_to(root)),
                "original_processing_seconds": result.get("provenance", {}).get("processing", {}).get("duration_seconds"),
                "observation_cache": result.get("provenance", {}).get("observation_cache", {}),
            },
        ))

    @app.get("/api/trends/montgomery")
    def montgomery_trend():
        periods = []
        for period_start in TREND_STARTS:
            query = normalized_query(
                MONTGOMERY["latitude"], MONTGOMERY["longitude"], period_start
            )
            identity = cache_identity(
                MONTGOMERY["latitude"], MONTGOMERY["longitude"], period_start, root
            )
            cached = load_compatible_result(root, identity)
            if not cached:
                periods.append({
                    "period": {"start": query["start"], "end": query["end"]},
                    "status": "unavailable",
                    "lai": None,
                    "usable_total": 0,
                    "gap": True,
                    "message": "This real period has not been processed with the current provenance.",
                })
                continue
            result, _ = cached
            counts = result["provenance"]["usable_counts"]
            periods.append({
                "period": result["provenance"]["composite"],
                "status": result["status"],
                "lai": result["lai"],
                "usable_total": sum(counts.values()),
                "usable_counts": counts,
                "gap": result["lai"] is None,
                "message": result.get("data_message"),
            })
        return jsonify({
            "label": "Research estimate trend demonstration",
            "demo": True,
            "location": MONTGOMERY,
            "periods": periods,
            "gap_rule": "Periods without a research estimate are rendered as gaps; lines never bridge them.",
        })

    @app.post("/api/estimate")
    def estimate():
        payload = request.get_json(silent=True) or {}
        try:
            latitude, longitude, period_start, display_location = _parse_estimate_payload(
                payload, clock()
            )
            if not dependency_status(asset_audit())[
                "ready_for_verified_inference"
            ]:
                return jsonify({"error": "Verified preprocessing is not ready"}), 503
            job = jobs.start(latitude, longitude, period_start, display_location)
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
        return jsonify(job), 200 if job["state"] == "complete" else 202

    @app.get("/api/jobs/<job_id>")
    def estimate_job(job_id: str):
        job = jobs.snapshot(job_id)
        if job is None:
            return jsonify({"error": "Estimate job not found"}), 404
        return jsonify(job)

    @app.post("/api/history")
    def start_history():
        payload = request.get_json(silent=True) or {}
        try:
            latitude, longitude, display_location = _parse_location(payload)
            latest_start, _ = latest_completed_rolling_period(clock())
            if not dependency_status(asset_audit())[
                "ready_for_verified_inference"
            ]:
                return jsonify({"error": "Verified preprocessing is not ready"}), 503
            job = history_jobs.start(
                latitude,
                longitude,
                latest_start,
                display_location,
                expected_latest_key=(
                    str(payload["latest_query_key"])
                    if payload.get("latest_query_key")
                    else None
                ),
            )
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
        return jsonify(job), 200 if job["state"] == "complete" else 202

    @app.get("/api/history/<job_id>")
    def history_job(job_id: str):
        job = history_jobs.snapshot(job_id)
        if job is None:
            return jsonify({"error": "History job not found"}), 404
        return jsonify(job)

    @app.post("/api/history/<job_id>/cancel")
    def cancel_history_job(job_id: str):
        job = history_jobs.cancel(job_id)
        if job is None:
            return jsonify({"error": "History job not found"}), 404
        return jsonify(job)

    @app.get("/api/activity")
    def activity():
        return jsonify({
            "estimate_jobs": jobs.active_count(),
            "history_jobs": history_jobs.active_count(),
            "active_jobs": jobs.active_count() + history_jobs.active_count(),
        })

    return app


def run() -> None:
    create_app(validate_remote_navigation=True).run(
        host="127.0.0.1", port=8781, debug=False, threaded=True
    )
