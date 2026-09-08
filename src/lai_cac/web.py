from __future__ import annotations

from datetime import date
from pathlib import Path

import json

from flask import Flask, jsonify, render_template

from .assets import ReferenceAssets
from .composites import periods_after_cutoff

ROOT = Path(__file__).resolve().parents[2]


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
    if not assets["igbp"]["present"]:
        missing.append({"key": "igbp", "name": "Exact eight-year VIIRS IGBP grid", "filename": "S-NPP_VIIRS_GST_IGBP_8-Year_30arcsec.nc"})
        approximations.append("Cached example uses a manually supplied IGBP land-cover class.")
    else:
        verified.append("Exact eight-year VIIRS IGBP grid")
    if not assets["solar_geometry"]["present"]:
        missing.append({"key": "solar_geometry", "name": "Notebook solar-geometry helper", "filename": "geometry_goes19.py"})
        approximations.append("Cached example uses the documented NOAA solar-angle approximation.")
    else:
        verified.append("Notebook solar-geometry helper")
    ready = not missing
    if ready:
        headline = "All required scientific inputs are verified."
    elif assets["notebook"]["present"] and assets["model"]["valid"]:
        headline = "Notebook and model verified; live estimates remain blocked pending two exact preprocessing inputs."
    else:
        headline = "Required scientific inputs are missing or invalid; live estimates remain blocked."
    return {"ready_for_verified_inference": ready, "headline": headline,
            "verified": verified, "missing": missing, "approximations": approximations}


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
        provenance = result["provenance"]
        first_footprint = next(
            (attempt.get("sampled_pixel_corners") for attempt in provenance.get("attempts", [])
             if attempt.get("sampled_pixel_corners")),
            None,
        )
        return jsonify({
            "id": "montgomery-md-2026-04-07",
            "display_location": provenance.get("location_name", "Selected location"),
            "status": result["status"], "lai": result["lai"], "units": result["units"],
            "limitations": result["limitations"],
            "query": {"location": provenance["requested_location"], "period": provenance["composite"]},
            "sampled_pixel": {"center": provenance.get("sampled_pixel_center"), "footprint": first_footprint},
            "observation_support": {"passed_by_hour": provenance["usable_counts"], "possible_per_hour": 8},
            "provenance": provenance,
        })

    return app


def run() -> None:
    create_app().run(host="127.0.0.1", port=8781, debug=False)
