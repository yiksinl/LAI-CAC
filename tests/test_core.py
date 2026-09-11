from datetime import date, datetime, timedelta, timezone
from copy import deepcopy
import json
import math
import threading
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
    NominatimPlaceLookup,
    PlaceLookupError,
    create_app,
    dependency_status,
    place_name_from_nominatim,
    places_from_nominatim_search,
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


def test_place_name_parser_uses_only_response_backed_us_area_county_and_state():
    place = place_name_from_nominatim({
        "display_name": "A full address that must not be copied",
        "address": {
            "neighbourhood": "Woodside Park",
            "town": "Silver Spring",
            "county": "Montgomery County",
            "state": "Maryland",
            "province": "Unsupported province text",
            "country": "United States",
            "country_code": "us",
        },
    })
    assert place == {
        "display_name": "Woodside Park, Maryland",
        "county": "Montgomery County",
    }
    assert "country" not in place
    assert "province" not in place


def test_place_name_parser_falls_back_to_county_and_state_without_inference():
    assert place_name_from_nominatim({
        "address": {
            "road": "Unsupported as an area name",
            "county": "Garrett County",
            "state": "Maryland",
            "country_code": "us",
        },
    }) == {
        "display_name": "Garrett County, Maryland",
        "county": "Garrett County",
    }
    assert place_name_from_nominatim({
        "address": {
            "city": "Toronto",
            "state": "Ontario",
            "country_code": "ca",
        },
    }) is None
    assert place_name_from_nominatim({
        "address": {"county": "Unknown County", "country_code": "us"},
    }) is None


def test_nominatim_lookup_uses_exact_requested_coordinates_and_caches_response():
    class Response:
        status = 200
        data = json.dumps({
            "address": {
                "town": "Poolesville",
                "county": "Montgomery County",
                "state": "Maryland",
                "country_code": "us",
            },
        }).encode()

    class Pool:
        def __init__(self):
            self.calls = []

        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            return Response()

    pool = Pool()
    lookup = NominatimPlaceLookup(pool=pool, minimum_interval_seconds=0)
    expected = {
        "display_name": "Poolesville, Maryland",
        "county": "Montgomery County",
    }
    assert lookup(39.1234567, -77.2345678) == expected
    assert lookup(39.1234567, -77.2345678) == expected
    assert len(pool.calls) == 1
    method, url, options = pool.calls[0]
    assert method == "GET"
    assert url.endswith("/reverse")
    assert options["fields"]["lat"] == "39.123457"
    assert options["fields"]["lon"] == "-77.234568"
    assert options["fields"]["layer"] == "address"
    assert options["headers"]["User-Agent"].startswith("LeafView/")
    assert options["retries"] is False

    times = iter((10.0, 10.25, 11.0))
    sleeps = []
    throttled = NominatimPlaceLookup(
        pool=pool,
        minimum_interval_seconds=1.0,
        monotonic=lambda: next(times),
        sleeper=sleeps.append,
    )
    throttled(39.0, -77.0)
    throttled(39.1, -77.1)
    assert sleeps == [pytest.approx(0.75)]


def test_nominatim_search_parser_keeps_us_places_addresses_and_counties_clear():
    results = places_from_nominatim_search([
        {
            "lat": "39.114268",
            "lon": "-77.106981",
            "name": "Rock Creek Regional Park",
            "namedetails": {"name": "Rock Creek Regional Park"},
            "display_name": "Text including a country that must not be copied",
            "address": {
                "suburb": "Derwood",
                "county": "Montgomery County",
                "state": "Maryland",
                "province": "Unsupported province",
                "country": "United States",
                "country_code": "us",
            },
        },
        {
            "lat": "39.084100",
            "lon": "-77.152800",
            "address": {
                "house_number": "10",
                "road": "Main Street",
                "city": "Rockville",
                "county": "Montgomery County",
                "state": "Maryland",
                "country_code": "us",
            },
        },
        {
            "lat": "43.6532",
            "lon": "-79.3832",
            "name": "Toronto",
            "address": {
                "city": "Toronto", "state": "Ontario", "country_code": "ca"
            },
        },
    ])
    assert results == [
        {
            "display_name": "Rock Creek Regional Park, Derwood, Maryland",
            "county": "Montgomery County",
            "latitude": 39.114268,
            "longitude": -77.106981,
        },
        {
            "display_name": "10 Main Street, Rockville, Maryland",
            "county": "Montgomery County",
            "latitude": 39.0841,
            "longitude": -77.1528,
        },
    ]
    assert all("country" not in item and "province" not in item for item in results)


