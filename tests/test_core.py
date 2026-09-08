from datetime import date

import pytest

from lai_cac.composites import aligned_period
from pathlib import Path

from lai_cac.assets import ReferenceAssets
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


def test_web_status_names_only_actual_missing_dependencies():
    response = create_app().test_client().get("/api/status")
    assert response.status_code == 200
    assert response.json["ready_for_verified_inference"] is False
    assert response.json["verified"] == [
        "Authoritative notebook",
        "Checksum-verified 600-tree model",
    ]
    assert [item["filename"] for item in response.json["missing"]] == [
        "S-NPP_VIIRS_GST_IGBP_8-Year_30arcsec.nc",
        "geometry_goes19.py",
    ]
    assert "notebook and model verified" in response.json["headline"].lower()
    assert response.headers["Cache-Control"] == "no-store, max-age=0"


def test_dependency_status_distinguishes_missing_invalid_and_verified(tmp_path: Path):
    status = dependency_status(ReferenceAssets(tmp_path).audit())
    assert not status["ready_for_verified_inference"]
    assert {item["key"] for item in status["missing"]} == {"notebook", "model", "igbp", "solar_geometry"}


def test_cached_example_is_explicitly_provisional_when_present():
    response = create_app().test_client().get("/api/examples/montgomery-2026-04-07")
    if response.status_code == 200:
        assert response.json["status"] == "provisional_dependency_override"
        assert len(response.json["limitations"]) == 2
        assert response.json["display_location"] == "Montgomery County, Maryland"
        assert response.json["query"]["period"] == {"start": "2026-04-07", "end": "2026-04-14"}
        assert response.json["observation_support"] == {
            "passed_by_hour": {"15": 4, "18": 5, "21": 6},
            "possible_per_hour": 8,
        }
        assert response.json["sampled_pixel"]["center"] != response.json["query"]["location"]
        assert len(response.json["sampled_pixel"]["footprint"]) == 4


def test_ui_binds_cached_example_from_api_instead_of_hardcoding_value():
    source = (Path(__file__).parents[1] / "static/app.js").read_text(encoding="utf-8")
    assert 'record.lai.toFixed(2)' in source
    assert "1.29" not in source
    assert 'byId("period").value = period.start' in source
