from datetime import date, datetime, timedelta, timezone
from copy import deepcopy
import json
import math
import time

import pytest
from flask import jsonify

from lai_cac.composites import (
    aligned_period,
    archive_ready_after,
    latest_completed_rolling_period,
    monthly_snapshot_periods,
    rolling_period,
    rolling_periods_through,
    utc_window_boundaries,
)
from pathlib import Path

from lai_cac.assets import ReferenceAssets
from lai_cac.geometry import solar_angles
from lai_cac.goes import Scan, decode_dqf, passes_notebook_strict_quality
from lai_cac.inference import IGBP_CATEGORIES, ObservationMemo, seasonality_features
from lai_cac.web import (
    EstimateJobs,
    HistoryJobs,
    create_app,
    dependency_status,
    public_result,
)


def test_first_post_cutoff_period_is_aligned():
    assert aligned_period(date(2026, 4, 7)) == (date(2026, 4, 7), date(2026, 4, 14))


def test_off_calendar_period_is_rejected():
    with pytest.raises(ValueError):
        aligned_period(date(2026, 4, 8))


def test_training_period_is_rejected():
    with pytest.raises(ValueError, match="not after the model-development cutoff"):
        aligned_period(date(2026, 3, 30))


def test_daily_rolling_windows_accept_off_calendar_starts_and_stay_post_training():
    assert rolling_period(date(2026, 4, 8)) == (
        date(2026, 4, 8), date(2026, 4, 15)
    )
    with pytest.raises(ValueError, match="not after the model-development cutoff"):
        rolling_period(date(2026, 4, 6))
    windows = rolling_periods_through(date(2026, 4, 10))
    assert [start for start, _ in windows] == [
        date(2026, 4, 7), date(2026, 4, 8), date(2026, 4, 9), date(2026, 4, 10)
    ]


def test_monthly_snapshots_select_month_end_windows_before_latest_and_deduplicate():
    assert monthly_snapshot_periods(date(2026, 9, 2)) == [
        (date(2026, 4, 23), date(2026, 4, 30)),
        (date(2026, 5, 24), date(2026, 5, 31)),
        (date(2026, 6, 23), date(2026, 6, 30)),
        (date(2026, 7, 24), date(2026, 7, 31)),
        (date(2026, 8, 24), date(2026, 8, 31)),
        (date(2026, 9, 2), date(2026, 9, 9)),
    ]
    assert monthly_snapshot_periods(date(2026, 8, 24))[-1] == (
        date(2026, 8, 24), date(2026, 8, 31)
    )
    assert len(monthly_snapshot_periods(date(2026, 8, 24))) == 5


def test_latest_window_waits_for_the_archive_publication_delay_and_has_utc_bounds():
    before_ready = datetime(2026, 9, 9, 22, 59, tzinfo=timezone.utc)
    after_ready = datetime(2026, 9, 9, 23, 0, tzinfo=timezone.utc)
    assert latest_completed_rolling_period(before_ready) == (
        date(2026, 9, 1), date(2026, 9, 8)
    )
    assert latest_completed_rolling_period(after_ready) == (
        date(2026, 9, 2), date(2026, 9, 9)
    )
    assert archive_ready_after(date(2026, 9, 9)) == after_ready
    assert utc_window_boundaries(date(2026, 9, 2)) == {
        "start": "2026-09-02",
        "end": "2026-09-09",
        "start_utc": "2026-09-02T00:00:00Z",
        "end_exclusive_utc": "2026-09-10T00:00:00Z",
        "archive_ready_after_utc": "2026-09-09T23:00:00Z",
    }


def test_rolling_window_seasonality_uses_the_notebook_formula_on_its_start_date():
    start = date(2026, 7, 3)
    angle = 2 * math.pi * start.timetuple().tm_yday / 365.25
    assert seasonality_features(start) == pytest.approx({
        "dayOfYearSin": math.sin(angle),
        "dayOfYearCos": math.cos(angle),
    })