def test_nominatim_forward_search_is_us_restricted_and_cached():
    class Response:
        status = 200
        data = json.dumps([{
            "lat": "39.083997",
            "lon": "-77.152758",
            "name": "Rockville",
            "address": {
                "city": "Rockville",
                "county": "Montgomery County",
                "state": "Maryland",
                "country_code": "us",
            },
        }]).encode()

    class Pool:
        def __init__(self):
            self.calls = []

        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            return Response()

    pool = Pool()
    lookup = NominatimPlaceLookup(pool=pool, minimum_interval_seconds=0)
    first = lookup.search(" Rockville ")
    second = lookup.search("rockville")
    assert first == second
    assert len(pool.calls) == 1
    method, url, options = pool.calls[0]
    assert method == "GET"
    assert url.endswith("/search")
    assert options["fields"]["q"] == "Rockville"
    assert options["fields"]["countrycodes"] == "us"
    assert options["fields"]["addressdetails"] == "1"
    assert options["fields"]["limit"] == "8"


def test_place_name_endpoint_binds_response_to_clicked_coordinates_and_handles_failure():
    calls = []

    def lookup(latitude, longitude):
        calls.append((latitude, longitude))
        return {
            "display_name": "Germantown, Maryland",
            "county": "Montgomery County",
        }

    client = create_app(place_lookup=lookup).test_client()
    response = client.post("/api/place-name", json={
        "latitude": 39.1734567,
        "longitude": -77.2712345,
    })
    assert response.status_code == 200
    assert calls == [(39.173457, -77.271235)]
    assert response.json == {
        "display_name": "Germantown, Maryland",
        "county": "Montgomery County",
        "requested_location": {
            "latitude": 39.173457,
            "longitude": -77.271235,
        },
        "attribution": "© OpenStreetMap contributors",
    }

    def failed_lookup(_latitude, _longitude):
        raise PlaceLookupError("network unavailable")

    failed = create_app(place_lookup=failed_lookup).test_client().post(
        "/api/place-name",
        json={"latitude": 39.1, "longitude": -77.2},
    )
    assert failed.status_code == 502
    assert failed.json == {"error": "Place name lookup unavailable"}


