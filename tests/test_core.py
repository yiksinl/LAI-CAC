from datetime import date

import pytest

from lai_cac.composites import aligned_period
from pathlib import Path

from lai_cac.assets import ReferenceAssets
from lai_cac.goes import decode_dqf
from lai_cac.web import create_app


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


def test_missing_reference_assets_are_reported(tmp_path: Path):
    audit = ReferenceAssets(tmp_path).audit()
    assert audit["model"]["present"] is False
    assert audit["notebook"]["present"] is False


def test_web_status_never_presents_lai_without_assets():
    response = create_app().test_client().get("/api/status")
    assert response.status_code == 200
    assert response.json["ready_for_verified_inference"] is False
    assert "unavailable" in response.json["message"].lower()
