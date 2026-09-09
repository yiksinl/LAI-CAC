from datetime import date

import pytest

from lai_cac.composites import aligned_period
from pathlib import Path

from lai_cac.assets import ReferenceAssets
from lai_cac.geometry import solar_angles
from lai_cac.goes import decode_dqf, passes_notebook_strict_quality
from lai_cac.inference import IGBP_CATEGORIES
from lai_cac.web import create_app, dependency_status


def test_first_post_cutoff_period_is_aligned():
    assert aligned_period(date(2026, 4, 7)) == (date(2026, 4, 7), date(2026, 4, 14))


def test_off_calendar_period_is_rejected():
    with pytest.raises(ValueError):
        aligned_period(date(2026, 4, 8))


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
        assert response.json["query"]["period"] == {"start": "2026-04-07", "end": "2026-04-14"}
        assert response.json["observation_support"] == {
            "passed_by_hour": {"15": 4, "18": 5, "21": 6},
            "possible_per_hour": 8,
        }
        assert response.json["sampled_pixel"]["center"] != response.json["query"]["location"]
        assert len(response.json["sampled_pixel"]["footprint"]) == 4


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
    source = (Path(__file__).parents[1] / "static/app.js").read_text(encoding="utf-8")
    assert 'record.lai.toFixed(2)' in source
    assert "1.29" not in source
    assert 'byId("period").value = period.start' in source
    assert 'byId("methods-concerns")' in source
    assert "preprocessing_discrepancies" in source
    assert 'fetch("/api/estimate"' in source


def test_live_estimate_endpoint_validates_payload_without_running_pipeline():
    response = create_app().test_client().post("/api/estimate", json={"latitude": 39.0})
    assert response.status_code == 400