def test_observation_memo_restores_serialized_nonfinite_reflectance():
    scan = Scan(
        "ABI-L2-BRFF/example.nc",
        datetime(2026, 8, 10, 21, tzinfo=timezone.utc),
        123,
        "etag",
        "last-modified",
    )
    memo = ObservationMemo()
    memo.seed_result({
        "provenance": {
            "requested_location": {"latitude": 39.1547, "longitude": -77.2405},
            "attempts": [{
                "scan": scan.key,
                "scan_time_utc": scan.started_at.isoformat(),
                "date": "2026-08-10",
                "target_hour": 21,
                "brf": {"2": None, "3": 0.2, "5": None},
                "retrieval": {"remote_object": {
                    "key": scan.key,
                    "size_bytes": scan.size,
                    "etag": scan.etag,
                    "last_modified": scan.last_modified,
                }},
            }],
        },
    })
    sample = memo.get_sample(39.1547, -77.2405, scan)
    assert sample is not None
    assert math.isnan(sample["brf"]["2"])
    assert sample["brf"]["3"] == 0.2
    assert math.isnan(sample["brf"]["5"])
    assert memo.get_scan(date(2026, 8, 10), 21) == scan


def test_dqf_bits_are_reported_not_hidden():
    assert decode_dqf(16) == {
        "overall_quality": 0,
        "retrieval_path": 2,
        "small_scattering_angle": False,
        "not_absolutely_clear": False,
        "invalid_aerosol_climatology": False,
    }
    assert passes_notebook_strict_quality(16)
    assert not passes_notebook_strict_quality(147)


def test_notebook_model_land_cover_categories_are_exact():
    assert IGBP_CATEGORIES == (1, 10, 11, 12, 13, 14, 16, 2, 3, 4, 5, 6, 7, 8, 9)


def test_missing_reference_assets_are_reported(tmp_path: Path):
    audit = ReferenceAssets(tmp_path).audit()
    assert audit["model"]["present"] is False
    assert audit["notebook"]["present"] is False
    assert audit["igbp"]["present"] is False
    assert audit["solar_geometry"]["present"] is False
    assert audit["navigation"]["present"] is False


def test_web_status_names_only_actual_missing_dependencies():
    response = create_app(clock=lambda: datetime(
        2026, 9, 10, 1, 0, tzinfo=timezone.utc
    )).test_client().get("/api/status")
    assert response.status_code == 200
    assert response.json["ready_for_estimate_execution"] is True
    assert response.json["ready_for_verified_inference"] is True
    assert response.json["verified"] == [
        "Authoritative notebook",
        "Checksum-verified 600-tree model",
        "Checksum-verified eight-year VIIRS IGBP grid",
        "Checksum-verified notebook solar-geometry helper",
        "Checksum-verified original GOES-East navigation raster",
    ]
    assert response.json["missing"] == []
    assert response.json["preprocessing_discrepancies"] == []
    assert response.json["operational_readiness"]["status"] == "ready"
    assert response.json["preprocessing_validation"]["status"] == "verified"
    assert response.json["historical_numerical_reproduction"] == {
        "completed": False,
        "detail": (
            "Reproduction of the original training-period results remains unverified because "
            "historical feature rows are unavailable."
        ),
        "required_for_runtime": False,
        "status": "unverified",
    }
    assert response.json["latest_window"]["start"] == "2026-09-02"
    assert response.json["latest_window"]["end"] == "2026-09-09"
    assert response.json["historical_date_range"] == {
        "minimum_end": "2026-04-14", "maximum_end": "2026-09-09",
    }
    assert response.json["history_scope"]["available_start"] == "2026-04-07"
    assert response.json["history_scope"]["available_first_end"] == "2026-04-14"
    assert response.json["history_scope"]["available_latest_start"] == "2026-09-02"
    assert response.json["history_scope"]["outside_scope"]["end"] == "2026-04-13"
    assert response.json["history_scope"]["mode"] == "monthly_snapshots"
    assert response.json["history_scope"]["window_count"] == 6
    assert response.json["history_scope"]["explanation"] == (
        "Each point summarizes eight days of satellite observations."
    )
    assert response.json["rolling_window_accuracy"]["status"] == "separately_unevaluated"
    assert response.headers["Cache-Control"] == "no-store, max-age=0"


