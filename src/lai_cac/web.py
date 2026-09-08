from __future__ import annotations

from datetime import date
from pathlib import Path

import json

from flask import Flask, jsonify, render_template

from .assets import ReferenceAssets
from .composites import periods_after_cutoff

ROOT = Path(__file__).resolve().parents[2]


def create_app() -> Flask:
    app = Flask(__name__, template_folder=str(ROOT / "templates"), static_folder=str(ROOT / "static"))

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/status")
    def status():
        assets = ReferenceAssets(ROOT / "artifacts/reference").audit()
        ready = bool(assets["model"]["valid"] and assets["notebook"]["present"]
                     and assets["igbp"]["present"] and assets["solar_geometry"]["present"])
        return jsonify({
            "ready_for_verified_inference": ready,
            "assets": assets,
            "available_completed_periods": [
                {"start": start.isoformat(), "end": end.isoformat()}
                for start, end in periods_after_cutoff(date.today())
            ],
            "message": ("Reference assets found; notebook extraction and validation can proceed."
                        if ready else "LAI is unavailable until the authoritative notebook and checksum-matched model are supplied."),
        })

    @app.get("/api/examples/montgomery-2026-04-07")
    def cached_example():
        path = ROOT / "data/results/montgomery-md-2026-04-07.json"
        if not path.is_file():
            return jsonify({"status": "unavailable"}), 404
        result = json.loads(path.read_text(encoding="utf-8"))
        return jsonify({key: result[key] for key in ("status", "lai", "units", "limitations", "provenance")})

    return app


def run() -> None:
    create_app().run(host="127.0.0.1", port=8781, debug=False)