def test_place_search_endpoint_returns_bound_us_suggestions_and_no_match_list():
    calls = []

    def search(query):
        calls.append(query)
        if query == "nowhere nearby":
            return []
        return [{
            "display_name": "Rockville, Maryland",
            "county": "Montgomery County",
            "latitude": 39.083997,
            "longitude": -77.152758,
        }]

    client = create_app(place_search=search).test_client()
    response = client.get("/api/place-search?q=%20Rockville%20")
    assert response.status_code == 200
    assert response.json == {
        "query": "Rockville",
        "results": [{
            "display_name": "Rockville, Maryland",
            "county": "Montgomery County",
            "latitude": 39.083997,
            "longitude": -77.152758,
        }],
        "attribution": "© OpenStreetMap contributors",
    }
    no_match = client.get("/api/place-search?q=nowhere%20nearby")
    assert no_match.status_code == 200
    assert no_match.json["results"] == []
    assert calls == ["Rockville", "nowhere nearby"]
    assert client.get("/api/place-search?q=x").status_code == 400


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
    styles = (root / "static/app.css").read_text(encoding="utf-8")
    template = (root / "templates/index.html").read_text(encoding="utf-8")
    assert 'Number(record.lai).toFixed(2)' in source
    assert "1.29" not in source
    assert 'id="period"' not in template
    assert 'id="load-example"' not in template
    assert "Use Montgomery example" not in template
    assert 'byId("load-example")' not in source
    assert 'id="estimate"' not in template
    assert "Get latest estimate" not in template
    assert 'byId("estimate")' not in source
    assert "Choose a location on the map to automatically load its" in template
    assert 'id="location-search"' in template
    assert 'placeholder="Search a U.S. place, address, or coordinates"' in template
    assert 'id="location-search-suggestions"' in template
    assert 'fetch(`/api/place-search?q=${encodeURIComponent(query)}`' in source
    assert "coordinateSearch(query)" in source
    assert "Typing does not start an estimate." in source
    assert "No matching place found. Try a nearby town or enter coordinates." in source
    assert "normalizedSearchText(input.value) !== query" in source
    assert "searchController.abort()" in source
    assert "chooseSearchSuggestion" in source
    assert "selectLocation(coordinates, true, true)" in source
    assert 'placeNameSource: "OpenStreetMap"' in source
    assert 'id="selected-location-details"' in template
    assert 'id="result-location-details"' in template
    assert "Place names © OpenStreetMap contributors" in template
    assert 'fetch("/api/place-name"' in source
    assert "placeNameGeneration" in source
    assert "generation !== placeNameGeneration" in source
    assert "!sameLocation(selectedQuery, expected)" in source
    assert "place.requested_location" in source
    assert "latitude: expected.latitude" in source
    assert "longitude: expected.longitude" in source
    assert "placeNameController.abort()" in source
    assert "Place name unavailable" in source
    assert "record.sampled_pixel.center" not in source[source.index("async function resolvePlaceName"):source.index("function schedulePlaceNameLookup")]
    assert 'id="retry-estimate"' in template
    assert 'id="retry-history"' in template
    assert "estimateRetryAction" in source
    assert 'byId("retry-history").hidden = false' in source
    assert "latestPeriod.start" in source
    assert "Latest observation period:" in source
    assert "formatObservationPeriod" in source
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
    assert "expected observations were usable" in source
    assert '"LAI (m²/m²)"' in source
    assert "formatTrendPeriod(item.period)" in source
    assert 'fetch("/api/history"' in source
    assert 'fetch(`/api/history/${history.job_id}`' in source
    assert 'fetch(`/api/history/${jobId}/cancel`' in source
    assert "latest_query_key: activeRecord?.query?.key" in source
    assert "renderHistoryFrame(message)" in source
    assert "Loading history: ${data.progress.monthly_completed} of ${data.progress.monthly_total} monthly snapshots ready." in source
    assert "plot.dataset.renderMilliseconds" in source
    assert "await delay(250)" in source
    assert 'fetch("/api/trends/montgomery"' not in source
    assert "historyResponseMatchesQuery" in source
    assert "generation !== historyGeneration" in source
    assert "scope.label" in source
    assert "item.snapshot.is_latest" in source
    assert "historyObservationDates" in source
    assert "clearHistory" in source
    assert "Calculated using cached satellite observations." not in source
    assert "const ordered = [corners[0], corners[1], corners[3], corners[2]]" in source
    assert 'id="map-tile-message"' in template
    assert 'id="calculation-source"' in template
    assert 'id="calculated-at"' in template
    assert 'id="latest-window-details"' in template
    assert 'id="processing-diagnostics"' in template
    assert 'id="observation-details"' in template
    assert 'id="pixel-details"' in template
    assert 'class="results-layout"' in template
    assert 'id="results-location-name"' in template
    assert 'id="results-location-details"' in template
    assert '<h3 id="example-title">Estimated leaf area</h3>' in template
    assert 'id="observation-period-dates"' in template
    assert 'id="observation-total"' in template
    assert "This describes data availability, not prediction confidence." in template
    assert "distinctUsableDates.size" in source
    assert "Usable observations from ${distinctUsableDates.size} of ${support.possible_days} days" in source
    assert "passed_total / 3" not in source
    assert "passed_total/3" not in source
    assert "grid-template-columns: minmax(300px, 1fr) minmax(0, 2fr)" in styles
    assert "@media (max-width: 760px)" in styles
    assert "April 6, 2026, the model’s training cutoff" in template
    assert "How LeafView works" in template
    assert "How LeafView estimates leaf area" in template
    assert "Observation periods and history" in template
    assert "Processing checks and validation" in template
    assert "Interpreting the results" in template
    assert "Technical details" in template
    assert "implementation consistency, not prediction accuracy" in template
    assert "LAI alone cannot diagnose plant health or drought" in template
    assert "Methods and limitations" not in template
    assert "Estimated leaf area" in template
    assert "Explore leaf area near you" in template
    assert "latest estimate and monthly snapshots" in template
    assert "Each estimate uses eight days of satellite observations." in template
    assert "What does LAI mean?" in template
    assert "3 m² of leaves per m² of ground" in template
    assert "Higher LAI means more foliage, not necessarily healthier plants." in template
    assert "highlighted satellite area" in template
    assert "Accuracy for rolling eight-day estimates has not yet been evaluated." in template
    assert "Leaf area, seen from orbit" not in template
    assert "Choose a landscape to explore" not in template
    assert "What will you see?" not in template
    assert "Choose a location below to begin." not in template
    assert "View an older eight-day window" in template
    assert "History at selected location" in template
    assert "Monthly snapshots" in template
    assert "Each point summarizes eight days of satellite observations." in template
    assert "Network or retrieval error" in template
    assert "Network or retrieval error" in source
    assert "Calculation time unavailable." in source
    assert 'showEstimateState("Estimate ready")' in source
    assert "Binding this result to the selected location and UTC dates" not in source
    assert template.count('id="result-badge"') == 1


