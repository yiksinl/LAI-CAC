from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from .assets import MODEL_SHA256, sha256
from .errors import InvalidInputError, MissingDependencyError


class LaiModel:
    def __init__(self, path: Path):
        if not path.is_file():
            raise MissingDependencyError(f"Missing model: {path}")
        if sha256(path) != MODEL_SHA256:
            raise MissingDependencyError("Refusing to load a model with an unexpected checksum")
        try:
            import xgboost as xgb
        except Exception as exc:
            raise MissingDependencyError(f"XGBoost runtime could not load: {exc}") from exc

        self.xgb = xgb
        self.booster = xgb.Booster()
        self.booster.load_model(path)
        self.features = self.booster.feature_names or []
        if len(self.features) != 56:
            raise InvalidInputError(f"Expected 56 named model features, found {len(self.features)}")

    def predict(self, row: dict[str, float | None], usable_spectral_count: int) -> float:
        missing = set(self.features) - set(row)
        extra = set(row) - set(self.features)
        if missing or extra:
            raise InvalidInputError(f"Feature schema mismatch; missing={sorted(missing)}, extra={sorted(extra)}")
        if usable_spectral_count <= 0:
            raise InvalidInputError("Refusing prediction from an entirely missing reflectance window")
        values = np.asarray([[math.nan if row[name] is None else float(row[name]) for name in self.features]], dtype=np.float32)
        result = float(self.booster.predict(self.xgb.DMatrix(values, feature_names=self.features))[0])
        if not math.isfinite(result):
            raise InvalidInputError("Model returned a non-finite prediction")
        return result