def test_dependency_status_distinguishes_missing_invalid_and_verified(tmp_path: Path):
    status = dependency_status(ReferenceAssets(tmp_path).audit())
    assert not status["ready_for_estimate_execution"]
    assert not status["ready_for_verified_inference"]
    assert {item["key"] for item in status["missing"]} == {"notebook", "model", "igbp", "solar_geometry"}
    assert {item["key"] for item in status["preprocessing_discrepancies"]} == {"navigation_equivalence"}


def test_cached_example_separates_verified_preprocessing_from_historical_validation():
    response = create_app().test_client().get("/api/examples/montgomery-2026-04-07")
    if response.status_code == 200:
        assert response.json["status"] == "verified"
        assert response.json["limitations"] == []
        assert len(response.json["scientific_concerns"]) == 1
        assert response.json["readiness"]["preprocessing_validation"]["verified"] is True
        assert response.json["readiness"]["historical_numerical_reproduction"]["completed"] is False
        assert response.json["display_location"] == "Montgomery County, Maryland"
        assert response.json["label"] == "Research estimate"
        assert response.json["query"]["period"]["start"] == "2026-04-07"
        assert response.json["query"]["period"]["end"] == "2026-04-14"
        assert response.json["query"]["period"]["start_utc"] == "2026-04-07T00:00:00Z"
        assert response.json["query"]["period"]["end_exclusive_utc"] == "2026-04-15T00:00:00Z"
        assert response.json["observation_support"]["passed_by_hour"] == {
            "15": 4, "18": 5, "21": 6,
        }
        assert response.json["observation_support"]["possible_per_hour"] == 8
        assert response.json["observation_support"]["possible_total"] == 24
        assert response.json["observation_support"]["usable_days"] == 7
        assert response.json["observation_support"]["possible_days"] == 8
        assert response.json["sampled_pixel"]["center"] != response.json["query"]["location"]
        assert len(response.json["sampled_pixel"]["footprint"]) == 4
        assert response.json["sampled_pixel"]["row"] == 797
        assert response.json["sampled_pixel"]["column"] == 2619
        assert response.json["observation_support"]["passed_total"] == 15
        assert [item["date"] for item in response.json["observation_support"]["usable_dates"]] == [
            "2026-04-07", "2026-04-08", "2026-04-09", "2026-04-10",
            "2026-04-11", "2026-04-12", "2026-04-14",
        ]


def test_original_navigation_raster_matches_exact_cached_pixel():
    from lai_cac.goes import sample_pixel
    from lai_cac.navigation import read_navigation_pixel

    root = Path(__file__).parents[1]
    assets = ReferenceAssets(root / "artifacts/reference")
    scan = sorted((root / "data/cache").glob("*.nc"))[0]
    navigation = read_navigation_pixel(
        assets.require_navigation(), sample_pixel(scan, 39.1547, -77.2405), 39.1547, -77.2405
    )
    assert navigation["verified_for_exact_pixel"] is True
    assert navigation["array_orientation"] == "row_column"
    assert navigation["goes_row"] == 797
    assert navigation["goes_column"] == 2619
    assert navigation["raster_pixel"]["latitude"] == pytest.approx(39.1502685546875)
    assert navigation["raster_pixel"]["longitude"] == pytest.approx(-77.2435531616211)
    assert navigation["raster_pixel"]["local_zenith_angle"] == pytest.approx(45.385860443115234)
    assert navigation["raster_pixel"]["land_mask"] == 1.0
    assert navigation["feature_geometry"]["view_azimuth"] == pytest.approx(176.76275634765625)
    assert navigation["comparison"]["raster_minus_calculated_view_zenith_degrees"] == pytest.approx(
        0.04203033447265625
    )