def test_ui_has_compact_always_visible_lai_reference_strip():
    root = Path(__file__).parents[1]
    source = (root / "static/app.js").read_text(encoding="utf-8")
    styles = (root / "static/app.css").read_text(encoding="utf-8")
    template = (root / "templates/index.html").read_text(encoding="utf-8")

    assert '<section id="landscape-guide" class="lai-reference"' in template
    assert '<h2 id="lai-reference-title" tabindex="-1">LAI reference · Published averages</h2>' in template
    assert template.index('id="landscape-guide"') < template.index('class="workspace"')
    for landscape, average in (
        ("Deserts", "1.3"),
        ("Grasslands", "1.7"),
        ("Crops", "3.6"),
        ("Tropical rainforests", "4.8"),
        ("Temperate deciduous forests", "5.1"),
        ("Temperate conifer forests", "5.5"),
    ):
        assert f"<strong>{average}</strong><span>{landscape}</span>" in template

    assert "Values vary with season and location." in template
    assert "Published averages for context." not in template
    assert '<details id="lai-reference-sources" class="lai-reference-sources">' in template
    assert "reference examples—not category boundaries, health ratings" in template
    assert "Leaf area across different landscapes" not in template
    assert "2.0 dry / 3.0 wet averages" not in template
    assert "7.1 peak" not in template
    assert "landscape-table" not in template
    assert "https://doi.org/10.1046/j.1466-822X.2003.00026.x" in template
    assert "https://doi.org/10.3390/rs11070829" in template
    assert "https://doi.org/10.1093/forestscience/50.3.387" in template

    assert 'id="compare-landscapes" href="#landscape-guide"' in template
    assert 'byId("compare-landscapes").addEventListener("click"' in source
    assert "guide.open = true" not in source
    assert 'byId("lai-reference-title").focus' in source
    assert "grid-template-columns: repeat(6, minmax(0, 1fr))" in styles
    assert "grid-template-columns: repeat(3, minmax(0, 1fr))" in styles
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in styles
    assert "@media (max-width: 680px)" in styles


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


def test_search_and_map_labels_at_identical_coordinates_share_query_and_result():
    client = create_app().test_client()
    searched = client.post("/api/estimate", json={
        "latitude": 39.1547,
        "longitude": -77.2405,
        "display_location": "Gaithersburg, Maryland",
        "start": "2026-04-07",
    })
    clicked = client.post("/api/estimate", json={
        "latitude": 39.1547,
        "longitude": -77.2405,
        "display_location": "39.1547, −77.2405",
        "start": "2026-04-07",
    })
    assert searched.status_code == clicked.status_code == 200
    assert searched.json["query_key"] == clicked.json["query_key"]
    assert searched.json["result"]["query"] == clicked.json["result"]["query"]
    assert searched.json["result"]["lai"] == clicked.json["result"]["lai"]


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
        def compatible_result(self, identity):
            period_start = date.fromisoformat(str(identity["key"]).rsplit(":", 1)[-1])
            if period_start != date(2026, 9, 2):
                return None
            return self._result(period_start), tmp_path / "latest.json"

        @staticmethod
        def _result(period_start):
            return {
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
            }

        def start(self, latitude, longitude, period_start, display_location):
            calls.append(period_start)
            fake_identity(latitude, longitude, period_start, tmp_path)
            return {
                "job_id": period_start.isoformat(),
                "state": "complete",
                "result": self._result(period_start),
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
        date(2026, 8, 24), date(2026, 7, 24),
        date(2026, 6, 23), date(2026, 5, 24), date(2026, 4, 23),
    ]
    assert history["latest_reused"] is True
    assert history["progress"]["monthly_total"] == 5
    assert history["progress"]["monthly_completed"] == 5
    reopened = jobs.start(
        requested[0], requested[1], date(2026, 9, 2), "Custom location"
    )
    assert reopened["job_id"] == history["job_id"]
    assert len(calls) == 5


