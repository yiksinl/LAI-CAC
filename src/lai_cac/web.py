from __future__ import annotations

import json
import hashlib
import math
import threading
import time
import uuid
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from flask import Flask, jsonify, render_template, request

from .assets import ReferenceAssets
from .composites import (
    ARCHIVE_PUBLICATION_DELAY,
    HISTORY_DAYS,
    POST_CUTOFF_ANCHOR,
    TRAINING_END,
    archive_ready_after,
    latest_completed_rolling_period,
    rolling_period,
    rolling_periods_through,
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

NAVIGATION_DISCREPANCY = {
    "key": "navigation_equivalence",
    "name": "Notebook navigation-grid equivalence",
    "detail": (
        "The notebook reads pixel centers and LocalZenithAngle from "
        "GOES_Navigation_2kmFD-GOES-East.nc. A checksum-verified readable raster is required "
        "to validate navigation-raster Latitude, Longitude, LocalZenithAngle, and LandMask."
    ),
}


def dependency_status(assets: dict) -> dict:
    verified = []
    missing = []
    approximations = []
    if assets["notebook"]["present"]:
        verified.append("Authoritative notebook")
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
        verified.append("Checksum-verified original GOES-East navigation raster")
    else:
        discrepancies.append(NAVIGATION_DISCREPANCY)
    operational_ready = bool(
        assets["model"]["valid"]
        and assets["igbp"]["valid"]
        and assets["solar_geometry"]["valid"]
    )
    preprocessing_verified = bool(
        operational_ready
        and assets["notebook"]["present"]
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
    elif assets["notebook"]["present"] and assets["model"]["valid"]:
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
            "kind": "previous_result",
            "summary": "Loaded a previously calculated LAI result.",
        }
    elif int(observation_cache.get("downloaded", 0)) or int(
        observation_cache.get("partial_remote", 0)
    ):
        calculation = {
            "kind": "new_observations",
            "summary": "Calculated after downloading new satellite observations.",
        }
    elif int(observation_cache.get("reused", 0)) or int(
        observation_cache.get("partial_reused", 0)
    ) or int(observation_cache.get("memory_reused", 0)):
        calculation = {
            "kind": "cached_observations",
            "summary": "Calculated using cached satellite observations.",
        }
    else:
        calculation = {
            "kind": "calculated",
            "summary": "Calculated from the selected satellite observations.",
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
    windows = rolling_periods_through(latest_start)
    latest_end = windows[-1][1]
    requested_start = latest_end - timedelta(days=HISTORY_DAYS - 1)
    first_start, first_end = windows[0]
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
        "available_latest_start": windows[-1][0].isoformat(),
        "available_end": latest_end.isoformat(),
        "training_cutoff": TRAINING_END.isoformat(),
        "outside_scope": outside_scope,
        "window_count": len(windows),
    }


def _history_item(
    identity: dict[str, object],
    period_start: date,
    result: dict | None = None,
    *,
    state: str = "loading",
    message: str | None = None,
) -> dict[str, object]:
    base = {
        "period": utc_window_boundaries(period_start),
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

    def _set(self, job_id: str, **changes: object) -> None:
        with self._lock:
            self._jobs[job_id].update(changes)

    def _progress(self, job_id: str, update: dict[str, object]) -> None:
        self._set(job_id, progress=update)

    def snapshot(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return deepcopy(job) if job else None

    def active_count(self) -> int:
        with self._lock:
            return len(self._running_by_query)

    def start(
        self, latitude: float, longitude: float, period_start: date, display_location: str
    ) -> dict:
        request_started = time.perf_counter()
        identity = cache_identity(latitude, longitude, period_start, self.root)
        key = str(identity["key"])
        cached = load_compatible_result(self.root, identity)
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
            }
            job = {
                "job_id": job_id,
                "state": "complete",
                "query_key": key,
                "progress": {
                    "percent": 100,
                    "stage": "cache",
                    "detail": "Loaded a provenance-matched cached research estimate",
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
        worker = threading.Thread(
            target=self._run,
            args=(job_id, key, identity, latitude, longitude, period_start, display_location),
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
    ) -> None:
        started = time.perf_counter()
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
                "request_seconds": round(time.perf_counter() - started, 3),
                "source": str(output.relative_to(self.root)),
                "observation_cache": result.get("provenance", {}).get("observation_cache", {}),
            }
            self._set(
                job_id,
                state="complete",
                progress={
                    "percent": 100,
                    "stage": "complete",
                    "detail": (
                        "No usable observations; the period is shown as a gap"
                        if result["status"] == "insufficient_data"
                        else "Research estimate complete"
                    ),
                },
                result=public_result(result, key[:16], display_location, delivery),
            )
        except Exception as error:
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

    def start(
        self,
        latitude: float,
        longitude: float,
        latest_start: date,
        display_location: str,
    ) -> dict:
        started = time.perf_counter()
        windows = rolling_periods_through(latest_start)
        identities = [
            cache_identity(latitude, longitude, period_start, self.root)
            for period_start, _ in windows
        ]
        query_document = {
            "version": "daily-rolling-history-v1",
            "latitude": latitude,
            "longitude": longitude,
            "latest_start": latest_start.isoformat(),
            "first_start": windows[0][0].isoformat(),
        }
        query_key = hashlib.sha256(
            json.dumps(query_document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        with self._lock:
            running_id = self._running_by_query.get(query_key)
            if running_id:
                return deepcopy(self._jobs[running_id])

        items = []
        completed = 0
        for (period_start, _), identity in zip(windows, identities, strict=True):
            cached = load_compatible_result(self.root, identity)
            if cached:
                result, _ = cached
                self.observation_memo.seed_result(result)
                items.append(_history_item(identity, period_start, result))
                completed += 1
            else:
                items.append(_history_item(
                    identity,
                    period_start,
                    message="This rolling window is waiting to be calculated.",
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
            "progress": {
                "completed": completed,
                "total": len(items),
                "percent": round(100 * completed / len(items), 1),
                "detail": (
                    "Available history loaded from provenance-matched results."
                    if state == "complete" else
                    "Loading the newest available rolling windows first."
                ),
            },
            "elapsed_seconds": round(time.perf_counter() - started, 3) if state == "complete" else None,
            "error_count": 0,
        }
        with self._lock:
            self._jobs[job_id] = job
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
                    latitude,
                    longitude,
                    display_location,
                    completed,
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
        latitude: float,
        longitude: float,
        display_location: str,
        completed: int,
        started: float,
    ) -> None:
        errors = 0
        total = len(windows)
        try:
            for index in range(total - 1, -1, -1):
                with self._lock:
                    if self._jobs[job_id]["periods"][index]["state"] != "loading":
                        continue
                period_start = windows[index][0]
                identity = identities[index]

                estimate_job = self.estimate_jobs.start(
                    latitude,
                    longitude,
                    period_start,
                    display_location,
                )
                while estimate_job["state"] == "running":
                    update = estimate_job.get("progress", {})
                    overall = 100 * (
                        completed + float(update.get("percent", 0)) / 100
                    ) / total
                    self._set(job_id, progress={
                        "completed": completed,
                        "total": total,
                        "percent": round(overall, 1),
                        "current_period": utc_window_boundaries(period_start),
                        "window_progress": update,
                        "detail": f"Loading {period_start.isoformat()} rolling window.",
                    })
                    time.sleep(0.1)
                    estimate_job = self.estimate_jobs.snapshot(estimate_job["job_id"])
                    if estimate_job is None:
                        break
                if estimate_job is None:
                    errors += 1
                    item = _history_item(
                        identity,
                        period_start,
                        state="error",
                        message="The shared estimate job disappeared before completion.",
                    )
                elif estimate_job["state"] == "error":
                    errors += 1
                    item = _history_item(
                        identity,
                        period_start,
                        state=str(estimate_job.get("error_kind") or "error"),
                        message=str(estimate_job.get("error") or "Estimate failed"),
                    )
                else:
                    item = _history_item(
                        identity, period_start, estimate_job["result"]
                    )
                completed += 1
                self._set_item(job_id, index, item, error_count=errors, progress={
                    "completed": completed,
                    "total": total,
                    "percent": round(100 * completed / total, 1),
                    "detail": f"Loaded {completed} of {total} rolling windows.",
                })
            self._set(
                job_id,
                state="complete",
                elapsed_seconds=round(time.perf_counter() - started, 3),
                progress={
                    "completed": completed,
                    "total": total,
                    "percent": 100.0,
                    "detail": "Available rolling-window history finished loading.",
                },
                error_count=errors,
            )
        except Exception as error:
            self._set(
                job_id,
                state="error",
                elapsed_seconds=round(time.perf_counter() - started, 3),
                progress={
                    "completed": completed,
                    "total": total,
                    "percent": round(100 * completed / total, 1),
                    "detail": "Rolling-window history stopped before it finished.",
                },
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
) -> Flask:
    app = Flask(__name__, template_folder=str(root / "templates"), static_folder=str(root / "static"))
    app.json = StrictJSONProvider(app)
    clock = clock or (lambda: datetime.now(timezone.utc))
    observation_memo = ObservationMemo()
    jobs = EstimateJobs(root, observation_memo)
    history_jobs = HistoryJobs(root, observation_memo, jobs)

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
        assets = ReferenceAssets(root / "artifacts/reference").audit()
        dependencies = dependency_status(assets)
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
            "rolling_window_accuracy": {
                "status": "separately_unevaluated",
                "detail": (
                    "Rolling-window construction and numerical consistency are checked, "
                    "but rolling-window prediction accuracy has not been evaluated."
                ),
            },
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
            if not dependency_status(ReferenceAssets(root / "artifacts/reference").audit())[
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
            if not dependency_status(ReferenceAssets(root / "artifacts/reference").audit())[
                "ready_for_verified_inference"
            ]:
                return jsonify({"error": "Verified preprocessing is not ready"}), 503
            job = history_jobs.start(
                latitude, longitude, latest_start, display_location
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

    @app.get("/api/activity")
    def activity():
        return jsonify({
            "estimate_jobs": jobs.active_count(),
            "history_jobs": history_jobs.active_count(),
            "active_jobs": jobs.active_count() + history_jobs.active_count(),
        })

    return app


def run() -> None:
    create_app().run(host="127.0.0.1", port=8781, debug=False, threaded=True)