def test_exact_igbp_grid_assigns_cached_example_class():
    from lai_cac.inference import read_igbp_class

    root = Path(__file__).parents[1]
    path = root / "artifacts/reference/S-NPP_VIIRS_GST_IGBP_8-Year_30arcsec.nc"
    if path.is_file():
        assert read_igbp_class(path, 39.1547, -77.2405) == 4


def test_exact_igbp_grid_rejects_unsupported_water_class():
    from lai_cac.inference import read_igbp_class

    root = Path(__file__).parents[1]
    path = root / "artifacts/reference/S-NPP_VIIRS_GST_IGBP_8-Year_30arcsec.nc"
    with pytest.raises(ValueError, match="Unsupported IGBP class 17"):
        read_igbp_class(path, 38.414, -76.096)


def test_goes_coverage_check_rejects_a_point_outside_the_visible_disk():
    import h5py
    from lai_cac.goes import fixed_grid_xy

    root = Path(__file__).parents[1]
    scan = sorted((root / "data/cache").glob("*.nc"))[0]
    with h5py.File(scan, "r") as source:
        with pytest.raises(ValueError, match="outside the GOES-19 visible disk"):
            fixed_grid_xy(0.0, 120.0, source["goes_imager_projection"])


def test_checksum_verified_solar_helper_import_skips_demo_and_uses_notebook_conversions():
    from datetime import datetime, timezone

    root = Path(__file__).parents[1]
    path = root / "artifacts/reference/geometry_goes19.py"
    zenith, azimuth = solar_angles(
        39.150266,
        -77.243545,
        datetime(2026, 4, 7, 15, 5, 6, tzinfo=timezone.utc),
        path,
    )
    assert zenith == pytest.approx(42.842487, abs=1e-5)
    assert azimuth == pytest.approx(49.675236, abs=1e-5)


def test_ui_uses_location_first_latest_estimate_and_query_bound_progressive_history():
    root = Path(__file__).parents[1]
    source = (root / "static/app.js").read_text(encoding="utf-8")
    template = (root / "templates/index.html").read_text(encoding="utf-8")
    assert 'Number(record.lai).toFixed(2)' in source
    assert "1.29" not in source
    assert 'id="period"' not in template
    assert "latestPeriod.start" in source
    assert "Latest observation period:" in source
    assert "formatObservationPeriod" in source
    assert 'byId("methods-concerns")' in source
    assert "preprocessing_discrepancies" in source
    assert 'fetch("/api/estimate"' in source
    assert "queryMatchesRecord" in source
    assert "resetForLocation" in source
    assert "requestGeneration" in source
    assert 'fetch(`/api/jobs/${current.job_id}`' in source
    assert 'byId("observation-dates")' in source
    assert "partial-cache reuses" in source
    assert 'left.state === "available" && right.state === "available"' in source
    assert 'detectRetina: true' in source
    assert 'new ResizeObserver(resizeMap)' in source
    assert '"Ready to estimate"' in source
    assert "observations usable, covering" in source
    assert '"LAI (m²/m²)"' in source
    assert "formatTrendPeriod(item.period)" in source
    assert 'fetch("/api/history"' in source
    assert 'fetch(`/api/history/${history.job_id}`' in source
    assert 'fetch("/api/trends/montgomery"' not in source
    assert "historyResponseMatchesQuery" in source
    assert "generation !== historyGeneration" in source
    assert "scope.label" in source
    assert "item.snapshot.is_latest" in source
    assert "historyObservationDates" in source
    assert "clearHistory" in source
    assert "Calculated using cached satellite observations." not in source
    assert "No differences from the supplied preprocessing were identified for this query." in source
    assert "The original helper calculates solar direction differently" in source
    assert "const ordered = [corners[0], corners[1], corners[3], corners[2]]" in source
    assert 'id="map-tile-message"' in template
    assert 'id="calculation-source"' in template
    assert 'id="calculated-at"' in template
    assert 'id="latest-window-details"' in template
    assert 'id="processing-diagnostics"' in template
    assert 'id="observation-details"' in template
    assert 'id="pixel-details"' in template
    assert "April 6, 2026 training cutoff" in template
    assert "preprocessing compatibility, not prediction accuracy" in template
    assert "not a direct plant-health score" in template
    assert "An eight-day estimate updated daily." in template
    assert "Explore the leaves in your landscape" in template
    assert "What is leaf area index?" in template
    assert "3 square meters of leaves for every square meter of ground" in template
    assert "Choose a landscape to explore" in template
    assert "Latest estimated leaf area" in template
    assert "Observation availability" in template
    assert "LeafView selects the observation period automatically." in template
    assert "Research estimates: prediction accuracy for the rolling eight-day windows has not yet been evaluated." in template
    assert "Choose a location below to begin." in template
    assert "View an older eight-day window" in template
    assert "History at selected location" in template
    assert "Monthly snapshots" in template
    assert "Each point summarizes eight days of satellite observations." in template
    assert "Network or retrieval error" in template
    assert "Network or retrieval error" in source
    assert "Prediction accuracy for rolling eight-day windows has not been evaluated." in template
    assert "Calculation time unavailable." in source
    assert 'showEstimateState("Estimate ready")' in source
    assert "Binding this result to the selected location and UTC dates" not in source
    assert template.count('id="result-badge"') == 1


