from __future__ import annotations

import json
import math
from collections.abc import Mapping
from numbers import Real
from typing import Any

from flask.json.provider import DefaultJSONProvider


def json_export_value(value: Any) -> Any:
    """Return a JSON-safe copy without changing the in-memory scientific values."""
    if isinstance(value, Mapping):
        return {key: json_export_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_export_value(item) for item in value]
    if isinstance(value, Real) and not isinstance(value, bool) and not math.isfinite(float(value)):
        return None
    return value


def strict_json_dumps(value: Any, **kwargs: Any) -> str:
    """Serialize exported data as standards-compliant JSON."""
    return json.dumps(json_export_value(value), allow_nan=False, **kwargs)


class StrictJSONProvider(DefaultJSONProvider):
    """Flask JSON provider that never emits JavaScript NaN/Infinity tokens."""

    def dumps(self, obj: Any, **kwargs: Any) -> str:
        kwargs["allow_nan"] = False
        return super().dumps(json_export_value(obj), **kwargs)
