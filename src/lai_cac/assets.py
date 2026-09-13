from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path

from .errors import MissingDependencyError

MODEL_NAME = "lai_xgboost_model.json"
NOTEBOOK_NAME = "LAI_Machine_Learning_Project.ipynb"
MODEL_SHA256 = "815ed3b7cb92b95d598e24385527874864c5e6e282cf05d7dabbcca26391d4c6"
IGBP_NAME = "S-NPP_VIIRS_GST_IGBP_8-Year_30arcsec.nc"
IGBP_SHA256 = "59ed29f607a989e67c379cb572901aed84a0087b5f35ba5c619e0d777cd4e1c5"
SOLAR_GEOMETRY_NAME = "geometry_goes19.py"
SOLAR_GEOMETRY_SHA256 = "1ebf9fe03be6de656554dbf5f094928b9f49265e291cf8cd53ecb9597e06e0bd"
NAVIGATION_NAME = "GOES_Navigation_2kmFD-GOES-East.nc"
NAVIGATION_SHA256 = "c08fa491793996ab204c936f4bb25f299f4cf18383b0bd3673db87f8bd780e96"
NAVIGATION_SOURCE_CONFIG_NAME = "navigation-source.json"

_HASH_CACHE: dict[tuple[str, int, int], str] = {}
_HASH_CACHE_LOCK = threading.Lock()


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

    @property
    def navigation(self) -> Path:
        if self.navigation_source_config.is_file():
            configured = json.loads(self.navigation_source_config.read_text(encoding="utf-8"))
            path = Path(configured["path"]).expanduser()
            return path if path.is_absolute() else self.root / path
        return self.root / NAVIGATION_NAME

    @property
    def navigation_source_config(self) -> Path:
        return self.root / NAVIGATION_SOURCE_CONFIG_NAME

    def audit(self) -> dict[str, object]:
        model_hash = sha256(self.model) if self.model.is_file() else None
        igbp_hash = sha256(self.igbp) if self.igbp.is_file() else None
        solar_geometry_hash = sha256(self.solar_geometry) if self.solar_geometry.is_file() else None
        navigation_hash = sha256(self.navigation) if self.navigation.is_file() else None
        return {
            "root": str(self.root.resolve()),
            "model": {"path": str(self.model), "present": self.model.is_file(), "sha256": model_hash,
                      "valid": model_hash == MODEL_SHA256},
            "notebook": {"path": str(self.notebook), "present": self.notebook.is_file()},
            "igbp": {"path": str(self.igbp), "present": self.igbp.is_file(), "sha256": igbp_hash,
                     "valid": igbp_hash == IGBP_SHA256},
            "solar_geometry": {"path": str(self.solar_geometry), "present": self.solar_geometry.is_file(),
                               "sha256": solar_geometry_hash,
                               "valid": solar_geometry_hash == SOLAR_GEOMETRY_SHA256},
            "navigation": {"path": str(self.navigation), "present": self.navigation.is_file(),
                           "sha256": navigation_hash,
                           "expected_sha256": NAVIGATION_SHA256,
                           "valid": navigation_hash == NAVIGATION_SHA256,
                           "source_config": str(self.navigation_source_config)},
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

    def require_igbp(self) -> Path:
        if not self.igbp.is_file():
            raise MissingDependencyError(f"Missing required IGBP grid: {self.igbp}")
        actual = sha256(self.igbp)
        if actual != IGBP_SHA256:
            raise MissingDependencyError(
                f"IGBP checksum mismatch for {self.igbp}: expected {IGBP_SHA256}, got {actual}"
            )
        return self.igbp

    def require_solar_geometry(self) -> Path:
        if not self.solar_geometry.is_file():
            raise MissingDependencyError(f"Missing required solar-geometry helper: {self.solar_geometry}")
        actual = sha256(self.solar_geometry)
        if actual != SOLAR_GEOMETRY_SHA256:
            raise MissingDependencyError(
                f"Solar-geometry checksum mismatch for {self.solar_geometry}: "
                f"expected {SOLAR_GEOMETRY_SHA256}, got {actual}"
            )
        return self.solar_geometry

    def require_navigation(self) -> Path:
        if not self.navigation.is_file():
            raise MissingDependencyError(f"Missing required GOES navigation raster: {self.navigation}")
        actual = sha256(self.navigation)
        if actual != NAVIGATION_SHA256:
            raise MissingDependencyError(
                f"Navigation checksum mismatch for {self.navigation}: "
                f"expected {NAVIGATION_SHA256}, got {actual}"
            )
        return self.navigation


def sha256(path: Path) -> str:
    stat = path.stat()
    key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    with _HASH_CACHE_LOCK:
        cached = _HASH_CACHE.get(key)
    if cached is not None:
        return cached
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    value = digest.hexdigest()
    with _HASH_CACHE_LOCK:
        _HASH_CACHE[key] = value
    return value