def test_result_support_and_calculation_copy_are_derived_from_metadata():
    root = Path(__file__).parents[1]
    result = deepcopy(json.loads(
        (root / "data/results/montgomery-md-2026-04-07.json").read_text()
    ))
    result["provenance"]["composite"] = {
        "start": "2026-07-04", "end": "2026-07-11",
    }
    result["provenance"]["usable_counts"] = {"15": 2, "18": 2, "21": 1}
    result["provenance"]["processing"]["calculated_at_utc"] = (
        "2026-09-10T17:42:31.123456Z"
    )
    result["provenance"]["attempts"] = [
        {"strict": True, "date": "2026-07-04", "target_hour": hour, "scan": f"scan-04-{hour}"}
        for hour in (15, 18, 21)
    ] + [
        {"strict": True, "date": "2026-07-05", "target_hour": hour, "scan": f"scan-05-{hour}"}
        for hour in (15, 18)
    ]
    delivery = {
        "cache_hit": False,
        "request_seconds": 1.379,
        "observation_cache": {
            "reused": 0, "downloaded": 0,
            "partial_remote": 0, "partial_reused": 23,
        },
    }
    displayed = public_result(result, "custom", "Custom location", delivery)
    assert displayed["observation_support"] == {
        "passed_by_hour": {"15": 2, "18": 2, "21": 1},
        "passed_total": 5,
        "possible_per_hour": 8,
        "possible_total": 24,
        "usable_days": 2,
        "possible_days": 8,
        "usable_dates": [
            {"date": "2026-07-04", "count": 3, "hours_utc": [15, 18, 21]},
            {"date": "2026-07-05", "count": 2, "hours_utc": [15, 18]},
        ],
        "observation_dates": [
            {
                "date": "2026-07-04", "observations": 3, "usable": 3,
                "hours_utc": [15, 18, 21], "usable_hours_utc": [15, 18, 21],
            },
            {
                "date": "2026-07-05", "observations": 2, "usable": 2,
                "hours_utc": [15, 18], "usable_hours_utc": [15, 18],
            },
        ],
    }
    assert displayed["delivery"]["calculation"] == {
        "kind": "new_result",
        "summary": "New estimate calculated.",
    }
    assert displayed["calculated_at_utc"] == "2026-09-10T17:42:31.123456Z"

    loaded = public_result(result, "custom", "Custom location", {
        **delivery, "cache_hit": True,
    })
    assert loaded["delivery"]["calculation"]["kind"] == "saved_result"
    assert loaded["delivery"]["calculation"]["summary"] == (
        "Showing a saved estimate for this location and period."
    )
    assert loaded["calculated_at_utc"] == displayed["calculated_at_utc"]

    downloaded = public_result(result, "custom", "Custom location", {
        **delivery,
        "observation_cache": {
            **delivery["observation_cache"], "partial_remote": 1,
        },
    })
    assert downloaded["delivery"]["calculation"]["kind"] == "new_result"


