from __future__ import annotations

from datetime import date
from pathlib import Path

import json

from flask import Flask, jsonify, render_template, request

from .assets import ReferenceAssets
from .composites import periods_after_cutoff
from .inference import run_estimate

ROOT = Path(__file__).resolve().parents[2]

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
            "Estimate execution is ready and supplied preprocessing is verified; "
            "historical numerical reproduction remains unverified."
        )
    elif operational_ready:
        headline = "Estimate execution is ready; supplied preprocessing validation remains provisional."
    elif assets["notebook"]["present"] and assets["model"]["valid"]:
        count = len(missing)
        noun = "input" if count == 1 else "inputs"
        headline = f"Notebook and model verified; estimate execution is blocked pending {count} runtime {noun}."
    else:
        headline = "Required runtime inputs are missing or invalid; estimate execution is blocked."
    return {"ready_for_estimate_execution": operational_ready,
            "ready_for_verified_inference": preprocessing_verified, "headline": headline,
            "verified": verified, "missing": missing, "approximations": approximations,
            "preprocessing_discrepancies": discrepancies,
            "operational_readiness": {
                "ready": operational_ready,
                "status": "ready" if operational_ready else "blocked",
                "detail": "The installed runtime assets can execute an estimate." if operational_ready else
                          "One or more runtime assets are missing or invalid.",
            },
            "preprocessing_validation": {
                "verified": preprocessing_verified,
                "status": "verified" if preprocessing_verified else "provisional",
                "detail": "The supplied research preprocessing is matched." if preprocessing_verified else
                          "A research-pipeline equivalence check remains unresolved.",
            },
            "historical_numerical_reproduction": {
                "completed": False,
                "status": "unverified",
                "required_for_runtime": False,
                "detail": "Historical feature rows are validation evidence, not runtime inputs.",
            }}


def public_result(result: dict, identifier: str, display_location: str) -> dict:
    provenance = result["provenance"]
    first_footprint = next(
        (attempt.get("sampled_pixel_corners") for attempt in provenance.get("attempts", [])
         if attempt.get("sampled_pixel_corners")),
        None,
    )
    return {
        "id": identifier,
        "display_location": display_location,
        "status": result["status"], "lai": result["lai"], "units": result["units"],
        "limitations": result["limitations"],
        "readiness": result.get("readiness", {}),
        "scientific_concerns": result.get("scientific_concerns", []),
        "query": {"location": provenance["requested_location"], "period": provenance["composite"]},
        "sampled_pixel": {"center": provenance.get("sampled_pixel_center"), "footprint": first_footprint},
        "observation_support": {"passed_by_hour": provenance["usable_counts"], "possible_per_hour": 8},
        "provenance": provenance,
    }


def create_app() -> Flask:
    app = Flask(__name__, template_folder=str(ROOT / "templates"), static_folder=str(ROOT / "static"))

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
        assets = ReferenceAssets(ROOT / "artifacts/reference").audit()
        dependencies = dependency_status(assets)
        return jsonify({
            **dependencies,
            "assets": assets,
            "available_completed_periods": [
                {"start": start.isoformat(), "end": end.isoformat()}
                for start, end in periods_after_cutoff(date.today())
            ],
        })

    @app.get("/api/examples/montgomery-2026-04-07")
    def cached_example():
        path = ROOT / "data/results/montgomery-md-2026-04-07.json"
        if not path.is_file():
            return jsonify({"status": "unavailable"}), 404
        result = json.loads(path.read_text(encoding="utf-8"))
        return jsonify(public_result(
            result,
            "montgomery-md-2026-04-07",
            result["provenance"].get("location_name", "Selected location"),
        ))

    @app.post("/api/estimate")
    def estimate():
        payload = request.get_json(silent=True) or {}
        try:
            latitude = float(payload["latitude"])
            longitude = float(payload["longitude"])
            period_start = date.fromisoformat(payload["start"])
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                raise ValueError("Latitude or longitude is outside its valid range")
            if not dependency_status(ReferenceAssets(ROOT / "artifacts/reference").audit())[
                "ready_for_verified_inference"
            ]:
                return jsonify({"error": "Verified preprocessing is not ready"}), 503
            result = run_estimate(
                latitude,
                longitude,
                period_start,
                None,
                ROOT,
                ROOT / "data/results/runtime-latest.json",
                "Selected location",
            )
        except (KeyError, TypeError, ValueError) as error:
            return jsonify({"error": str(error)}), 400
        return jsonify(public_result(result, "runtime-latest", "Selected location"))

    return app


def run() -> None:
    create_app().run(host="127.0.0.1", port=8781, debug=False)
