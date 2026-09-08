from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .errors import MissingDependencyError

MODEL_NAME = "lai_xgboost_model.json"
NOTEBOOK_NAME = "LAI_Machine_Learning_Project.ipynb"
MODEL_SHA256 = "815ed3b7cb92b95d598e24385527874864c5e6e282cf05d7dabbcca26391d4c6"
IGBP_NAME = "S-NPP_VIIRS_GST_IGBP_8-Year_30arcsec.nc"
SOLAR_GEOMETRY_NAME = "geometry_goes19.py"


@dataclass(frozen=True)
class ReferenceAssets:
    root: Path

    @property
    def model(self) -> Path:
        return self.root / MODEL_NAME

    @property
    def notebook(self) -> Path:
        return self.root / NOTEBOOK_NAME

    @property
    def igbp(self) -> Path:
        return self.root / IGBP_NAME

    @property
    def solar_geometry(self) -> Path:
        return self.root / SOLAR_GEOMETRY_NAME

    def audit(self) -> dict[str, object]:
        model_hash = sha256(self.model) if self.model.is_file() else None
        return {
            "root": str(self.root.resolve()),
            "model": {"path": str(self.model), "present": self.model.is_file(), "sha256": model_hash,
                      "valid": model_hash == MODEL_SHA256},
            "notebook": {"path": str(self.notebook), "present": self.notebook.is_file()},
            "igbp": {"path": str(self.igbp), "present": self.igbp.is_file()},
            "solar_geometry": {"path": str(self.solar_geometry), "present": self.solar_geometry.is_file()},
        }

    def require_model(self) -> Path:
        if not self.model.is_file():
            raise MissingDependencyError(f"Missing required trained model: {self.model}")
        actual = sha256(self.model)
        if actual != MODEL_SHA256:
            raise MissingDependencyError(
                f"Model checksum mismatch for {self.model}: expected {MODEL_SHA256}, got {actual}"
            )
        return self.model


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