def test_live_estimate_endpoint_validates_payload_without_running_pipeline():
    response = create_app().test_client().post("/api/estimate", json={"latitude": 39.0})
    assert response.status_code == 400


def test_live_estimate_reuses_matching_cached_result():
    response = create_app().test_client().post("/api/estimate", json={
        "latitude": 39.1547,
        "longitude": -77.2405,
        "start": "2026-04-07",
        "display_location": "Montgomery County, Maryland",
    })
    assert response.status_code == 200
    assert response.json["state"] == "complete"
    assert response.json["progress"]["detail"] == (
        "Showing a saved estimate for this location and period."
    )
    assert response.json["result"]["delivery"]["cache_hit"] is True
    assert response.json["result"]["delivery"]["calculation"] == {
        "kind": "saved_result",
        "summary": "Showing a saved estimate for this location and period.",
    }
    assert response.json["result"]["calculated_at_utc"] is None
    assert response.json["result"]["query"]["period"]["start"] == "2026-04-07"
    assert response.json["result"]["lai"] == pytest.approx(1.20430588722229)


def test_result_cache_rejects_an_explicit_land_cover_override():
    from lai_cac.result_cache import cache_identity, result_matches_identity

    root = Path(__file__).parents[1]
    identity = cache_identity(39.1547, -77.2405, date(2026, 4, 7), root)
    path = root / "data/results/montgomery-md-2026-04-07.json"
    result = json.loads(path.read_text())
    result["provenance"]["igbp"]["source"] = "explicit_argument"
    assert result_matches_identity(result, identity) is False


def test_cached_results_are_query_bound_across_period_changes():
    client = create_app().test_client()
    first = client.post("/api/estimate", json={
        "latitude": 39.1547, "longitude": -77.2405, "start": "2026-04-07"
    })
    second = client.post("/api/estimate", json={
        "latitude": 39.1547, "longitude": -77.2405, "start": "2026-04-15"
    })
    assert first.status_code == second.status_code == 200
    assert first.json["query_key"] != second.json["query_key"]
    assert first.json["result"]["query"]["period"]["start"] == "2026-04-07"
    assert second.json["result"]["query"]["period"]["start"] == "2026-04-15"
    assert second.json["result"]["lai"] == pytest.approx(2.283097743988037)


def test_real_trend_has_three_consecutive_query_bound_periods():
    response = create_app().test_client().get("/api/trends/montgomery")
    assert response.status_code == 200
    assert response.json["demo"] is True
    assert [item["period"]["start"] for item in response.json["periods"]] == [
        "2026-04-07", "2026-04-15", "2026-04-23"
    ]
    assert [item["lai"] for item in response.json["periods"]] == pytest.approx([
        1.20430588722229, 2.283097743988037, 2.62589955329895
    ])
    assert all(item["gap"] is False for item in response.json["periods"])