def test_history_bounds_concurrency_and_cancellation_stops_new_scheduling(
    tmp_path: Path, monkeypatch
):
    import lai_cac.web as web

    requested = (39.265474, -77.225871)
    calls = []
    lock = threading.Lock()

    def fake_identity(latitude, longitude, period_start, root):
        return {"key": f"{latitude:.6f}:{longitude:.6f}:{period_start.isoformat()}"}

    class WaitingEstimateJobs:
        def compatible_result(self, identity):
            period_start = date.fromisoformat(str(identity["key"]).rsplit(":", 1)[-1])
            if period_start != date(2026, 9, 2):
                return None
            return {
                "status": "verified", "lai": 2.0,
                "provenance": {
                    "composite": {"start": "2026-09-02", "end": "2026-09-09"},
                    "usable_counts": {"15": 1, "18": 1, "21": 1},
                    "attempts": [],
                },
            }, tmp_path / "latest.json"

        def start(self, latitude, longitude, period_start, display_location):
            with lock:
                calls.append(period_start)
            return {
                "job_id": period_start.isoformat(),
                "state": "running",
                "progress": {"percent": 25, "stage": "retrieving_observations"},
            }

        def snapshot(self, job_id):
            if job_id == "2026-08-24":
                period_start = date.fromisoformat(job_id)
                return {
                    "job_id": job_id,
                    "state": "complete",
                    "result": {
                        "status": "verified", "lai": 2.0,
                        "provenance": {
                            "composite": {
                                "start": job_id,
                                "end": (period_start + timedelta(days=7)).isoformat(),
                            },
                            "usable_counts": {"15": 1, "18": 1, "21": 1},
                            "attempts": [],
                        },
                    },
                }
            return {
                "job_id": job_id,
                "state": "running",
                "progress": {"percent": 25, "stage": "retrieving_observations"},
            }

    monkeypatch.setattr(web, "cache_identity", fake_identity)
    jobs = HistoryJobs(tmp_path, ObservationMemo(), WaitingEstimateJobs())
    history = jobs.start(
        requested[0], requested[1], date(2026, 9, 2), "Cancellation test"
    )
    for _ in range(100):
        with lock:
            if len(calls) == 3:
                break
        time.sleep(0.01)
    assert calls == [
        date(2026, 8, 24), date(2026, 7, 24), date(2026, 6, 23)
    ]
    assert history["max_concurrent_windows"] == 2
    jobs.cancel(history["job_id"])
    for _ in range(100):
        cancelled = jobs.snapshot(history["job_id"])
        if cancelled["state"] == "cancelled":
            break
        time.sleep(0.01)
    assert cancelled["state"] == "cancelled"
    assert len(calls) == 3


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


def test_history_endpoint_rejects_a_mismatched_latest_result_key():
    clock = lambda: datetime(2026, 4, 14, 23, 0, tzinfo=timezone.utc)
    response = create_app(clock=clock).test_client().post("/api/history", json={
        "latitude": 39.1547,
        "longitude": -77.2405,
        "display_location": "Montgomery County, Maryland",
        "latest_query_key": "a-result-from-another-query",
    })
    assert response.status_code == 400
    assert response.json["error"] == (
        "The latest estimate does not match the requested history"
    )


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
    timing = completed["result"]["delivery"]["timing"]
    assert timing["queue_wait_seconds"] >= 0
    assert timing["phase_seconds"]["observations"] >= 0
    assert timing["total_seconds"] >= timing["queue_wait_seconds"]
