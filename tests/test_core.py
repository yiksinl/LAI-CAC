from datetime import date, timedelta
import json
import math
import time

import pytest
from flask import jsonify

from lai_cac.composites import aligned_period
from pathlib import Path

from lai_cac.assets import ReferenceAssets
from lai_cac.geometry import solar_angles
from lai_cac.goes import decode_dqf, passes_notebook_strict_quality
from lai_cac.inference import IGBP_CATEGORIES
from lai_cac.web import EstimateJobs, create_app, dependency_status


def test_first_post_cutoff_period_is_aligned():
    assert aligned_period(date(2026, 4, 7)) == (date(2026, 4, 7), date(2026, 4, 14))


def test_off_calendar_period_is_rejected():
    with pytest.raises(ValueError):
        aligned_period(date(2026, 4, 8))


def test_training_period_is_rejected():
    with pytest.raises(ValueError, match="not after the model-development cutoff"):
        aligned_period(date(2026, 3, 30))


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
    response = create_app().test_client().get("/api/status")
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
        "detail": "Historical feature rows are validation evidence, not runtime inputs.",
        "required_for_runtime": False,
        "status": "unverified",
    }
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
        assert response.json["query"]["period"] == {"start": "2026-04-07", "end": "2026-04-14"}
        assert response.json["observation_support"]["passed_by_hour"] == {
            "15": 4, "18": 5, "21": 6,
        }
        assert response.json["observation_support"]["possible_per_hour"] == 8
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


def test_ui_binds_cached_example_from_api_instead_of_hardcoding_value():
    root = Path(__file__).parents[1]
    source = (root / "static/app.js").read_text(encoding="utf-8")
    template = (root / "templates/index.html").read_text(encoding="utf-8")
    assert 'record.lai.toFixed(2)' in source
    assert "1.29" not in source
    assert 'byId("period").value = period.start' in source
    assert 'byId("methods-concerns")' in source
    assert "preprocessing_discrepancies" in source
    assert 'fetch("/api/estimate"' in source
    assert "queryMatchesRecord" in source
    assert "resetResultForQuery" in source
    assert "requestGeneration" in source
    assert 'fetch(`/api/jobs/${current.job_id}`' in source
    assert 'byId("observation-dates")' in source
    assert "partial-cache reused" in source
    assert "left.lai != null && right.lai != null" in source
    assert 'detectRetina: true' in source
    assert 'new ResizeObserver(resizeMap)' in source
    assert '"Ready to estimate"' in source
    assert "observations usable" in source
    assert '"LAI (m²/m²)"' in source
    assert "formatTrendPeriod(item.period)" in source
    assert "`${item.usable_total}/24`" in source
    assert 'id="map-tile-message"' in template
    assert 'id="processing-diagnostics"' in template
    assert 'id="observation-details"' in template
    assert 'id="pixel-details"' in template
    assert template.count('id="result-badge"') == 1


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
    assert response.json["result"]["delivery"]["cache_hit"] is True
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
    assert [item["period"]["start"] for item in response.json["periods"]] == [
        "2026-04-07", "2026-04-15", "2026-04-23"
    ]
    assert [item["lai"] for item in response.json["periods"]] == pytest.approx([
        1.20430588722229, 2.283097743988037, 2.62589955329895
    ])
    assert all(item["gap"] is False for item in response.json["periods"])


def test_incomplete_period_is_rejected():
    latest_start = date.today() - timedelta(days=(date.today() - date(2026, 4, 7)).days % 8)
    response = create_app().test_client().post("/api/estimate", json={
        "latitude": 39.1547,
        "longitude": -77.2405,
        "start": latest_start.isoformat(),
    })
    assert response.status_code == 400
    assert "fully completed" in response.json["error"]


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
    output = tmp_path / "gap.json"
    result = inference.run_estimate(
        39.0, -77.0, date(2026, 4, 7), None, tmp_path, output,
        identity={"key": "gap"},
    )
    assert result["status"] == "insufficient_data"
    assert result["lai"] is None
    assert "shown as a gap" in result["data_message"]
    assert json.loads(output.read_text())["lai"] is None


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