def test_history_schedules_only_month_end_snapshots_and_latest_newest_first(
    tmp_path: Path, monkeypatch
):
    import lai_cac.web as web

    requested = (39.165474, -77.325871)
    calls = []

    def fake_identity(latitude, longitude, period_start, root):
        assert (latitude, longitude) == requested
        return {"key": f"{latitude:.6f}:{longitude:.6f}:{period_start.isoformat()}"}

    class FakeEstimateJobs:
        def start(self, latitude, longitude, period_start, display_location):
            calls.append(period_start)
            identity = fake_identity(latitude, longitude, period_start, tmp_path)
            return {
                "job_id": period_start.isoformat(),
                "state": "complete",
                "result": {
                    "status": "verified",
                    "lai": float(period_start.day),
                    "provenance": {
                        "composite": {
                            "start": period_start.isoformat(),
                            "end": (period_start + timedelta(days=7)).isoformat(),
                        },
                        "usable_counts": {"15": 1, "18": 1, "21": 1},
                        "attempts": [],
                    },
                },
            }

        def snapshot(self, job_id):
            raise AssertionError("Completed fake jobs must not be polled")

    monkeypatch.setattr(web, "cache_identity", fake_identity)
    monkeypatch.setattr(web, "load_compatible_result", lambda *args: None)
    jobs = HistoryJobs(tmp_path, ObservationMemo(), FakeEstimateJobs())
    history = jobs.start(
        requested[0], requested[1], date(2026, 9, 2), "Custom location"
    )
    for _ in range(100):
        history = jobs.snapshot(history["job_id"])
        if history["state"] == "complete":
            break
        time.sleep(0.01)
    assert history["state"] == "complete"
    assert history["query"]["location"]["latitude"] == requested[0]
    assert [item["period"]["start"] for item in history["periods"]] == [
        "2026-04-23", "2026-05-24", "2026-06-23",
        "2026-07-24", "2026-08-24", "2026-09-02",
    ]
    assert all(item["state"] == "available" for item in history["periods"])
    assert [item["lai"] for item in history["periods"]] == [
        23.0, 24.0, 23.0, 24.0, 24.0, 2.0,
    ]
    assert [item["snapshot"]["label"] for item in history["periods"]] == [
        "April 2026", "May 2026", "June 2026",
        "July 2026", "August 2026", "September 2026 · latest",
    ]
    assert calls == [
        date(2026, 9, 2), date(2026, 8, 24), date(2026, 7, 24),
        date(2026, 6, 23), date(2026, 5, 24), date(2026, 4, 23),
    ]


def test_history_endpoint_uses_the_latest_query_and_existing_cached_result():
    clock = lambda: datetime(2026, 4, 14, 23, 0, tzinfo=timezone.utc)
    response = create_app(clock=clock).test_client().post("/api/history", json={
        "latitude": 39.1547,
        "longitude": -77.2405,
        "display_location": "Montgomery County, Maryland",
    })
    assert response.status_code == 200
    assert response.json["state"] == "complete"
    assert response.json["query"]["latest_period"]["start"] == "2026-04-07"
    assert len(response.json["periods"]) == 1
    assert response.json["periods"][0]["state"] == "available"
    assert response.json["periods"][0]["lai"] == pytest.approx(1.20430588722229)


def test_incomplete_period_is_rejected():
    clock = lambda: datetime(2026, 9, 9, 22, 59, tzinfo=timezone.utc)
    response = create_app(clock=clock).test_client().post("/api/estimate", json={
        "latitude": 39.1547,
        "longitude": -77.2405,
        "start": "2026-09-02",
    })
    assert response.status_code == 400
    assert "archive publication delay" in response.json["error"]


def test_insufficient_data_returns_a_gap_without_calling_model(tmp_path: Path, monkeypatch):
    import lai_cac.inference as inference

    features = {"placeholder": None}
    provenance = {
        "requested_location": {"latitude": 39.0, "longitude": -77.0},
        "sampled_pixel_center": {"latitude": 39.0, "longitude": -77.0},
        "composite": {"start": "2026-04-07", "end": "2026-04-14"},
        "igbp": {"class": 4, "source": "S-NPP_VIIRS_GST_IGBP_8-Year_30arcsec.nc"},
        "navigation": {"verified_for_exact_pixel": True},
        "attempts": [],
        "usable_counts": {"15": 0, "18": 0, "21": 0},
        "observation_cache": {"reused": 24, "downloaded": 0, "total_selected": 24},
    }
    monkeypatch.setattr(inference, "prepare_composite", lambda *args, **kwargs: (features, provenance))
    monkeypatch.setattr(
        inference,
        "calculation_timestamp_utc",
        lambda: "2026-09-10T18:04:05.123456Z",
    )
    output = tmp_path / "gap.json"
    result = inference.run_estimate(
        39.0, -77.0, date(2026, 4, 7), None, tmp_path, output,
        identity={"key": "gap"},
    )
    assert result["status"] == "insufficient_data"
    assert result["lai"] is None
    assert result["provenance"]["processing"]["calculated_at_utc"] == (
        "2026-09-10T18:04:05.123456Z"
    )
    assert "shown as a gap" in result["data_message"]
    stored = json.loads(output.read_text())
    assert stored["lai"] is None
    assert stored["provenance"]["processing"]["calculated_at_utc"] == (
        "2026-09-10T18:04:05.123456Z"
    )


def test_nonfinite_rejected_observation_exports_as_null_without_changing_internal_values(
    tmp_path: Path, monkeypatch
):
    import lai_cac.inference as inference

    root = Path(__file__).parents[1]
    baseline = json.loads(
        (root / "data/results/montgomery-md-2026-04-07.json").read_text()
    )
    features = baseline["features"]
    provenance = baseline["provenance"]
    rejected = {
        **provenance["attempts"][0],
        "strict": False,
        "rejection_reasons": ["dqf_not_notebook_strict", "non_finite_brf_bands:2,3,5"],
        "brf": {"2": math.nan, "3": math.nan, "5": math.nan},
    }
    provenance["attempts"] = [rejected, provenance["attempts"][1]]
    provenance["usable_counts"] = {"15": 1, "18": 0, "21": 0}
    monkeypatch.setattr(
        inference, "prepare_composite", lambda *args, **kwargs: (features, provenance)
    )

    output = tmp_path / "result.json"
    result = inference.run_estimate(
        39.1547, -77.2405, date(2026, 4, 7), None, root, output,
        identity={"key": "nonfinite-regression"},
    )
    assert math.isnan(result["provenance"]["attempts"][0]["brf"]["2"])

    stored = json.loads(output.read_text())
    stored_rejected = stored["provenance"]["attempts"][0]
    assert stored_rejected["brf"] == {"2": None, "3": None, "5": None}
    assert stored_rejected["rejection_reasons"] == [
        "dqf_not_notebook_strict", "non_finite_brf_bands:2,3,5"
    ]

    app = create_app(root)
    app.add_url_rule("/_strict-json-regression", view_func=lambda: jsonify(result))
    response = app.test_client().get("/_strict-json-regression")
    assert response.status_code == 200
    assert b"NaN" not in response.data
    assert response.json["provenance"]["attempts"][0]["brf"]["2"] is None


def test_background_jobs_publish_progress_and_complete(tmp_path: Path, monkeypatch):
    import lai_cac.web as web

    identity = {"key": "test-key"}
    monkeypatch.setattr(web, "cache_identity", lambda *args, **kwargs: identity)
    monkeypatch.setattr(web, "load_compatible_result", lambda *args, **kwargs: None)

    def fake_run(*args, progress, **kwargs):
        progress({"percent": 50, "stage": "observations", "detail": "Processed 12 of 24"})
        return {
            "status": "verified", "lai": 1.5, "units": "unit", "limitations": [],
            "readiness": {}, "scientific_concerns": [],
            "provenance": {
                "requested_location": {"latitude": 39.0, "longitude": -77.0},
                "sampled_pixel_center": {"latitude": 39.0, "longitude": -77.0},
                "composite": {"start": "2026-04-07", "end": "2026-04-14"},
                "cache_identity": identity,
                "navigation": {"goes_row": 1, "goes_column": 2},
                "attempts": [], "usable_counts": {"15": 1, "18": 0, "21": 0},
                "observation_cache": {"reused": 1, "downloaded": 0, "total_selected": 1},
            },
        }

    monkeypatch.setattr(web, "run_estimate", fake_run)
    jobs = EstimateJobs(tmp_path)
    started = jobs.start(39.0, -77.0, date(2026, 4, 7), "Test location")
    for _ in range(100):
        completed = jobs.snapshot(started["job_id"])
        if completed["state"] != "running":
            break
        time.sleep(0.01)
    assert completed["state"] == "complete"
    assert completed["progress"]["percent"] == 100
    assert completed["result"]["delivery"]["cache_hit"] is False
